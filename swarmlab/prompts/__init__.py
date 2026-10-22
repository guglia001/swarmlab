"""Carga y renderiza los prompts de ``swarmlab/prompts/*.md``.

Se usa ``string.Template`` (``$variable`` / ``${variable}``) para que las llaves de los
ejemplos JSON no interfieran. ``render`` es estricto: una variable sin valor es un error.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path
from string import Template

PROMPTS_DIR = Path(__file__).parent


@cache
def load(name: str) -> str:
    path = PROMPTS_DIR / f"{name}.md"
    if not path.is_file():
        raise FileNotFoundError(f"prompt no encontrado: {path}")
    return path.read_text(encoding="utf-8").strip() + "\n"


def render(name: str, **variables: object) -> str:
    return Template(load(name)).substitute({k: str(v) for k, v in variables.items()})


def available() -> list[str]:
    return sorted(p.stem for p in PROMPTS_DIR.glob("*.md"))
