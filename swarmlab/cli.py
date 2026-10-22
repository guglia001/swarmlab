"""CLI de SwarmLab (typer + rich)."""

from __future__ import annotations

import asyncio
import contextlib
import signal
import time
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.prompt import Prompt
from rich.table import Table

from swarmlab.config import Settings, load_settings

app = typer.Typer(add_completion=False, no_args_is_help=True, rich_markup_mode="rich")
console = Console()

ConfigOpt = Annotated[Path | None, typer.Option("--config", "-c", help="config.toml a usar")]
StanceModeOpt = Annotated[
    str | None,
    typer.Option(
        "--stance-mode",
        help="impresion: la postura inicial se mide (primera impresión en personaje); cuotas: se fija por plan",
    ),
]
ReportLLMOpt = Annotated[
    bool | None,
    typer.Option(
        "--report-llm/--no-report-llm", help="informe y narrativas con el modelo pesado (no = solo métricas)"
    ),
]
RunsArg = Annotated[
    list[str], typer.Argument(help="runs: ids, listas con comas o patrones entre comillas ('idea05-*')")
]
STATUS_COLORS = {
    "completed": "green",
    "paused": "yellow",
    "running": "cyan",
    "failed": "red",
    "stopped": "blue",
}


@app.callback()
def _root() -> None:
    """SwarmLab: simulador de enjambre de agentes para estresar ideas, con Claude Code headless (claude -p)."""


def _settings(config: Path | None, **overrides: Any) -> Settings:
    try:
        return load_settings(config, overrides)
    except Exception as exc:
        console.print(f"[red]Configuración inválida:[/] {exc}")
        raise typer.Exit(2) from exc


# --------------------------------------------------------------------------- doctor


@app.command()
def doctor(
    config: ConfigOpt = None,
    offline: Annotated[bool, typer.Option("--offline", help="no hace llamadas reales")] = False,
    probe: Annotated[int, typer.Option(help="llamadas paralelas para medir concurrencia (0 = no)")] = 3,
) -> None:
    """Comprueba que claude está instalado, logueado, aislado y responde; muestra versión y flags."""
    from swarmlab.doctor import run_doctor

    settings = _settings(config)
    with console.status("Comprobando claude…"):
        rep = asyncio.run(run_doctor(settings, live=not offline, probe=probe))
    table = Table(title=f"swarmlab doctor — {rep.version or 'claude no encontrado'}")
    table.add_column("Comprobación")
    table.add_column("Estado")
    table.add_column("Detalle", overflow="fold")
    for c in rep.checks:
        state = "[green]OK[/]" if c.ok else ("[yellow]AVISO[/]" if c.warn else "[red]FALLO[/]")
        table.add_row(c.name, state, c.detail)
    console.print(table)
    if rep.flags_found:
        console.print(f"Flags detectados: {' '.join(rep.flags_found)} (+ --max-turns validado en vivo)")
    if rep.suggested_concurrency is not None:
        console.print(
            f"MAX_CONCURRENCY actual={settings.max_concurrency}; sugerido={rep.suggested_concurrency}"
        )
    raise typer.Exit(0 if rep.ok else 1)


# --------------------------------------------------------------------------- estimate


def _estimate(
    settings: Settings,
    seed: Path,
    agents: int,
    rounds: int,
    apc: int,
    max_calls: int | None,
    personas_from: str | None = None,
    survey: bool = False,
    stance_mode: str | None = None,
    report_llm: bool | None = None,
) -> Any:
    from swarmlab.engine import estimate_seed

    return estimate_seed(
        settings,
        seed,
        agents=agents,
        rounds=rounds,
        agents_per_call=apc,
        max_calls=max_calls,
        personas_from=personas_from,
        survey=survey,
        stance_mode=stance_mode,
        report_llm=report_llm,
    )


def _print_estimate(est: Any, agents: int, rounds: int, apc: int, concurrency: int) -> None:
    table = Table(
        title=f"Estimación: {agents} agentes × {rounds} rondas, {apc} agentes/llamada, concurrencia {concurrency}"
    )
    table.add_column("Fase")
    table.add_column("Llamadas", justify="right")
    for phase, n in est.calls_by_phase.items():
        table.add_row(phase, str(n))
    table.add_row("[bold]Total[/]", f"[bold]{est.total_calls}[/]")
    console.print(table)
    console.print(
        f"≈ {est.minutes:.0f} min · ≈ {est.input_tokens / 1000:.0f}k tokens de entrada y "
        f"{est.output_tokens / 1000:.0f}k de salida · {est.expected_active_per_round:.0f} agentes activos/ronda · "
        f"tope MAX_CALLS_PER_RUN={est.max_calls}"
    )
    if est.exceeds_cap:
        console.print(f"[yellow]La estimación supera el tope de {est.max_calls} llamadas.[/]")


@app.command()
def estimate(
    seed: Annotated[Path, typer.Option("--seed", exists=True, dir_okay=False, help="documento semilla")],
    agents: Annotated[int | None, typer.Option(help="número de agentes (10–200)")] = None,
    rounds: Annotated[int | None, typer.Option(help="número de rondas")] = None,
    agents_per_call: Annotated[int | None, typer.Option("--agents-per-call")] = None,
    concurrency: Annotated[int | None, typer.Option(help="MAX_CONCURRENCY")] = None,
    max_calls: Annotated[int | None, typer.Option("--max-calls")] = None,
    survey: Annotated[
        bool | None, typer.Option("--survey/--no-survey", help="incluir la encuesta final")
    ] = None,
    stance_mode: StanceModeOpt = None,
    report_llm: ReportLLMOpt = None,
    config: ConfigOpt = None,
) -> None:
    """Calcula llamadas, tiempo y tokens aproximados para una simulación."""
    settings = _settings(config, max_concurrency=concurrency, stance_mode=stance_mode)
    n, r = agents or settings.default_agents, rounds or settings.default_rounds
    apc = agents_per_call or settings.agents_per_call
    do_survey = settings.survey if survey is None else survey
    est = _estimate(settings, seed, n, r, apc, max_calls, survey=do_survey, report_llm=report_llm)
    _print_estimate(est, n, r, apc, settings.max_concurrency)
    if est.exceeds_cap and not typer.confirm("¿Continuar de todos modos? (solo informativo)", default=True):
        raise typer.Exit(1)


# --------------------------------------------------------------------------- ejecución con progreso


async def _drive(engine: Any, until: str | None, interactive: bool, est_calls: int) -> str:
    from swarmlab.events import EventSpec

    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    presses = 0
    started = time.monotonic()
    progress = Progress(
        SpinnerColumn(),
        TextColumn("[bold]{task.description}"),
        BarColumn(),
        TextColumn("ronda {task.completed:.0f}/{task.total:.0f}"),
        TextColumn("· llamadas {task.fields[calls]}/~{task.fields[est]}"),
        TimeElapsedColumn(),
        console=console,
    )
    done_rounds = int(engine.store.get_meta("current_round", 0))
    task_id = progress.add_task(
        engine.stage, total=engine.cfg.rounds, completed=done_rounds, calls=engine.budget.used, est=est_calls
    )

    def on_event(kind: str, data: dict[str, Any]) -> None:
        if kind == "stage":
            progress.update(
                task_id,
                description={
                    "ontology": "Ontología",
                    "personas": "Personas",
                    "simulation": "Simulación",
                    "report": "Informe",
                }.get(data["name"], data["name"]),
            )
        elif kind == "round_start":
            progress.update(task_id, description=f"Ronda {data['round']}")
        elif kind == "batches":
            progress.console.print(
                f"  R{data['round']}: {data['active']} agentes activos en {data['batches']} lotes"
            )
        elif kind == "round_end":
            s = data["stats"]
            progress.update(task_id, completed=data["round"])
            progress.console.print(
                f"[green]✓ Ronda {data['round']}[/] media {s['mean_before']:+.2f}→{s['mean']:+.2f} · "
                f"{s['posts_created']} posts · polarización {s['polarization']:.2f} · {data['summary'].get('mood', '')}"
            )
        elif kind == "ontology":
            st = data["stats"]
            progress.console.print(
                f"[green]✓ Ontología[/] {data['entities']} entidades, {data['relations']} relaciones ({st})"
            )
        elif kind == "personas":
            div = data["diversity"]
            progress.console.print(
                f"[green]✓ Personas[/] {data['count']} agentes · diversidad {div['score']:.2f} "
                f"(similitud media de bios {div['mean_bio_similarity']:.2f}) · {data['follows']} follows"
            )
            for w in div["warnings"]:
                progress.console.print(f"  [yellow]aviso:[/] {w}")
        elif kind == "log":
            progress.console.print(f"  {data['message']}")
        elif kind == "status" and data.get("status") == "paused":
            progress.console.print(
                f"[yellow]⏸ Pausado:[/] {data.get('pause_reason')} {data.get('reset_hint') or ''}"
            )

    engine.on_event = on_event

    def on_sigint() -> None:
        nonlocal presses
        presses += 1
        if presses == 1:
            engine.stop_requested = True
            progress.console.print(
                "[yellow]Ctrl+C: se pausará al terminar la ronda en curso (otra vez para cortar ya).[/]"
            )
        elif task is not None:
            task.cancel()

    async def ticker() -> None:
        while True:
            progress.update(task_id, calls=engine.budget.used)
            await asyncio.sleep(0.5)

    if interactive:

        async def hook(r: int) -> list[Any] | str | None:
            progress.stop()
            try:
                text = await asyncio.to_thread(
                    Prompt.ask,
                    f"[cyan]Modo dios[/] — evento para la ronda {r + 1} (Enter = seguir, 'stop' = pausar)",
                    default="",
                )
            finally:
                progress.start()
            text = text.strip()
            if text.lower() == "stop":
                return "stop"
            if text:
                return [EventSpec(round=r + 1, title=text[:120], text=text, origin="interactive")]
            return None

        engine.round_hook = hook

    with contextlib.suppress(NotImplementedError):
        loop.add_signal_handler(signal.SIGINT, on_sigint)
    tick = asyncio.create_task(ticker())
    progress.start()
    try:
        return await engine.run(until=until)
    finally:
        tick.cancel()
        progress.update(task_id, calls=engine.budget.used)
        progress.stop()
        with contextlib.suppress(NotImplementedError):
            loop.remove_signal_handler(signal.SIGINT)
        console.print(f"Tiempo: {time.monotonic() - started:.0f} s · llamadas usadas: {engine.budget.used}")


def _finish(engine: Any, status: str) -> None:
    meta = engine.store.meta_all()
    color = STATUS_COLORS.get(status, "white")
    console.print(
        f"[{color}]Estado: {status}[/] · run [bold]{engine.cfg.run_id}[/] · ronda {meta.get('current_round')}/{engine.cfg.rounds}"
    )
    if status == "paused":
        reason = meta.get("pause_reason")
        hint = f" (reinicio: {meta.get('reset_hint')})" if meta.get("reset_hint") else ""
        console.print(f"Motivo: {reason}{hint}. Continúa con: [bold]swarmlab resume {engine.cfg.run_id}[/]")
        if reason == "budget":
            console.print("Sube el tope con: swarmlab resume <run_id> --max-calls N")
    if engine.paths.report.is_file():
        console.print(f"Informe: {engine.paths.report}")
    if engine.paths.brief.is_file():
        console.print(f"Brief: {engine.paths.brief}")


def _run_engine(engine: Any, until: str | None, interactive: bool, est_calls: int) -> None:
    try:
        status = asyncio.run(_drive(engine, until, interactive, est_calls))
    except (KeyboardInterrupt, asyncio.CancelledError):
        console.print(
            f"[yellow]Interrumpido.[/] Estado guardado en la última ronda completa. Continúa con: swarmlab resume {engine.cfg.run_id}"
        )
        raise typer.Exit(130) from None
    _finish(engine, status)


@app.command()
def run(
    seed: Annotated[
        Path, typer.Option("--seed", exists=True, dir_okay=False, help="documento semilla (.md/.txt/.pdf)")
    ],
    prompt: Annotated[
        str, typer.Option("--prompt", "-p", help="pregunta que debe responder la simulación")
    ] = "",
    agents: Annotated[int | None, typer.Option(help="número de agentes (10–200)")] = None,
    rounds: Annotated[int | None, typer.Option(help="número de rondas")] = None,
    events: Annotated[
        Path | None, typer.Option("--events", exists=True, dir_okay=False, help="YAML de eventos")
    ] = None,
    agents_per_call: Annotated[int | None, typer.Option("--agents-per-call")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="sin red: respuestas deterministas")] = False,
    until: Annotated[
        str | None, typer.Option(help="parar tras la etapa ontology|personas|simulation")
    ] = None,
    interactive: Annotated[
        bool, typer.Option("--interactive", "-i", help="pausa tras cada ronda para inyectar eventos")
    ] = False,
    platform: Annotated[str, typer.Option(help="twitter | forum")] = "twitter",
    rng_seed: Annotated[int, typer.Option("--rng-seed", help="semilla aleatoria")] = 7,
    max_calls: Annotated[int | None, typer.Option("--max-calls", help="tope de llamadas de este run")] = None,
    interval: Annotated[str | None, typer.Option(help="tiempo simulado por ronda, p. ej. '1 semana'")] = None,
    language: Annotated[str | None, typer.Option(help="forzar idioma de los agentes")] = None,
    run_id: Annotated[
        str | None, typer.Option("--run-id", help="id del run (por defecto fecha + semilla)")
    ] = None,
    personas_from: Annotated[
        str | None, typer.Option("--personas-from", help="reutilizar la población de otro run (A/B)")
    ] = None,
    survey: Annotated[
        bool | None,
        typer.Option("--survey/--no-survey", help="encuesta final de intención de uso/compra en personaje"),
    ] = None,
    stance_mode: StanceModeOpt = None,
    report_llm: ReportLLMOpt = None,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="no pedir confirmación")] = False,
    debug: Annotated[bool, typer.Option("--debug", help="guardar prompts y respuestas completos")] = False,
    config: ConfigOpt = None,
) -> None:
    """Lanza una simulación completa: ontología → personas → rondas → informe."""
    from swarmlab.engine import Engine

    settings = _settings(config, debug=debug or None, stance_mode=stance_mode)
    n, r = agents or settings.default_agents, rounds or settings.default_rounds
    apc = agents_per_call or settings.agents_per_call
    do_survey = settings.survey if survey is None else survey
    est = _estimate(settings, seed, n, r, apc, max_calls, personas_from, do_survey, report_llm=report_llm)
    _print_estimate(est, n, r, apc, settings.max_concurrency)
    if (
        est.exceeds_cap
        and not yes
        and not typer.confirm(
            f"Supera el tope de {est.max_calls} llamadas. ¿Lanzar igualmente?", default=False
        )
    ):
        raise typer.Exit(1)
    try:
        engine = Engine.create(
            settings,
            seed=seed,
            question=prompt,
            n_agents=n,
            rounds=r,
            agents_per_call=apc,
            events_path=events,
            dry_run=dry_run,
            rng_seed=rng_seed,
            platform=platform,
            language=language,
            max_calls=max_calls,
            interactive=interactive,
            personas_from=personas_from,
            survey=do_survey,
            round_interval=interval,
            run_id=run_id,
            report_llm=report_llm,
        )
    except (ValueError, FileExistsError, FileNotFoundError) as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(2) from exc
    console.print(f"Run [bold]{engine.cfg.run_id}[/] en {engine.paths.root}{' (dry-run)' if dry_run else ''}")
    _run_engine(engine, until, interactive, est.total_calls)


@app.command()
def resume(
    run_id: Annotated[str, typer.Argument(help="id del run")],
    max_calls: Annotated[int | None, typer.Option("--max-calls", help="nuevo tope de llamadas")] = None,
    until: Annotated[
        str | None, typer.Option(help="parar tras la etapa ontology|personas|simulation")
    ] = None,
    interactive: Annotated[bool, typer.Option("--interactive", "-i")] = False,
    config: ConfigOpt = None,
) -> None:
    """Continúa un run pausado o interrumpido desde su último checkpoint."""
    from swarmlab.engine import Engine

    settings = _settings(config)
    try:
        engine = Engine.open(settings, run_id, max_calls=max_calls)
    except FileNotFoundError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(2) from exc
    meta = engine.store.meta_all()
    if meta.get("status") == "completed":
        console.print("Este run ya está completo. Usa `swarmlab report` para regenerar el informe.")
        raise typer.Exit(0)
    console.print(
        f"Reanudando [bold]{run_id}[/] desde la etapa {meta.get('stage')} · ronda {meta.get('current_round')}/{engine.cfg.rounds}"
    )
    _run_engine(
        engine,
        until,
        interactive or engine.cfg.interactive,
        engine.budget.used + max(0, engine.cfg.rounds - int(meta.get("current_round", 0))) * 5,
    )


@app.command()
def inject(
    run_id: Annotated[str, typer.Argument()],
    round_no: Annotated[int, typer.Option("--round", help="ronda en la que aparece")],
    title: Annotated[str, typer.Option(help="titular del evento")],
    text: Annotated[str, typer.Option(help="texto completo")] = "",
    source: Annotated[str, typer.Option(help="quién lo publica")] = "Noticias",
    handle: Annotated[str, typer.Option(help="handle de la cuenta que lo publica")] = "noticias",
    config: ConfigOpt = None,
) -> None:
    """Añade un evento ("modo dios") a un run pausado, para una ronda que aún no se ha jugado."""
    from swarmlab.engine import Engine
    from swarmlab.events import EventSpec

    engine = Engine.open(_settings(config), run_id)
    current = int(engine.store.get_meta("current_round", 0))
    if round_no <= current:
        console.print(f"[red]La ronda {round_no} ya se jugó (última completada: {current}).[/]")
        raise typer.Exit(2)
    ev = engine.add_event(
        EventSpec(round=round_no, title=title, text=text, source=source, handle=handle, origin="cli")
    )
    console.print(f"Evento {ev.id} añadido a la ronda {round_no}. Continúa con: swarmlab resume {run_id}")


# --------------------------------------------------------------------------- informe, chat y entrevistas


@app.command()
def report(
    run_id: Annotated[str, typer.Argument()],
    no_llm: Annotated[bool, typer.Option("--no-llm", help="informe determinista sin llamadas")] = False,
    show: Annotated[bool, typer.Option("--show", help="mostrar el informe en la terminal")] = False,
    config: ConfigOpt = None,
) -> None:
    """(Re)genera report.md, métricas CSV/JSON y gráficas PNG de un run."""
    from swarmlab.engine import Engine
    from swarmlab.report import generate_report

    engine = Engine.open(_settings(config), run_id)
    meta = engine.store.meta_all()
    provisional = int(meta.get("current_round", 0)) < engine.cfg.rounds
    with console.status("Generando informe…"):
        path = asyncio.run(generate_report(engine, provisional=provisional, allow_llm=not no_llm))
    check = engine.store.get_meta("report", {}).get("check") or {}
    console.print(
        f"Informe: {path} · citas a posts reales: {check.get('post_citations', '?')} · "
        f"ids desconocidos: {len(check.get('unknown_posts', []))} · secciones que faltan: {check.get('missing_sections', [])}"
    )
    if show:
        console.print(Markdown(path.read_text(encoding="utf-8")))


def _llm_for_session(settings: Settings, engine_root: Path, dry: bool) -> Any:
    from swarmlab.llm import build_llm

    return build_llm(settings, engine_root, dry_run=dry)


@app.command()
def chat(
    run_id: Annotated[str, typer.Argument()],
    message: Annotated[
        list[str] | None, typer.Option("--message", "-m", help="mensaje no interactivo (repetible)")
    ] = None,
    new: Annotated[bool, typer.Option("--new", help="empezar una sesión nueva")] = False,
    config: ConfigOpt = None,
) -> None:
    """Conversa con el analista del informe. Comandos: /buscar <texto>, /agente <handle>, /post <id>, /salir."""
    from swarmlab.report import ReportChat
    from swarmlab.store import RunStore

    settings = _settings(config)
    store = RunStore.open_existing(settings.runs_dir / run_id)
    dry = bool(store.get_meta("config", {}).get("dry_run"))
    session = ReportChat(store, settings, _llm_for_session(settings, store.paths.root, dry), new=new)
    console.print(
        Panel(
            f"Chat sobre el informe de {run_id}{' (sesión reanudada)' if session.resumed else ''}. Comandos: /buscar, /agente, /post, /salir"
        )
    )
    _session_loop(session, message)


@app.command()
def interview(
    run_id: Annotated[str, typer.Argument()],
    handle: Annotated[str, typer.Argument(help="handle del agente, con o sin @")],
    question: Annotated[
        list[str] | None, typer.Option("--question", "-q", help="pregunta no interactiva (repetible)")
    ] = None,
    new: Annotated[bool, typer.Option("--new", help="empezar una entrevista nueva")] = False,
    config: ConfigOpt = None,
) -> None:
    """Entrevista en personaje a un agente, reanudando la sesión entre preguntas."""
    from swarmlab.interview import Interview
    from swarmlab.store import RunStore

    settings = _settings(config)
    store = RunStore.open_existing(settings.runs_dir / run_id)
    dry = bool(store.get_meta("config", {}).get("dry_run"))
    try:
        session = Interview(
            store, settings, _llm_for_session(settings, store.paths.root, dry), handle, new=new
        )
    except KeyError as exc:
        console.print(f"[red]{exc.args[0]}[/]")
        raise typer.Exit(2) from exc
    p = session.persona
    console.print(
        Panel(
            f"Entrevista a @{p.handle} — {p.name}, {p.occupation}\n{p.bio}{' (sesión reanudada)' if session.resumed else ''}"
        )
    )
    _session_loop(session, question)


def _session_loop(session: Any, scripted: list[str] | None) -> None:
    async def turn(text: str) -> None:
        if text.startswith("/") and hasattr(session, "command"):
            shown = session.command(text)
            if shown is not None:
                console.print(Panel(shown, title="datos (se añadirán a tu próxima pregunta)"))
                return
        with console.status("Pensando…"):
            answer = await session.ask(text)
        console.print(Markdown(answer))

    async def main() -> None:
        if scripted:
            for text in scripted:
                console.print(f"[bold cyan]> {text}[/]")
                await turn(text)
            return
        while True:
            try:
                text = (await asyncio.to_thread(Prompt.ask, "[bold cyan]tú[/]")).strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not text:
                continue
            if text in ("/salir", "/exit", "/quit"):
                break
            await turn(text)

    asyncio.run(main())


# --------------------------------------------------------------------------- list / show


@app.command()
def survey(
    run_id: Annotated[str, typer.Argument()],
    question: Annotated[str | None, typer.Option("--question", "-q", help="pregunta de la encuesta")] = None,
    regenerate_report: Annotated[
        bool, typer.Option("--report", help="regenerar el informe con la encuesta")
    ] = False,
    config: ConfigOpt = None,
) -> None:
    """Encuesta final en personaje: elección frente a su alternativa, pago, precio y compromiso."""
    from swarmlab.engine import Engine
    from swarmlab.report import generate_report
    from swarmlab.survey import CERTAINTY_MIN, pct, price_line, run_survey

    engine = Engine.open(_settings(config), run_id)
    with console.status("Encuestando a los agentes…"), engine.budget.reserve_unlocked():
        data = asyncio.run(
            run_survey(engine.llm, engine.store, engine.settings, question, cache=engine.store)
        )
    engine.store.update_meta(survey=data | {"round": engine.store.get_meta("current_round")})
    s = data["summary"]
    table = Table(title=f"Encuesta final — {data['question']}")
    cols = ("grupo", "n", "sí", "prob.", "quizá", "no", "índice", "pagarían", f"seguros≥{CERTAINTY_MIN}")
    for col in (*cols, "eligen", "pagarían ya", "firmes", "Juster"):
        table.add_column(col, justify="right")

    def with_ci(share: float | None, ci: list[float] | None) -> str:
        return pct(share) + (f" [{ci[0]:.0%}–{ci[1]:.0%}]" if ci and share is not None else "")

    rows = [("TOTAL", s["overall"])] + ([("clientes", s["customers"])] if s.get("customers") else [])
    rows += [(f"seg: {k}", v) for k, v in s["by_segment"].items()]
    for name, b in rows:
        i = b["intent"]
        table.add_row(
            name,
            str(b["n"]),
            str(i["si"]),
            str(i["probablemente"]),
            str(i["quiza"]),
            str(i["no"]),
            f"{b['adoption_index']:.2f}",
            pct(b["would_pay_share"]),
            pct(b.get("would_pay_certain_share")),
            with_ci(b.get("choice_share"), b.get("choice_ci")),
            pct(b.get("pay_now_share")),
            with_ci(b.get("firm_share"), b.get("firm_ci")),
            pct(b.get("juster_mean")),
        )
    console.print(table)
    if line := price_line(s.get("price")):
        console.print(line)
    console.print(
        "[dim]Clientes = público objetivo, lo compren o no. Firmes = eligen la propuesta, pagarían con "
        f"seguridad ≥{CERTAINTY_MIN} y pagarían ya. Entre corchetes, IC de Wilson al 95 %.[/]"
    )
    if regenerate_report:
        engine.cfg.survey = True
        provisional = int(engine.store.get_meta("current_round", 0)) < engine.cfg.rounds
        path = asyncio.run(generate_report(engine, provisional=provisional, allow_llm=True))
        console.print(f"Informe regenerado con la encuesta: {path}")


@app.command("list")
def list_cmd(config: ConfigOpt = None) -> None:
    """Lista las simulaciones guardadas en runs/."""
    from swarmlab.store import list_runs

    settings = _settings(config)
    runs = list_runs(settings.runs_dir)
    table = Table(title=f"Runs en {settings.runs_dir}")
    for col in (
        "run_id",
        "estado",
        "etapa",
        "ronda",
        "agentes",
        "postura inicial",
        "encuesta",
        "llamadas",
        "semilla",
    ):
        table.add_column(col)
    for r in runs:
        color = STATUS_COLORS.get(r["status"], "white")
        table.add_row(
            r["run_id"],
            f"[{color}]{r['status']}[/]",
            str(r["stage"]),
            f"{r['round']}/{r['rounds']}",
            str(r["agents"]),
            "medida" if r["stance_mode"] == "impresion" else "cuotas",
            "sí" if r["survey"] else "–",
            str(r["calls"]),
            f"{r['seed']}{' (dry-run)' if r['dry_run'] else ''}",
        )
    console.print(table)


@app.command()
def show(run_id: Annotated[str, typer.Argument()], config: ConfigOpt = None) -> None:
    """Estado y métricas principales de un run."""
    from swarmlab.metrics import summary_metrics
    from swarmlab.store import RunStore

    settings = _settings(config)
    store = RunStore.open_existing(settings.runs_dir / run_id)
    meta = store.meta_all()
    cfg = meta.get("config", {})
    console.print(
        Panel(
            f"[bold]{run_id}[/] · {meta.get('status')} · etapa {meta.get('stage')} · ronda {meta.get('current_round')}/{cfg.get('rounds')} · llamadas {meta.get('calls_used')}\nPregunta: {cfg.get('question')}"
        )
    )
    console.print(
        "Postura inicial: "
        + (
            "medida (primera impresión en personaje)"
            if cfg.get("stance_mode") == "impresion"
            else "fijada por cuotas (no dice nada de la idea)"
        )
        + (f" · réplica de {cfg['replica_of']}" if cfg.get("replica_of") else "")
    )
    if meta.get("diversity"):
        d = meta["diversity"]
        console.print(
            f"Diversidad: {d['score']:.2f} · similitud media de bios {d['mean_bio_similarity']:.2f} · arquetipos repetidos {len(d['duplicate_archetypes'])}"
        )
    if store.metrics():
        s = summary_metrics(store)
        table = Table(title="Postura por ronda")
        for col in ("ronda", "media", "desv", "polarización", "% a favor", "% en contra", "activos", "posts"):
            table.add_column(col, justify="right")
        for m in store.metrics():
            table.add_row(
                str(m["round"]),
                f"{m['mean']:+.2f}",
                f"{m['std']:.2f}",
                f"{m['polarization']:.2f}",
                f"{m['positive_share']:.0%}",
                f"{m['negative_share']:.0%}",
                str(m["active_agents"]),
                str(m["posts_created"]),
            )
        console.print(table)
        console.print(
            "Por segmento: "
            + "; ".join(
                f"{k}: {v['mean']:+.2f} ({v['positive_share']:.0%} a favor)"
                for k, v in s["by_segment"].items()
            )
        )
    survey_data = meta.get("survey")
    if survey_data and survey_data.get("answers"):
        from swarmlab.survey import block_line, price_line, summarize

        summ = summarize(
            survey_data["answers"],
            {p["id"]: p for p in store.load_persona_data()},
            survey_data.get("reference"),
        )
        console.print(f"Encuesta, todos ({summ['overall']['n']}): {block_line(summ['overall'])}")
        if summ.get("customers"):
            console.print(
                f"Encuesta, clientes potenciales ({summ['customers']['n']}): {block_line(summ['customers'])}"
            )
        if line := price_line(summ.get("price")):
            console.print(line)


# --------------------------------------------------------------------------- comparación A/B y PDF


@app.command()
def compare(
    run_a: Annotated[str, typer.Argument(help="lado A: un run, varios separados por comas o un patrón")],
    run_b: Annotated[str, typer.Argument(help="lado B: igual que A")],
    out: Annotated[Path | None, typer.Option(help="fichero Markdown de salida")] = None,
    config: ConfigOpt = None,
) -> None:
    """Compara dos ideas o versiones (A/B), con réplicas si las hay: métricas, p, segmentos y narrativas.

    Con ≥ 2 réplicas por lado usa una t de Welch sobre los runs; con un solo run, el ruido medido por
    `swarmlab calibrate` si existe, y si no, no da veredicto.
    """
    from swarmlab.compare import compare_runs

    settings = _settings(config)
    try:
        md, path = compare_runs(settings, run_a, run_b, out)
    except FileNotFoundError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(2) from exc
    console.print(Markdown(md))
    console.print(f"Comparación guardada en {path}")


# --------------------------------------------------------------------------- réplicas, resultados y calibración


@app.command()
def replicate(
    run_id: Annotated[str, typer.Argument(help="run de referencia")],
    n: Annotated[int, typer.Option("-n", help="réplicas nuevas que lanzar", min=1, max=20)] = 2,
    same_population: Annotated[
        bool,
        typer.Option(
            "--same-population", help="reutilizar la población del run (solo mide el ruido de la dinámica)"
        ),
    ] = False,
    stance_mode: StanceModeOpt = None,
    report_llm: ReportLLMOpt = None,
    config: ConfigOpt = None,
) -> None:
    """Repite un run con otras semillas: mismo documento, pregunta, eventos y configuración.

    Las réplicas se llaman <grupo>-r2, <grupo>-r3… y se agrupan en `scorecard`, `compare` y `calibrate`.
    Si el run reutilizaba la población de otro (--personas-from X) y X tiene réplicas, cada réplica se
    empareja con la réplica del mismo número de X.
    """
    from swarmlab.replicate import create_replica, plan_replicas

    settings = _settings(config)
    try:
        plan = plan_replicas(
            settings,
            run_id,
            n,
            same_population=same_population,
            stance_mode=stance_mode,
            report_llm=report_llm,
        )
    except FileNotFoundError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(2) from exc
    cfg = plan.cfg
    est = _estimate(
        settings, plan.seed, cfg.n_agents, cfg.rounds, cfg.agents_per_call, cfg.max_calls,
        None, cfg.survey, stance_mode=plan.stance_mode, report_llm=plan.report_llm,
    )  # fmt: skip
    console.print(
        f"{n} réplica(s) de [bold]{plan.group}[/] ({plan.stance_mode}, {cfg.n_agents} agentes × "
        f"{cfg.rounds} rondas): ≈ {est.total_calls} llamadas y {est.minutes:.0f} min cada una"
    )
    for k in plan.indexes:
        try:
            engine = create_replica(settings, plan, k)
        except (ValueError, FileExistsError, FileNotFoundError) as exc:
            console.print(f"[red]{exc}[/]")
            raise typer.Exit(2) from exc
        console.print(
            f"Réplica [bold]{engine.cfg.run_id}[/] · semilla {engine.cfg.rng_seed}"
            + (f" · población de {engine.cfg.personas_from}" if engine.cfg.personas_from else "")
        )
        _run_engine(engine, None, False, est.total_calls)


@app.command()
def scorecard(
    runs: RunsArg,
    out: Annotated[Path | None, typer.Option(help="guardar la tabla en Markdown")] = None,
    config: ConfigOpt = None,
) -> None:
    """Tabla de resultados de varios runs; agrupa las réplicas de una idea (media ± desviación)."""
    from swarmlab.scorecard import load_rows, scorecard_md
    from swarmlab.store import expand_run_ids

    settings = _settings(config)
    try:
        ids = expand_run_ids(settings.runs_dir, runs)
    except FileNotFoundError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(2) from exc
    md = scorecard_md(load_rows(settings.runs_dir, ids, with_objection=True))
    console.print(md, markup=False, highlight=False, soft_wrap=True)
    if out:
        out.write_text(md, encoding="utf-8")
        console.print(f"Guardado en {out}")


@app.command()
def calibrate(
    runs: RunsArg,
    outcomes: Annotated[
        Path | None,
        typer.Option(
            "--outcomes", exists=True, dir_okay=False, help="TOML con el desenlace real de cada caso"
        ),
    ] = None,
    out: Annotated[Path | None, typer.Option(help="guardar el resultado en Markdown")] = None,
    save: Annotated[
        bool, typer.Option("--save/--no-save", help="guardar el ruido medido para que `compare` lo use")
    ] = True,
    config: ConfigOpt = None,
) -> None:
    """¿Distinguen las métricas una idea de otra? Ruido entre réplicas (ICC) y, con --outcomes, AUC frente a la realidad."""
    from swarmlab.scorecard import (
        load_outcomes,
        load_rows,
        noise_md,
        noise_rows,
        outcome_md,
        outcome_rows,
        save_noise_reference,
        scorecard_md,
    )
    from swarmlab.store import expand_run_ids

    settings = _settings(config)
    try:
        ids = expand_run_ids(settings.runs_dir, runs)
    except FileNotFoundError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(2) from exc
    rows = load_rows(settings.runs_dir, ids)
    noise = noise_rows(rows)
    parts = ["# Calibración de SwarmLab", "", "## Resultados por idea", "", scorecard_md(rows)]
    parts += ["## Ruido entre réplicas frente a diferencias entre ideas", "", noise_md(noise)]
    if outcomes:
        parts += ["## ¿Separan las métricas los éxitos de los fracasos reales?", ""]
        parts.append(outcome_md(outcome_rows(rows, load_outcomes(outcomes))))
    md = "\n".join(parts)
    console.print(md, markup=False, highlight=False, soft_wrap=True)
    if save and noise:
        path = save_noise_reference(settings.runs_dir, noise, rows)
        console.print(f"Ruido de referencia guardado en {path} (lo usa `swarmlab compare` con runs sueltos)")
    if out:
        out.write_text(md, encoding="utf-8")
        console.print(f"Guardado en {out}")


@app.command("export-pdf")
def export_pdf(run_id: Annotated[str, typer.Argument()], config: ConfigOpt = None) -> None:
    """Exporta report.md (con gráficas) a PDF."""
    from swarmlab.pdf import report_to_pdf

    settings = _settings(config)
    path = report_to_pdf(settings.runs_dir / run_id)
    console.print(f"PDF: {path}")


@app.command()
def web(
    host: Annotated[str, typer.Option()] = "127.0.0.1",
    port: Annotated[int, typer.Option()] = 8008,
    config: ConfigOpt = None,
) -> None:
    """Arranca la interfaz web mínima (FastAPI + HTMX)."""
    import uvicorn

    from swarmlab.web.app import create_app

    uvicorn.run(create_app(_settings(config)), host=host, port=port, log_level="info")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
