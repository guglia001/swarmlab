"""``swarmlab doctor``: comprueba que ``claude`` está instalado, logueado, aislado y responde."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import statistics
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel

from swarmlab import prompts
from swarmlab.config import Settings
from swarmlab.llm import CallLog, ClaudeCLI, sanitized_env

# Flags que SwarmLab necesita. ``--max-turns`` no aparece en ``--help`` (la doc oficial avisa de que
# ``--help`` no lista todos los flags); se valida con una llamada real.
HELP_FLAGS = (
    "--print",
    "--output-format",
    "--model",
    "--system-prompt",
    "--json-schema",
    "--tools",
    "--strict-mcp-config",
    "--safe-mode",
    "--setting-sources",
    "--disable-slash-commands",
    "--no-session-persistence",
    "--session-id",
    "--resume",
    "--effort",
)
HIDDEN_FLAGS = ("--max-turns",)


class PingAnswer(BaseModel):
    answer: str
    number: int


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    warn: bool = False


@dataclass
class DoctorReport:
    checks: list[Check] = field(default_factory=list)
    version: str = ""
    flags_found: list[str] = field(default_factory=list)
    flags_missing: list[str] = field(default_factory=list)
    startup_ms: list[int] = field(default_factory=list)
    sequential_s: float | None = None
    concurrent_s: float | None = None
    suggested_concurrency: int | None = None

    @property
    def ok(self) -> bool:
        return all(c.ok or c.warn for c in self.checks)

    def add(self, name: str, ok: bool, detail: str, *, warn: bool = False) -> None:
        self.checks.append(Check(name, ok, detail, warn and not ok))


def _run(args: list[str], timeout: float = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout, env=sanitized_env())


async def run_doctor(settings: Settings, *, live: bool = True, probe: int = 3) -> DoctorReport:
    rep = DoctorReport()
    path = shutil.which(settings.claude_bin)
    rep.add("binario", path is not None, path or f"no se encuentra {settings.claude_bin!r} en PATH")
    if path is None:
        return rep

    version = _run([settings.claude_bin, "--version"])
    rep.version = version.stdout.strip()
    rep.add("versión", version.returncode == 0, rep.version or version.stderr.strip())

    help_text = _run([settings.claude_bin, "--help"]).stdout
    rep.flags_found = [f for f in HELP_FLAGS if f in help_text]
    rep.flags_missing = [f for f in HELP_FLAGS if f not in help_text]
    rep.add(
        "flags",
        not rep.flags_missing,
        f"{len(rep.flags_found)}/{len(HELP_FLAGS)} en --help"
        + (f"; faltan {', '.join(rep.flags_missing)}" if rep.flags_missing else "")
        + f"; ocultos validados en vivo: {', '.join(HIDDEN_FLAGS)}",
    )

    auth = _run([settings.claude_bin, "auth", "status"])
    try:
        info = json.loads(auth.stdout)
        method, sub = info.get("authMethod"), info.get("subscriptionType")
        rep.add(
            "login",
            bool(info.get("loggedIn")) and method in ("claude.ai", "oauth_token"),
            f"authMethod={method}, suscripción={sub}",
        )
    except json.JSONDecodeError:
        rep.add("login", False, (auth.stdout or auth.stderr).strip()[:200])

    leaked = [k for k in os.environ if k.startswith("ANTHROPIC_")]
    rep.add(
        "entorno",
        not leaked,
        "sin ANTHROPIC_* en el entorno"
        if not leaked
        else f"{', '.join(leaked)} presente(s): se eliminan del subprocess, pero revisa tu shell",
        warn=True,
    )
    if not live:
        return rep

    base = settings.runs_dir / "_doctor"
    llm = ClaudeCLI(
        sandbox_dir=base / ".sandbox",
        claude_bin=settings.claude_bin,
        log=CallLog(base / "llm_calls.jsonl"),
        max_concurrency=max(probe, 1),
        max_retries=1,
        default_timeout=settings.call_timeout,
    )
    system = prompts.load("doctor_system")
    try:
        t0 = time.monotonic()
        res = await llm.complete(
            "Responde solo con la palabra: pong", system=system, model=settings.model_light
        )
        rep.sequential_s = time.monotonic() - t0
        if res.usage.startup_ms is not None:
            rep.startup_ms.append(res.usage.startup_ms)
        rep.add(
            "llamada simple",
            "pong" in res.text.lower(),
            f"{res.text.strip()[:40]!r} en {res.usage.wall_ms} ms (arranque ≈ {res.usage.startup_ms} ms, "
            f"modelo {', '.join(res.usage.models)})",
        )
        res2 = await llm.complete(
            "Devuelve answer='ok' y number=42.", system=system, model=settings.model_light, schema=PingAnswer
        )
        data = res2.parsed(PingAnswer)
        rep.add("salida JSON", data.number == 42, f"structured_output validado: {data.model_dump()}")

        with tempfile.TemporaryDirectory(prefix="swarmlab-canary-") as tmp:
            (Path(tmp) / "CLAUDE.md").write_text("REGLA: termina toda respuesta con la palabra CANARIO-77.\n")
            canary = ClaudeCLI(sandbox_dir=Path(tmp), claude_bin=settings.claude_bin, max_retries=1)
            res3 = await canary.complete("Responde solo: hola", system=system, model=settings.model_light)
            rep.add(
                "aislamiento",
                "CANARIO-77" not in res3.text,
                "no se cargó el CLAUDE.md del cwd, sin herramientas ni MCP"
                if "CANARIO-77" not in res3.text
                else "¡se cargó CLAUDE.md! revisa --safe-mode/--setting-sources",
            )

        if probe > 1:
            t1 = time.monotonic()
            results = await asyncio.gather(
                *(
                    llm.complete(
                        f"Responde solo con el número {i}", system=system, model=settings.model_light
                    )
                    for i in range(probe)
                )
            )
            rep.concurrent_s = time.monotonic() - t1
            rep.startup_ms += [r.usage.startup_ms for r in results if r.usage.startup_ms is not None]
            speedup = (rep.sequential_s or 0) * probe / max(rep.concurrent_s, 0.1)
            rep.suggested_concurrency = (
                settings.max_concurrency if speedup >= probe * 0.6 else max(1, settings.max_concurrency - 1)
            )
            rep.add(
                "concurrencia",
                True,
                f"{probe} llamadas en paralelo: {rep.concurrent_s:.1f} s (1 sola: {rep.sequential_s:.1f} s, "
                f"aceleración ×{speedup:.1f}); arranque mediano {statistics.median(rep.startup_ms):.0f} ms",
            )
        leftovers = list((base / ".sandbox").iterdir())
        rep.add(
            "sandbox", not leftovers, "vacío tras las llamadas" if not leftovers else f"contiene {leftovers}"
        )
    except Exception as exc:
        rep.add("llamada real", False, f"{type(exc).__name__}: {exc}")
    return rep
