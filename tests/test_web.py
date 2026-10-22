"""Panel web: lanzamiento de un run dry-run, páginas, pestañas, fichas de agente, acciones, SSE y herramientas."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from swarmlab.config import Settings, load_settings
from swarmlab.engine import Engine
from swarmlab.store import RunStore
from swarmlab.web.app import create_app

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "gestoria-ia.md"
EVENTS = ROOT / "examples" / "gestoria-events.yaml"


def wait_task(app: FastAPI, run_id: str) -> str:
    live = app.state.web.live[run_id]
    deadline = time.monotonic() + 90
    while (live.task is None or not live.task.done()) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert live.task is not None
    return live.task.result()


@pytest.fixture(scope="module")
def web(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[TestClient, str, Settings]]:
    """Un run dry-run (10 agentes x 2 rondas) lanzado desde el formulario y terminado."""
    tmp_path = tmp_path_factory.mktemp("web")
    settings = load_settings(None, {"runs_dir": tmp_path / "runs"}, cwd=tmp_path, environ={})
    app = create_app(settings, poll_interval=0.05)
    with TestClient(app) as client:
        res = client.post(
            "/runs",
            data={
                "question": "¿Cómo reaccionarán autónomos y pymes?",
                "agents": "10",
                "rounds": "2",
                "agents_per_call": "5",
                "platform": "twitter",
                "stance_mode": "impresion",
                "survey": "1",
                "report_llm": "1",
                "dry_run": "1",
                "run_id": "gestoria-web",
            },
            files={
                "seed": (EXAMPLE.name, EXAMPLE.read_bytes(), "text/markdown"),
                "events": (EVENTS.name, EVENTS.read_bytes(), "application/x-yaml"),
            },
            follow_redirects=False,
        )
        assert res.status_code == 303, res.text
        run_id = res.headers["location"].rsplit("/", 1)[-1]
        assert run_id == "gestoria-web"
        assert wait_task(app, run_id) == "completed"
        yield client, run_id, settings


def test_index_and_new_form(web: tuple[TestClient, str, Settings]) -> None:
    client, run_id, settings = web
    res = client.get("/")
    assert res.status_code == 200
    assert "Panel" in res.text and run_id in res.text and "completado" in res.text
    assert client.get("/", params={"q": "no-existe"}).text.count(f"/runs/{run_id}") == 0
    res = client.get("/nueva")
    assert res.status_code == 200
    assert "Nueva simulación" in res.text and f'value="{settings.default_agents}"' in res.text
    assert "gestoria-ia" in res.text  # ejemplos incluidos
    assert f'value="{run_id}"' in client.get("/nueva", params={"personas_from": run_id}).text


def test_create_run_validation_rerenders_form(web: tuple[TestClient, str, Settings]) -> None:
    client, _, settings = web
    before = sorted(p.name for p in settings.runs_dir.iterdir())
    res = client.post(
        "/runs",
        data={"question": "x", "agents": "3", "rounds": "2", "agents_per_call": "5", "platform": "twitter"},
        files={"seed": ("idea.md", b"# Idea\n\nTexto.", "text/markdown")},
    )
    assert res.status_code == 400
    assert "agentes" in res.text and "Nueva simulación" in res.text
    res = client.post(
        "/runs",
        data={"agents": "10", "rounds": "2", "agents_per_call": "5", "platform": "twitter"},
        files={"seed": ("idea.exe", b"binario", "application/octet-stream")},
    )
    assert res.status_code == 400 and "no soportado" in res.text
    res = client.post(
        "/runs",
        data={"agents": "10", "rounds": "2", "agents_per_call": "5", "platform": "twitter", "dry_run": "1"},
    )
    assert res.status_code == 400 and "falta el documento semilla" in res.text
    res = client.post(
        "/runs",
        data={
            "agents": "10",
            "rounds": "2",
            "agents_per_call": "5",
            "platform": "twitter",
            "dry_run": "1",
            "seed_text": "# Idea\n\nTexto.",
            "events_text": "events: [{round: 0, title: 'mal'}]",
        },
    )
    assert res.status_code == 400 and "eventos inválidos" in res.text
    assert sorted(p.name for p in settings.runs_dir.iterdir()) == before
    assert not any((settings.runs_dir / "_uploads").iterdir())


def test_estimate_partial(web: tuple[TestClient, str, Settings]) -> None:
    client, run_id, _ = web
    res = client.post(
        "/estimate", data={"example": EXAMPLE.name, "agents": "20", "rounds": "5", "survey": "1"}
    )
    assert res.status_code == 200 and "Llamadas" in res.text and "min" in res.text
    res = client.post(
        "/estimate", data={"seed_text": EXAMPLE.read_text(encoding="utf-8"), "personas_from": run_id}
    )
    assert res.status_code == 200 and "Llamadas" in res.text
    res = client.post("/estimate", data={"agents": "20"})
    assert res.status_code == 200 and "Sube o pega la semilla" in res.text


def test_run_page_and_tabs(web: tuple[TestClient, str, Settings]) -> None:
    client, run_id, settings = web
    store = RunStore.open_existing(settings.runs_dir / run_id)
    try:
        assert store.get_meta("status") == "completed"
        cfg = store.get_meta("config")
        assert cfg["dry_run"] is True and cfg["stance_mode"] == "impresion" and cfg["survey"] is True
        report = store.paths.report.read_text(encoding="utf-8")
        handles = [p["handle"] for p in store.load_persona_data()]
    finally:
        store.close()
    res = client.get(f"/runs/{run_id}")
    assert res.status_code == 200
    assert "medida (primera impresión)" in res.text and "Índice de adopción" in res.text
    assert f"/runs/{run_id}/tab/feed" in res.text and "Reanudar" not in res.text and "Pausar" not in res.text
    assert client.get(f"/runs/{run_id}", params={"tab": "informe"}).status_code == 200
    assert client.get(f"/runs/{run_id}", params={"tab": "nada"}).status_code == 404
    assert client.get("/runs/no-existe").status_code == 404

    resumen = client.get(f"/runs/{run_id}/tab/resumen").text
    assert "Postura media por ronda" in resumen and "Encuesta final" in resumen and "<svg" in resumen
    assert "Por segmento" in resumen
    feed = client.get(f"/runs/{run_id}/tab/feed").text
    assert f"/runs/{run_id}/timeline?round=2" in feed
    agentes = client.get(f"/runs/{run_id}/tab/agentes").text
    assert "Red de seguimiento" in agentes and all(f"@{h}" in agentes for h in handles)
    assert f"/runs/{run_id}/agents/{handles[0]}" in agentes
    encuesta = client.get(f"/runs/{run_id}/tab/encuesta").text
    assert "Firmes" in encuesta and "Respuestas una a una" in encuesta
    informe = client.get(f"/runs/{run_id}/tab/informe").text
    first_heading = next(line for line in report.splitlines() if line.startswith("## "))
    assert first_heading[3:].strip()[:20] in informe
    assert f"/runs/{run_id}/charts/stance_mean.png" in informe and "report.md" in informe
    eventos = client.get(f"/runs/{run_id}/tab/eventos").text
    assert "Inyectar un evento" in eventos and "anuncio automático" in eventos
    assert "Chat con el analista" in client.get(f"/runs/{run_id}/tab/chat").text
    llamadas = client.get(f"/runs/{run_id}/tab/llamadas").text
    assert "Sin llamadas registradas" in llamadas or "Por modelo" in llamadas
    assert client.get(f"/runs/{run_id}/tab/nada").status_code == 404
    assert client.get(f"/runs/{run_id}/controls").status_code == 200


def test_agents_filters_and_card(web: tuple[TestClient, str, Settings]) -> None:
    client, run_id, settings = web
    store = RunStore.open_existing(settings.runs_dir / run_id)
    try:
        personas = store.load_persona_data()
    finally:
        store.close()
    seg = personas[0]["segment"]
    res = client.get(f"/runs/{run_id}/tab/agentes", params={"segment": seg})
    assert res.status_code == 200
    shown = [p["handle"] for p in personas if p["segment"] == seg]
    hidden = [p["handle"] for p in personas if p["segment"] != seg]
    assert all(f'agents/{h}"' in res.text for h in shown)
    assert all(f'<div class="name truncate">@{h}' not in res.text for h in hidden)
    assert client.get(f"/runs/{run_id}/tab/agentes", params={"side": "influencer"}).status_code == 200
    card = client.get(f"/runs/{run_id}/agents/{personas[0]['handle']}")
    assert card.status_code == 200
    assert personas[0]["name"] in card.text and "Entrevista en personaje" in card.text
    assert "Encuesta final" in card.text and "Qué pensó en cada ronda" in card.text
    assert client.get(f"/runs/{run_id}/agents/@{personas[0]['handle']}").status_code == 200
    assert client.get(f"/runs/{run_id}/agents/nadie_asi").status_code == 404
    assert client.get(f"/runs/{run_id}/agents/..%2Fx").status_code == 404


def test_interview(web: tuple[TestClient, str, Settings]) -> None:
    client, run_id, settings = web
    store = RunStore.open_existing(settings.runs_dir / run_id)
    try:
        handle = store.load_persona_data()[0]["handle"]
    finally:
        store.close()
    res = client.post(
        f"/runs/{run_id}/interview/{handle}", data={"message": "¿Qué te haría cambiar de opinión?"}
    )
    assert res.status_code == 200 and 'class="a markdown"' in res.text
    assert client.post(f"/runs/{run_id}/interview/{handle}", data={"message": "  "}).status_code == 200
    assert client.post(f"/runs/{run_id}/interview/nadie_asi", data={"message": "hola"}).status_code == 404
    card = client.get(f"/runs/{run_id}/agents/{handle}").text
    assert "Tú: ¿Qué te haría cambiar de opinión?" in card  # la transcripción persiste


def test_timeline_partial_filters(web: tuple[TestClient, str, Settings]) -> None:
    client, run_id, settings = web
    store = RunStore.open_existing(settings.runs_dir / run_id)
    try:
        posts = [dict(p) for p in store.load_posts()]
    finally:
        store.close()
    assert posts
    res = client.get(f"/runs/{run_id}/timeline")
    assert res.status_code == 200 and "<html" not in res.text
    assert all(f"post-{p['id']}" in res.text for p in posts)
    res = client.get(f"/runs/{run_id}/timeline", params={"round": "1"})
    assert all((f'id="post-{p["id"]}"' in res.text) == (p["round"] == 1) for p in posts)
    res = client.get(f"/runs/{run_id}/timeline", params={"kind": "event"})
    assert all((f'id="post-{p["id"]}"' in res.text) == (p["kind"] == "event") for p in posts)
    assert client.get(f"/runs/{run_id}/timeline", params={"sort": "eco"}).status_code == 200
    res = client.get(f"/runs/{run_id}/timeline", params={"q": "precio"})
    assert res.status_code == 200 and "Resultados para «precio»" in res.text
    assert client.get(f"/runs/{run_id}/timeline", params={"round": "x"}).status_code == 400


def test_chart_and_file_routes(web: tuple[TestClient, str, Settings]) -> None:
    client, run_id, _ = web
    res = client.get(f"/runs/{run_id}/charts/stance_mean.png")
    assert res.status_code == 200 and res.headers["content-type"] == "image/png"
    assert res.content.startswith(b"\x89PNG")
    for bad in ("..%2Fswarm.db", "..%2F..%2Fswarm.db", "swarm.db", "nope.png", "..png"):
        assert client.get(f"/runs/{run_id}/charts/{bad}").status_code == 404, bad
    assert client.get("/runs/..%2F..%2Fetc/charts/x.png").status_code == 404
    res = client.get(f"/runs/{run_id}/files/report.md")
    assert res.status_code == 200 and res.text.startswith("#")
    assert client.get(f"/runs/{run_id}/files/personas.json").status_code == 200
    assert client.get(f"/runs/{run_id}/files/metrics.json").status_code == 200
    for bad in ("swarm.db", "..%2Fswarm.db", "x.md"):
        assert client.get(f"/runs/{run_id}/files/{bad}").status_code == 404, bad
    res = client.get(f"/runs/{run_id}/pdf")
    assert res.status_code == 200 and res.headers["content-type"] == "application/pdf"
    assert res.content.startswith(b"%PDF")


def test_chat_answers_and_commands(web: tuple[TestClient, str, Settings]) -> None:
    client, run_id, _ = web
    res = client.post(f"/runs/{run_id}/chat", data={"message": "¿Qué segmento es más escéptico?"})
    assert res.status_code == 200
    assert "Tú: ¿Qué segmento es más escéptico?" in res.text and 'class="a markdown"' in res.text
    res = client.post(f"/runs/{run_id}/chat", data={"message": "/buscar precio"})
    assert res.status_code == 200 and "Búsqueda «precio»" in res.text and "<pre>" in res.text
    res = client.post(f"/runs/{run_id}/chat", data={"message": "<script>alert(1)</script>"})
    assert "<script>alert(1)</script>" not in res.text
    assert "Tú: ¿Qué segmento es más escéptico?" in client.get(f"/runs/{run_id}/tab/chat").text


def test_sse_streams_and_terminates(web: tuple[TestClient, str, Settings]) -> None:
    client, run_id, _ = web
    with client.stream("GET", f"/runs/{run_id}/events") as res:
        assert res.status_code == 200
        assert res.headers["content-type"].startswith("text/event-stream")
        events = [json.loads(line[5:]) for line in res.iter_lines() if line.startswith("data:")]
    assert events and events[-1]["final"] is True
    last = events[-1]
    assert last["status"] == "completed" and last["current_round"] == 2 and last["rounds"] == 2
    assert last["queued"] is False and last["job"] is None
    assert any("Ronda 2/2 terminada" in line for e in events for line in e["logs"])


def test_survey_and_report_jobs(web: tuple[TestClient, str, Settings]) -> None:
    client, run_id, settings = web
    app: FastAPI = client.app  # type: ignore[assignment]
    res = client.post(
        f"/runs/{run_id}/survey",
        data={"question": "¿Pagarías por esto?", "report": "1"},
        follow_redirects=False,
    )
    assert res.status_code == 303 and res.headers["location"].endswith("?tab=encuesta")
    assert wait_task(app, run_id) == "completed"
    store = RunStore.open_existing(settings.runs_dir / run_id)
    try:
        assert store.get_meta("survey")["question"] == "¿Pagarías por esto?"
    finally:
        store.close()
    res = client.post(f"/runs/{run_id}/report", data={}, follow_redirects=False)
    assert res.status_code == 303 and res.headers["location"].endswith("?tab=informe")
    assert wait_task(app, run_id) == "completed"
    assert "Informe" in client.get(f"/runs/{run_id}/tab/informe").text


def test_resume_pause_and_inject(web: tuple[TestClient, str, Settings]) -> None:
    client, run_id, settings = web
    app: FastAPI = client.app  # type: ignore[assignment]
    # un run terminado no se reanuda
    assert client.post(f"/runs/{run_id}/resume", follow_redirects=False).status_code == 303
    assert app.state.web.live[run_id].task.done()
    # run detenido tras las personas (como con --until personas desde la CLI)
    engine = Engine.create(
        settings, seed=EXAMPLE, question="q", n_agents=10, rounds=100, dry_run=True, run_id="cli-run"
    )
    assert asyncio.run(engine.run(until="personas")) == "stopped"
    engine.store.close()
    page = client.get("/runs/cli-run")
    assert "Reanudar" in page.text and "Pausar" not in page.text
    # evento inyectado desde el panel antes de reanudar
    res = client.post(
        "/runs/cli-run/inject",
        data={"round": "2", "title": "Un competidor baja el precio"},
        follow_redirects=False,
    )
    assert res.status_code == 303
    eventos = client.get("/runs/cli-run/tab/eventos").text
    assert "Un competidor baja el precio" in eventos and "panel" in eventos
    assert client.post("/runs/cli-run/inject", data={"round": "0", "title": "x"}).status_code == 400
    assert client.post("/runs/cli-run/inject", data={"round": "3", "title": " "}).status_code == 400
    assert client.post("/runs/cli-run/resume", follow_redirects=False).status_code == 303
    assert client.post("/runs/cli-run/pause", follow_redirects=False).status_code == 303
    live = app.state.web.live["cli-run"]
    assert live.engine.stop_requested is True
    assert wait_task(app, "cli-run") == "paused"
    store = RunStore.open_existing(settings.runs_dir / "cli-run")
    try:
        assert store.get_meta("pause_reason") == "stop_requested"
        assert store.get_meta("current_round") < 100
        ids = {json.loads(r["data"])["id"] for r in store.load_events()}
    finally:
        store.close()
    assert "e02" in ids or "e01" in ids
    assert "Reanudar" in client.get("/runs/cli-run").text


def test_replicate_scorecard_compare_calibrate(web: tuple[TestClient, str, Settings]) -> None:
    client, run_id, settings = web
    app: FastAPI = client.app  # type: ignore[assignment]
    res = client.post(f"/runs/{run_id}/replicate", data={"n": "1"}, follow_redirects=False)
    assert res.status_code == 303 and f"q={run_id}" in res.headers["location"]
    assert wait_task(app, f"{run_id}-r2") == "completed"
    assert "réplica" in client.get("/", params={"q": run_id}).text
    sc = client.get("/scorecard", params={"runs": f"{run_id}*"})
    assert sc.status_code == 200 and "(×2)" in sc.text
    sc = client.get("/scorecard", params={"sel": [run_id, f"{run_id}-r2"]})
    assert sc.status_code == 200 and "(×2)" in sc.text
    assert 'class="error"' in client.get("/scorecard", params={"runs": "nada*"}).text
    cmp_ = client.get("/comparar", params={"a": run_id, "b": f"{run_id}-r2"})
    assert cmp_.status_code == 200 and "<table" in cmp_.text
    assert "Indica los dos lados" in client.get("/comparar", params={"a": run_id}).text
    assert client.get("/comparar", params={"sel": [run_id, f"{run_id}-r2"]}).status_code == 200
    cal = client.get("/calibracion", params={"runs": f"{run_id}*"})
    assert cal.status_code == 200 and "Ruido entre réplicas" in cal.text and "Guardar como ruido" in cal.text
    # con una sola idea no hay ruido entre ideas que medir
    saved = client.post("/calibracion/guardar", data={"runs": f"{run_id}*"})
    assert saved.status_code == 200 and "al menos 2 ideas" in saved.text
    # segunda idea (texto pegado) con una réplica: ya se puede guardar el ruido de referencia
    res = client.post(
        "/runs",
        data={
            "seed_text": EXAMPLE.read_text(encoding="utf-8").replace("Lince", "Zorro"),
            "question": "¿Y esta variante?",
            "agents": "10",
            "rounds": "2",
            "agents_per_call": "5",
            "platform": "twitter",
            "survey": "1",
            "dry_run": "1",
            "run_id": "idea2",
        },
        follow_redirects=False,
    )
    assert res.status_code == 303, res.text
    assert wait_task(app, "idea2") == "completed"
    assert client.post("/runs/idea2/replicate", data={"n": "1"}, follow_redirects=False).status_code == 303
    assert wait_task(app, "idea2-r2") == "completed"
    saved = client.post("/calibracion/guardar", data={"runs": f"{run_id}* idea2*"})
    assert saved.status_code == 200 and "guardado" in saved.text
    assert (settings.runs_dir / "_calibration" / "ruido.json").is_file()
    assert "ideas" in client.get("/").text
    bad = client.get("/calibracion", params={"runs": f"{run_id}*", "outcomes": "no-existe.toml"})
    assert bad.status_code == 200 and "no existe" in bad.text


def test_settings_and_doctor(web: tuple[TestClient, str, Settings]) -> None:
    client, _, _ = web
    res = client.get("/ajustes")
    assert res.status_code == 200 and "WEB_PARALLEL_RUNS" in res.text and "MODEL_HEAVY" in res.text
    res = client.post("/ajustes/doctor")
    assert res.status_code == 200 and "binario" in res.text


def test_delete_run(web: tuple[TestClient, str, Settings]) -> None:
    client, run_id, settings = web
    replica = f"{run_id}-r2"
    assert (settings.runs_dir / replica).is_dir()
    res = client.post(f"/runs/{replica}/delete", follow_redirects=False)
    assert res.status_code == 303
    assert not (settings.runs_dir / replica).exists()
    assert client.get(f"/runs/{replica}").status_code == 404
