"""Eventos inyectados ("modo dios"): desde YAML, de forma interactiva o con ``swarmlab inject``."""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, model_validator


class EventSpec(BaseModel):
    id: str = ""
    round: int = Field(ge=1)
    title: str
    text: str = ""
    source: str = "Noticias"
    handle: str = "noticias"
    origin: Literal["yaml", "interactive", "cli", "auto", "web"] = "yaml"

    @model_validator(mode="after")
    def _defaults(self) -> EventSpec:
        if not self.text.strip():
            self.text = self.title
        h = unicodedata.normalize("NFKD", self.handle).encode("ascii", "ignore").decode().lower()
        self.handle = re.sub(r"[^a-z0-9_]+", "_", h).strip("_")[:20] or "noticias"
        return self

    @property
    def post_text(self) -> str:
        return self.text if self.text.startswith(self.title) else f"{self.title}. {self.text}"


def load_events(path: Path) -> list[EventSpec]:
    """Lee ``events:`` (o una lista en la raíz) de un YAML. Campos: round, title, text, source, handle."""
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    items = data.get("events", []) if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise ValueError(f"{path}: se esperaba una lista de eventos")
    events = [EventSpec.model_validate(item) for item in items]
    return number_events(events)


def number_events(events: list[EventSpec], start: int = 1) -> list[EventSpec]:
    for i, ev in enumerate(sorted(events, key=lambda e: e.round), start=start):
        if not ev.id:
            ev.id = f"e{i:02d}"
    return sorted(events, key=lambda e: (e.round, e.id))
