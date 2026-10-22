"""Fase 6: A/B con la misma población, plataforma foro y export a PDF (todo en dry-run)."""

from __future__ import annotations

import asyncio
from pathlib import Path

from pypdf import PdfReader

from swarmlab.compare import compare_runs
from swarmlab.config import Settings
from swarmlab.engine import Engine
from swarmlab.llm import FakeClaudeCLI
from swarmlab.pdf import report_to_pdf


def _run(settings: Settings, seed: Path, run_id: str, **kw: object) -> Engine:
    engine = Engine.create(
        settings,
        seed=seed,
        question="¿Qué versión convence más?",
        n_agents=10,
        rounds=2,
        agents_per_call=5,
        dry_run=True,
        run_id=run_id,
        llm=FakeClaudeCLI(seed=1),
        **kw,  # type: ignore[arg-type]
    )
    assert asyncio.run(engine.run()) == "completed"
    return engine


def test_ab_with_same_population_and_pdf(settings: Settings, example: Path, tmp_path: Path) -> None:
    a = _run(settings, example, "idea-a")
    variant = tmp_path / "idea-b.md"
    variant.write_text(
        example.read_text(encoding="utf-8").replace("29 € al mes", "19 € al mes"), encoding="utf-8"
    )
    b = _run(settings, variant, "idea-b", personas_from="idea-a")
    pa = {p["id"]: p["handle"] for p in a.store.load_persona_data()}
    pb = {p["id"]: p["handle"] for p in b.store.load_persona_data()}
    assert pa == pb
    fa = sorted((r["follower_id"], r["followee_id"]) for r in a.store.load_follows() if r["since_round"] == 0)
    fb = sorted((r["follower_id"], r["followee_id"]) for r in b.store.load_follows() if r["since_round"] == 0)
    assert fa == fb  # mismo grafo social inicial
    md, path = compare_runs(settings, "idea-a", "idea-b")
    assert path.is_file() and "Veredicto" in md and "Misma población" in md
    assert "| Postura media final |" in md

    pdf = report_to_pdf(a.paths.root)
    text = "".join(page.extract_text() for page in PdfReader(str(pdf)).pages)
    assert "Informe de predicción" in text and "Anexo" in text


def test_forum_platform_end_to_end(settings: Settings, example: Path) -> None:
    engine = _run(settings, example, "foro", platform="forum")
    kinds = {r["kind"] for r in engine.store.load_posts()}
    assert "repost" not in kinds
    invalid = engine.store.conn.execute(
        "SELECT COUNT(*) FROM actions WHERE type='repost' AND valid=1"
    ).fetchone()[0]
    assert invalid == 0


def test_survey_measures_intent_by_segment_and_feeds_dossier(settings: Settings, example: Path) -> None:
    engine = _run(settings, example, "encuesta", survey=True)
    data = engine.store.get_meta("survey")
    assert data and len(data["answers"]) == 10 and data["round"] == 2
    overall = data["summary"]["overall"]
    assert overall["n"] == 10 and sum(overall["intent"].values()) == 10
    assert 0.0 <= overall["adoption_index"] <= 1.0
    assert set(data["summary"]["by_segment"]) == {p["segment"] for p in engine.store.load_persona_data()}
    dossier = engine.paths.dossier.read_text(encoding="utf-8")
    assert "Encuesta final de intención" in dossier and "índice de adopción" in dossier
