"""Panel web de SwarmLab (FastAPI + HTMX).

Lanza y sigue simulaciones (progreso en vivo por SSE, cola de ejecución), explora cada run por
pestañas (resumen con gráficas, feed de la red social, fichas de agentes con entrevista, encuesta,
informe, eventos «modo dios», chat con el analista, registro de llamadas) y reúne las herramientas de
medición: réplicas, scorecard, comparación A/B y calibración.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import shutil
import uuid
from collections import Counter
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markdown_it import MarkdownIt
from markupsafe import Markup, escape

from swarmlab.compare import compare_runs
from swarmlab.config import Settings
from swarmlab.doctor import run_doctor
from swarmlab.engine import Engine, estimate_seed
from swarmlab.events import EventSpec, load_events
from swarmlab.ingest import SUPPORTED_SUFFIXES
from swarmlab.interview import Interview
from swarmlab.llm import build_llm, count_logged_calls
from swarmlab.metrics import side_switchers, summary_metrics, trajectories
from swarmlab.pdf import report_to_pdf
from swarmlab.replicate import create_replica, plan_replicas
from swarmlab.report import ReportChat, generate_report
from swarmlab.scorecard import (
    load_noise_reference,
    load_outcomes,
    load_rows,
    noise_md,
    noise_rows,
    outcome_md,
    outcome_rows,
    save_noise_reference,
    scorecard_md,
)
from swarmlab.store import RunStore, expand_run_ids, list_runs
from swarmlab.survey import run_survey
from swarmlab.web import charts

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
EXAMPLES_DIR = ROOT / "examples"
TERMINAL = frozenset({"completed", "failed", "paused", "stopped"})
RESUMABLE = frozenset({"paused", "stopped"})
CHARTS = (
    ("stance_mean", "Postura media"),
    ("stance_distribution", "Distribución de posturas"),
    ("polarization", "Polarización"),
    ("activity", "Actividad"),
    ("influencers", "Influencers"),
    ("knowledge_graph", "Grafo de conocimiento"),
)
CHAT_COMMANDS = ("/buscar", "/agente", "/post")
STATUS_LABELS = {
    "created": "creado",
    "queued": "en cola",
    "running": "en curso",
    "paused": "pausado",
    "stopped": "detenido",
    "completed": "completado",
    "failed": "fallido",
}
STAGE_LABELS = {
    "ontology": "ontología",
    "personas": "personas",
    "simulation": "simulación",
    "report": "informe",
    "done": "terminado",
}
JOB_LABELS = {"run": "simulando", "survey": "encuestando", "report": "escribiendo el informe"}
TABS = (
    ("resumen", "Resumen"),
    ("feed", "Feed"),
    ("agentes", "Agentes"),
    ("encuesta", "Encuesta"),
    ("informe", "Informe"),
    ("eventos", "Eventos"),
    ("chat", "Chat"),
    ("llamadas", "Llamadas"),
)
DOWNLOADS = {
    "report.md": "report",
    "dossier.md": "dossier",
    "scenario_brief.md": "brief",
    "personas.json": "personas_json",
    "graph.json": "graph_json",
    "llm_calls.jsonl": "calls_log",
}
METRIC_FILES = {"metrics.json", "rounds.csv", "agents.csv", "posts.csv", "narratives.csv", "influencers.csv"}
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][\w.\-]*$")
_HANDLE_RE = re.compile(r"^@?[a-z0-9_]{3,24}$")
_CHART_RE = re.compile(r"^[\w\-]+\.png$")
_md = MarkdownIt("commonmark", {"html": False}).enable("table")


def render_markdown(text: str | None) -> Markup:
    """Markdown → HTML seguro (sin HTML crudo: el contenido del usuario/LLM queda escapado)."""
    return Markup(_md.render(text or ""))


def fmt_pct(x: float | None) -> str:
    return "–" if x is None else f"{x:.0%}".replace("%", " %")


def fmt_pct_ci(share: float | None, ci: list[float] | None) -> Markup:
    if share is None:
        return Markup("–")
    out = escape(fmt_pct(share))
    if ci:
        out += Markup(f' <span class="ci">[{ci[0]:.0%}–{ci[1]:.0%}]</span>')
    return Markup(out)


def fmt_stance(x: float | None) -> Markup:
    if x is None:
        return Markup('<span class="muted">–</span>')
    return Markup(f'<span class="stance {charts.stance_class(x)}">{x:+.2f}</span>')


def fmt_num(x: float | int | None) -> str:
    if x is None:
        return "–"
    return f"{round(x):,}".replace(",", ".")


def fmt_dt(value: str | None) -> str:
    if not value:
        return "–"
    try:
        return datetime.fromisoformat(str(value)).strftime("%d %b %H:%M")
    except ValueError:
        return str(value)[:16]


# --------------------------------------------------------------------------- estado en memoria


@dataclass
class RunLog:
    """Mensajes recientes de un run lanzado desde la web (numerados para el SSE)."""

    lines: list[tuple[int, str]] = field(default_factory=list)
    count: int = 0

    def add(self, text: str) -> None:
        self.count += 1
        self.lines.append((self.count, text))
        del self.lines[:-500]

    def since(self, seen: int) -> tuple[list[str], int]:
        return [t for n, t in self.lines if n > seen], self.count


@dataclass
class LiveRun:
    engine: Engine
    kind: str = "run"  # run | survey | report
    queued: bool = True
    task: asyncio.Task[Any] | None = None

    @property
    def active(self) -> bool:
        return self.task is not None and not self.task.done()


@dataclass
class WebState:
    gate: asyncio.Semaphore
    live: dict[str, LiveRun] = field(default_factory=dict)
    logs: dict[str, RunLog] = field(default_factory=dict)
    chats: dict[str, ReportChat] = field(default_factory=dict)
    interviews: dict[tuple[str, str], Interview] = field(default_factory=dict)
    locks: dict[str, asyncio.Lock] = field(default_factory=dict)

    def is_live(self, run_id: str) -> bool:
        live = self.live.get(run_id)
        return live is not None and live.active

    def is_queued(self, run_id: str) -> bool:
        live = self.live.get(run_id)
        return live is not None and live.active and live.queued

    def job(self, run_id: str) -> str | None:
        live = self.live.get(run_id)
        return live.kind if live is not None and live.active else None

    def drop_sessions(self, run_id: str) -> None:
        chat = self.chats.pop(run_id, None)
        if chat is not None:
            chat.store.close()
        for key in [k for k in self.interviews if k[0] == run_id]:
            self.interviews.pop(key).store.close()


def describe_event(kind: str, data: dict[str, Any]) -> str | None:
    """Traduce un evento del motor a una línea de log legible."""
    if kind == "log":
        return str(data.get("message", ""))
    if kind == "stage":
        name = str(data.get("name", ""))
        return f"Etapa: {STAGE_LABELS.get(name, name)}"
    if kind == "status":
        status = str(data.get("status", ""))
        reason = data.get("pause_reason")
        return f"Estado: {STATUS_LABELS.get(status, status)}" + (f" ({reason})" if reason else "")
    if kind == "round_start":
        return f"Ronda {data.get('round')}/{data.get('rounds')} en marcha"
    if kind == "batches":
        return (
            f"Ronda {data.get('round')}: {data.get('active')} agentes activos en {data.get('batches')} lotes"
        )
    if kind == "round_end":
        stats = data.get("stats") or {}
        return (
            f"Ronda {data.get('round')}/{data.get('rounds')} terminada: postura media {stats.get('mean', 0):+.2f}, "
            f"polarización {stats.get('polarization', 0):.2f}, {stats.get('posts_created', 0)} posts"
        )
    if kind == "ontology":
        return f"Ontología: {data.get('entities')} entidades, {data.get('relations')} relaciones"
    if kind == "personas":
        return f"{data.get('count')} personas generadas, {data.get('follows')} relaciones de seguimiento"
    return None


# --------------------------------------------------------------------------- utilidades


def _int_field(raw: str, name: str, lo: int, hi: int) -> int:
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"«{name}» debe ser un número entero") from None
    if not lo <= value <= hi:
        raise ValueError(f"«{name}» debe estar entre {lo} y {hi}")
    return value


def _safe_filename(name: str, default: str) -> str:
    base = Path(name.replace("\\", "/")).name
    clean = re.sub(r"[^\w.\-]+", "_", base).strip("._")
    return clean or default


async def _save_upload(upload: UploadFile, dest_dir: Path, default: str) -> Path:
    dest = dest_dir / _safe_filename(upload.filename or "", default)
    dest.write_bytes(await upload.read())
    return dest


def _examples() -> list[tuple[str, str]]:
    if not EXAMPLES_DIR.is_dir():
        return []
    return [
        (p.name, p.stem) for p in sorted(EXAMPLES_DIR.iterdir()) if p.suffix.lower() in SUPPORTED_SUFFIXES
    ]


def _posts_view(store: RunStore, rows: list[Any]) -> list[dict[str, Any]]:
    """Posts listos para la plantilla: autor, segmento, tipo y contexto (respuesta/cita/repost)."""
    accounts = store.load_accounts()
    personas = {p["id"]: p for p in store.load_persona_data()}
    handles: dict[str, str] = {pid: p["handle"] for pid, p in personas.items()}
    handles.update({a["id"]: a["handle"] for a in accounts})
    system = {a["id"] for a in accounts if a["kind"] == "system"}
    all_posts = {p["id"]: dict(p) for p in store.load_posts()}
    out: list[dict[str, Any]] = []
    for row in rows:
        p = dict(row)
        parent = all_posts.get(p["parent_id"]) if p.get("parent_id") else None
        p["handle"] = handles.get(p["author_id"], p["author_id"])
        p["segment"] = personas.get(p["author_id"], {}).get("segment")
        p["system"] = p["kind"] == "event" or p["author_id"] in system
        if parent:
            p["parent"] = {
                "id": parent["id"],
                "handle": handles.get(parent["author_id"], parent["author_id"]),
                "text": parent["text"] or "",
            }
        out.append(p)
    return out


def _engagement(p: dict[str, Any]) -> float:
    return (
        (p.get("likes") or 0)
        + 2 * (p.get("reposts") or 0)
        + 1.5 * (p.get("quotes") or 0)
        + (p.get("replies") or 0)
    )


def _survey_kpis(summary: dict[str, Any] | None) -> list[dict[str, str]]:
    if not summary:
        return []
    o = summary["overall"]
    out = [
        {
            "label": "Índice de adopción",
            "value": f"{o['adoption_index']:.2f}",
            "sub": f"{o['n']} encuestados",
        },
        {
            "label": "Eligen la propuesta",
            "value": fmt_pct(o.get("choice_share")),
            "sub": "frente a su alternativa",
        },
        {
            "label": "Prob. de compra (Juster)",
            "value": fmt_pct(o.get("juster_mean")),
            "sub": "toda la población",
        },
        {
            "label": "Compradores firmes",
            "value": fmt_pct(o.get("firm_share")),
            "sub": "eligen, seguros ≥ 8, pagarían ya",
        },
    ]
    if c := summary.get("customers"):
        out.append(
            {
                "label": "Clientes que pagarían",
                "value": fmt_pct(c.get("would_pay_share")),
                "sub": f"{c['n']} clientes potenciales",
            }
        )
    return out


def _calls_view(path: Path, last: int = 60) -> dict[str, Any]:
    """Resumen del registro ``llm_calls.jsonl``: totales, por modelo, por propósito y últimas llamadas."""
    rows: list[dict[str, Any]] = []
    if path.is_file():
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("kind", "attempt") == "attempt":
                    rows.append(entry)
    if not rows:
        return {"total": 0}
    ok = [r for r in rows if r.get("outcome") == "ok"]
    seconds = [(r.get("duration_ms") or r.get("wall_ms") or 0) / 1000 for r in rows]
    startups = [(r.get("startup_ms") or 0) / 1000 for r in rows if r.get("startup_ms")]

    def group(key: str) -> list[dict[str, Any]]:
        acc: dict[str, dict[str, float]] = {}
        for r, s in zip(rows, seconds, strict=True):
            g = acc.setdefault(str(r.get(key) or "?"), {"n": 0, "cost": 0.0, "s": 0.0})
            g["n"] += 1
            g["cost"] += r.get("cost_usd") or 0.0
            g["s"] += s
        return sorted(
            (
                {"name": k, "n": int(v["n"]), "cost": v["cost"], "avg_s": v["s"] / v["n"]}
                for k, v in acc.items()
            ),
            key=lambda d: -d["n"],
        )

    return {
        "total": len(rows),
        "ok": len(ok),
        "failed": len(rows) - len(ok),
        "minutes": sum(seconds) / 60,
        "avg_s": sum(seconds) / len(seconds),
        "startup_s": sum(startups) / len(startups) if startups else 0.0,
        "input_tokens": sum(r.get("input_tokens") or 0 for r in rows),
        "output_tokens": sum(r.get("output_tokens") or 0 for r in rows),
        "cache_read": sum(r.get("cache_read_tokens") or 0 for r in rows),
        "cost": sum(r.get("cost_usd") or 0.0 for r in rows),
        "by_model": group("model"),
        "by_purpose": group("purpose"),
        "rows": [
            {
                "ts": str(r.get("ts", ""))[11:19],
                "purpose": r.get("purpose"),
                "model": r.get("model"),
                "attempt": r.get("attempt"),
                "outcome": r.get("outcome") or "?",
                "error": r.get("error"),
                "seconds": s,
                "output_tokens": r.get("output_tokens") or 0,
                "cost": r.get("cost_usd") or 0.0,
            }
            for r, s in list(zip(rows, seconds, strict=True))[-last:][::-1]
        ],
    }


def _noise_info(settings: Settings) -> dict[str, Any] | None:
    noise = load_noise_reference(settings.runs_dir)
    if not noise:
        return None
    metrics = noise.get("metrics") or {}
    first = next(iter(metrics.values()), {}) if metrics else {}
    return {
        "created_at": noise.get("created_at"),
        "runs": len(noise.get("runs") or []),
        "ideas": first.get("groups", "?"),
    }


def _split_specs(spec: str, sel: list[str] | None = None) -> list[str]:
    specs = [s for s in re.split(r"\s+", (spec or "").strip()) if s]
    return specs + [s for s in (sel or []) if s]


# --------------------------------------------------------------------------- aplicación


def create_app(settings: Settings, *, poll_interval: float = 1.0) -> FastAPI:
    state = WebState(gate=asyncio.Semaphore(settings.web_parallel_runs))

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        for live in list(state.live.values()):
            if live.active and live.task is not None:
                live.task.cancel()
        for live in list(state.live.values()):
            if live.task is not None:
                with contextlib.suppress(BaseException):
                    await live.task
        for run_id in list(state.chats) + [k[0] for k in state.interviews]:
            state.drop_sessions(run_id)

    app = FastAPI(title="SwarmLab", lifespan=lifespan)
    app.state.web = state
    app.state.settings = settings
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.filters.update(
        md=render_markdown, pct=fmt_pct, pctci=fmt_pct_ci, stance=fmt_stance, num=fmt_num, dt=fmt_dt
    )
    templates.env.globals.update(status_labels=STATUS_LABELS, stage_labels=STAGE_LABELS, settings=settings)

    def page(request: Request, name: str, ctx: dict[str, Any], status_code: int = 200) -> Response:
        return templates.TemplateResponse(request, name, ctx, status_code=status_code)

    def run_root(run_id: str) -> Path:
        if not _RUN_ID_RE.match(run_id):
            raise HTTPException(404, "run no encontrado")
        root = (settings.runs_dir / run_id).resolve()
        if root.parent != settings.runs_dir.resolve() or not (root / "swarm.db").is_file():
            raise HTTPException(404, "run no encontrado")
        return root

    @contextlib.contextmanager
    def open_store(run_id: str) -> Iterator[RunStore]:
        store = RunStore.open_existing(run_root(run_id))
        try:
            yield store
        finally:
            store.close()

    def run_ids() -> list[str]:
        return [r["run_id"] for r in list_runs(settings.runs_dir)]

    # ------------------------------------------------------------------ trabajos en segundo plano

    def launch(
        engine: Engine, *, kind: str = "run", work: Callable[[], Awaitable[Any]] | None = None
    ) -> None:
        """Encola un trabajo sobre un run (simulación, encuesta o informe); corren ``web_parallel_runs`` a la vez."""
        run_id = engine.cfg.run_id
        log = state.logs.setdefault(run_id, RunLog())
        previous = engine.on_event

        def on_event(k: str, data: dict[str, Any]) -> None:
            if previous is not None:
                previous(k, data)
            if line := describe_event(k, data):
                log.add(line)

        engine.on_event = on_event
        state.drop_sessions(run_id)  # el informe va a cambiar: chat y entrevistas se recrean
        live = LiveRun(engine=engine, kind=kind)

        async def job() -> Any:
            if state.gate.locked():
                log.add(f"En cola: {JOB_LABELS.get(kind, kind)} cuando haya hueco")
            async with state.gate:
                live.queued = False
                log.add(f"Empieza: {JOB_LABELS.get(kind, kind)}")
                return await (work() if work is not None else engine.run())

        task = asyncio.create_task(job(), name=f"swarmlab-{kind}-{run_id}")
        live.task = task

        def done(t: asyncio.Task[Any]) -> None:
            if not t.cancelled() and (exc := t.exception()) is not None:
                log.add(f"Error: {type(exc).__name__}: {exc}")
            else:
                log.add(f"Terminado: {JOB_LABELS.get(kind, kind)}")
            state.drop_sessions(run_id)
            engine.store.close()

        task.add_done_callback(done)
        state.live[run_id] = live

    def status_of(run_id: str, meta: dict[str, Any]) -> tuple[str, str]:
        if state.is_queued(run_id):
            return "queued", STATUS_LABELS["queued"]
        if job := state.job(run_id):
            return "running", JOB_LABELS.get(job, job) if job != "run" else STATUS_LABELS["running"]
        status = str(meta.get("status", "?"))
        return status, STATUS_LABELS.get(status, status)

    # ------------------------------------------------------------------ panel

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request, q: str = "", status: str = "", notice: str = "") -> Response:
        rows = []
        kpis = Counter()
        groups: set[str] = set()
        for r in list_runs(settings.runs_dir):
            store = RunStore.open_existing(settings.runs_dir / r["run_id"])
            try:
                meta = store.meta_all()
                stances = store.latest_stances()
            finally:
                store.close()
            cfg = meta.get("config", {})
            brief = meta.get("brief") or {}
            summary = (meta.get("survey") or {}).get("summary") or {}
            overall = summary.get("overall") or {}
            key, label = status_of(r["run_id"], meta)
            r.update(
                title=brief.get("one_liner") or brief.get("title"),
                replica_of=cfg.get("replica_of"),
                personas_from=cfg.get("personas_from"),
                status_key=key,
                status_label=label,
                final_mean=(sum(stances.values()) / len(stances)) if stances else None,
                adoption=overall.get("adoption_index"),
                choice=overall.get("choice_share"),
                juster=overall.get("juster_mean"),
            )
            kpis["runs"] += 1
            kpis["completed"] += r["status"] == "completed"
            kpis["running"] += key == "running"
            kpis["queued"] += key == "queued"
            kpis["calls"] += int(r["calls"] or 0)
            kpis["agents"] += int(r["agents"] or 0)
            kpis["surveyed"] += bool(r["survey"])
            kpis["replicas"] += bool(cfg.get("replica_of"))
            groups.add(cfg.get("replica_of") or r["run_id"])
            if q and q.lower() not in f"{r['run_id']} {r['seed'] or ''} {r['title'] or ''}".lower():
                continue
            if status and key != status:
                continue
            rows.append(r)
        noise = _noise_info(settings)
        ctx = {
            "runs": rows,
            "runs_dir": settings.runs_dir,
            "q": q,
            "status": status,
            "statuses": list(STATUS_LABELS.items()),
            "notice": notice,
            "kpis": {
                **kpis,
                "ideas": len(groups),
                "parallel": settings.web_parallel_runs,
                "noise": f"{noise['ideas']} ideas, {noise['runs']} runs" if noise else None,
            },
        }
        return page(request, "index.html", ctx)

    # ------------------------------------------------------------------ nueva simulación

    def form_defaults(**over: Any) -> dict[str, Any]:
        base = {
            "question": "",
            "agents": settings.default_agents,
            "rounds": settings.default_rounds,
            "agents_per_call": settings.agents_per_call,
            "platform": "twitter",
            "stance_mode": settings.stance_mode,
            "survey": settings.survey,
            "report_llm": settings.report_llm,
            "dry_run": False,
            "interval": "",
            "language": "",
            "run_id": "",
            "rng_seed": 7,
            "max_calls": "",
            "personas_from": "",
            "example": "",
            "seed_text": "",
            "events_text": "",
        }
        return {**base, **over}

    def new_page(
        request: Request, *, error: str | None = None, form: dict[str, Any] | None = None
    ) -> Response:
        ctx = {
            "form": form_defaults(**(form or {})),
            "error": error,
            "suffixes": ",".join(SUPPORTED_SUFFIXES),
            "runs": run_ids(),
            "examples": _examples(),
        }
        return page(request, "new.html", ctx, status_code=400 if error else 200)

    @app.get("/nueva", response_class=HTMLResponse)
    async def new_run(request: Request, personas_from: str = "") -> Response:
        return new_page(request, form={"personas_from": personas_from} if personas_from else None)

    async def resolve_seed(
        upload_dir: Path, seed: UploadFile | None, seed_text: str, example: str
    ) -> Path | None:
        if seed is not None and seed.filename:
            if Path(seed.filename).suffix.lower() not in SUPPORTED_SUFFIXES:
                raise ValueError(f"formato de semilla no soportado; usa {', '.join(SUPPORTED_SUFFIXES)}")
            upload_dir.mkdir(parents=True, exist_ok=True)
            return await _save_upload(seed, upload_dir, "semilla.md")
        if seed_text.strip():
            upload_dir.mkdir(parents=True, exist_ok=True)
            path = upload_dir / "semilla.md"
            path.write_text(seed_text.strip() + "\n", encoding="utf-8")
            return path
        if example:
            path = (EXAMPLES_DIR / _safe_filename(example, "")).resolve()
            if path.parent != EXAMPLES_DIR.resolve() or not path.is_file():
                raise ValueError("ejemplo desconocido")
            return path
        return None

    async def resolve_events(upload_dir: Path, events: UploadFile | None, events_text: str) -> Path | None:
        if events is not None and events.filename:
            if Path(events.filename).suffix.lower() not in (".yaml", ".yml"):
                raise ValueError("los eventos deben ser un fichero .yaml/.yml")
            upload_dir.mkdir(parents=True, exist_ok=True)
            path = await _save_upload(events, upload_dir, "events.yaml")
        elif events_text.strip():
            upload_dir.mkdir(parents=True, exist_ok=True)
            path = upload_dir / "events.yaml"
            path.write_text(events_text, encoding="utf-8")
        else:
            return None
        try:
            load_events(path)
        except Exception as exc:
            raise ValueError(f"eventos inválidos: {exc}") from exc
        return path

    @app.post("/runs", response_model=None)
    async def create_run(
        request: Request,
        seed: Annotated[UploadFile | None, File()] = None,
        events: Annotated[UploadFile | None, File()] = None,
        seed_text: Annotated[str, Form()] = "",
        example: Annotated[str, Form()] = "",
        events_text: Annotated[str, Form()] = "",
        question: Annotated[str, Form()] = "",
        agents: Annotated[str, Form()] = "",
        rounds: Annotated[str, Form()] = "",
        agents_per_call: Annotated[str, Form()] = "",
        platform: Annotated[str, Form()] = "twitter",
        stance_mode: Annotated[str, Form()] = "",
        survey: Annotated[str | None, Form()] = None,
        report_llm: Annotated[str | None, Form()] = None,
        dry_run: Annotated[str | None, Form()] = None,
        interval: Annotated[str, Form()] = "",
        language: Annotated[str, Form()] = "",
        run_id: Annotated[str, Form()] = "",
        rng_seed: Annotated[str, Form()] = "7",
        max_calls: Annotated[str, Form()] = "",
        personas_from: Annotated[str, Form()] = "",
    ) -> Response:
        form = {
            "question": question,
            "agents": agents,
            "rounds": rounds,
            "agents_per_call": agents_per_call,
            "platform": platform,
            "stance_mode": stance_mode or settings.stance_mode,
            "survey": survey is not None,
            "report_llm": report_llm is not None,
            "dry_run": dry_run is not None,
            "interval": interval,
            "language": language,
            "run_id": run_id,
            "rng_seed": rng_seed,
            "max_calls": max_calls,
            "personas_from": personas_from,
            "example": example,
            "seed_text": seed_text,
            "events_text": events_text,
        }
        upload_dir = settings.runs_dir / "_uploads" / uuid.uuid4().hex
        try:
            n_agents = _int_field(agents, "agentes", 10, 200)
            n_rounds = _int_field(rounds, "rondas", 1, 100)
            per_call = _int_field(agents_per_call, "agentes por llamada", 1, 20)
            seed_rng = _int_field(rng_seed or "7", "semilla aleatoria", 0, 10**9)
            cap = _int_field(max_calls, "tope de llamadas", 10, 10**6) if max_calls.strip() else None
            if platform not in ("twitter", "forum"):
                raise ValueError("plataforma desconocida")
            if run_id.strip() and not _RUN_ID_RE.match(run_id.strip()):
                raise ValueError("el id del run solo admite letras, números, «.», «_» y «-»")
            if personas_from and not (settings.runs_dir / personas_from / "swarm.db").is_file():
                raise ValueError(f"no existe el run {personas_from} para reutilizar su población")
            seed_path = await resolve_seed(upload_dir, seed, seed_text, example)
            if seed_path is None:
                raise ValueError("falta el documento semilla: súbelo, pega el texto o elige un ejemplo")
            events_path = await resolve_events(upload_dir, events, events_text)
            engine = Engine.create(
                settings,
                seed=seed_path,
                question=question.strip(),
                n_agents=n_agents,
                rounds=n_rounds,
                agents_per_call=per_call,
                events_path=events_path,
                dry_run=dry_run is not None,
                rng_seed=seed_rng,
                platform=platform,
                language=language.strip() or None,
                max_calls=cap,
                personas_from=personas_from or None,
                survey=survey is not None,
                round_interval=interval.strip() or None,
                run_id=run_id.strip() or None,
                stance_mode=stance_mode or None,
                report_llm=report_llm is not None,
            )
        except Exception as exc:  # validación: se vuelve a mostrar el formulario
            return new_page(request, error=str(exc) or type(exc).__name__, form=form)
        finally:
            shutil.rmtree(upload_dir, ignore_errors=True)
        launch(engine)
        return RedirectResponse(f"/runs/{engine.cfg.run_id}", status_code=303)

    @app.post("/estimate", response_class=HTMLResponse)
    async def estimate(
        request: Request,
        seed: Annotated[UploadFile | None, File()] = None,
        seed_text: Annotated[str, Form()] = "",
        example: Annotated[str, Form()] = "",
        agents: Annotated[str, Form()] = "",
        rounds: Annotated[str, Form()] = "",
        agents_per_call: Annotated[str, Form()] = "",
        stance_mode: Annotated[str, Form()] = "",
        survey: Annotated[str | None, Form()] = None,
        report_llm: Annotated[str | None, Form()] = None,
        dry_run: Annotated[str | None, Form()] = None,
        max_calls: Annotated[str, Form()] = "",
        personas_from: Annotated[str, Form()] = "",
    ) -> Response:
        upload_dir = settings.runs_dir / "_uploads" / uuid.uuid4().hex
        ctx: dict[str, Any] = {"est": None, "error": None, "dry_run": dry_run is not None}
        try:
            seed_path = await resolve_seed(upload_dir, seed, seed_text, example)
            if seed_path is None:
                raise ValueError(
                    "Sube o pega la semilla (o elige un ejemplo) para estimar llamadas y tiempo."
                )
            if personas_from and not (settings.runs_dir / personas_from / "swarm.db").is_file():
                raise ValueError(f"No existe el run {personas_from}.")
            ctx["est"] = estimate_seed(
                settings,
                seed_path,
                agents=_int_field(agents or str(settings.default_agents), "agentes", 10, 200),
                rounds=_int_field(rounds or str(settings.default_rounds), "rondas", 1, 100),
                agents_per_call=_int_field(
                    agents_per_call or str(settings.agents_per_call), "agentes por llamada", 1, 20
                ),
                max_calls=_int_field(max_calls, "tope de llamadas", 10, 10**6) if max_calls.strip() else None,
                personas_from=personas_from or None,
                survey=survey is not None,
                stance_mode=stance_mode or None,
                report_llm=report_llm is not None,
            )
        except Exception as exc:
            ctx["error"] = str(exc) or type(exc).__name__
        finally:
            shutil.rmtree(upload_dir, ignore_errors=True)
        return page(request, "_estimate.html", ctx)

    # ------------------------------------------------------------------ página del run

    def controls_ctx(run_id: str, meta: dict[str, Any]) -> dict[str, Any]:
        live = state.is_live(run_id)
        return {
            "run_id": run_id,
            "live": live and not state.is_queued(run_id),
            "queued": state.is_queued(run_id),
            "job": state.job(run_id),
            "job_label": JOB_LABELS.get(state.job(run_id) or "", ""),
            "resumable": meta.get("status") in RESUMABLE and not live,
            "group": meta.get("config", {}).get("replica_of") or run_id,
        }

    @app.get("/runs/{run_id}", response_class=HTMLResponse)
    async def run_page(request: Request, run_id: str, tab: str = "resumen") -> Response:
        if tab not in dict(TABS):
            raise HTTPException(404, "pestaña desconocida")
        with open_store(run_id) as store:
            meta = store.meta_all()
            cfg = meta.get("config", {})
            brief = meta.get("brief") or {}
            n_posts = len(store.load_posts())
            n_agents = len(store.load_persona_data())
            n_events = len(store.load_events())
            n_calls = count_logged_calls(store.paths.calls_log) if store.paths.calls_log.is_file() else 0
            summary = summary_metrics(store) if store.metrics() else None
        key, label = status_of(run_id, meta)
        kpis = []
        if summary:
            kpis.append(
                {
                    "label": "Postura inicial → final",
                    "value": f"{summary['initial']['mean']:+.2f} → {summary['final']['mean']:+.2f}",
                    "sub": f"{summary['final']['positive_share']:.0%} a favor · {summary['final']['negative_share']:.0%} en contra",
                }
            )
        kpis += _survey_kpis((meta.get("survey") or {}).get("summary"))
        counts = {
            "feed": n_posts or None,
            "agentes": n_agents or None,
            "eventos": n_events or None,
            "llamadas": n_calls or None,
        }
        ctx = {
            "run_id": run_id,
            "meta": meta,
            "cfg": cfg,
            "title": brief.get("title") or brief.get("one_liner") or cfg.get("seed_name") or run_id,
            "status_key": key,
            "status_label": label,
            "progress": min(
                100, round(100 * int(meta.get("current_round") or 0) / max(int(cfg.get("rounds") or 1), 1))
            ),
            "kpis": kpis,
            "tab": tab,
            "tabs": [(k, lbl, counts.get(k)) for k, lbl in TABS],
            **controls_ctx(run_id, meta),
        }
        return page(request, "run.html", ctx)

    @app.get("/runs/{run_id}/controls", response_class=HTMLResponse)
    async def controls(request: Request, run_id: str) -> Response:
        with open_store(run_id) as store:
            meta = store.meta_all()
        return page(request, "_controls.html", controls_ctx(run_id, meta))

    @app.get("/runs/{run_id}/tab/{tab}", response_class=HTMLResponse)
    async def run_tab(request: Request, run_id: str, tab: str, segment: str = "", side: str = "") -> Response:
        if tab not in dict(TABS):
            raise HTTPException(404, "pestaña desconocida")
        with open_store(run_id) as store:
            meta = store.meta_all()
            cfg = meta.get("config", {})
            live = state.is_live(run_id)
            ctx: dict[str, Any] = {"run_id": run_id, "cfg": cfg, "live": live}
            if tab == "resumen":
                ctx.update(tab_resumen(store, meta))
            elif tab == "feed":
                ctx["round_choices"] = list(range(1, int(meta.get("current_round") or 0) + 1))
            elif tab == "agentes":
                ctx.update(tab_agentes(store, run_id, segment, side))
            elif tab == "encuesta":
                ctx.update(tab_encuesta(store, meta, live))
            elif tab == "informe":
                ctx.update(tab_informe(store, run_id, meta, live))
            elif tab == "eventos":
                ctx.update(tab_eventos(store, meta))
            elif tab == "chat":
                ctx["transcript"] = [dict(t) for t in store.transcript(ReportChat.KIND, ReportChat.KEY)]
            elif tab == "llamadas":
                ctx["calls"] = _calls_view(store.paths.calls_log)
        return page(request, f"_tab_{tab}.html", ctx)

    def tab_resumen(store: RunStore, meta: dict[str, Any]) -> dict[str, Any]:
        metrics = store.metrics()
        paths = store.paths
        ctx: dict[str, Any] = {
            "metrics": metrics,
            "brief_md": paths.brief.read_text(encoding="utf-8") if paths.brief.is_file() else None,
        }
        if not metrics:
            return ctx
        summary = summary_metrics(store)
        handles = {p["id"]: p["handle"] for p in store.load_persona_data()}
        survey_summary = (meta.get("survey") or {}).get("summary") or {}
        blocks = [("Todos", survey_summary.get("overall"))]
        if survey_summary.get("customers"):
            blocks.append(("Clientes potenciales", survey_summary["customers"]))
        ctx.update(
            summary=summary,
            svg_stance=charts.stance_lines_svg(metrics),
            svg_dist=charts.distribution_svg(metrics),
            svg_segments=charts.segments_svg(summary["by_segment"]),
            switchers=[
                {**s, "handle": handles.get(s["agent_id"], s["agent_id"])} for s in summary["switchers"]
            ],
            narratives=store.narratives(),
            summaries=store.round_summaries(),
            survey_blocks=[(name, b, charts.intent_bar_svg(b)) for name, b in blocks if b],
        )
        return ctx

    def tab_agentes(store: RunStore, run_id: str, segment: str, side: str) -> dict[str, Any]:
        personas = store.load_persona_data()
        traj = trajectories(store)
        switched = {s["agent_id"] for s in side_switchers(traj)}
        latest = store.latest_stances()
        follows = [(r["follower_id"], r["followee_id"]) for r in store.load_follows()]
        followers = Counter(b for _, b in follows)
        agents = []
        for p in sorted(personas, key=lambda p: p["handle"]):
            final = latest.get(p["id"], p["initial_stance"])
            agents.append(
                {
                    **p,
                    "initial": p["initial_stance"],
                    "final": final,
                    "spark": charts.sparkline_svg(traj.get(p["id"], [(0, p["initial_stance"])])),
                    "followers": followers.get(p["id"], 0),
                    "switched": p["id"] in switched,
                }
            )
        shown = agents
        if segment:
            shown = [a for a in shown if a["segment"] == segment]
        if side in ("pro", "mid", "con"):
            shown = [a for a in shown if charts.stance_class(a["final"]) == side]
        elif side == "switch":
            shown = [a for a in shown if a["switched"]]
        elif side == "influencer":
            shown = [a for a in shown if a.get("influencer")]
        return {
            "agents": agents,
            "shown": shown,
            "segments": sorted({p["segment"] for p in personas}),
            "segment": segment,
            "side": side,
            "svg_network": charts.network_svg(run_id, personas, follows, latest),
        }

    def tab_encuesta(store: RunStore, meta: dict[str, Any], live: bool) -> dict[str, Any]:
        survey = meta.get("survey")
        has_population = bool(store.load_persona_data())
        ctx: dict[str, Any] = {"survey": survey, "can_survey": has_population and not live}
        if not survey:
            return ctx
        s = survey["summary"]
        personas = {p["id"]: p for p in store.load_persona_data()}
        blocks: list[tuple[str, dict[str, Any]]] = [("Todos", s["overall"])]
        if s.get("customers"):
            blocks.append(("Clientes potenciales", s["customers"]))
        blocks += [(f"Papel: {k}", v) for k, v in (s.get("by_role") or {}).items()]
        blocks += [(f"Segmento: {k}", v) for k, v in (s.get("by_segment") or {}).items()]
        price = s.get("price")
        ctx.update(
            blocks=[(name, b, charts.intent_bar_svg(b)) for name, b in blocks],
            price=price,
            svg_price=charts.price_curve_svg(price) if price else None,
            answers=[
                {
                    **a,
                    "handle": personas.get(a["agent_id"], {}).get("handle", a["agent_id"]),
                    "segment": personas.get(a["agent_id"], {}).get("segment", ""),
                }
                for a in survey.get("answers", [])
            ],
        )
        return ctx

    def tab_informe(store: RunStore, run_id: str, meta: dict[str, Any], live: bool) -> dict[str, Any]:
        paths = store.paths
        return {
            "report_md": (
                paths.report.read_text(encoding="utf-8").replace("](charts/", f"](/runs/{run_id}/charts/")
                if paths.report.is_file()
                else None
            ),
            "dossier": paths.dossier.is_file(),
            "can_report": not live and int(meta.get("current_round") or 0) >= 1,
            "charts": [
                (f"{name}.png", label, int((paths.charts_dir / f"{name}.png").stat().st_mtime))
                for name, label in CHARTS
                if (paths.charts_dir / f"{name}.png").is_file()
            ],
        }

    def tab_eventos(store: RunStore, meta: dict[str, Any], error: str | None = None) -> dict[str, Any]:
        current = int(meta.get("current_round") or 0)
        rounds = int(meta.get("config", {}).get("rounds") or 0)
        events = []
        for r in store.load_events():
            data = json.loads(r["data"])
            events.append({**data, "post_id": r["post_id"]})
        return {
            "events": events,
            "current_round": current,
            "rounds": rounds,
            "can_inject": current < rounds,
            "error": error,
        }

    @app.post("/runs/{run_id}/inject", response_model=None)
    async def inject(
        request: Request,
        run_id: str,
        round: Annotated[str, Form()] = "",
        title: Annotated[str, Form()] = "",
        text: Annotated[str, Form()] = "",
        source: Annotated[str, Form()] = "Noticias",
        handle: Annotated[str, Form()] = "noticias",
    ) -> Response:
        with open_store(run_id) as store:
            meta = store.meta_all()
            current = int(meta.get("current_round") or 0)
            rounds = int(meta.get("config", {}).get("rounds") or 0)
            try:
                round_no = _int_field(round, "ronda", current + 1, rounds)
                if not title.strip():
                    raise ValueError("falta el titular")
                ev = EventSpec(
                    round=round_no,
                    title=title.strip(),
                    text=text,
                    source=source or "Noticias",
                    handle=handle,
                    origin="web",
                )
                ev.id = f"e{len(store.load_events()) + 1:02d}"
                store.save_event(ev)
            except ValueError as exc:
                ctx = {"run_id": run_id, "cfg": meta.get("config", {}), "live": state.is_live(run_id)}
                ctx.update(tab_eventos(store, meta, error=str(exc)))
                return page(request, "_tab_eventos.html", ctx, status_code=400)
        state.logs.setdefault(run_id, RunLog()).add(
            f"Evento {ev.id} añadido a la ronda {round_no}: {ev.title}"
        )
        return RedirectResponse(f"/runs/{run_id}?tab=eventos", status_code=303)

    @app.post("/runs/{run_id}/pause", response_model=None)
    async def pause_run(run_id: str) -> Response:
        run_root(run_id)
        live = state.live.get(run_id)
        if live is not None and live.active and live.kind == "run":
            live.engine.stop_requested = True
            state.logs.setdefault(run_id, RunLog()).add("Pausa solicitada: se detendrá al terminar la ronda")
        return RedirectResponse(f"/runs/{run_id}", status_code=303)

    @app.post("/runs/{run_id}/resume", response_model=None)
    async def resume_run(run_id: str) -> Response:
        with open_store(run_id) as store:
            status = store.get_meta("status")
        if status in RESUMABLE and not state.is_live(run_id):
            launch(Engine.open(settings, run_id))
        return RedirectResponse(f"/runs/{run_id}", status_code=303)

    @app.post("/runs/{run_id}/survey", response_model=None)
    async def survey_run(
        run_id: str, question: Annotated[str, Form()] = "", report: Annotated[str | None, Form()] = None
    ) -> Response:
        run_root(run_id)
        if not state.is_live(run_id):
            engine = Engine.open(settings, run_id)

            async def work() -> str:
                with engine.budget.reserve_unlocked():
                    data = await run_survey(
                        engine.llm,
                        engine.store,
                        engine.settings,
                        question.strip() or None,
                        cache=engine.store,
                    )
                engine.store.update_meta(survey=data | {"round": engine.store.get_meta("current_round")})
                if report is not None:
                    engine.cfg.survey = True
                    provisional = int(engine.store.get_meta("current_round", 0)) < engine.cfg.rounds
                    await generate_report(engine, provisional=provisional, allow_llm=True)
                return "completed"

            launch(engine, kind="survey", work=work)
        return RedirectResponse(f"/runs/{run_id}?tab=encuesta", status_code=303)

    @app.post("/runs/{run_id}/report", response_model=None)
    async def report_run(run_id: str, llm: Annotated[str | None, Form()] = None) -> Response:
        run_root(run_id)
        if not state.is_live(run_id):
            engine = Engine.open(settings, run_id)

            async def work() -> str:
                provisional = int(engine.store.get_meta("current_round", 0)) < engine.cfg.rounds
                await generate_report(
                    engine, provisional=provisional, allow_llm=True, llm_report=llm is not None
                )
                return "completed"

            launch(engine, kind="report", work=work)
        return RedirectResponse(f"/runs/{run_id}?tab=informe", status_code=303)

    @app.post("/runs/{run_id}/replicate", response_model=None)
    async def replicate_run(
        run_id: str, n: Annotated[str, Form()] = "2", same_population: Annotated[str | None, Form()] = None
    ) -> Response:
        run_root(run_id)
        try:
            count = _int_field(n, "réplicas", 1, 10)
            plan = plan_replicas(settings, run_id, count, same_population=same_population is not None)
            engines = [create_replica(settings, plan, k) for k in plan.indexes]
        except (ValueError, FileExistsError, FileNotFoundError) as exc:
            raise HTTPException(400, str(exc)) from exc
        for engine in engines:
            launch(engine)
        notice = f"{count} réplica(s) de {plan.group} en cola: " + ", ".join(e.cfg.run_id for e in engines)
        return RedirectResponse(f"/?q={plan.group}&notice={notice}", status_code=303)

    @app.post("/runs/{run_id}/delete", response_model=None)
    async def delete_run(run_id: str) -> Response:
        root = run_root(run_id)
        if state.is_live(run_id):
            raise HTTPException(409, "el run está en marcha")
        state.drop_sessions(run_id)
        state.live.pop(run_id, None)
        state.logs.pop(run_id, None)
        shutil.rmtree(root, ignore_errors=True)
        return RedirectResponse(f"/?notice=Run {run_id} borrado", status_code=303)

    @app.get("/runs/{run_id}/events")
    async def run_events(request: Request, run_id: str) -> StreamingResponse:
        root = run_root(run_id)

        async def stream() -> AsyncIterator[str]:
            store = RunStore.open_existing(root)
            seen = 0
            try:
                while True:
                    live = state.is_live(run_id)
                    meta = store.meta_all()
                    log = state.logs.get(run_id)
                    lines, seen = log.since(seen) if log else ([], seen)
                    key, label = status_of(run_id, meta)
                    final = meta.get("status", "?") in TERMINAL and not live
                    payload = {
                        "status": key,
                        "status_label": label,
                        "stage": meta.get("stage"),
                        "stage_label": STAGE_LABELS.get(meta.get("stage", ""), meta.get("stage")),
                        "current_round": meta.get("current_round", 0),
                        "rounds": meta.get("config", {}).get("rounds"),
                        "calls_used": meta.get("calls_used", 0),
                        "pause_reason": meta.get("pause_reason"),
                        "live": live,
                        "queued": state.is_queued(run_id),
                        "job": state.job(run_id),
                        "final": final,
                        "logs": lines,
                    }
                    yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                    if final or await request.is_disconnected():
                        break
                    await asyncio.sleep(poll_interval)
            finally:
                store.close()

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/runs/{run_id}/charts/{name}", response_model=None)
    async def chart(run_id: str, name: str) -> Response:
        charts_dir = (run_root(run_id) / "charts").resolve()
        if not _CHART_RE.match(name):
            raise HTTPException(404, "gráfico no encontrado")
        path = (charts_dir / name).resolve()
        if path.parent != charts_dir or not path.is_file():
            raise HTTPException(404, "gráfico no encontrado")
        return FileResponse(path, media_type="image/png")

    @app.get("/runs/{run_id}/files/{name}", response_model=None)
    async def download(run_id: str, name: str) -> Response:
        root = run_root(run_id)
        paths = RunStore.open_existing(root).paths  # solo rutas; la conexión se cierra enseguida
        if name in DOWNLOADS:
            path = getattr(paths, DOWNLOADS[name])
        elif name in METRIC_FILES:
            path = paths.metrics_dir / name
        else:
            raise HTTPException(404, "fichero no encontrado")
        if not path.is_file():
            raise HTTPException(404, "fichero no encontrado")
        return FileResponse(path, filename=f"{run_id}-{name}")

    @app.get("/runs/{run_id}/pdf", response_model=None)
    async def pdf(run_id: str) -> Response:
        root = run_root(run_id)
        if not (root / "report.md").is_file():
            raise HTTPException(404, "todavía no hay informe")
        path = await asyncio.to_thread(report_to_pdf, root)
        return FileResponse(path, media_type="application/pdf", filename=f"{run_id}-informe.pdf")

    @app.get("/runs/{run_id}/timeline", response_class=HTMLResponse)
    async def timeline(
        request: Request, run_id: str, round: str = "", q: str = "", kind: str = "", sort: str = ""
    ) -> Response:
        round_no: int | None = None
        if round.strip():
            try:
                round_no = int(round)
            except ValueError:
                raise HTTPException(400, "ronda inválida") from None
        with open_store(run_id) as store:
            rows = store.search_posts(q, limit=50) if q.strip() else store.load_posts()
            if round_no is not None:
                rows = [r for r in rows if r["round"] == round_no]
            if kind:
                rows = [r for r in rows if r["kind"] == kind]
            posts = _posts_view(store, rows)
        if sort == "eco":
            posts.sort(key=lambda p: -_engagement(p))
        ctx = {"run_id": run_id, "posts": posts, "round": round_no, "q": q.strip()}
        return page(request, "_timeline.html", ctx)

    # ------------------------------------------------------------------ agentes y entrevistas

    def find_persona(store: RunStore, handle: str) -> dict[str, Any]:
        handle = handle.lstrip("@").lower()
        if not _HANDLE_RE.match(handle):
            raise HTTPException(404, "agente no encontrado")
        for p in store.load_persona_data():
            if p["handle"] == handle:
                return p
        raise HTTPException(404, "agente no encontrado")

    @app.get("/runs/{run_id}/agents/{handle}", response_class=HTMLResponse)
    async def agent_card(request: Request, run_id: str, handle: str) -> Response:
        with open_store(run_id) as store:
            p = find_persona(store, handle)
            traj = trajectories(store).get(p["id"], [(0, p["initial_stance"])])
            answer = next(
                (a for a in (store.get_meta("survey") or {}).get("answers", []) if a["agent_id"] == p["id"]),
                None,
            )
            posts = _posts_view(store, [r for r in store.load_posts() if r["author_id"] == p["id"]])
            ctx = {
                "run_id": run_id,
                "p": p,
                "traits": sorted((p.get("traits") or {}).items()),
                "final": traj[-1][1],
                "spark": charts.sparkline_svg(traj),
                "answer": answer,
                "decisions": [dict(d) for d in store.decisions_for(p["id"])],
                "posts": posts,
                "transcript": [dict(t) for t in store.transcript(Interview.KIND, p["handle"])],
                "dry": bool(store.get_meta("config", {}).get("dry_run")),
            }
        return page(request, "_agent.html", ctx)

    @app.post("/runs/{run_id}/interview/{handle}", response_class=HTMLResponse)
    async def interview(
        request: Request, run_id: str, handle: str, message: Annotated[str, Form()] = ""
    ) -> Response:
        root = run_root(run_id)
        text = message.strip()
        ctx: dict[str, Any] = {"message": text, "command": None, "answer": None, "error": None}
        if not text:
            ctx["error"] = "Escribe una pregunta."
            return page(request, "_chat_message.html", ctx)
        key = (run_id, handle.lstrip("@").lower())
        lock = state.locks.setdefault(f"interview:{run_id}:{key[1]}", asyncio.Lock())
        async with lock:
            session = state.interviews.get(key)
            if session is None:
                store = RunStore.open_existing(root)
                dry = bool(store.get_meta("config", {}).get("dry_run"))
                try:
                    session = Interview(store, settings, build_llm(settings, root, dry_run=dry), key[1])
                except KeyError:
                    store.close()
                    raise HTTPException(404, "agente no encontrado") from None
                state.interviews[key] = session
            try:
                ctx["answer"] = await session.ask(text)
            except Exception as exc:  # errores del LLM: se muestran en la entrevista
                ctx["error"] = f"{type(exc).__name__}: {exc}"
        return page(request, "_chat_message.html", ctx)

    @app.post("/runs/{run_id}/chat", response_class=HTMLResponse)
    async def chat(request: Request, run_id: str, message: Annotated[str, Form()] = "") -> Response:
        root = run_root(run_id)
        text = message.strip()
        ctx: dict[str, Any] = {"message": text, "command": None, "answer": None, "error": None}
        if not text:
            ctx["error"] = "Escribe una pregunta o un comando (/buscar, /agente, /post)."
            return page(request, "_chat_message.html", ctx)
        lock = state.locks.setdefault(f"chat:{run_id}", asyncio.Lock())
        async with lock:
            session = state.chats.get(run_id)
            if session is None:
                store = RunStore.open_existing(root)
                dry = bool(store.get_meta("config", {}).get("dry_run"))
                session = ReportChat(store, settings, build_llm(settings, root, dry_run=dry))
                state.chats[run_id] = session
            if text.split(" ", 1)[0] in CHAT_COMMANDS:
                output = session.command(text)
                if output is None:
                    ctx["error"] = "Uso: /buscar <texto>, /agente <@handle>, /post <id>"
                else:
                    ctx["command"] = output
            else:
                try:
                    ctx["answer"] = await session.ask(text)
                except Exception as exc:  # errores del LLM: se muestran en el chat
                    ctx["error"] = f"{type(exc).__name__}: {exc}"
        return page(request, "_chat_message.html", ctx)

    # ------------------------------------------------------------------ comparar, scorecard, calibración

    @app.get("/comparar", response_class=HTMLResponse)
    async def compare_page(
        request: Request, a: str = "", b: str = "", sel: Annotated[list[str] | None, Query()] = None
    ) -> Response:
        sel = [s for s in (sel or []) if s]
        if not a and len(sel) >= 1:
            a = sel[0]
        if not b and len(sel) >= 2:
            b = ",".join(sel[1:])
        ctx: dict[str, Any] = {"a": a, "b": b, "md": None, "path": None, "error": None, "runs": run_ids()}
        if a and b:
            try:
                md, path = await asyncio.to_thread(compare_runs, settings, a, b)
                ctx.update(md=md, path=path)
            except Exception as exc:
                ctx["error"] = str(exc) or type(exc).__name__
        elif a or b:
            ctx["error"] = "Indica los dos lados."
        return page(request, "compare.html", ctx)

    @app.get("/scorecard", response_class=HTMLResponse)
    async def scorecard_page(
        request: Request, runs: str = "", sel: Annotated[list[str] | None, Query()] = None
    ) -> Response:
        specs = _split_specs(runs, sel)
        spec = " ".join(specs)
        ctx: dict[str, Any] = {"spec": spec, "md": None, "error": None, "run_ids": run_ids()}
        if specs:
            try:
                ids = expand_run_ids(settings.runs_dir, specs)
                ctx["md"] = await asyncio.to_thread(
                    lambda: scorecard_md(load_rows(settings.runs_dir, ids, with_objection=True))
                )
            except Exception as exc:
                ctx["error"] = str(exc) or type(exc).__name__
        return page(request, "scorecard.html", ctx)

    def calibration(specs: list[str], outcomes: str, save: bool) -> tuple[str, str | None]:
        ids = expand_run_ids(settings.runs_dir, specs)
        rows = load_rows(settings.runs_dir, ids)
        noise = noise_rows(rows)
        parts = ["## Resultados por idea", "", scorecard_md(rows)]
        parts += ["## Ruido entre réplicas frente a diferencias entre ideas", "", noise_md(noise)]
        if outcomes.strip():
            path = Path(outcomes.strip()).expanduser()
            if not path.is_absolute():
                path = ROOT / path
            if not path.is_file():
                raise FileNotFoundError(f"no existe el TOML de desenlaces {outcomes}")
            parts += ["## ¿Separan las métricas los éxitos de los fracasos reales?", ""]
            parts.append(outcome_md(outcome_rows(rows, load_outcomes(path))))
        saved = None
        if save and noise:
            saved = f"Ruido de referencia guardado en {save_noise_reference(settings.runs_dir, noise, rows)}"
        elif save:
            raise ValueError("para medir el ruido hacen falta al menos 2 ideas con 2 réplicas cada una")
        return "\n".join(parts), saved

    @app.get("/calibracion", response_class=HTMLResponse)
    async def calibrate_page(request: Request, runs: str = "", outcomes: str = "") -> Response:
        specs = _split_specs(runs)
        ctx: dict[str, Any] = {
            "spec": " ".join(specs),
            "outcomes": outcomes,
            "md": None,
            "error": None,
            "saved": None,
            "can_save": False,
            "noise": _noise_info(settings),
            "run_ids": run_ids(),
        }
        if specs:
            try:
                ctx["md"], _ = await asyncio.to_thread(calibration, specs, outcomes, False)
                ctx["can_save"] = True
            except Exception as exc:
                ctx["error"] = str(exc) or type(exc).__name__
        return page(request, "calibrate.html", ctx)

    @app.post("/calibracion/guardar", response_class=HTMLResponse)
    async def calibrate_save(request: Request, runs: Annotated[str, Form()] = "") -> Response:
        specs = _split_specs(runs)
        ctx: dict[str, Any] = {
            "spec": " ".join(specs),
            "outcomes": "",
            "md": None,
            "error": None,
            "saved": None,
            "can_save": False,
            "run_ids": run_ids(),
        }
        try:
            ctx["md"], ctx["saved"] = await asyncio.to_thread(calibration, specs, "", True)
        except Exception as exc:
            ctx["error"] = str(exc) or type(exc).__name__
        ctx["noise"] = _noise_info(settings)
        return page(request, "calibrate.html", ctx)

    # ------------------------------------------------------------------ ajustes

    @app.get("/ajustes", response_class=HTMLResponse)
    async def settings_page(request: Request) -> Response:
        rows = [(k.upper(), v) for k, v in settings.model_dump().items()]
        ctx = {"rows": rows, "noise": _noise_info(settings), "n_runs": len(run_ids())}
        return page(request, "settings.html", ctx)

    @app.post("/ajustes/doctor", response_class=HTMLResponse)
    async def doctor(request: Request) -> Response:
        report = await run_doctor(settings, live=False, probe=0)
        return page(request, "_doctor.html", {"report": report})

    return app
