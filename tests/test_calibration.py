"""Medición honesta: primera impresión, encuesta con medidas estrictas, réplicas, A/B y calibración."""

from __future__ import annotations

import asyncio
import json
import random
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from swarmlab.cli import app
from swarmlab.compare import compare_runs
from swarmlab.config import Settings
from swarmlab.engine import Engine, estimate_run
from swarmlab.events import EventSpec
from swarmlab.ingest import chunk_document, load_document
from swarmlab.llm import FakeClaudeCLI
from swarmlab.ontology import build_ontology, generate_brief
from swarmlab.personas import ACTIVITY_RANGES, generate_personas, stance_bucket_for
from swarmlab.scorecard import noise_rows, outcome_rows, run_metrics, save_noise_reference, scorecard_md
from swarmlab.store import RunStore, expand_run_ids
from swarmlab.survey import block, is_firm_buyer, price_summary, summarize


def _run(settings: Settings, seed: Path, run_id: str, **kw: Any) -> Engine:
    engine = Engine.create(
        settings,
        seed=seed,
        question="¿Lo comprarían?",
        n_agents=kw.pop("n_agents", 10),
        rounds=kw.pop("rounds", 2),
        agents_per_call=5,
        dry_run=True,
        run_id=run_id,
        llm=kw.pop("llm", None) or FakeClaudeCLI(seed=1),
        **kw,
    )
    assert asyncio.run(engine.run()) == "completed"
    return engine


def _variant(example: Path, tmp_path: Path, title: str, name: str) -> Path:
    text = example.read_text(encoding="utf-8")
    _, rest = text.split("\n", 1)
    path = tmp_path / name
    path.write_text(f"# {title}\n{rest}", encoding="utf-8")
    return path


# --------------------------------------------------------------------------- primera impresión


def test_impression_mode_measures_stance_instead_of_fixing_it(example: Path, settings: Settings) -> None:
    llm = FakeClaudeCLI(seed=3)
    doc = load_document(example)
    ont, _ = asyncio.run(build_ontology(llm, doc, chunk_document(doc), "¿?", settings))
    brief = asyncio.run(generate_brief(llm, ont, doc, "¿?", settings))
    personas, div = asyncio.run(
        generate_personas(
            llm, brief, ont, 20, "¿?", "español", settings, random.Random(5), stance_mode="impresion"
        )
    )
    profile_prompts = [r.prompt for r in llm.requests if r.purpose == "personas.profile"]
    assert profile_prompts and all("postura" not in p.lower() for p in profile_prompts)
    impressions = [r for r in llm.requests if r.purpose == "personas.impression"]
    assert len(impressions) == 2  # 20 agentes en lotes de MEMORY_BATCH=10
    assert div.stance_mode == "impresion"
    for p in personas:
        assert p.first_impression and p.current_solution
        assert p.stance_bucket == stance_bucket_for(p.initial_stance, p.involvement)
        lo, hi = ACTIVITY_RANGES[p.involvement]
        assert lo <= p.activity <= (0.98 if p.influencer else hi)
    assert 1 <= sum(p.influencer for p in personas) <= 3
    # sin cuotas: la distribución es la que sale de las impresiones, no un plan fijo
    assert not any("mínimo" in w for w in div.warnings)


def test_ab_same_population_remeasures_first_impression(
    settings: Settings, example: Path, tmp_path: Path
) -> None:
    a = _run(settings, _variant(example, tmp_path, "Versión A de la gestoría", "a.md"), "imp-a")
    b = _run(
        settings,
        _variant(example, tmp_path, "Versión B de la gestoría", "b.md"),
        "imp-b",
        personas_from="imp-a",
    )
    pa = {p["id"]: p for p in a.store.load_persona_data()}
    pb = {p["id"]: p for p in b.store.load_persona_data()}
    assert {k: v["handle"] for k, v in pa.items()} == {k: v["handle"] for k, v in pb.items()}
    assert {k: v["influencer"] for k, v in pa.items()} == {k: v["influencer"] for k, v in pb.items()}
    assert [pa[k]["initial_stance"] for k in pa] != [pb[k]["initial_stance"] for k in pa]
    fa = sorted((r["follower_id"], r["followee_id"]) for r in a.store.load_follows() if r["since_round"] == 0)
    fb = sorted((r["follower_id"], r["followee_id"]) for r in b.store.load_follows() if r["since_round"] == 0)
    assert fa == fb  # mismas personas, mismo grafo social; solo cambia la opinión
    md, _ = compare_runs(settings, "imp-a", "imp-b")
    assert "Misma población" in md and "Primera impresión emparejada" in md
    assert "Sin réplicas no hay veredicto" in md


def test_quota_mode_still_available(settings: Settings, example: Path) -> None:
    e = _run(settings, example, "cuotas", stance_mode="cuotas")
    assert e.cfg.stance_mode == "cuotas"
    personas = e.store.load_persona_data()
    assert sum(p["initial_stance"] <= -0.2 for p in personas) >= 0.15 * len(personas)
    assert all(not p["first_impression"] for p in personas)
    with pytest.raises(ValueError):
        Engine.create(
            settings, seed=example, question="?", n_agents=10, rounds=1, dry_run=True, stance_mode="otra"
        )


def test_estimate_counts_impressions_and_optional_report(settings: Settings) -> None:
    kw: dict[str, Any] = {"n_chunks": 1, "n_agents": 20, "rounds": 5, "agents_per_call": 7, "concurrency": 3}
    quotas = estimate_run(settings, **kw, stance_mode="cuotas")
    impression = estimate_run(settings, **kw, stance_mode="impresion")
    lean = estimate_run(settings, **kw, stance_mode="impresion", report_llm=False)
    assert impression.calls_by_phase["personas"] == quotas.calls_by_phase["personas"] + 2
    assert lean.total_calls == impression.total_calls - 2 and lean.minutes < impression.minutes


def test_no_report_llm_skips_narratives_and_report(settings: Settings, example: Path) -> None:
    e = _run(settings, example, "medicion", survey=True, report_llm=False)
    purposes = [json.loads(line)["purpose"] for line in e.paths.calls_log.read_text().splitlines()]
    assert "survey" in purposes and "narratives" not in purposes and "report" not in purposes
    assert "REPORT_LLM=false" in e.paths.report.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- encuesta


def _answer(**kw: Any) -> dict[str, Any]:
    base = {
        "agent_id": "a01",
        "role": "cliente",
        "current_alternative": "nada",
        "reason": "r",
        "deal_breaker": "d",
        "would_change_mind": "w",
        "choice": "propuesta",
        "intent": "si",
        "purchase_probability": 8,
        "would_pay": "si",
        "pay_certainty": 9,
        "max_price": 30.0,
        "fair_price": "30",
        "commitment": "pagaria_ya",
    }
    return base | kw


def test_strict_purchase_measures() -> None:
    answers = [
        _answer(),  # comprador firme
        _answer(pay_certainty=6),  # «sí» poco seguro: no cuenta como pago seguro
        _answer(choice="alternativa", commitment="nada", intent="probablemente"),
        _answer(would_pay="no", pay_certainty=0, intent="no", choice="nada", commitment="nada", max_price=0),
    ]
    assert [is_firm_buyer(a) for a in answers] == [True, False, False, False]
    b = block(answers)
    assert b["would_pay_share"] == 0.75 and b["would_pay_certain_share"] == 0.5
    assert b["choice_share"] == 0.5 and b["firm_share"] == 0.25 and b["firm_count"] == 1
    assert b["firm_ci"][0] < 0.25 < b["firm_ci"][1]
    assert b["juster_mean"] == 0.8


def test_old_survey_answers_still_summarize() -> None:
    old = [{k: v for k, v in _answer().items() if k in {"agent_id", "role", "intent", "would_pay", "fair_price", "reason", "deal_breaker", "would_change_mind"}}]  # fmt: skip
    s = summarize(old, {"a01": {"segment": "S"}})
    assert s["overall"]["would_pay_share"] == 1.0 and s["overall"]["choice_share"] is None
    assert s["overall"]["v2_n"] == 0 and s["price"] is None


def test_price_curve_against_reference() -> None:
    answers = [_answer(agent_id=f"a{i}", max_price=p) for i, p in enumerate([0, 10, 20, 30, 40])]
    p = price_summary(answers, {"price": 20.0, "unit": "€/mes"})
    assert p is not None and p["median"] == 20 and p["unit"] == "€/mes"
    assert p["at_reference_share"] == 0.6
    assert dict((x, y) for x, y in p["curve"])[40.0] == 0.2


def test_survey_v2_end_to_end(settings: Settings, example: Path) -> None:
    e = _run(settings, example, "encuesta2", survey=True)
    data = e.store.get_meta("survey")
    assert data["version"] == 2 and data["reference"] == {"price": 29.0, "unit": "€/mes"}
    o = data["summary"]["overall"]
    assert o["v2_n"] == 10 and o["choice_ci"] is not None
    dossier = e.paths.dossier.read_text(encoding="utf-8")
    assert "compradores firmes" in dossier and "Curva de demanda" in dossier
    assert "postura inicial MEDIDA" in dossier and "Primera impresión" in dossier


# --------------------------------------------------------------------------- réplicas, scorecard, calibración


def _row(group: str, stem: str, initial: float, choice: float) -> dict[str, Any]:
    return {
        "run_id": f"{group}-{initial}",
        "group": group,
        "seed_stem": stem,
        "initial_mean": initial,
        "choice_all": choice,
        "final_negative": 1 - choice,
        "agents": 20,
        "rounds_completed": 5,
        "stance_mode": "impresion",
    }


def test_noise_and_outcome_tables() -> None:
    rows = [
        _row("ok1", "c1", 0.30, 0.60),
        _row("ok1", "c1", 0.32, 0.55),
        _row("ok2", "c2", 0.25, 0.50),
        _row("ok2", "c2", 0.27, 0.52),
        _row("ko1", "c3", -0.10, 0.10),
        _row("ko1", "c3", -0.08, 0.15),
    ]
    noise = {r["metric"]: r for r in noise_rows(rows)}
    assert noise["initial_mean"]["icc"] > 0.9 and noise["initial_mean"]["groups"] == 3
    outcomes = {
        "c1": {"resultado": "exito", "porcentaje": 200},
        "c2": {"resultado": "exito", "porcentaje": 160},
    }
    outcomes["c3"] = {"resultado": "fracaso", "porcentaje": 20}
    res = {r["metric"]: r for r in outcome_rows(rows, outcomes)}
    assert res["initial_mean"]["auc"] == 1.0 and res["initial_mean"]["n_exito"] == 2
    assert res["final_negative"]["auc"] == 1.0  # menos es mejor: se invierte
    assert res["initial_mean"]["spearman"] == pytest.approx(1.0)


def test_expand_run_ids(settings: Settings, example: Path) -> None:
    for rid in ("x1", "x2", "y1"):
        _run(settings, example, rid, rounds=1)
    assert expand_run_ids(settings.runs_dir, ["x*"]) == ["x1", "x2"]
    assert expand_run_ids(settings.runs_dir, ["y1,x1", "x1"]) == ["y1", "x1"]
    with pytest.raises(FileNotFoundError):
        expand_run_ids(settings.runs_dir, ["nada*"])
    with pytest.raises(FileNotFoundError):
        expand_run_ids(settings.runs_dir, ["z9"])


def test_replicate_scorecard_calibrate_and_compare(settings: Settings, example: Path, tmp_path: Path) -> None:
    runner = CliRunner()
    env = {"SWARMLAB_RUNS_DIR": str(settings.runs_dir)}
    _run(settings, example, "idea", survey=True, report_llm=False)
    store = RunStore.open_existing(settings.runs_dir / "idea")
    store.save_event(EventSpec(round=2, title="Noticia", id="e01"))
    store.close()
    res = runner.invoke(app, ["replicate", "idea", "-n", "2"], env=env)
    assert res.exit_code == 0, res.output
    for k, seed in ((2, 8), (3, 9)):
        s = RunStore.open_existing(settings.runs_dir / f"idea-r{k}")
        cfg = s.get_meta("config")
        assert cfg["replica_of"] == "idea" and cfg["rng_seed"] == seed and cfg["report_llm"] is False
        origins = sorted(
            (json.loads(r["data"])["origin"], json.loads(r["data"])["title"]) for r in s.load_events()
        )
        assert [o for o, _ in origins] == ["auto", "yaml"] and origins[1][1] == "Noticia"  # su propio anuncio
        s.close()
    variant = _variant(example, tmp_path, "Otra idea", "otra.md")
    _run(settings, variant, "otra", survey=True, report_llm=False, personas_from="idea")
    res = runner.invoke(app, ["replicate", "otra", "-n", "2"], env=env)
    assert res.exit_code == 0, res.output
    s = RunStore.open_existing(settings.runs_dir / "otra-r3")
    assert s.get_meta("config")["personas_from"] == "idea-r3"  # A/B replicado y emparejado
    s.close()

    res = runner.invoke(app, ["scorecard", "idea*", "otra*"], env=env)
    assert res.exit_code == 0 and "idea (×3)" in res.output and "otra (×3)" in res.output

    out = tmp_path / "cal.md"
    cases = tmp_path / "casos.toml"
    cases.write_text('[gestoria-ia]\nresultado = "exito"\nporcentaje = 150\n[otra]\nresultado = "fracaso"\n')
    res = runner.invoke(
        app, ["calibrate", "idea*", "otra*", "--outcomes", str(cases), "--out", str(out)], env=env
    )
    assert res.exit_code == 0, res.output
    text = out.read_text(encoding="utf-8")
    assert "ICC" in text and "AUC" in text
    assert (settings.runs_dir / "_calibration" / "ruido.json").is_file()

    md, _ = compare_runs(settings, "idea,idea-r2,idea-r3", "otra*")
    assert "réplicas (Welch)" in md and "Réplicas: A = 3, B = 3" in md
    md1, _ = compare_runs(settings, "idea", "otra")
    assert "ruido de referencia" in md1 and "Aproximado" in md1


def test_scorecard_row_and_markdown(settings: Settings, example: Path) -> None:
    e = _run(settings, example, "fila", survey=True, report_llm=False)
    row = run_metrics(e.store)
    assert row["stance_mode"] == "impresion" and row["customers_n"] > 0
    assert row["choice_customers"] is not None and row["price"]["reference"] == 29.0
    md = scorecard_md([row])
    assert "| fila | impresion |" in md and "[" in md  # IC de Wilson en la celda de un run suelto
    path = save_noise_reference(settings.runs_dir, [], [row])
    assert json.loads(path.read_text())["runs"] == ["fila"]
