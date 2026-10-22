"""Backend LLM de SwarmLab: Claude Code headless (``claude -p``) usado como *completion* pura.

Pieza central del proyecto. ``ClaudeCLI.complete`` lanza un subprocess por llamada con:

- el prompt por **stdin** (nunca como argumento), sin ``shell=True``;
- ``--output-format json`` y, si se pide un modelo pydantic, ``--json-schema``;
- todas las herramientas desactivadas, ``--max-turns 1``, sin MCP, sin CLAUDE.md, sin settings
  de usuario/proyecto ni hooks (``--safe-mode --setting-sources ""``);
- ``cwd`` en un directorio vacío y aislado (``runs/<run_id>/.sandbox``);
- un ``env`` sin ``ANTHROPIC_API_KEY`` ni similares: siempre factura la suscripción.

Robustez: timeout por llamada, reintentos con backoff exponencial + jitter, validación pydantic
con hasta ``schema_retries`` reintentos que devuelven el error al modelo, detección de límite de
uso (``UsageLimitError`` → el motor pausa y guarda checkpoint), semáforo de concurrencia, tope de
llamadas (``CallBudget``) y log JSONL por intento.

``FakeClaudeCLI`` implementa la misma interfaz con respuestas deterministas (``--dry-run`` y
tests). Cada módulo de dominio registra su generador con ``@fake_responder("<propósito>")``.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import random
import re
import signal
import time
import uuid
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import cache
from pathlib import Path
from typing import Any, Literal, Protocol, TypeVar, get_origin

from pydantic import BaseModel, Field, ValidationError, create_model

from swarmlab import prompts
from swarmlab.config import Settings

T = TypeVar("T", bound=BaseModel)
B = TypeVar("B", bound=BaseModel)

# --------------------------------------------------------------------------- entorno

_ENV_STRIP_PREFIXES = ("ANTHROPIC_",)
_ENV_STRIP_EXACT = frozenset(
    {
        "CLAUDECODE",
        "CLAUDE_CODE_ENTRYPOINT",
        "CLAUDE_CODE_SIMPLE",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
        "CLAUDE_CODE_USE_FOUNDRY",
        "AWS_BEARER_TOKEN_BEDROCK",
        "OPENAI_API_KEY",
    }
)


def sanitized_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """Copia del entorno sin credenciales de API ni marcadores de sesión padre."""
    src = os.environ if base is None else base
    return {
        k: v for k, v in src.items() if not k.startswith(_ENV_STRIP_PREFIXES) and k not in _ENV_STRIP_EXACT
    }


# --------------------------------------------------------------------------- errores


class LLMError(RuntimeError):
    """Error base del backend LLM."""


class UsageLimitError(LLMError):
    """Límite de uso de la suscripción o rate limit persistente: hay que pausar, no fallar."""

    def __init__(self, message: str, reset_hint: str | None = None) -> None:
        super().__init__(message)
        self.reset_hint = reset_hint


class AuthError(LLMError):
    """El CLI no está logueado o la sesión expiró."""


class CallBudgetExceeded(LLMError):
    """Se alcanzó ``MAX_CALLS_PER_RUN``."""


class LLMCallFailed(LLMError):
    """Fallo no recuperable o reintentos agotados."""


class StructuredOutputError(LLMError):
    """La salida no validó contra el schema tras los reintentos permitidos."""


class JSONExtractionError(ValueError):
    """No se encontró JSON parseable en el texto."""


# --------------------------------------------------------------------------- JSON tolerante

_FENCE_RE = re.compile(r"```[a-zA-Z0-9_-]*[ \t]*\n?(.*?)```", re.S)
_TRAILING_COMMA_RE = re.compile(r",(\s*[}\]])")


def _balanced_spans(text: str) -> list[str]:
    """Fragmentos ``{...}``/``[...]`` de primer nivel, respetando cadenas y escapes."""
    spans: list[str] = []
    depth = 0
    start = -1
    in_str = False
    escape = False
    for i, ch in enumerate(text):
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"' and depth > 0:
            in_str = True
        elif ch in "{[":
            if depth == 0:
                start = i
            depth += 1
        elif ch in "}]" and depth > 0:
            depth -= 1
            if depth == 0 and start >= 0:
                spans.append(text[start : i + 1])
                start = -1
    return sorted(spans, key=len, reverse=True)


def extract_json(text: str) -> Any:
    """Extrae el primer documento JSON útil de una respuesta con ruido alrededor.

    Prueba, en orden: el texto completo, los bloques de código, y los fragmentos balanceados
    (de mayor a menor). Cada candidato se intenta tal cual y con una reparación ligera (comas
    finales). Acepta saltos de línea crudos dentro de cadenas (``strict=False``).
    """
    s = (text or "").strip().lstrip("\ufeff")
    if not s:
        raise JSONExtractionError("respuesta vacía")
    candidates: list[str] = [s]
    candidates += [m.group(1).strip() for m in _FENCE_RE.finditer(s)]
    candidates += _balanced_spans(s)
    seen: set[str] = set()
    for cand in candidates:
        if not cand or cand in seen:
            continue
        seen.add(cand)
        for variant in (cand, _TRAILING_COMMA_RE.sub(r"\1", cand)):
            try:
                return json.loads(variant, strict=False)
            except json.JSONDecodeError:
                continue
    raise JSONExtractionError(f"no hay JSON válido en la respuesta (empieza por: {s[:120]!r})")


def _clean_schema(node: Any) -> Any:
    """Quita ``title``/``default`` y marca TODAS las propiedades como obligatorias.

    Con salida estructurada, los modelos tienden a omitir los campos opcionales (p. ej. dejaban
    fuera ``actions`` y razonaban acciones que nunca se aplicaban). Obligar a emitir cada clave
    (``null`` si es anulable) evita esas pérdidas silenciosas; los valores por defecto de pydantic
    siguen sirviendo para construir modelos en Python.
    """
    if isinstance(node, dict):
        out: dict[str, Any] = {}
        for key, value in node.items():
            if key in ("title", "default"):
                continue
            if key in ("properties", "$defs", "definitions", "patternProperties") and isinstance(value, dict):
                out[key] = {pk: _clean_schema(pv) for pk, pv in value.items()}
            else:
                out[key] = _clean_schema(value)
        if isinstance(out.get("properties"), dict):
            out["required"] = list(out["properties"])
        return out
    if isinstance(node, list):
        return [_clean_schema(x) for x in node]
    return node


@cache
def schema_for_cli(schema: type[BaseModel]) -> dict[str, Any]:
    """JSON Schema compacto (sin ``title``/``default``, todo obligatorio) apto para ``--json-schema``."""
    return _clean_schema(schema.model_json_schema())


def per_agent_schema(batch: type[B], field: str, item: type[BaseModel], agent_ids: list[str]) -> type[B]:
    """Schema de un lote con exactamente un elemento por agente y ``agent_id`` limitado a sus ids.

    El CLI rechaza ids inventados o agentes olvidados sin gastar llamadas extra. Los nombres
    (``<Lote>_<hash>``) son estables porque forman parte de la clave de caché de cada llamada.
    """
    key = hashlib.sha1(",".join(agent_ids).encode()).hexdigest()[:10]
    ids_literal: Any = Literal[tuple(agent_ids)]  # type: ignore[valid-type]
    sub = create_model(f"{item.__name__}_{key}", __base__=item, agent_id=(ids_literal, ...))
    bounded = Field(min_length=len(agent_ids), max_length=len(agent_ids))
    return create_model(f"{batch.__name__}_{key}", __base__=batch, **{field: (list[sub], bounded)})  # type: ignore[call-overload,no-any-return,valid-type]


def schema_instructions(schema: type[BaseModel]) -> str:
    return prompts.render(
        "json_fallback", schema=json.dumps(schema_for_cli(schema), ensure_ascii=False, indent=1)
    )


def _coercions(data: Any, schema: type[BaseModel]) -> Iterator[Any]:
    fields = schema.model_fields
    list_fields = [n for n, f in fields.items() if get_origin(f.annotation) is list]
    if isinstance(data, list) and len(list_fields) == 1:
        yield {list_fields[0]: data}
    if isinstance(data, dict):
        if len(data) == 1:
            (only,) = data.values()
            if isinstance(only, dict):
                yield only
            if isinstance(only, list) and len(list_fields) == 1:
                yield {list_fields[0]: only}
        if len(list_fields) == 1 and list_fields[0] not in data:
            yield {list_fields[0]: [data]}


def validate_with_coercion(data: Any, schema: type[T]) -> T:
    """Valida con pydantic; si falla, prueba envolturas típicas (lista desnuda, clave extra)."""
    try:
        return schema.model_validate(data)
    except ValidationError as first:
        for alt in _coercions(data, schema):
            try:
                return schema.model_validate(alt)
            except ValidationError:
                continue
        raise first


def parse_structured(payload: dict[str, Any], schema: type[T]) -> T:
    """Obtiene el modelo validado del JSON del CLI (``structured_output`` o texto de ``result``)."""
    structured = payload.get("structured_output")
    if structured is not None:
        return validate_with_coercion(structured, schema)
    return validate_with_coercion(extract_json(str(payload.get("result") or "")), schema)


def describe_error(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        parts = [f"{'.'.join(str(p) for p in e['loc']) or '<raíz>'}: {e['msg']}" for e in exc.errors()[:12]]
        more = len(exc.errors()) - 12
        return "; ".join(parts) + (f" (+{more} errores más)" if more > 0 else "")
    return str(exc)


# --------------------------------------------------------------------------- clasificación de fallos

FailureKind = Literal["usage_limit", "rate_limit", "auth", "transient", "fatal"]

_TRANSIENT_FIRST = re.compile(
    r"temporarily limiting requests|not your usage limit|overloaded|\b529\b|high load", re.I
)
_USAGE_LIMIT = re.compile(
    r"hit your [\w' ]{0,30}?limit|usage limit|spend limit|weekly limit|session limit|"
    r"out of (?:extra )?usage|usage credits",
    re.I,
)
_AUTH = re.compile(
    r"not logged in|please run /login|invalid api key|oauth token (?:revoked|has expired)|login expired|"
    r"authentication[_ ]failed|could not resolve authentication|account is on hold|"
    r"authentication required|invalid authentication credentials|does not have access to claude",
    re.I,
)
_FATAL = re.compile(
    r"unknown option|not a valid json schema|prompt is too long|input is too long|unrecognized_model|"
    r"not a recognized model|model \S{0,80} not found|usage policy|input must be provided|"
    r"no conversation found|error: option",
    re.I,
)
_RATE_LIMIT = re.compile(r"\b429\b|rate[ _-]?limit|too many requests", re.I)
_RESET_RE = re.compile(r"resets?\s+(?:at\s+)?([^\n·|]{1,40})", re.I)
_LEGACY_RESET_RE = re.compile(r"usage limit reached\|(\d{9,})", re.I)


def classify_failure(text: str) -> FailureKind:
    """Clasifica un fallo del CLI por su mensaje (resultado + stderr)."""
    if _TRANSIENT_FIRST.search(text):
        return "transient"
    if _USAGE_LIMIT.search(text):
        return "usage_limit"
    if _AUTH.search(text):
        return "auth"
    if _FATAL.search(text):
        return "fatal"
    if _RATE_LIMIT.search(text):
        return "rate_limit"
    return "transient"


def reset_hint(text: str) -> str | None:
    legacy = _LEGACY_RESET_RE.search(text)
    if legacy:
        return datetime.fromtimestamp(int(legacy.group(1))).strftime("%Y-%m-%d %H:%M")
    m = _RESET_RE.search(text)
    return m.group(1).strip(" .") if m else None


# --------------------------------------------------------------------------- resultados y contabilidad


@dataclass
class LLMUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    cost_usd: float = 0.0  # coste nocional estimado por el CLI (con suscripción no se factura)
    duration_ms: int = 0  # duración interna reportada por el CLI
    duration_api_ms: int = 0
    wall_ms: int = 0  # reloj real medido por nosotros (incluye el arranque del proceso)
    num_turns: int = 0
    models: list[str] = field(default_factory=list)

    @property
    def total_input_tokens(self) -> int:
        return self.input_tokens + self.cache_creation_input_tokens + self.cache_read_input_tokens

    @property
    def startup_ms(self) -> int | None:
        return self.wall_ms - self.duration_ms if self.duration_ms else None

    @classmethod
    def from_payload(cls, payload: dict[str, Any], wall_ms: int) -> LLMUsage:
        u = payload.get("usage") or {}
        return cls(
            input_tokens=int(u.get("input_tokens") or 0),
            output_tokens=int(u.get("output_tokens") or 0),
            cache_creation_input_tokens=int(u.get("cache_creation_input_tokens") or 0),
            cache_read_input_tokens=int(u.get("cache_read_input_tokens") or 0),
            cost_usd=float(payload.get("total_cost_usd") or 0.0),
            duration_ms=int(payload.get("duration_ms") or 0),
            duration_api_ms=int(payload.get("duration_api_ms") or 0),
            wall_ms=wall_ms,
            num_turns=int(payload.get("num_turns") or 0),
            models=sorted((payload.get("modelUsage") or {}).keys()),
        )


@dataclass
class LLMResult:
    text: str
    data: Any
    session_id: str | None
    model: str
    usage: LLMUsage
    attempts: int
    call_id: str

    def parsed(self, schema: type[T]) -> T:
        if not isinstance(self.data, schema):
            raise TypeError(f"se esperaba {schema.__name__}, hay {type(self.data).__name__}")
        return self.data


@dataclass
class LLMStats:
    calls: int = 0  # subprocesses lanzados (cada uno consume cuota)
    ok: int = 0
    failures: int = 0
    retries: int = 0
    invalid_outputs: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    wall_ms: int = 0
    startup_ms: list[int] = field(default_factory=list)
    by_purpose: dict[str, int] = field(default_factory=dict)

    def record_ok(self, purpose: str, usage: LLMUsage) -> None:
        self.ok += 1
        self.input_tokens += usage.total_input_tokens
        self.output_tokens += usage.output_tokens
        self.cost_usd += usage.cost_usd
        self.wall_ms += usage.wall_ms
        if usage.startup_ms is not None:
            self.startup_ms.append(usage.startup_ms)
        self.by_purpose[purpose] = self.by_purpose.get(purpose, 0) + 1


class CallBudget:
    """Tope duro de llamadas por simulación, con una reserva para el informe final."""

    def __init__(self, limit: int | None = None, *, reserve: int = 0, used: int = 0) -> None:
        self.limit = limit
        self.reserve = reserve
        self.used = used
        self._reserve_unlocked = False

    @property
    def cap(self) -> int | None:
        if self.limit is None:
            return None
        return self.limit if self._reserve_unlocked else max(0, self.limit - self.reserve)

    @property
    def remaining(self) -> int | None:
        cap = self.cap
        return None if cap is None else max(0, cap - self.used)

    def take(self) -> None:
        cap = self.cap
        if cap is not None and self.used >= cap:
            raise CallBudgetExceeded(
                f"tope de llamadas alcanzado ({self.used}/{self.limit}, reserva {self.reserve})"
            )
        self.used += 1

    @contextlib.contextmanager
    def reserve_unlocked(self) -> Iterator[None]:
        previous = self._reserve_unlocked
        self._reserve_unlocked = True
        try:
            yield
        finally:
            self._reserve_unlocked = previous


class CallLog:
    """Log JSONL por intento. Sin contenido completo salvo ``debug=True``."""

    def __init__(self, path: Path | None, *, debug: bool = False) -> None:
        self.path = path
        self.debug = debug
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, entry: dict[str, Any]) -> None:
        if self.path is None:
            return
        entry = {"ts": datetime.now(UTC).isoformat(timespec="milliseconds"), **entry}
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")


def count_logged_calls(path: Path) -> int:
    """Subprocesses ya lanzados según el log (fuente de verdad del presupuesto al reanudar)."""
    if not path.is_file():
        return 0
    n = 0
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            with contextlib.suppress(json.JSONDecodeError):
                if json.loads(line).get("kind", "attempt") == "attempt":
                    n += 1
    return n


def _scalar_meta(meta: dict[str, Any] | None) -> dict[str, Any]:
    """Metadatos pequeños para el log; las claves ``_privadas`` (datos para el fake) no se registran."""
    if not meta:
        return {}
    return {
        k: v
        for k, v in meta.items()
        if not k.startswith("_")
        and k != "kind"
        and isinstance(v, str | int | float | bool)
        and not (isinstance(v, str) and len(v) > 120)
    }


def _clip(text: str, n: int) -> str:
    text = text or ""
    return text if len(text) <= n else text[:n] + "…"


# --------------------------------------------------------------------------- interfaz común


class LLM(Protocol):
    stats: LLMStats
    budget: CallBudget

    async def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        model: str = "haiku",
        schema: type[BaseModel] | None = None,
        session_id: str | None = None,
        resume: bool = False,
        timeout: float | None = None,
        purpose: str = "call",
        meta: dict[str, Any] | None = None,
        effort: str | None = None,
    ) -> LLMResult: ...


# --------------------------------------------------------------------------- subprocess


@dataclass
class ProcessOutput:
    returncode: int
    stdout: str
    stderr: str
    wall_ms: int


Runner = Callable[[list[str], str, Path, dict[str, str], float], Awaitable[ProcessOutput]]


async def _terminate(proc: asyncio.subprocess.Process) -> None:
    """Mata el grupo de procesos de ``claude`` (SIGTERM y, si no basta, SIGKILL)."""
    for sig, grace in ((signal.SIGTERM, 3.0), (signal.SIGKILL, 2.0)):
        if proc.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, sig)
        try:
            await asyncio.wait_for(proc.wait(), grace)
            return
        except (TimeoutError, asyncio.CancelledError):
            continue


async def run_subprocess(
    args: list[str], stdin_text: str, cwd: Path, env: dict[str, str], timeout: float
) -> ProcessOutput:
    """Ejecuta ``args`` sin shell, con el prompt por stdin y timeout duro."""
    t0 = time.monotonic()
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(cwd),
        env=env,
        start_new_session=True,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(stdin_text.encode("utf-8")), timeout)
    except BaseException:
        await _terminate(proc)
        raise
    return ProcessOutput(
        returncode=proc.returncode if proc.returncode is not None else -1,
        stdout=out.decode("utf-8", "replace"),
        stderr=err.decode("utf-8", "replace"),
        wall_ms=int((time.monotonic() - t0) * 1000),
    )


def parse_cli_stdout(stdout: str) -> dict[str, Any] | None:
    """El objeto ``result`` de ``--output-format json`` (tolera líneas extra alrededor)."""
    s = (stdout or "").strip()
    if not s:
        return None
    with contextlib.suppress(json.JSONDecodeError):
        obj = json.loads(s)
        if isinstance(obj, dict):
            return obj
        if isinstance(obj, list):
            for item in reversed(obj):
                if isinstance(item, dict) and item.get("type") == "result":
                    return item
    for line in reversed(s.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        with contextlib.suppress(json.JSONDecodeError):
            obj = json.loads(line)
            if isinstance(obj, dict) and ("result" in obj or obj.get("type") == "result"):
                return obj
    return None


def _failure_text(po: ProcessOutput, payload: dict[str, Any] | None) -> str:
    parts: list[str] = [f"rc={po.returncode}"]
    if payload:
        for key in ("subtype", "api_error_status", "result", "errors", "terminal_reason"):
            if payload.get(key):
                parts.append(f"{key}={payload[key]}")
    elif po.stdout.strip():
        parts.append(f"stdout={po.stdout.strip()[-600:]}")
    if po.stderr.strip():
        parts.append(f"stderr={po.stderr.strip()[-800:]}")
    return " | ".join(parts)


_MAX_INLINE_ARG = 100_000


class ClaudeCLI:
    """Cliente de ``claude -p`` en modo "LLM puro". Ver docstring del módulo."""

    def __init__(
        self,
        *,
        sandbox_dir: Path,
        claude_bin: str = "claude",
        log: CallLog | None = None,
        max_concurrency: int = 3,
        budget: CallBudget | None = None,
        max_retries: int = 3,
        schema_retries: int = 2,
        native_schema: bool = True,
        default_timeout: float = 180,
        max_turns: int = 1,
        schema_max_turns: int = 3,
        runner: Runner | None = None,
        env: dict[str, str] | None = None,
        backoff_base: float = 2.0,
        backoff_cap: float = 60.0,
        rate_limit_backoff: float = 20.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.claude_bin = claude_bin
        self.sandbox_dir = sandbox_dir
        self.sandbox_dir.mkdir(parents=True, exist_ok=True)
        self.log = log or CallLog(None)
        self.budget = budget or CallBudget(None)
        self.max_retries = max_retries
        self.schema_retries = schema_retries
        self.native_schema = native_schema
        self.default_timeout = default_timeout
        self.max_turns = max_turns
        # Con --json-schema el CLI entrega la salida con una herramienta interna; si no valida, necesita
        # otro turno para que el modelo la corrija dentro del MISMO proceso. No hay más herramientas,
        # así que esos turnos extra solo pueden ser autocorrecciones del JSON (ver DECISIONS.md).
        self.schema_max_turns = schema_max_turns
        self.stats = LLMStats()
        self._runner: Runner = runner or run_subprocess
        self._env = sanitized_env(env)
        self._sem = asyncio.Semaphore(max_concurrency)
        self._backoff_base = backoff_base
        self._backoff_cap = backoff_cap
        self._rate_limit_backoff = rate_limit_backoff
        self._sleep = sleep

    # -- argumentos -------------------------------------------------------------------------------

    def _system_file(self, system: str) -> Path:
        folder = self.sandbox_dir.parent / ".sysprompts"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{hashlib.sha1(system.encode()).hexdigest()[:16]}.md"
        if not path.exists():
            path.write_text(system, encoding="utf-8")
        return path

    def build_args(
        self,
        *,
        model: str,
        system: str | None,
        json_schema: dict[str, Any] | None,
        session_id: str | None,
        resume: bool,
        effort: str | None,
    ) -> list[str]:
        args = [
            self.claude_bin,
            "-p",
            "--output-format",
            "json",
            "--model",
            model,
            "--tools",
            "",
            "--max-turns",
            str(self.schema_max_turns if json_schema is not None else self.max_turns),
            "--strict-mcp-config",
            "--safe-mode",
            "--setting-sources",
            "",
            "--disable-slash-commands",
        ]
        if system:
            if len(system.encode("utf-8")) > _MAX_INLINE_ARG:
                args += ["--system-prompt-file", str(self._system_file(system))]
            else:
                args += ["--system-prompt", system]
        if json_schema is not None:
            args += ["--json-schema", json.dumps(json_schema, ensure_ascii=False, separators=(",", ":"))]
        if session_id:
            args += ["--resume", session_id] if resume else ["--session-id", session_id]
        else:
            args.append("--no-session-persistence")
        if effort:
            args += ["--effort", effort]
        return args

    def _backoff(self, attempt: int, kind: FailureKind) -> float:
        base = self._rate_limit_backoff if kind == "rate_limit" else self._backoff_base
        return min(self._backoff_cap, base * 2 ** (attempt - 1)) * random.uniform(0.5, 1.5)

    # -- llamada ----------------------------------------------------------------------------------

    async def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        model: str = "haiku",
        schema: type[BaseModel] | None = None,
        session_id: str | None = None,
        resume: bool = False,
        timeout: float | None = None,
        purpose: str = "call",
        meta: dict[str, Any] | None = None,
        effort: str | None = None,
    ) -> LLMResult:
        if not prompt.strip():
            raise ValueError("prompt vacío")
        call_id = uuid.uuid4().hex[:12]
        native = schema is not None and self.native_schema
        json_schema = schema_for_cli(schema) if (schema is not None and native) else None
        base_prompt = (
            prompt if schema is None or native else prompt.rstrip() + "\n\n" + schema_instructions(schema)
        )
        current = base_prompt
        sid, is_resume = session_id, resume
        attempts_total = 0
        for schema_try in range(self.schema_retries + 1):
            payload, po, attempts, used_sid = await self._invoke(
                current,
                system=system,
                model=model,
                json_schema=json_schema,
                session_id=sid,
                resume=is_resume,
                timeout=timeout or self.default_timeout,
                purpose=purpose,
                call_id=call_id,
                effort=effort,
                schema_name=schema.__name__ if schema else None,
                meta=meta,
            )
            attempts_total += attempts
            text = str(payload.get("result") or "")
            if used_sid:
                sid, is_resume = str(payload.get("session_id") or used_sid), True
            usage = LLMUsage.from_payload(payload, po.wall_ms)
            if schema is None:
                return LLMResult(text, None, sid, model, usage, attempts_total, call_id)
            try:
                data = parse_structured(payload, schema)
            except (ValidationError, JSONExtractionError) as exc:
                errors = describe_error(exc)
                self.stats.invalid_outputs += 1
                self.log.write(
                    {
                        "kind": "validation",
                        "call_id": call_id,
                        "purpose": purpose,
                        "schema": schema.__name__,
                        "try": schema_try + 1,
                        "error": _clip(errors, 600),
                    }
                )
                if schema_try >= self.schema_retries:
                    raise StructuredOutputError(
                        f"{purpose}: salida inválida tras {schema_try + 1} intentos: {_clip(errors, 300)}"
                    ) from exc
                previous = text or json.dumps(payload.get("structured_output"), ensure_ascii=False)
                repair = prompts.render("json_repair", errors=errors, previous=_clip(previous, 1500))
                current = repair if sid else base_prompt.rstrip() + "\n\n" + repair
                continue
            if not text:
                text = json.dumps(payload.get("structured_output"), ensure_ascii=False)
            return LLMResult(text, data, sid, model, usage, attempts_total, call_id)
        raise AssertionError("inalcanzable")

    async def _invoke(
        self,
        prompt: str,
        *,
        system: str | None,
        model: str,
        json_schema: dict[str, Any] | None,
        session_id: str | None,
        resume: bool,
        timeout: float,
        purpose: str,
        call_id: str,
        effort: str | None,
        schema_name: str | None,
        meta: dict[str, Any] | None,
    ) -> tuple[dict[str, Any], ProcessOutput, int, str | None]:
        last = ""
        for attempt in range(1, self.max_retries + 2):
            self.budget.take()
            sid = session_id
            if session_id and not resume and attempt > 1:
                sid = str(uuid.uuid4())  # la sesión del intento fallido pudo quedar a medias
            args = self.build_args(
                model=model,
                system=system,
                json_schema=json_schema,
                session_id=sid,
                resume=resume,
                effort=effort,
            )
            self.stats.calls += 1
            entry: dict[str, Any] = {
                "kind": "attempt",
                "call_id": call_id,
                "attempt": attempt,
                "purpose": purpose,
                "model": model,
                "schema": schema_name,
                "session": ("resume" if resume else "new") if sid else "none",
                "prompt_chars": len(prompt),
                "system_chars": len(system or ""),
                **_scalar_meta(meta),
            }
            if self.log.debug:
                entry["prompt"] = prompt
                entry["system"] = system
            t0 = time.monotonic()
            kind: FailureKind
            try:
                async with self._sem:
                    po = await self._runner(args, prompt, self.sandbox_dir, self._env, timeout)
            except TimeoutError:
                last = f"timeout tras {timeout:.0f}s"
                kind = "transient"
                entry.update(outcome="timeout", wall_ms=int((time.monotonic() - t0) * 1000), error=last)
            else:
                payload = parse_cli_stdout(po.stdout)
                ok = po.returncode == 0 and payload is not None and not payload.get("is_error")
                if (
                    payload is not None
                    and payload.get("subtype") == "error_max_turns"
                    and (payload.get("result") or payload.get("structured_output") is not None)
                ):
                    ok = True  # hay contenido utilizable aunque el CLI marque el tope de turnos
                usage = LLMUsage.from_payload(payload or {}, po.wall_ms)
                entry.update(
                    exit_code=po.returncode,
                    wall_ms=po.wall_ms,
                    duration_ms=usage.duration_ms,
                    duration_api_ms=usage.duration_api_ms,
                    startup_ms=usage.startup_ms,
                    input_tokens=usage.input_tokens,
                    cache_creation_tokens=usage.cache_creation_input_tokens,
                    cache_read_tokens=usage.cache_read_input_tokens,
                    output_tokens=usage.output_tokens,
                    cost_usd=usage.cost_usd,
                    models=usage.models,
                    num_turns=usage.num_turns,
                    session_id=(payload or {}).get("session_id"),
                )
                if self.log.debug:
                    entry["response"] = po.stdout[:200_000]
                    entry["stderr"] = po.stderr[-4000:]
                if ok and payload is not None:
                    entry["outcome"] = "ok"
                    self.log.write(entry)
                    self.stats.record_ok(purpose, usage)
                    return payload, po, attempt, sid
                last = _failure_text(po, payload)
                kind = classify_failure(last)
                entry.update(outcome=kind, error=_clip(last, 600))
            self.log.write(entry)
            self.stats.failures += 1
            if kind == "usage_limit":
                raise UsageLimitError(_clip(last, 400), reset_hint(last))
            if kind == "auth":
                raise AuthError(
                    f"claude no está autenticado: {_clip(last, 300)}. Ejecuta `claude auth login`."
                )
            if kind == "fatal":
                raise LLMCallFailed(f"{purpose}: error no recuperable: {_clip(last, 400)}")
            if attempt > self.max_retries:
                if kind == "rate_limit":
                    raise UsageLimitError(f"rate limit persistente: {_clip(last, 300)}", reset_hint(last))
                break
            self.stats.retries += 1
            await self._sleep(self._backoff(attempt, kind))
        raise LLMCallFailed(f"{purpose}: falló tras {self.max_retries + 1} intentos: {_clip(last, 400)}")


# --------------------------------------------------------------------------- sesiones


class Conversation:
    """Sesión reanudable: la 1.ª llamada crea la sesión (``--session-id``), las siguientes ``--resume``.

    ``preamble`` se antepone al primer mensaje (p. ej. el dossier del informe). Si el CLI ya no
    encuentra la sesión, se recrea automáticamente volviendo a enviar el preámbulo.
    """

    def __init__(
        self,
        llm: LLM,
        *,
        system: str | None,
        model: str,
        purpose: str,
        preamble: str | None = None,
        session_id: str | None = None,
        started: bool = False,
        timeout: float | None = None,
        effort: str | None = None,
    ) -> None:
        self.llm = llm
        self.system = system
        self.model = model
        self.purpose = purpose
        self.preamble = preamble
        self.session_id = session_id
        self.started = started and session_id is not None
        self.timeout = timeout
        self.effort = effort
        self.turns = 0

    async def send(self, message: str, schema: type[BaseModel] | None = None) -> LLMResult:
        try:
            return await self._send(message, schema)
        except LLMCallFailed as exc:
            if not self.started or "no conversation found" not in str(exc).lower():
                raise
            self.session_id, self.started = None, False
            return await self._send(message, schema)

    async def _send(self, message: str, schema: type[BaseModel] | None) -> LLMResult:
        sid = self.session_id or str(uuid.uuid4())
        prompt = message
        if not self.started and self.preamble:
            prompt = f"{self.preamble.rstrip()}\n\n{message}"
        res = await self.llm.complete(
            prompt,
            system=self.system,
            model=self.model,
            schema=schema,
            session_id=sid,
            resume=self.started,
            timeout=self.timeout,
            purpose=self.purpose,
            effort=self.effort,
        )
        self.session_id = res.session_id or sid
        self.started = True
        self.turns += 1
        return res


# --------------------------------------------------------------------------- caché por etapa


class CallCache(Protocol):
    def cache_get(self, key: str) -> Any: ...

    def cache_put(self, key: str, data: Any) -> None: ...


def cache_key(stage: str, *parts: str) -> str:
    digest = hashlib.sha1("\x1f".join(parts).encode("utf-8")).hexdigest()[:20]
    return f"{stage}:{digest}"


async def cached_complete(
    cache: CallCache | None,
    stage: str,
    llm: LLM,
    prompt: str,
    *,
    schema: type[T],
    system: str | None,
    model: str,
    purpose: str,
    meta: dict[str, Any] | None = None,
    effort: str | None = None,
    timeout: float | None = None,
) -> T:
    """``complete`` con memoización persistente: misma etapa + mismo prompt = misma respuesta, gratis.

    Permite reanudar una etapa interrumpida sin volver a pagar las llamadas que ya terminaron.
    """
    key = cache_key(stage, model, system or "", schema.__name__, prompt)
    if cache is not None:
        hit = cache.cache_get(key)
        if hit is not None:
            return schema.model_validate(hit)
    res = await llm.complete(
        prompt,
        system=system,
        model=model,
        schema=schema,
        purpose=purpose,
        meta=meta,
        effort=effort,
        timeout=timeout,
    )
    data = res.parsed(schema)
    if cache is not None:
        cache.cache_put(key, data.model_dump(mode="json"))
    return data


async def cached_text(
    cache: CallCache | None,
    stage: str,
    llm: LLM,
    prompt: str,
    *,
    system: str | None,
    model: str,
    purpose: str,
    meta: dict[str, Any] | None = None,
    effort: str | None = None,
    timeout: float | None = None,
) -> str:
    """Como ``cached_complete`` pero para respuestas de texto libre."""
    key = cache_key(stage, model, system or "", "text", prompt)
    if cache is not None:
        hit = cache.cache_get(key)
        if isinstance(hit, str):
            return hit
    res = await llm.complete(
        prompt, system=system, model=model, purpose=purpose, meta=meta, effort=effort, timeout=timeout
    )
    if cache is not None:
        cache.cache_put(key, res.text)
    return res.text


# --------------------------------------------------------------------------- fake determinista


@dataclass
class FakeRequest:
    prompt: str
    system: str | None
    model: str
    schema: type[BaseModel] | None
    purpose: str
    meta: dict[str, Any]
    rng: random.Random
    call_index: int
    session_id: str | None
    history: list[str]


FakeResponder = Callable[[FakeRequest], Any]
_FAKE_RESPONDERS: dict[str, FakeResponder] = {}


def fake_responder(purpose: str) -> Callable[[FakeResponder], FakeResponder]:
    """Registra el generador determinista de respuestas de un propósito (``"ontology.chunk"``…)."""

    def deco(fn: FakeResponder) -> FakeResponder:
        _FAKE_RESPONDERS[purpose] = fn
        return fn

    return deco


class FakeClaudeCLI:
    """Sustituto sin red de ``ClaudeCLI`` para ``--dry-run`` y tests.

    Respuestas deterministas (semilla + propósito + hash del prompt). Simula sesiones, consume
    presupuesto y escribe el mismo log JSONL. ``usage_limit_at=N`` lanza ``UsageLimitError`` a
    partir de la llamada N (para probar pausa y reanudación).
    """

    def __init__(
        self,
        *,
        log: CallLog | None = None,
        budget: CallBudget | None = None,
        responders: dict[str, FakeResponder] | None = None,
        usage_limit_at: int | None = None,
        seed: int | str = 0,
        latency: float = 0.0,
    ) -> None:
        self.log = log or CallLog(None)
        self.budget = budget or CallBudget(None)
        self.responders = dict(responders or {})
        self.usage_limit_at = usage_limit_at
        self.seed = seed
        self.latency = latency
        self.stats = LLMStats()
        self.sessions: dict[str, list[str]] = {}
        self.requests: list[FakeRequest] = []

    def _responder(self, purpose: str) -> FakeResponder:
        for table in (self.responders, _FAKE_RESPONDERS):
            if purpose in table:
                return table[purpose]
        raise LLMCallFailed(f"FakeClaudeCLI: no hay respuesta simulada para el propósito {purpose!r}")

    async def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        model: str = "haiku",
        schema: type[BaseModel] | None = None,
        session_id: str | None = None,
        resume: bool = False,
        timeout: float | None = None,
        purpose: str = "call",
        meta: dict[str, Any] | None = None,
        effort: str | None = None,
    ) -> LLMResult:
        if not prompt.strip():
            raise ValueError("prompt vacío")
        self.budget.take()
        self.stats.calls += 1
        idx = self.stats.calls
        call_id = f"fake{idx:05d}"
        entry: dict[str, Any] = {
            "kind": "attempt",
            "call_id": call_id,
            "attempt": 1,
            "purpose": purpose,
            "model": model,
            "schema": schema.__name__ if schema else None,
            "session": ("resume" if resume else "new") if session_id else "none",
            "prompt_chars": len(prompt),
            "system_chars": len(system or ""),
            "fake": True,
            **_scalar_meta(meta),
        }
        if self.usage_limit_at is not None and idx >= self.usage_limit_at:
            self.stats.failures += 1
            self.log.write({**entry, "outcome": "usage_limit"})
            raise UsageLimitError("You've hit your session limit · resets 3:45pm (simulado)", "3:45pm")
        if session_id and resume and session_id not in self.sessions:
            self.log.write({**entry, "outcome": "fatal"})
            raise LLMCallFailed(f"No conversation found with session ID: {session_id}")
        if self.latency:
            await asyncio.sleep(self.latency)
        history = self.sessions.setdefault(session_id, []) if session_id else []
        digest = hashlib.sha1(prompt.encode("utf-8")).hexdigest()[:16]
        req = FakeRequest(
            prompt=prompt,
            system=system,
            model=model,
            schema=schema,
            purpose=purpose,
            meta=dict(meta or {}),
            rng=random.Random(f"{self.seed}:{purpose}:{digest}"),
            call_index=idx,
            session_id=session_id,
            history=list(history),
        )
        self.requests.append(req)
        value = self._responder(purpose)(req)
        if session_id:
            history.append(prompt)
        data: Any = None
        if schema is not None:
            if isinstance(value, BaseModel):
                value = value.model_dump(mode="json")
            if isinstance(value, str):
                value = extract_json(value)
            data = validate_with_coercion(value, schema)
            text = data.model_dump_json()
        else:
            text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        usage = LLMUsage(
            input_tokens=len(prompt) // 4,
            output_tokens=len(text) // 4,
            duration_ms=1,
            wall_ms=1,
            num_turns=1,
            models=[f"fake-{model}"],
        )
        self.stats.record_ok(purpose, usage)
        self.log.write(
            {
                **entry,
                "outcome": "ok",
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
            }
        )
        sid = session_id
        return LLMResult(text, data, sid, model, usage, 1, call_id)


# --------------------------------------------------------------------------- fábrica


def build_llm(
    settings: Settings,
    run_dir: Path | None,
    *,
    dry_run: bool = False,
    budget: CallBudget | None = None,
    debug: bool | None = None,
    seed: int | str = 0,
) -> ClaudeCLI | FakeClaudeCLI:
    """Crea el backend adecuado; sin ``run_dir`` usa ``runs/_scratch`` como sandbox."""
    base = run_dir or (settings.runs_dir / "_scratch")
    log = CallLog(base / "llm_calls.jsonl", debug=settings.debug if debug is None else debug)
    if dry_run:
        return FakeClaudeCLI(log=log, budget=budget, seed=seed)
    return ClaudeCLI(
        sandbox_dir=base / ".sandbox",
        claude_bin=settings.claude_bin,
        log=log,
        max_concurrency=settings.max_concurrency,
        budget=budget,
        max_retries=settings.max_retries,
        schema_retries=settings.schema_retries,
        native_schema=settings.native_json_schema,
        default_timeout=settings.call_timeout,
    )
