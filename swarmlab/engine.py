"""Motor de simulación: etapas, bucle de rondas, lotes, eventos, checkpoints y presupuesto de llamadas.

Etapas: ``ontology`` → ``personas`` → ``simulation`` → ``report``. Cada etapa se marca como hecha en
``meta.stages_done``; las rondas se confirman una a una (``meta.current_round``). Todo es reanudable:
``Engine.open(run_id).run()`` continúa donde se quedó.
"""

from __future__ import annotations

import asyncio
import json
import math
import random
import shutil
from collections import defaultdict
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from swarmlab.agent import RoundContext, agent_block, decide_batch, make_batches, select_active
from swarmlab.config import STANCE_MODES, Settings
from swarmlab.events import EventSpec, load_events
from swarmlab.graph import (
    build_follow_graph,
    build_knowledge_graph,
    graph_stats,
    graph_to_json,
    plot_knowledge_graph,
)
from swarmlab.ingest import chunk_document, load_document
from swarmlab.llm import (
    LLM,
    AuthError,
    CallBudget,
    CallBudgetExceeded,
    CallLog,
    LLMCallFailed,
    LLMError,
    StructuredOutputError,
    UsageLimitError,
    build_llm,
    count_logged_calls,
)
from swarmlab.memory import AgentMemory, record_round, summarize_memories
from swarmlab.metrics import round_metrics
from swarmlab.ontology import (
    Entity,
    Fact,
    Ontology,
    Relation,
    ScenarioBrief,
    Tension,
    build_ontology,
    compact_brief,
    generate_brief,
    render_brief_md,
)
from swarmlab.personas import Persona, diversity_report, first_impressions, generate_personas
from swarmlab.rounds import RoundSummary, fallback_summary, summarize_round
from swarmlab.social.actions import ActionOutcome, AgentDecision
from swarmlab.social.feed import FeedItem
from swarmlab.social.platform import Account, Platform, make_platform
from swarmlab.store import RunStore, new_run_id, now_iso

STAGES = ("ontology", "personas", "simulation", "report")
EventCallback = Callable[[str, dict[str, Any]], None]
RoundHook = Callable[[int], Awaitable[list[EventSpec] | str | None]]


class RunConfig(BaseModel):
    run_id: str
    seed_file: str
    seed_name: str
    question: str
    n_agents: int = Field(ge=10, le=200)
    rounds: int = Field(ge=1, le=100)
    agents_per_call: int = Field(ge=1, le=20)
    round_interval: str
    platform: str = "twitter"
    rng_seed: int = 7
    dry_run: bool = False
    language: str | None = None
    max_calls: int
    created_at: str
    interactive: bool = False
    personas_from: str | None = None
    survey: bool = False
    # los runs anteriores a la primera impresión usaban cuotas de postura
    stance_mode: str = "cuotas"
    report_llm: bool = True
    replica_of: str | None = None
    launch_post: bool = False


class RunPaused(Exception):
    def __init__(self, reason: str, hint: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.hint = hint


# --------------------------------------------------------------------------- estimación


class Estimate(BaseModel):
    calls_by_phase: dict[str, int]
    total_calls: int
    minutes: float
    input_tokens: int
    output_tokens: int
    max_calls: int
    exceeds_cap: bool
    expected_active_per_round: float


# segundos por llamada (incluye ~1,5 s de arranque); calibrados con llamadas reales
LATENCY_S = {
    "ontology": 70.0,
    "merge": 25.0,
    "brief": 60.0,
    "personas": 75.0,
    "decide": 30.0,
    "summary": 25.0,
    "memory": 20.0,
    "narratives": 45.0,
    "report": 180.0,
    "impression": 20.0,  # medido: 16 s por lote de 10 con sonnet
    "survey": 25.0,  # medido: 22 s por lote de 10 con sonnet
}
EXPECTED_ACTIVITY = 0.5


def estimate_seed(
    settings: Settings,
    seed: Path,
    *,
    agents: int,
    rounds: int,
    agents_per_call: int,
    max_calls: int | None = None,
    personas_from: str | None = None,
    survey: bool = False,
    stance_mode: str | None = None,
    report_llm: bool | None = None,
) -> Estimate:
    """Estimación para un documento semilla concreto (lo trocea y, con ``personas_from``, mide esa población)."""
    from swarmlab.ingest import chunk_document, load_document

    doc = load_document(seed)
    chunks = chunk_document(doc, settings.chunk_chars)
    activity = EXPECTED_ACTIVITY
    if personas_from:
        src = RunStore.open_existing(settings.runs_dir / personas_from)
        try:
            acts = [p["activity"] for p in src.load_persona_data()]
        finally:
            src.close()
        agents, activity = len(acts) or agents, (sum(acts) / len(acts)) if acts else activity
    return estimate_run(
        settings,
        n_chunks=len(chunks),
        n_agents=agents,
        rounds=rounds,
        agents_per_call=agents_per_call,
        concurrency=settings.max_concurrency,
        max_calls=max_calls,
        expected_activity=activity,
        reuse_personas=personas_from is not None,
        survey=survey,
        stance_mode=stance_mode or settings.stance_mode,
        report_llm=settings.report_llm if report_llm is None else report_llm,
    )


def estimate_run(
    settings: Settings,
    *,
    n_chunks: int,
    n_agents: int,
    rounds: int,
    agents_per_call: int,
    concurrency: int,
    expected_activity: float = EXPECTED_ACTIVITY,
    max_calls: int | None = None,
    reuse_personas: bool = False,
    survey: bool = False,
    stance_mode: str = "cuotas",
    report_llm: bool = True,
) -> Estimate:
    active = n_agents * expected_activity
    decide_per_round = math.ceil(active / agents_per_call) if active else 0
    memory_rounds = rounds // settings.memory_every
    impressions = math.ceil(n_agents / settings.memory_batch) if stance_mode == "impresion" else 0
    calls = {
        "ontology": n_chunks + (1 if n_chunks else 0) + 1,
        "personas": (0 if reuse_personas else math.ceil(n_agents / settings.persona_batch) + 1) + impressions,
        "rounds.decide": rounds * decide_per_round,
        "rounds.summary": rounds,
        "memory": memory_rounds * math.ceil(n_agents * 0.9 / settings.memory_batch),
        "narratives+report": 2 if report_llm else 0,
        "survey": math.ceil(n_agents / settings.memory_batch) if survey else 0,
    }
    total = sum(calls.values())
    waves = lambda n: math.ceil(n / max(concurrency, 1))  # noqa: E731
    seconds = (
        waves(n_chunks) * LATENCY_S["ontology"]
        + LATENCY_S["merge"]
        + LATENCY_S["brief"]
        + calls["personas"] * LATENCY_S["personas"]
        + rounds
        * (
            waves(decide_per_round) * LATENCY_S["decide"] * (0.6 + 0.07 * agents_per_call)
            + LATENCY_S["summary"]
        )
        + memory_rounds * waves(math.ceil(n_agents * 0.9 / settings.memory_batch)) * LATENCY_S["memory"]
        + waves(impressions) * LATENCY_S["impression"]
        + waves(calls["survey"]) * LATENCY_S["survey"]
        + (LATENCY_S["narratives"] + LATENCY_S["report"] if report_llm else 0)
    )
    per_agent_in = 1000  # perfil + memoria + feed de 8 posts
    tokens_in = (
        n_chunks * 4000
        + 3000
        + 4000
        + calls["personas"] * 3500
        + rounds * decide_per_round * (1200 + per_agent_in * agents_per_call)
        + rounds * 3500
        + calls["memory"] * 2500
        + (impressions + calls["survey"]) * 4500
        + (9000 + 12000 if report_llm else 0)
    )
    tokens_out = (
        n_chunks * 2500
        + 800
        + 2000
        + calls["personas"] * 4000
        + rounds * decide_per_round * 180 * agents_per_call
        + rounds * 500
        + calls["memory"] * 900
        + (impressions + calls["survey"]) * 2000
        + (1500 + 5000 if report_llm else 0)
    )
    cap = settings.max_calls_per_run if max_calls is None else max_calls
    return Estimate(
        calls_by_phase=calls,
        total_calls=total,
        minutes=round(seconds / 60, 1),
        input_tokens=tokens_in,
        output_tokens=tokens_out,
        max_calls=cap,
        exceeds_cap=total > cap,
        expected_active_per_round=round(active, 1),
    )


# --------------------------------------------------------------------------- motor


class Engine:
    def __init__(
        self,
        store: RunStore,
        settings: Settings,
        cfg: RunConfig,
        llm: LLM | None = None,
        on_event: EventCallback | None = None,
    ) -> None:
        self.store = store
        self.settings = settings
        self.cfg = cfg
        self.paths = store.paths
        used = count_logged_calls(self.paths.calls_log)
        self.budget = CallBudget(cfg.max_calls, reserve=settings.report_reserve_calls, used=used)
        if llm is None:
            llm = build_llm(
                settings, self.paths.root, dry_run=cfg.dry_run, budget=self.budget, seed=cfg.rng_seed
            )
        else:
            llm.budget = self.budget
            log = getattr(llm, "log", None)
            if isinstance(log, CallLog) and log.path is None:
                llm.log = CallLog(self.paths.calls_log, debug=settings.debug)  # type: ignore[attr-defined]
        self.llm = llm
        self.on_event = on_event
        self.round_hook: RoundHook | None = None
        self.stop_requested = False
        self.stage = store.get_meta("stage", "ontology")

    # ------------------------------------------------------------------ creación / apertura

    @classmethod
    def create(
        cls,
        settings: Settings,
        *,
        seed: Path,
        question: str,
        n_agents: int,
        rounds: int,
        agents_per_call: int | None = None,
        events_path: Path | None = None,
        dry_run: bool = False,
        rng_seed: int = 7,
        platform: str = "twitter",
        language: str | None = None,
        max_calls: int | None = None,
        interactive: bool = False,
        personas_from: str | None = None,
        survey: bool | None = None,
        round_interval: str | None = None,
        run_id: str | None = None,
        stance_mode: str | None = None,
        report_llm: bool | None = None,
        replica_of: str | None = None,
        launch_post: bool | None = None,
        llm: LLM | None = None,
        on_event: EventCallback | None = None,
    ) -> Engine:
        load_document(seed)  # valida antes de crear nada
        if stance_mode is not None and stance_mode not in STANCE_MODES:
            raise ValueError(f"--stance-mode debe ser uno de {STANCE_MODES}")
        if personas_from:
            src = RunStore.open_existing(settings.runs_dir / personas_from)
            try:
                n_src = len(src.load_persona_data())
            finally:
                src.close()
            if not n_src:
                raise ValueError(f"el run {personas_from} no tiene personas que reutilizar")
            n_agents = n_src
        make_platform(platform)
        events = load_events(events_path) if events_path else []
        run_id = run_id or new_run_id(seed.name)
        root = settings.runs_dir / run_id
        if (root / "swarm.db").exists():
            raise FileExistsError(f"el run {run_id} ya existe")
        store = RunStore(root)
        store.paths.seed_dir.mkdir(parents=True, exist_ok=True)
        seed_copy = store.paths.seed_dir / seed.name
        shutil.copy2(seed, seed_copy)
        if events_path:
            shutil.copy2(events_path, store.paths.seed_dir / "events.yaml")
        cfg = RunConfig(
            run_id=run_id,
            seed_file=str(seed_copy.relative_to(root)),
            seed_name=seed.name,
            question=question.strip() or "¿Cómo reaccionará la gente a esta propuesta?",
            n_agents=n_agents,
            rounds=rounds,
            agents_per_call=agents_per_call or settings.agents_per_call,
            round_interval=round_interval or settings.round_interval,
            platform=platform,
            rng_seed=rng_seed,
            dry_run=dry_run,
            language=language,
            max_calls=max_calls or settings.max_calls_per_run,
            created_at=now_iso(),
            interactive=interactive,
            personas_from=personas_from,
            survey=settings.survey if survey is None else survey,
            stance_mode=stance_mode or settings.stance_mode,
            report_llm=settings.report_llm if report_llm is None else report_llm,
            replica_of=replica_of,
            launch_post=settings.launch_post if launch_post is None else launch_post,
        )
        with store.transaction():
            store.set_meta("config", cfg)
            store.set_meta("status", "created")
            store.set_meta("stage", "ontology")
            store.set_meta("stages_done", [])
            store.set_meta("current_round", 0)
            store.set_meta("calls_used", 0)
            for ev in events:
                store.save_event(ev)
        return cls(store, settings, cfg, llm=llm, on_event=on_event)

    @classmethod
    def open(
        cls,
        settings: Settings,
        run_id: str,
        *,
        llm: LLM | None = None,
        on_event: EventCallback | None = None,
        max_calls: int | None = None,
    ) -> Engine:
        root = settings.runs_dir / run_id
        store = RunStore.open_existing(root)
        cfg = RunConfig.model_validate(store.get_meta("config"))
        if max_calls:
            cfg = cfg.model_copy(update={"max_calls": max_calls})
            store.set_meta("config", cfg)
        return cls(store, settings, cfg, llm=llm, on_event=on_event)

    # ------------------------------------------------------------------ utilidades

    def emit(self, kind: str, **data: Any) -> None:
        if self.on_event:
            self.on_event(kind, data)

    def rng(self, *parts: object) -> random.Random:
        return random.Random(":".join([str(self.cfg.rng_seed), *(str(p) for p in parts)]))

    def _stages_done(self) -> list[str]:
        return list(self.store.get_meta("stages_done", []))

    def _mark_done(self, stage: str) -> None:
        done = self._stages_done()
        if stage not in done:
            done.append(stage)
        nxt = STAGES[STAGES.index(stage) + 1] if stage != STAGES[-1] else "done"
        self.stage = nxt
        self.store.update_meta(stages_done=done, stage=nxt, calls_used=self.budget.used)

    def _set_status(self, status: str, **extra: Any) -> None:
        self.store.update_meta(status=status, calls_used=self.budget.used, updated_at=now_iso(), **extra)
        self.emit("status", status=status, **extra)

    def document(self) -> Any:
        doc = load_document(self.paths.root / self.cfg.seed_file)
        if self.cfg.language:
            doc.language = doc.language_name = self.cfg.language
        return doc

    def language(self) -> str:
        meta = self.store.get_meta("document", {})
        return self.cfg.language or meta.get("language_name") or "español"

    def load_ontology(self) -> Ontology:
        return Ontology(
            title=self.store.get_meta("document", {}).get("title", self.cfg.seed_name),
            language=self.language(),
            entities=[Entity.model_validate(d) for d in self.store.load_table_data("entities", "rowid")],
            relations=[Relation.model_validate(d) for d in self.store.load_table_data("relations", "id")],
            facts=[Fact.model_validate(d) for d in self.store.load_table_data("facts", "id")],
            tensions=[Tension.model_validate(d) for d in self.store.load_table_data("tensions", "id")],
        )

    def load_brief(self) -> ScenarioBrief:
        return ScenarioBrief.model_validate(self.store.get_meta("brief"))

    def load_personas(self) -> list[Persona]:
        return [Persona.model_validate(d) for d in self.store.load_persona_data()]

    def load_events(self) -> list[EventSpec]:
        return [EventSpec.model_validate(json.loads(r["data"])) for r in self.store.load_events()]

    def add_event(self, event: EventSpec) -> EventSpec:
        existing = self.load_events()
        if not event.id:
            event.id = f"e{len(existing) + 1:02d}"
        self.store.save_event(event)
        return event

    # ------------------------------------------------------------------ ejecución

    async def run(self, until: str | None = None) -> str:
        """Ejecuta (o continúa) el run. Devuelve el estado final: completed, paused o stopped."""
        if until is not None and until not in STAGES:
            raise ValueError(f"--until debe ser una de {STAGES}")
        models = {
            "heavy": self.settings.model_heavy,
            "medium": self.settings.model_medium,
            "light": self.settings.model_light,
        }
        self._set_status("running", pause_reason=None, reset_hint=None, models=models)
        try:
            for stage in STAGES:
                if stage in self._stages_done():
                    if until == stage:
                        break
                    continue
                self.stage = stage
                self.store.update_meta(stage=stage)
                self.emit("stage", name=stage)
                if stage == "ontology":
                    await self.stage_ontology()
                elif stage == "personas":
                    await self.stage_personas()
                elif stage == "simulation":
                    await self.stage_simulation()
                else:
                    await self.stage_report()
                self._mark_done(stage)
                if until == stage:
                    break
        except RunPaused as pause:
            self._set_status("paused", pause_reason=pause.reason, reset_hint=pause.hint)
            await self._report_after_pause(pause)
            return "paused"
        except UsageLimitError as exc:
            self._set_status("paused", pause_reason="usage_limit", reset_hint=exc.reset_hint)
            self.emit("log", message=f"Límite de uso alcanzado ({exc}). Checkpoint guardado.")
            await self._report_after_pause(RunPaused("usage_limit", exc.reset_hint))
            return "paused"
        except CallBudgetExceeded as exc:
            self._set_status("paused", pause_reason="budget")
            self.emit("log", message=str(exc))
            await self._report_after_pause(RunPaused("budget"))
            return "paused"
        except asyncio.CancelledError:
            self._set_status("paused", pause_reason="interrupted")
            raise
        except AuthError:
            self._set_status("failed", pause_reason="auth")
            raise
        except Exception as exc:
            self._set_status("failed", pause_reason=f"{type(exc).__name__}: {exc}"[:300])
            raise
        if "report" in self._stages_done():
            self._set_status("completed")
            return "completed"
        self._set_status("stopped", pause_reason=f"--until {until}")
        return "stopped"

    async def _report_after_pause(self, pause: RunPaused) -> None:
        """Al pausar con rondas hechas, se genera el informe con lo que haya (sin LLM si hay límite de uso)."""
        if self.store.get_meta("current_round", 0) < 1 or "personas" not in self._stages_done():
            return
        from swarmlab.report import generate_report

        try:
            await generate_report(
                self,
                provisional=True,
                allow_llm=pause.reason != "usage_limit",
                llm_report=self.cfg.report_llm,
            )
            self.emit("log", message=f"Informe provisional escrito en {self.paths.report}")
        except LLMError as exc:
            self.emit("log", message=f"No se pudo generar el informe provisional: {exc}")

    # ------------------------------------------------------------------ etapa 1: ontología

    async def stage_ontology(self) -> None:
        doc = self.document()
        chunks = chunk_document(doc, self.settings.chunk_chars)
        self.store.save_chunks(chunks)
        self.store.update_meta(
            document={
                "title": doc.title,
                "language": doc.language,
                "language_name": doc.language_name,
                "chars": doc.chars,
                "chunks": len(chunks),
            }
        )
        self.emit(
            "log",
            message=f"Documento: {doc.title} ({doc.chars} caracteres, {len(chunks)} trozos, {doc.language_name})",
        )
        ont, stats = await build_ontology(
            self.llm,
            doc,
            chunks,
            self.cfg.question,
            self.settings,
            self.store,
            lambda m: self.emit("log", message=m),
        )
        self.store.save_ontology_rows(ont.entities, ont.relations, ont.facts, ont.tensions)
        brief = await generate_brief(self.llm, ont, doc, self.cfg.question, self.settings, self.store)
        g = build_knowledge_graph(ont)
        gstats = graph_stats(g)
        self.paths.graph_json.write_text(
            json.dumps(graph_to_json(g), ensure_ascii=False, indent=1), encoding="utf-8"
        )
        self.store.update_meta(brief=brief, ontology_stats=stats, graph_stats=gstats, graph=graph_to_json(g))
        self.paths.brief.write_text(render_brief_md(brief, ont, self.cfg.question, stats), encoding="utf-8")
        try:
            plot_knowledge_graph(g, self.paths.charts_dir / "knowledge_graph.png")
        except Exception as exc:
            self.emit("log", message=f"No se pudo dibujar el grafo: {exc}")
        self.emit(
            "ontology", entities=len(ont.entities), relations=len(ont.relations), stats=stats, graph=gstats
        )

    # ------------------------------------------------------------------ etapa 2: personas

    def _personas_from_run(self, run_id: str) -> tuple[list[Persona], Any, list[tuple[str, str]]]:
        """Reutiliza la población (y su grafo inicial) de otro run, para A/B con la misma muestra."""
        src = RunStore.open_existing(self.settings.runs_dir / run_id)
        try:
            personas = [Persona.model_validate(d) for d in src.load_persona_data()]
            div = src.get_meta("diversity")
            src_seed = src.get_meta("config", {}).get("rng_seed", self.cfg.rng_seed)
        finally:
            src.close()
        edges = build_follow_graph(personas, random.Random(f"{src_seed}:follow"))
        return personas, div, edges

    async def stage_personas(self) -> None:
        mode = self.cfg.stance_mode
        if self.cfg.personas_from:
            personas, div, edges = self._personas_from_run(self.cfg.personas_from)
            self.emit(
                "log", message=f"Población reutilizada de {self.cfg.personas_from} ({len(personas)} agentes)"
            )
            if mode == "impresion":
                # mismas personas y mismo grafo social; la opinión se vuelve a medir con ESTA propuesta
                personas = await first_impressions(
                    self.llm,
                    personas,
                    self.load_brief(),
                    self.language(),
                    self.settings,
                    self.rng("impression"),
                    self.store,
                    keep_social=True,
                    on_step=lambda m: self.emit("log", message=m),
                )
                div = diversity_report(personas, mode)
        else:
            ont, brief = self.load_ontology(), self.load_brief()
            personas, div = await generate_personas(
                self.llm,
                brief,
                ont,
                self.cfg.n_agents,
                self.cfg.question,
                self.language(),
                self.settings,
                self.rng("personas"),
                self.store,
                lambda m: self.emit("log", message=m),
                stance_mode=mode,
            )
            edges = build_follow_graph(personas, self.rng("follow"))
        followers: dict[str, int] = defaultdict(int)
        for _, b in edges:
            followers[b] += 1
        with self.store.transaction():
            self.store.save_personas(personas)
            self.store.replace_follows(edges)
            self.store.save_agent_states(
                0, [(p.id, p.initial_stance, False, followers[p.id]) for p in personas]
            )
            self.store.save_memories([AgentMemory(agent_id=p.id) for p in personas])
            self.store.set_meta("diversity", div)
        self.paths.personas_json.write_text(
            json.dumps([p.model_dump() for p in personas], ensure_ascii=False, indent=1), encoding="utf-8"
        )
        self.emit(
            "personas",
            count=len(personas),
            diversity=div.model_dump() if hasattr(div, "model_dump") else div,
            follows=len(edges),
        )

    # ------------------------------------------------------------------ etapa 3: simulación

    def expected_round_calls(self, personas: list[Persona], round_no: int) -> int:
        active = sum(p.activity for p in personas)
        calls = math.ceil(active / self.cfg.agents_per_call) + 1
        if round_no % self.settings.memory_every == 0:
            calls += math.ceil(len(personas) * 0.9 / self.settings.memory_batch)
        return calls

    def load_platform(self, personas: list[Persona]) -> Platform:
        cls = type(make_platform(self.cfg.platform))
        return cls.load(self.store, [Account(id=p.id, handle=p.handle, name=p.name) for p in personas])

    def ensure_launch_post(self) -> None:
        """La ronda 1 empieza con el anuncio de la propuesta, fijado en todos los feeds.

        Sin él, el feed de la ronda 1 está vacío: si nadie tiene una opinión extrema, nadie publica
        («feed vacío, nada que comentar») y la conversación no arranca. Una propuesta real se anuncia.
        """
        if not self.cfg.launch_post or int(self.store.get_meta("current_round", 0)) >= 1:
            return
        if any(e.origin == "auto" for e in self.load_events()):
            return
        brief = self.load_brief()
        self.add_event(
            EventSpec(
                round=1,
                title=brief.one_liner[:120],
                text=f"{brief.one_liner}\n{brief.proposal}"[:700],
                source="Anuncio de la propuesta",
                handle="lanzamiento",
                origin="auto",
            )
        )

    async def stage_simulation(self) -> None:
        self.ensure_launch_post()
        personas = self.load_personas()
        plat = self.load_platform(personas)
        memories = {m["agent_id"]: AgentMemory.model_validate(m) for m in self.store.load_memory_data()}
        stances = self.store.latest_stances()
        start = int(self.store.get_meta("current_round", 0)) + 1
        for r in range(start, self.cfg.rounds + 1):
            if self.stop_requested:
                raise RunPaused("stop_requested")
            need = self.expected_round_calls(personas, r)
            remaining = self.budget.remaining
            if remaining is not None and remaining < need:
                raise RunPaused("budget", f"quedan {remaining} llamadas y la ronda {r} necesita ~{need}")
            await self.run_round(r, personas, plat, memories, stances)
            if self.stop_requested:
                raise RunPaused("stop_requested")
            if self.round_hook is not None and r < self.cfg.rounds:
                answer = await self.round_hook(r)
                if answer == "stop":
                    raise RunPaused("stop_requested")
                if isinstance(answer, list):
                    for ev in answer:
                        self.add_event(ev)

    async def run_round(
        self,
        r: int,
        personas: list[Persona],
        plat: Platform,
        memories: dict[str, AgentMemory],
        stances: dict[str, float],
    ) -> None:
        by_id = {p.id: p for p in personas}
        self.emit("round_start", round=r, rounds=self.cfg.rounds)
        # 1. eventos de la ronda (visibles para todos los que actúan en ella)
        events = [e for e in self.load_events() if e.round == r]
        event_posts = {ev.id: plat.publish_event(r, ev.handle, ev.source, ev.post_text).id for ev in events}
        events_text = (
            "EVENTOS DE ESTA RONDA (aparecen fijados en todos los feeds): "
            + " | ".join(f"[{event_posts[e.id]}] {e.source}: {e.post_text}" for e in events)
            if events
            else "Sin eventos externos esta ronda."
        )
        # 2. quién actúa y qué ve (estado congelado de la ronda anterior)
        active = select_active(personas, self.rng("activation", r))
        feeds: dict[str, list[FeedItem]] = {
            p.id: plat.build_feed(
                p.id,
                stances,
                r,
                self.rng("feed", r, p.id),
                self.settings.feed_size,
                self.settings.post_max_chars,
            )
            for p in active
        }
        blocks = {
            p.id: agent_block(
                p,
                stances[p.id],
                memories[p.id],
                feeds[p.id],
                sorted(plat.handle(x) for x in plat.following(p.id)),
            )
            for p in active
        }
        batches = make_batches(
            [p.id for p in active], stances, self.cfg.agents_per_call, self.rng("batches", r)
        )
        brief = self.load_brief()
        ctx = RoundContext(
            round_no=r,
            rounds=self.cfg.rounds,
            interval=self.cfg.round_interval,
            brief=compact_brief(brief),
            events=events_text,
            language=self.language(),
            platform=plat.name,
        )
        self.emit("batches", round=r, active=len(active), batches=len(batches))
        results = await asyncio.gather(
            *(
                decide_batch(
                    self.llm, [by_id[i] for i in b], blocks, feeds, stances, ctx, self.settings, self.store, k
                )
                for k, b in enumerate(batches)
            ),
            return_exceptions=True,
        )
        decisions: list[AgentDecision] = []
        for batch, res in zip(batches, results, strict=True):
            if isinstance(res, UsageLimitError | CallBudgetExceeded | AuthError | asyncio.CancelledError):
                raise res
            if isinstance(res, LLMCallFailed | StructuredOutputError):
                self.emit(
                    "log",
                    message=f"Ronda {r}: un lote de {len(batch)} agentes falló ({res}); no actúan esta ronda",
                )
                continue
            if isinstance(res, BaseException):
                raise res
            decisions += [d for d in res.decisions if d.agent_id in batch]
        # 3. aplicación al cerrar la ronda, en orden determinista
        step = self.settings.max_stance_step
        new_stances = dict(stances)
        outcomes: list[ActionOutcome] = []
        decision_rows: list[dict[str, Any]] = []
        for dec in sorted(decisions, key=lambda d: d.agent_id):
            before = stances[dec.agent_id]
            after = round(min(max(dec.new_stance, before - step), before + step), 3)
            new_stances[dec.agent_id] = after
            outcomes += plat.apply_decision(
                dec, r, after, self.settings.max_actions_per_agent, self.settings.post_max_chars
            )
            decision_rows.append(
                {
                    "round": r,
                    "agent_id": dec.agent_id,
                    "stance_before": before,
                    "stance_requested": dec.new_stance,
                    "stance_after": after,
                    "reasoning": dec.reasoning[:400],
                }
            )
        record_round(memories, outcomes, feeds, plat, stances, new_stances, r, self.settings.memory_recent)
        if r % self.settings.memory_every == 0:
            await summarize_memories(self.llm, memories, by_id, r, self.language(), self.settings, self.store)
        # 4. métricas y resumen de la ronda
        initial = {p.id: p.initial_stance for p in personas}
        eng: dict[str, float] = defaultdict(float)
        for p in plat.posts.values():
            if p.round == r and p.parent_id and p.parent_id in plat.posts:
                eng[plat.handle(plat.posts[p.parent_id].author_id)] += 1
        for o in outcomes:
            if o.valid and o.type == "like" and o.target_post_id in plat.posts:
                eng[plat.handle(plat.posts[o.target_post_id].author_id)] += 1
        stats = round_metrics(
            r,
            stances,
            new_stances,
            initial,
            [o.row() for o in outcomes],
            [p.id for p in active],
            eng,
            [e.title for e in events],
        )
        shifts = [
            f"@{by_id[a].handle}: {stances[a]:+.2f} → {new_stances[a]:+.2f}"
            for a in sorted(new_stances, key=lambda a: -abs(new_stances[a] - stances[a]))
            if abs(new_stances[a] - stances[a]) >= 0.2
        ][:8]
        try:
            summary = await summarize_round(
                self.llm,
                plat,
                r,
                self.cfg.rounds,
                self.cfg.round_interval,
                compact_brief(brief, 900),
                self.language(),
                stats,
                shifts,
                [e.title for e in events],
                len(personas),
                self.settings,
                self.store,
            )
        except (LLMCallFailed, StructuredOutputError) as exc:
            self.emit("log", message=f"Ronda {r}: resumen sin LLM ({exc})")
            summary = fallback_summary(plat, r, stats, [e.title for e in events])
        # 5. checkpoint atómico
        followers = plat.follower_counts()
        active_ids = {p.id for p in active}
        with self.store.transaction():
            plat.persist_changes(self.store)
            self.store.insert_actions([o.row() for o in outcomes])
            self.store.insert_decisions(decision_rows)
            self.store.save_agent_states(
                r, [(p.id, new_stances[p.id], p.id in active_ids, followers.get(p.id, 0)) for p in personas]
            )
            self.store.save_memories(list(memories.values()))
            for ev in events:
                self.store.save_event(ev, event_posts[ev.id])
            self.store.save_round_summary(r, summary)
            self.store.save_metrics(r, stats)
            self.store.set_meta("current_round", r)
            self.store.set_meta("calls_used", self.budget.used)
            self.store.set_meta("updated_at", datetime.now(UTC).isoformat(timespec="seconds"))
        stances.clear()
        stances.update(new_stances)
        self.emit("round_end", round=r, rounds=self.cfg.rounds, stats=stats, summary=summary.model_dump())

    # ------------------------------------------------------------------ etapa 4: informe

    async def stage_report(self) -> None:
        from swarmlab.report import generate_report

        await generate_report(self, provisional=False, allow_llm=True, llm_report=self.cfg.report_llm)


def round_summaries(store: RunStore) -> list[RoundSummary]:
    return [RoundSummary.model_validate(d) for d in store.round_summaries()]
