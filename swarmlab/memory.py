"""Memoria de los agentes: últimas N interacciones + resumen a largo plazo (por lotes, cada K rondas)."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

from pydantic import BaseModel, Field

from swarmlab import prompts
from swarmlab.config import Settings
from swarmlab.llm import LLM, CallCache, FakeRequest, cached_complete, fake_responder
from swarmlab.personas import Persona
from swarmlab.social.actions import ActionOutcome
from swarmlab.social.feed import FeedItem
from swarmlab.social.platform import Platform


class MemoryItem(BaseModel):
    round: int
    text: str


class AgentMemory(BaseModel):
    agent_id: str
    summary: str = ""
    recent: list[MemoryItem] = Field(default_factory=list)
    summarized_through: int = 0

    def add(self, round_no: int, text: str, limit: int) -> None:
        self.recent.append(MemoryItem(round=round_no, text=text))
        self.recent = self.recent[-limit:]

    def pending(self) -> list[MemoryItem]:
        return [m for m in self.recent if m.round > self.summarized_through]

    def render(self, max_recent: int = 8) -> str:
        recent = "; ".join(f"r{m.round}: {m.text}" for m in self.recent[-max_recent:]) or "nada todavía"
        summary = self.summary or ("(aún sin resumen)" if self.recent else "sin recuerdos previos")
        return f"Resumen: {summary}\nRecientes: {recent}"


class MemorySummary(BaseModel):
    agent_id: str
    summary: str = Field(description="2-3 frases en tercera persona")


class MemoryBatch(BaseModel):
    summaries: list[MemorySummary]


def _clip(text: str, n: int = 90) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def record_round(
    memories: dict[str, AgentMemory],
    outcomes: Sequence[ActionOutcome],
    feeds: dict[str, list[FeedItem]],
    plat: Platform,
    stance_before: dict[str, float],
    stance_after: dict[str, float],
    round_no: int,
    limit: int,
) -> None:
    """Anota en la memoria de cada agente lo que hizo, lo que vio y lo que le hicieron esta ronda."""
    for out in outcomes:
        if not out.valid or out.type == "do_nothing":
            continue
        mem = memories[out.agent_id]
        target = plat.posts.get(out.target_post_id or "")
        who = f"@{plat.handle(target.author_id)}" if target else ""
        own = plat.posts.get(out.post_id or "")
        text = {
            "post": f"publicó «{_clip(own.text if own else '')}»",
            "reply": f"respondió a {who}: «{_clip(own.text if own else '')}»",
            "quote": f"citó a {who}: «{_clip(own.text if own else '')}»",
            "repost": f"compartió de {who} «{_clip(target.text if target else '', 60)}»",
            "like": f"le gustó de {who} «{_clip(target.text if target else '', 60)}»",
            "follow": f"empezó a seguir a @{out.target_handle}",
            "unfollow": f"dejó de seguir a @{out.target_handle}",
        }.get(out.type)
        if text:
            mem.add(round_no, text, limit)
        if target and out.type in ("reply", "quote", "repost") and target.author_id in memories:
            verb = {"reply": "le respondió", "quote": "le citó", "repost": "compartió su post"}[out.type]
            extra = f": «{_clip(own.text, 70)}»" if own and own.text else ""
            memories[target.author_id].add(round_no, f"@{plat.handle(out.agent_id)} {verb}{extra}", limit)
    for agent_id, feed in feeds.items():
        mem = memories[agent_id]
        for item in [i for i in feed if i.kind == "event"][:1]:
            mem.add(round_no, f"vio la noticia de @{item.author_handle}: «{_clip(item.text, 80)}»", limit)
        top = max(
            (i for i in feed if i.kind != "event"),
            key=lambda i: i.likes + i.reposts + i.replies,
            default=None,
        )
        if top is not None:
            mem.add(round_no, f"leyó a @{top.author_handle}: «{_clip(top.text, 70)}»", limit)
        before, after = stance_before.get(agent_id, 0.0), stance_after.get(agent_id, 0.0)
        if abs(after - before) >= 0.15:
            mem.add(round_no, f"su postura pasó de {before:+.2f} a {after:+.2f}", limit)


async def summarize_memories(
    llm: LLM,
    memories: dict[str, AgentMemory],
    personas: dict[str, Persona],
    round_no: int,
    language: str,
    settings: Settings,
    cache: CallCache | None,
) -> int:
    """Actualiza el resumen a largo plazo de los agentes con recuerdos nuevos. Devuelve cuántos."""
    pending = [m for m in memories.values() if m.pending()]
    if not pending:
        return 0
    batches = [pending[i : i + settings.memory_batch] for i in range(0, len(pending), settings.memory_batch)]

    async def run(batch: list[AgentMemory], idx: int) -> MemoryBatch:
        blocks = []
        for m in batch:
            p = personas[m.agent_id]
            items = "\n".join(f"  - r{i.round}: {i.text}" for i in m.pending())
            blocks.append(
                f"### {m.agent_id} — @{p.handle} ({p.occupation}, postura inicial {p.initial_stance:+.2f})\n"
                f"Resumen anterior: {m.summary or '(ninguno)'}\nRecuerdos nuevos:\n{items}"
            )
        prompt = prompts.render(
            "memory_batch", language=language, round=round_no, count=len(batch), agents="\n\n".join(blocks)
        )
        return await cached_complete(
            cache,
            f"memory.r{round_no}",
            llm,
            prompt,
            schema=MemoryBatch,
            system=prompts.load("memory_system"),
            model=settings.model_light,
            effort=settings.effort_light,
            purpose="memory.summary",
            meta={"round": round_no, "batch": idx, "_agent_ids": [m.agent_id for m in batch]},
        )

    results = await asyncio.gather(*(run(b, i) for i, b in enumerate(batches)))
    updated = 0
    for batch, res in zip(batches, results, strict=True):
        by_id = {s.agent_id: s.summary for s in res.summaries}
        for m in batch:
            if m.agent_id in by_id and by_id[m.agent_id].strip():
                m.summary = by_id[m.agent_id].strip()[:600]
                m.summarized_through = round_no
                updated += 1
    return updated


@fake_responder("memory.summary")
def _fake_memory(req: FakeRequest) -> MemoryBatch:
    ids = [str(i) for i in req.meta.get("_agent_ids", [])]
    return MemoryBatch(
        summaries=[
            MemorySummary(
                agent_id=i, summary=f"{i} ha seguido el debate hasta la ronda {req.meta.get('round')}."
            )
            for i in ids
        ]
    )
