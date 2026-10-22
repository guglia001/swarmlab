"""Configuración de SwarmLab.

Precedencia (de menor a mayor): valores por defecto < ``config.toml`` < ``.env`` <
variables de entorno ``SWARMLAB_<CLAVE>`` < opciones de la CLI.

En ``config.toml`` y ``.env`` las claves se escriben tal cual aparecen en el ejemplo
(``MAX_CONCURRENCY=3``); mayúsculas y minúsculas dan igual. Desde el entorno del proceso solo
se aceptan con prefijo (``SWARMLAB_MAX_CONCURRENCY=2``) para no chocar con variables ajenas.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator

MODEL_ALIASES = ("haiku", "sonnet", "opus", "fable")
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
STANCE_MODES = ("impresion", "cuotas")


class Settings(BaseModel):
    """Todos los parámetros ajustables. Ver ``config.example.toml``."""

    claude_bin: str = "claude"
    model_heavy: str = "opus"  # ontología e informe final
    model_medium: str = "sonnet"  # personas, resúmenes de ronda, entrevistas, chat
    model_light: str = "haiku"  # decisiones de los agentes y resúmenes de memoria
    effort_heavy: str | None = None
    effort_medium: str | None = None
    effort_light: str | None = "low"  # decisiones por lotes: ~40 % más rápidas, sin pérdida apreciable
    survey: bool = False  # encuesta final de intención de uso/compra en personaje (+1 llamada por lote)
    # impresion: la postura inicial se mide (primera impresión en personaje); cuotas: se fija por plan
    stance_mode: str = "impresion"
    report_llm: bool = True  # false: sin narrativas ni informe del modelo pesado (runs de calibración)
    launch_post: bool = True  # la ronda 1 empieza con el anuncio de la propuesta fijado en todos los feeds

    max_concurrency: int = Field(3, ge=1, le=16)
    web_parallel_runs: int = Field(
        2, ge=1, le=8
    )  # runs simultáneos desde el panel web; el resto espera en cola
    agents_per_call: int = Field(6, ge=1, le=20)
    max_calls_per_run: int = Field(600, ge=10)
    call_timeout: int = Field(180, ge=10)
    max_retries: int = Field(3, ge=0, le=10)
    schema_retries: int = Field(2, ge=0, le=5)
    native_json_schema: bool = True

    runs_dir: Path = Path("runs")
    feed_size: int = Field(8, ge=1, le=30)
    post_max_chars: int = Field(280, ge=80, le=2000)
    memory_recent: int = Field(8, ge=1, le=50)
    memory_every: int = Field(3, ge=1)
    memory_batch: int = Field(10, ge=1, le=30)
    persona_batch: int = Field(8, ge=1, le=20)
    max_stance_step: float = Field(0.4, gt=0, le=2)
    max_actions_per_agent: int = Field(4, ge=1, le=10)
    default_agents: int = Field(30, ge=10, le=200)
    default_rounds: int = Field(8, ge=1, le=100)
    round_interval: str = "1 día"
    report_reserve_calls: int = Field(3, ge=0, le=20)
    chunk_chars: int = Field(7000, ge=1000)
    debug: bool = False

    @field_validator("model_heavy", "model_medium", "model_light")
    @classmethod
    def _check_model(cls, v: str) -> str:
        v = v.strip()
        if v in MODEL_ALIASES or v.startswith("claude-"):
            return v
        raise ValueError(f"modelo desconocido {v!r}: usa un alias {MODEL_ALIASES} o un id 'claude-…'")

    @field_validator("effort_heavy", "effort_medium", "effort_light")
    @classmethod
    def _check_effort(cls, v: str | None) -> str | None:
        if v in (None, ""):
            return None
        if v not in EFFORT_LEVELS:
            raise ValueError(f"effort inválido {v!r}: {EFFORT_LEVELS}")
        return v

    @field_validator("stance_mode")
    @classmethod
    def _check_stance_mode(cls, v: str) -> str:
        v = v.strip().lower().replace("ó", "o")
        if v not in STANCE_MODES:
            raise ValueError(f"STANCE_MODE inválido {v!r}: {STANCE_MODES}")
        return v

    def model_for(self, tier: str) -> str:
        return {"heavy": self.model_heavy, "medium": self.model_medium, "light": self.model_light}[tier]

    def effort_for(self, tier: str) -> str | None:
        return {"heavy": self.effort_heavy, "medium": self.effort_medium, "light": self.effort_light}[tier]


def _parse_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip() if not raw.strip().startswith("#") else ""
        if not line or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _normalize(raw: dict[str, Any]) -> dict[str, Any]:
    fields = Settings.model_fields
    out: dict[str, Any] = {}
    for key, value in raw.items():
        k = key.lower()
        if k.startswith("swarmlab_"):
            k = k[len("swarmlab_") :]
        if k in fields:
            out[k] = value
    return out


def load_settings(
    config_path: Path | None = None,
    overrides: dict[str, Any] | None = None,
    *,
    cwd: Path | None = None,
    environ: dict[str, str] | None = None,
) -> Settings:
    """Carga la configuración aplicando la precedencia documentada en el módulo."""
    base = cwd or Path.cwd()
    env = dict(os.environ if environ is None else environ)
    merged: dict[str, Any] = {}

    toml_path = config_path or (Path(env["SWARMLAB_CONFIG"]) if env.get("SWARMLAB_CONFIG") else None)
    if toml_path is None and (base / "config.toml").is_file():
        toml_path = base / "config.toml"
    if toml_path is not None:
        if not toml_path.is_file():
            raise FileNotFoundError(f"no existe el fichero de configuración {toml_path}")
        with toml_path.open("rb") as fh:
            data = tomllib.load(fh)
        merged.update(_normalize(data.get("swarmlab", data)))

    dotenv = base / ".env"
    if dotenv.is_file():
        merged.update(_normalize(_parse_dotenv(dotenv)))

    merged.update(_normalize({k: v for k, v in env.items() if k.upper().startswith("SWARMLAB_")}))
    merged.pop("config", None)
    if overrides:
        merged.update({k: v for k, v in overrides.items() if v is not None})

    settings = Settings.model_validate(merged)
    if not settings.runs_dir.is_absolute():
        settings = settings.model_copy(update={"runs_dir": (base / settings.runs_dir).resolve()})
    return settings
