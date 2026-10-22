from __future__ import annotations

from pathlib import Path

import pytest

from swarmlab.config import Settings, load_settings

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "gestoria-ia.md"
EVENTS = ROOT / "examples" / "gestoria-events.yaml"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return load_settings(None, {"runs_dir": tmp_path / "runs"}, cwd=tmp_path, environ={})


@pytest.fixture
def example() -> Path:
    return EXAMPLE


@pytest.fixture
def events_file() -> Path:
    return EVENTS
