"""Motor end-to-end con FakeClaudeCLI: dry-run completo, pausas, reanudación y determinismo."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from swarmlab.config import Settings
from swarmlab.engine import Engine, estimate_run
from swarmlab.ingest import chunk_document, load_document
from swarmlab.llm import FakeClaudeCLI, count_logged_calls
from swarmlab.report import SECTION_HEADERS, check_report, deterministic_report
from swarmlab.store import RunStore


def create(
    settings: Settings, example: Path, events: Path | None, run_id: str, llm: Any = None, **kw: Any
) -> Engine:
    return Engine.create(
        settings,
        seed=example,
        question="¿Cómo reaccionarán autónomos y pymes?",
        n_agents=kw.pop("n_agents", 12),
        rounds=kw.pop("rounds", 4),
        agents_per_call=kw.pop("agents_per_call", 4),
        events_path=events,
        dry_run=True,
        run_id=run_id,
        llm=llm or FakeClaudeCLI(seed=7),
        **kw,
    )


def final_state(store: RunStore) -> dict[str, Any]:
    return {
        "stances": {
            r["agent_id"]: r["stance"]
            for r in store.agent_states()
            if r["round"] == store.get_meta("current_round")
        },
        "posts": [(r["id"], r["author_id"], r["text"], r["likes"]) for r in store.load_posts()],
        "follows": [(r["follower_id"], r["followee_id"]) for r in store.load_follows()],
    }


def test_dry_run_end_to_end(settings: Settings, example: Path, events_file: Path) -> None:
    engine = create(settings, example, events_file, "e2e", n_agents=30, rounds=8, agents_per_call=6)
    status = asyncio.run(engine.run())
    assert status == "completed"
    paths = engine.paths
    for f in (
        paths.brief,
        paths.graph_json,
        paths.personas_json,
        paths.report,
        paths.dossier,
        paths.calls_log,
    ):
        assert f.is_file(), f
    for name in (
        "stance_mean",
        "stance_distribution",
        "activity",
        "polarization",
        "influencers",
        "knowledge_graph",
    ):
        assert (paths.charts_dir / f"{name}.png").is_file()
    for name in ("metrics.json", "rounds.csv", "agents.csv", "posts.csv"):
        assert (paths.metrics_dir / name).is_file()
    store = engine.store
    assert store.get_meta("current_round") == 8 and store.get_meta("status") == "completed"
    rows = store.load_events()
    launch = [r for r in rows if json.loads(r["data"])["origin"] == "auto"]
    events = [r for r in rows if json.loads(r["data"])["origin"] != "auto"]
    assert [r["round"] for r in launch] == [1] and launch[0]["post_id"]  # anuncio de la propuesta
    assert [r["round"] for r in events] == [4, 6] and all(r["post_id"] for r in events)
    event_posts = {r["post_id"] for r in events}
    assert {p["round"] for p in store.load_posts() if p["id"] in event_posts} == {4, 6}
    assert len(store.round_summaries()) == 8 and len(store.metrics()) == 8
    report = paths.report.read_text()
    check = check_report(report, {p["id"] for p in store.load_posts()}, set())
    assert not check.missing_sections and check.post_citations >= 8
    # número de llamadas dentro del ±20 % del estimado
    doc = load_document(example)
    est = estimate_run(
        settings,
        n_chunks=len(chunk_document(doc, settings.chunk_chars)),
        n_agents=30,
        rounds=8,
        agents_per_call=6,
        concurrency=3,
        stance_mode=settings.stance_mode,
    )
    used = count_logged_calls(paths.calls_log)
    assert abs(used - est.total_calls) <= 0.2 * est.total_calls, (used, est.total_calls)


def test_round_decisions_use_frozen_state(settings: Settings, example: Path) -> None:
    engine = create(settings, example, None, "frozen", rounds=3)
    asyncio.run(engine.run())
    rows = engine.store.conn.execute(
        "SELECT a.round, p.round AS target_round, p.kind FROM actions a JOIN posts p ON p.id = a.target_post_id "
        "WHERE a.valid = 1"
    ).fetchall()
    # solo los eventos de la ronda (p. ej. el anuncio de la propuesta en R1) se ven en su misma ronda
    assert rows and all(r["target_round"] < r["round"] or r["kind"] == "event" for r in rows)
    # el cambio de postura por ronda está acotado
    for d in engine.store.conn.execute("SELECT * FROM decisions"):
        assert abs(d["stance_after"] - d["stance_before"]) <= settings.max_stance_step + 1e-9


def test_usage_limit_pauses_and_resume_matches_uninterrupted_run(
    settings: Settings, example: Path, events_file: Path
) -> None:
    reference = create(settings, example, events_file, "ref")
    assert asyncio.run(reference.run()) == "completed"
    ref_calls = count_logged_calls(reference.paths.calls_log)

    limited = create(
        settings, example, events_file, "paused", llm=FakeClaudeCLI(seed=7, usage_limit_at=ref_calls - 8)
    )
    assert asyncio.run(limited.run()) == "paused"
    meta = limited.store.meta_all()
    assert meta["pause_reason"] == "usage_limit" and meta["reset_hint"] == "3:45pm"
    assert 0 < meta["current_round"] < 4
    assert limited.paths.report.is_file()  # informe provisional sin LLM
    assert "sin LLM" in limited.paths.report.read_text()

    resumed = Engine.open(settings, "paused", llm=FakeClaudeCLI(seed=7))
    assert asyncio.run(resumed.run()) == "completed"
    assert final_state(resumed.store) == final_state(reference.store)
    # las llamadas ya pagadas antes de la pausa no se repiten (caché por etapa)
    total = count_logged_calls(resumed.paths.calls_log)
    assert total <= ref_calls + 2, (total, ref_calls)


class CancelAt(FakeClaudeCLI):
    """Simula un Ctrl+C (cancelación de la tarea) en la llamada N."""

    def __init__(self, at: int, **kw: Any) -> None:
        super().__init__(**kw)
        self.at = at

    async def complete(self, prompt: str, **kw: Any):  # type: ignore[override]
        if self.stats.calls + 1 == self.at:
            raise asyncio.CancelledError
        return await super().complete(prompt, **kw)


def test_ctrl_c_interrupt_then_resume(settings: Settings, example: Path) -> None:
    reference = create(settings, example, None, "ref2")
    asyncio.run(reference.run())
    engine = create(settings, example, None, "cancel", llm=CancelAt(at=12, seed=7))
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(engine.run())
    assert (
        engine.store.get_meta("status") == "paused" and engine.store.get_meta("pause_reason") == "interrupted"
    )
    resumed = Engine.open(settings, "cancel", llm=FakeClaudeCLI(seed=7))
    assert asyncio.run(resumed.run()) == "completed"
    assert final_state(resumed.store) == final_state(reference.store)


def test_stop_request_pauses_after_current_round(settings: Settings, example: Path) -> None:
    engine = create(settings, example, None, "stop")

    def on_event(kind: str, data: dict[str, Any]) -> None:
        if kind == "round_end" and data["round"] == 2:
            engine.stop_requested = True

    engine.on_event = on_event
    assert asyncio.run(engine.run()) == "paused"
    assert engine.store.get_meta("current_round") == 2
    assert engine.store.get_meta("report")["provisional"] is True


def test_call_budget_stops_before_round_and_keeps_report_reserve(settings: Settings, example: Path) -> None:
    engine = create(settings, example, None, "budget", max_calls=20)
    assert asyncio.run(engine.run()) == "paused"
    meta = engine.store.meta_all()
    assert meta["pause_reason"] == "budget"
    assert meta["report"]["llm"] is True  # las llamadas reservadas permiten el informe con LLM
    assert count_logged_calls(engine.paths.calls_log) <= 20
    resumed = Engine.open(settings, "budget", llm=FakeClaudeCLI(seed=7), max_calls=200)
    assert asyncio.run(resumed.run()) == "completed"


def test_interactive_hook_injects_event(settings: Settings, example: Path) -> None:
    from swarmlab.events import EventSpec

    engine = create(settings, example, None, "god", rounds=3)

    async def hook(r: int) -> list[EventSpec] | None:
        return (
            [EventSpec(round=r + 1, title="Un competidor baja precios un 30 %", origin="interactive")]
            if r == 1
            else None
        )

    engine.round_hook = hook
    asyncio.run(engine.run())
    events = [json.loads(r["data"]) for r in engine.store.load_events()]
    assert [(e["round"], e["origin"]) for e in events] == [(1, "auto"), (2, "interactive")]


def test_until_stage_and_deterministic_report(settings: Settings, example: Path) -> None:
    engine = create(settings, example, None, "until")
    assert asyncio.run(engine.run(until="personas")) == "stopped"
    assert engine.store.get_meta("stages_done") == ["ontology", "personas"]
    assert engine.store.get_meta("current_round") == 0
    asyncio.run(Engine.open(settings, "until", llm=FakeClaudeCLI(seed=7)).run())
    text = deterministic_report(engine.store, [], provisional=False)
    assert all(h in text for h in SECTION_HEADERS)
