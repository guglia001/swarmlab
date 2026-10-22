"""Agentes: perfil + memoria + feed → prompt por lote, y la llamada de decisión al modelo ligero."""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Any

from swarmlab import prompts
from swarmlab.config import Settings
from swarmlab.llm import LLM, CallCache, FakeRequest, cached_complete, fake_responder, per_agent_schema
from swarmlab.memory import AgentMemory
from swarmlab.personas import Persona
from swarmlab.social.actions import Action, AgentDecision, BatchDecisions
from swarmlab.social.feed import FeedItem


def stance_label(s: float) -> str:
    if s <= -0.6:
        return "muy en contra"
    if s <= -0.2:
        return "en contra"
    if s < 0.2:
        return "neutral"
    if s < 0.6:
        return "a favor"
    return "muy a favor"


def select_active(personas: list[Persona], rng: random.Random) -> list[Persona]:
    """Cada agente actúa con probabilidad igual a su nivel de actividad."""
    return [p for p in personas if rng.random() < p.activity]


def make_batches(
    agent_ids: list[str], stances: dict[str, float], size: int, rng: random.Random
) -> list[list[str]]:
    """Lotes que mezclan posturas: se ordena por postura y se reparte en round-robin."""
    if not agent_ids:
        return []
    ordered = sorted(agent_ids, key=lambda a: (stances.get(a, 0.0), a))
    n_groups = math.ceil(len(ordered) / size)
    groups = [ordered[i::n_groups] for i in range(n_groups)]
    for g in groups:
        rng.shuffle(g)
    return groups


@dataclass
class RoundContext:
    round_no: int
    rounds: int
    interval: str
    brief: str
    events: str
    language: str
    platform: str


def agent_block(
    persona: Persona, stance: float, memory: AgentMemory, feed: list[FeedItem], following: list[str]
) -> str:
    feed_lines = "\n".join(item.render() for item in feed) or "(feed vacío: todavía no hay publicaciones)"
    follows = ", ".join(f"@{h}" for h in following[:8]) or "nadie"
    return (
        f"### AGENTE {persona.id} — @{persona.handle}\n"
        f"Perfil: {persona.card()}\n"
        f"Postura actual: {stance:+.2f} ({stance_label(stance)})\n"
        f"{memory.render()}\n"
        f"Sigue a: {follows}\n"
        f"Feed:\n{feed_lines}"
    )


def decision_schema(agent_ids: list[str]) -> type[BatchDecisions]:
    """Schema del lote con los ``agent_id`` válidos como enum y exactamente una decisión por agente."""
    return per_agent_schema(BatchDecisions, "decisions", AgentDecision, agent_ids)


async def decide_batch(
    llm: LLM,
    batch: list[Persona],
    blocks: dict[str, str],
    feeds: dict[str, list[FeedItem]],
    stances: dict[str, float],
    ctx: RoundContext,
    settings: Settings,
    cache: CallCache | None,
    batch_index: int,
) -> BatchDecisions:
    ids = [p.id for p in batch]
    prompt = prompts.render(
        "agents_batch",
        brief=ctx.brief,
        round=ctx.round_no,
        rounds=ctx.rounds,
        interval=ctx.interval,
        events=ctx.events,
        count=len(batch),
        agents="\n\n".join(blocks[i] for i in ids),
        max_actions=settings.max_actions_per_agent,
    )
    system = prompts.render(
        "agents_system", platform=ctx.platform, language=ctx.language, max_chars=settings.post_max_chars
    )
    return await cached_complete(
        cache,
        f"round.{ctx.round_no}.decide",
        llm,
        prompt,
        schema=decision_schema(ids),
        system=system,
        model=settings.model_light,
        effort=settings.effort_light,
        purpose="round.decide",
        meta={
            "round": ctx.round_no,
            "batch": batch_index,
            "agents": len(ids),
            "_agents": [
                {
                    "id": i,
                    "stance": stances[i],
                    "feed": [f.post_id for f in feeds.get(i, [])],
                    "authors": [f.author_handle for f in feeds.get(i, [])],
                }
                for i in ids
            ],
        },
    )


@fake_responder("round.decide")
def _fake_decide(req: FakeRequest) -> dict[str, Any]:
    decisions = []
    round_no = req.meta.get("round", 0)
    for a in req.meta.get("_agents", []):
        r = req.rng.random()
        feed: list[str] = a["feed"]
        actions: list[Action] = []
        if feed and r < 0.7:
            actions.append(Action(type="like", target_post_id=feed[0]))
        if r < 0.35 or not feed:
            actions.append(
                Action(
                    type="post",
                    text=f"[dry-run] Opinión de {a['id']} en la ronda {round_no}: me genera dudas el precio.",
                )
            )
        if feed and r > 0.75:
            actions.append(
                Action(type="reply", target_post_id=feed[-1], text=f"[dry-run] Respuesta de {a['id']}.")
            )
        if a["authors"] and 0.5 < r < 0.6:
            actions.append(Action(type="follow", target_handle=a["authors"][0]))
        if not actions:
            actions.append(Action(type="do_nothing"))
        stance = max(-1.0, min(1.0, a["stance"] + req.rng.uniform(-0.2, 0.2)))
        decisions.append(
            AgentDecision(
                agent_id=a["id"], actions=actions, new_stance=round(stance, 2), reasoning="decisión simulada"
            )
        )
    return {"decisions": [d.model_dump() for d in decisions]}
