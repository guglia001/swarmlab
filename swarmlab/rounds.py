"""Resumen narrativo de cada ronda (modelo medio), con alternativa determinista si el LLM falla."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from swarmlab import prompts
from swarmlab.config import Settings
from swarmlab.llm import LLM, CallCache, FakeRequest, cached_complete, fake_responder
from swarmlab.social.platform import Platform, Post


class RoundSummary(BaseModel):
    round: int = 0
    narrative: str
    key_post_ids: list[str] = Field(default_factory=list)
    emerging_narratives: list[str] = Field(default_factory=list)
    notable_agents: list[str] = Field(default_factory=list)
    mood: str = ""
    fallback: bool = False


class RoundSummaryDraft(BaseModel):
    narrative: str
    key_post_ids: list[str]
    emerging_narratives: list[str]
    notable_agents: list[str]
    mood: str


def _post_line(plat: Platform, p: Post) -> str:
    parent = plat.posts.get(p.parent_id) if p.parent_id else None
    ctx = (
        f" (en respuesta a @{plat.handle(parent.author_id)})"
        if parent and p.kind in ("reply", "quote")
        else ""
    )
    stance = f"{p.author_stance:+.1f}" if p.author_stance is not None else "n/a"
    return f"[{p.id}] @{plat.handle(p.author_id)} postura {stance} ♥{p.likes} ↻{p.reposts} 💬{p.replies}{ctx}: {' '.join(p.text.split())[:220]}"


def top_round_posts(plat: Platform, round_no: int, limit: int = 15) -> list[Post]:
    posts = [p for p in plat.posts_in_round(round_no) if p.text and p.kind != "event"]
    return sorted(posts, key=lambda p: (-p.engagement, p.seq))[:limit]


async def summarize_round(
    llm: LLM,
    plat: Platform,
    round_no: int,
    rounds: int,
    interval: str,
    brief: str,
    language: str,
    stats: dict[str, Any],
    shifts: list[str],
    events: list[str],
    population: int,
    settings: Settings,
    cache: CallCache | None,
) -> RoundSummary:
    posts = top_round_posts(plat, round_no)
    prompt = prompts.render(
        "round_summary_user",
        brief=brief,
        language=language,
        round=round_no,
        rounds=rounds,
        interval=interval,
        events="; ".join(events) or "ninguno",
        active=stats["active_agents"],
        population=population,
        actions=", ".join(f"{k}={v}" for k, v in sorted(stats["actions"].items())) or "ninguna",
        mean_before=f"{stats['mean_before']:+.2f}",
        mean_after=f"{stats['mean']:+.2f}",
        distribution=", ".join(f"{k}: {v}" for k, v in stats["distribution"].items()),
        posts="\n".join(_post_line(plat, p) for p in posts) or "(nadie publicó texto esta ronda)",
        shifts="\n".join(shifts) or "(ninguno)",
    )
    draft = await cached_complete(
        cache,
        f"round.{round_no}.summary",
        llm,
        prompt,
        schema=RoundSummaryDraft,
        system=prompts.load("round_summary_system"),
        model=settings.model_medium,
        effort=settings.effort_medium,
        purpose="round.summary",
        meta={"round": round_no, "_post_ids": [p.id for p in posts]},
    )
    valid_ids = {p.id for p in plat.posts_in_round(round_no)}
    return RoundSummary(
        round=round_no,
        narrative=draft.narrative,
        key_post_ids=[i for i in draft.key_post_ids if i in valid_ids][:5],
        emerging_narratives=draft.emerging_narratives[:4],
        notable_agents=[h.lstrip("@") for h in draft.notable_agents][:5],
        mood=draft.mood,
    )


def fallback_summary(plat: Platform, round_no: int, stats: dict[str, Any], events: list[str]) -> RoundSummary:
    posts = top_round_posts(plat, round_no, 3)
    text = (
        f"Ronda {round_no}: {stats['active_agents']} agentes activos, {stats['posts_created']} publicaciones; "
        f"postura media {stats['mean_before']:+.2f} → {stats['mean']:+.2f}."
        + (f" Eventos: {'; '.join(events)}." if events else "")
    )
    return RoundSummary(
        round=round_no,
        narrative=text,
        key_post_ids=[p.id for p in posts],
        notable_agents=[plat.handle(p.author_id) for p in posts],
        mood="(sin resumen del LLM)",
        fallback=True,
    )


@fake_responder("round.summary")
def _fake_round_summary(req: FakeRequest) -> RoundSummaryDraft:
    ids = [str(i) for i in req.meta.get("_post_ids", [])]
    return RoundSummaryDraft(
        narrative=f"Resumen simulado de la ronda {req.meta.get('round')}.",
        key_post_ids=ids[:3],
        emerging_narratives=["precio", "confianza"],
        notable_agents=[],
        mood="expectante",
    )
