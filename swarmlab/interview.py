"""Entrevistas en personaje con un agente concreto, reanudando la sesión de ``claude`` entre preguntas."""

from __future__ import annotations

from swarmlab import prompts
from swarmlab.agent import stance_label
from swarmlab.config import Settings
from swarmlab.llm import LLM, Conversation, FakeRequest, fake_responder
from swarmlab.memory import AgentMemory
from swarmlab.ontology import ScenarioBrief, compact_brief
from swarmlab.personas import Persona
from swarmlab.report import RunData
from swarmlab.store import RunStore


class Interview:
    KIND = "interview"

    def __init__(
        self, store: RunStore, settings: Settings, llm: LLM, handle: str, *, new: bool = False
    ) -> None:
        self.store = store
        self.data = RunData(store)
        handle = handle.lstrip("@").lower()
        agent_id = self.data.by_handle.get(handle) or (handle if handle in self.data.personas else None)
        if agent_id is None:
            raise KeyError(f"no existe el agente @{handle} en este run")
        self.agent_id = agent_id
        self.persona = Persona.model_validate(self.data.personas[agent_id])
        memories = {m["agent_id"]: m for m in store.load_memory_data()}
        self.memory = AgentMemory.model_validate(memories.get(agent_id, {"agent_id": agent_id}))
        traj = self.data.traj.get(agent_id, [])
        stance = traj[-1][1] if traj else self.persona.initial_stance
        system = prompts.render(
            "interview_system",
            card=self.persona.card(),
            archetype=self.persona.archetype,
            segment=self.persona.segment,
            stance=f"{stance:+.2f}",
            stance_label=stance_label(stance),
            initial_stance=f"{self.persona.initial_stance:+.2f} ({stance_label(self.persona.initial_stance)})",
            memory=self.memory.render(max_recent=8),
            language=store.get_meta("document", {}).get("language_name", "español"),
        )
        own = [p for p in self.data.posts if p["author_id"] == agent_id and p["text"]]
        decisions = store.decisions_for(agent_id)
        brief_meta = store.get_meta("brief")
        brief = compact_brief(ScenarioBrief.model_validate(brief_meta)) if brief_meta else ""
        rounds = (
            "\n".join(f"- R{s['round']}: {s['narrative'][:400]}" for s in store.round_summaries())
            or "(sin rondas)"
        )
        preamble = prompts.render(
            "interview_preamble",
            brief=brief,
            rounds=rounds[:6000],
            posts="\n".join(self.data.post_line(p, 220) for p in own[-12:]) or "(no publicaste nada)",
            trajectory="\n".join(
                f"- R{d['round']}: {d['stance_before']:+.2f} → {d['stance_after']:+.2f}: {d['reasoning']}"
                for d in decisions
            )
            or "(no participaste activamente)",
        )
        self.key = self.persona.handle
        if new:
            store.delete_session(self.KIND, self.key)
        row = store.get_session(self.KIND, self.key)
        self.conv = Conversation(
            llm,
            system=system,
            model=settings.model_medium,
            purpose="interview",
            preamble=preamble,
            session_id=row["session_id"] if row else None,
            started=row is not None,
            effort=settings.effort_medium,
        )

    @property
    def resumed(self) -> bool:
        return self.conv.started

    async def ask(self, question: str) -> str:
        self.store.add_transcript(self.KIND, self.key, "user", question)
        res = await self.conv.send(question)
        self.store.add_transcript(self.KIND, self.key, "assistant", res.text)
        if self.conv.session_id:
            self.store.save_session(
                self.KIND, self.key, self.conv.session_id, self.conv.model, self.conv.turns
            )
        return res.text


@fake_responder("interview")
def _fake_interview(req: FakeRequest) -> str:
    return f"(dry-run, en personaje) Respuesta {len(req.history) + 1}: sigo pensando lo mismo."
