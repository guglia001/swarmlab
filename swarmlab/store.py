"""Persistencia SQLite: cada simulación vive en ``runs/<run_id>/`` con su propio ``swarm.db``.

El estado de una ronda se confirma en una única transacción (``commit_round``); lo que no se
ha confirmado no existe al reanudar. Las respuestas del LLM de una etapa se guardan en
``llm_cache`` en cuanto llegan, para no volver a pagarlas si se repite la etapa.
"""

from __future__ import annotations

import contextlib
import fnmatch
import json
import re
import sqlite3
import unicodedata
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS chunks (id TEXT PRIMARY KEY, idx INTEGER, title TEXT, text TEXT);
CREATE TABLE IF NOT EXISTS entities (id TEXT PRIMARY KEY, name TEXT, type TEXT, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS relations (
    id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT, target TEXT, type TEXT, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS facts (id TEXT PRIMARY KEY, chunk_id TEXT, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tensions (id TEXT PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS personas (id TEXT PRIMARY KEY, handle TEXT UNIQUE, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS accounts (id TEXT PRIMARY KEY, handle TEXT UNIQUE, name TEXT, kind TEXT);
CREATE TABLE IF NOT EXISTS posts (
    id TEXT PRIMARY KEY, round INTEGER, seq INTEGER, author_id TEXT, kind TEXT, text TEXT,
    parent_id TEXT, likes INTEGER DEFAULT 0, reposts INTEGER DEFAULT 0, quotes INTEGER DEFAULT 0,
    replies INTEGER DEFAULT 0, author_stance REAL);
CREATE INDEX IF NOT EXISTS posts_round ON posts(round);
CREATE INDEX IF NOT EXISTS posts_author ON posts(author_id);
CREATE INDEX IF NOT EXISTS posts_parent ON posts(parent_id);
CREATE TABLE IF NOT EXISTS likes (agent_id TEXT, post_id TEXT, round INTEGER, PRIMARY KEY (agent_id, post_id));
CREATE TABLE IF NOT EXISTS follows (
    follower_id TEXT, followee_id TEXT, since_round INTEGER, PRIMARY KEY (follower_id, followee_id));
CREATE TABLE IF NOT EXISTS actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, round INTEGER, agent_id TEXT, type TEXT, target_post_id TEXT,
    target_handle TEXT, post_id TEXT, valid INTEGER, reason TEXT);
CREATE INDEX IF NOT EXISTS actions_round ON actions(round);
CREATE TABLE IF NOT EXISTS decisions (
    round INTEGER, agent_id TEXT, stance_before REAL, stance_requested REAL, stance_after REAL,
    reasoning TEXT, PRIMARY KEY (round, agent_id));
CREATE TABLE IF NOT EXISTS agent_state (
    round INTEGER, agent_id TEXT, stance REAL, active INTEGER, followers INTEGER,
    PRIMARY KEY (round, agent_id));
CREATE TABLE IF NOT EXISTS memories (agent_id TEXT PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events (id TEXT PRIMARY KEY, round INTEGER, data TEXT NOT NULL, post_id TEXT);
CREATE TABLE IF NOT EXISTS round_summaries (round INTEGER PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS metrics (round INTEGER PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS narratives (id TEXT PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS llm_cache (key TEXT PRIMARY KEY, data TEXT NOT NULL, created_at TEXT);
CREATE TABLE IF NOT EXISTS sessions (
    kind TEXT, key TEXT, session_id TEXT, model TEXT, created_at TEXT, turns INTEGER DEFAULT 0,
    PRIMARY KEY (kind, key));
CREATE TABLE IF NOT EXISTS transcripts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, key TEXT, role TEXT, content TEXT, ts TEXT);
"""

FTS_SCHEMA = (
    "CREATE VIRTUAL TABLE IF NOT EXISTS posts_fts USING fts5("
    "post_id UNINDEXED, text, tokenize='unicode61 remove_diacritics 2')"
)


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def slugify(text: str, max_len: int = 32) -> str:
    norm = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", norm).strip("-").lower()
    return slug[:max_len].strip("-") or "run"


def new_run_id(seed_name: str) -> str:
    return f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{slugify(Path(seed_name).stem, 24)}"


@dataclass(frozen=True)
class RunPaths:
    """Rutas de los artefactos de un run."""

    root: Path

    @property
    def run_id(self) -> str:
        return self.root.name

    @property
    def db(self) -> Path:
        return self.root / "swarm.db"

    @property
    def calls_log(self) -> Path:
        return self.root / "llm_calls.jsonl"

    @property
    def sandbox(self) -> Path:
        return self.root / ".sandbox"

    @property
    def seed_dir(self) -> Path:
        return self.root / "seed"

    @property
    def brief(self) -> Path:
        return self.root / "scenario_brief.md"

    @property
    def graph_json(self) -> Path:
        return self.root / "graph.json"

    @property
    def personas_json(self) -> Path:
        return self.root / "personas.json"

    @property
    def metrics_dir(self) -> Path:
        return self.root / "metrics"

    @property
    def charts_dir(self) -> Path:
        return self.root / "charts"

    @property
    def report(self) -> Path:
        return self.root / "report.md"

    @property
    def dossier(self) -> Path:
        return self.root / "dossier.md"

    @property
    def transcripts_dir(self) -> Path:
        return self.root / "transcripts"


def _dump(value: Any) -> str:
    if isinstance(value, BaseModel):
        return value.model_dump_json()
    return json.dumps(value, ensure_ascii=False, default=str)


class RunStore:
    """Acceso a ``swarm.db`` de un run."""

    def __init__(self, root: Path) -> None:
        self.paths = RunPaths(root)
        root.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.paths.db, isolation_level=None, check_same_thread=False, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)
        try:
            self.conn.execute(FTS_SCHEMA)
            self.has_fts = True
        except sqlite3.OperationalError:  # SQLite sin FTS5
            self.has_fts = False
        self._in_tx = False

    @classmethod
    def open_existing(cls, root: Path) -> RunStore:
        if not (root / "swarm.db").is_file():
            raise FileNotFoundError(f"no existe el run {root}")
        return cls(root)

    def close(self) -> None:
        self.conn.close()

    @contextlib.contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        if self._in_tx:
            yield self.conn
            return
        self.conn.execute("BEGIN IMMEDIATE")
        self._in_tx = True
        try:
            yield self.conn
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")
        finally:
            self._in_tx = False

    # ------------------------------------------------------------------ meta

    def get_meta(self, key: str, default: Any = None) -> Any:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def set_meta(self, key: str, value: Any) -> None:
        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, _dump(value)),
        )

    def update_meta(self, **values: Any) -> None:
        with self.transaction():
            for key, value in values.items():
                self.set_meta(key, value)

    def meta_all(self) -> dict[str, Any]:
        return {r["key"]: json.loads(r["value"]) for r in self.conn.execute("SELECT key, value FROM meta")}

    # ------------------------------------------------------------------ caché de llamadas LLM

    def cache_get(self, key: str) -> Any:
        row = self.conn.execute("SELECT data FROM llm_cache WHERE key=?", (key,)).fetchone()
        return json.loads(row["data"]) if row else None

    def cache_put(self, key: str, data: Any) -> None:
        self.conn.execute(
            "INSERT INTO llm_cache(key, data, created_at) VALUES(?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET data=excluded.data",
            (key, _dump(data), now_iso()),
        )

    # ------------------------------------------------------------------ documento y ontología

    def save_chunks(self, chunks: list[BaseModel]) -> None:
        with self.transaction() as c:
            c.execute("DELETE FROM chunks")
            for ch in chunks:
                d = ch.model_dump()
                c.execute(
                    "INSERT INTO chunks(id, idx, title, text) VALUES(?, ?, ?, ?)",
                    (d["id"], d["index"], d["title"], d["text"]),
                )

    def load_chunk_rows(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM chunks ORDER BY idx"))

    def save_ontology_rows(
        self,
        entities: list[BaseModel],
        relations: list[BaseModel],
        facts: list[BaseModel],
        tensions: list[BaseModel],
    ) -> None:
        with self.transaction() as c:
            for table in ("entities", "relations", "facts", "tensions"):
                c.execute(f"DELETE FROM {table}")
            for e in entities:
                d = e.model_dump()
                c.execute(
                    "INSERT INTO entities(id, name, type, data) VALUES(?, ?, ?, ?)",
                    (d["id"], d["name"], d["type"], _dump(e)),
                )
            for r in relations:
                d = r.model_dump()
                c.execute(
                    "INSERT INTO relations(source, target, type, data) VALUES(?, ?, ?, ?)",
                    (d["source"], d["target"], d["type"], _dump(r)),
                )
            for f in facts:
                d = f.model_dump()
                c.execute(
                    "INSERT INTO facts(id, chunk_id, data) VALUES(?, ?, ?)",
                    (d["id"], d["chunk_id"], _dump(f)),
                )
            for t in tensions:
                c.execute("INSERT INTO tensions(id, data) VALUES(?, ?)", (t.model_dump()["id"], _dump(t)))

    def load_table_data(self, table: str, order: str = "rowid") -> list[dict[str, Any]]:
        return [
            json.loads(r["data"]) for r in self.conn.execute(f"SELECT data FROM {table} ORDER BY {order}")
        ]

    # ------------------------------------------------------------------ personas y cuentas

    def save_personas(self, personas: list[BaseModel]) -> None:
        with self.transaction() as c:
            c.execute("DELETE FROM personas")
            for p in personas:
                d = p.model_dump()
                c.execute(
                    "INSERT INTO personas(id, handle, data) VALUES(?, ?, ?)", (d["id"], d["handle"], _dump(p))
                )

    def load_persona_data(self) -> list[dict[str, Any]]:
        return self.load_table_data("personas", "id")

    def upsert_account(self, account_id: str, handle: str, name: str, kind: str = "system") -> None:
        self.conn.execute(
            "INSERT INTO accounts(id, handle, name, kind) VALUES(?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET handle=excluded.handle, name=excluded.name",
            (account_id, handle, name, kind),
        )

    def load_accounts(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM accounts ORDER BY id"))

    # ------------------------------------------------------------------ plataforma social

    def insert_post(self, post: dict[str, Any]) -> None:
        self.conn.execute(
            "INSERT INTO posts(id, round, seq, author_id, kind, text, parent_id, likes, reposts, quotes, "
            "replies, author_stance) VALUES(:id, :round, :seq, :author_id, :kind, :text, :parent_id, :likes, "
            ":reposts, :quotes, :replies, :author_stance)",
            post,
        )
        if self.has_fts and post.get("text"):
            self.conn.execute("INSERT INTO posts_fts(post_id, text) VALUES(?, ?)", (post["id"], post["text"]))

    def update_post_counters(self, post: dict[str, Any]) -> None:
        self.conn.execute(
            "UPDATE posts SET likes=:likes, reposts=:reposts, quotes=:quotes, replies=:replies WHERE id=:id",
            post,
        )

    def load_posts(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM posts ORDER BY seq"))

    def load_likes(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM likes"))

    def load_follows(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM follows ORDER BY follower_id, followee_id"))

    def replace_follows(self, edges: list[tuple[str, str]], since_round: int = 0) -> None:
        with self.transaction() as c:
            c.execute("DELETE FROM follows")
            c.executemany(
                "INSERT INTO follows(follower_id, followee_id, since_round) VALUES(?, ?, ?)",
                [(a, b, since_round) for a, b in edges],
            )

    def search_posts(self, query: str, limit: int = 10) -> list[sqlite3.Row]:
        terms = [t for t in re.findall(r"\w+", query, flags=re.UNICODE) if len(t) > 1]
        if not terms:
            return []
        if self.has_fts:
            match = " OR ".join(f'"{t}"' for t in terms)
            return list(
                self.conn.execute(
                    "SELECT p.* FROM posts_fts f JOIN posts p ON p.id = f.post_id WHERE posts_fts MATCH ? "
                    "ORDER BY bm25(posts_fts) LIMIT ?",
                    (match, limit),
                )
            )
        like = " OR ".join("lower(text) LIKE ?" for _ in terms)
        return list(
            self.conn.execute(
                f"SELECT * FROM posts WHERE {like} ORDER BY likes + reposts DESC LIMIT ?",
                [*(f"%{t.lower()}%" for t in terms), limit],
            )
        )

    # ------------------------------------------------------------------ estado de agentes

    def save_agent_states(self, round_no: int, rows: list[tuple[str, float, bool, int]]) -> None:
        self.conn.executemany(
            "INSERT OR REPLACE INTO agent_state(round, agent_id, stance, active, followers) VALUES(?, ?, ?, ?, ?)",
            [(round_no, a, s, int(act), f) for a, s, act, f in rows],
        )

    def agent_states(self, agent_id: str | None = None) -> list[sqlite3.Row]:
        if agent_id:
            return list(
                self.conn.execute("SELECT * FROM agent_state WHERE agent_id=? ORDER BY round", (agent_id,))
            )
        return list(self.conn.execute("SELECT * FROM agent_state ORDER BY round, agent_id"))

    def latest_stances(self) -> dict[str, float]:
        row = self.conn.execute("SELECT MAX(round) AS r FROM agent_state").fetchone()
        if row is None or row["r"] is None:
            return {}
        return {
            r["agent_id"]: r["stance"]
            for r in self.conn.execute("SELECT agent_id, stance FROM agent_state WHERE round=?", (row["r"],))
        }

    def save_memories(self, memories: list[BaseModel]) -> None:
        self.conn.executemany(
            "INSERT INTO memories(agent_id, data) VALUES(?, ?) ON CONFLICT(agent_id) DO UPDATE SET data=excluded.data",
            [(m.model_dump()["agent_id"], _dump(m)) for m in memories],
        )

    def load_memory_data(self) -> list[dict[str, Any]]:
        return self.load_table_data("memories", "agent_id")

    def insert_actions(self, rows: list[dict[str, Any]]) -> None:
        self.conn.executemany(
            "INSERT INTO actions(round, agent_id, type, target_post_id, target_handle, post_id, valid, reason) "
            "VALUES(:round, :agent_id, :type, :target_post_id, :target_handle, :post_id, :valid, :reason)",
            rows,
        )

    def insert_decisions(self, rows: list[dict[str, Any]]) -> None:
        self.conn.executemany(
            "INSERT OR REPLACE INTO decisions(round, agent_id, stance_before, stance_requested, stance_after, "
            "reasoning) VALUES(:round, :agent_id, :stance_before, :stance_requested, :stance_after, :reasoning)",
            rows,
        )

    def decisions_for(self, agent_id: str) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM decisions WHERE agent_id=? ORDER BY round", (agent_id,)))

    def actions_for_round(self, round_no: int) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM actions WHERE round=? ORDER BY id", (round_no,)))

    # ------------------------------------------------------------------ eventos, resúmenes, métricas

    def save_event(self, event: BaseModel, post_id: str | None = None) -> None:
        d = event.model_dump()
        self.conn.execute(
            "INSERT INTO events(id, round, data, post_id) VALUES(?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET data=excluded.data, post_id=COALESCE(excluded.post_id, events.post_id)",
            (d["id"], d["round"], _dump(event), post_id),
        )

    def load_events(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM events ORDER BY round, id"))

    def save_round_summary(self, round_no: int, summary: BaseModel) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO round_summaries(round, data) VALUES(?, ?)", (round_no, _dump(summary))
        )

    def round_summaries(self) -> list[dict[str, Any]]:
        return self.load_table_data("round_summaries", "round")

    def save_metrics(self, round_no: int, data: dict[str, Any]) -> None:
        self.conn.execute("INSERT OR REPLACE INTO metrics(round, data) VALUES(?, ?)", (round_no, _dump(data)))

    def metrics(self) -> list[dict[str, Any]]:
        return self.load_table_data("metrics", "round")

    def save_narratives(self, narratives: list[BaseModel]) -> None:
        with self.transaction() as c:
            c.execute("DELETE FROM narratives")
            for n in narratives:
                c.execute("INSERT INTO narratives(id, data) VALUES(?, ?)", (n.model_dump()["id"], _dump(n)))

    def narratives(self) -> list[dict[str, Any]]:
        return self.load_table_data("narratives", "id")

    # ------------------------------------------------------------------ sesiones y transcripciones

    def get_session(self, kind: str, key: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM sessions WHERE kind=? AND key=?", (kind, key)).fetchone()

    def save_session(self, kind: str, key: str, session_id: str, model: str, turns: int) -> None:
        self.conn.execute(
            "INSERT INTO sessions(kind, key, session_id, model, created_at, turns) VALUES(?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(kind, key) DO UPDATE SET session_id=excluded.session_id, turns=excluded.turns",
            (kind, key, session_id, model, now_iso(), turns),
        )

    def delete_session(self, kind: str, key: str) -> None:
        self.conn.execute("DELETE FROM sessions WHERE kind=? AND key=?", (kind, key))

    def add_transcript(self, kind: str, key: str, role: str, content: str) -> None:
        self.conn.execute(
            "INSERT INTO transcripts(kind, key, role, content, ts) VALUES(?, ?, ?, ?, ?)",
            (kind, key, role, content, now_iso()),
        )

    def transcript(self, kind: str, key: str) -> list[sqlite3.Row]:
        return list(
            self.conn.execute("SELECT * FROM transcripts WHERE kind=? AND key=? ORDER BY id", (kind, key))
        )


def list_runs(runs_dir: Path) -> list[dict[str, Any]]:
    """Resumen de todos los runs de ``runs_dir`` (más recientes primero)."""
    out: list[dict[str, Any]] = []
    if not runs_dir.is_dir():
        return out
    for root in sorted(runs_dir.iterdir(), reverse=True):
        if not (root / "swarm.db").is_file():
            continue
        store = RunStore(root)
        try:
            meta = store.meta_all()
        finally:
            store.close()
        cfg = meta.get("config", {})
        out.append(
            {
                "run_id": root.name,
                "status": meta.get("status", "?"),
                "stage": meta.get("stage", "?"),
                "round": meta.get("current_round", 0),
                "rounds": cfg.get("rounds"),
                "agents": cfg.get("n_agents"),
                "seed": cfg.get("seed_name"),
                "dry_run": cfg.get("dry_run", False),
                "stance_mode": cfg.get("stance_mode", "cuotas"),
                "survey": bool(meta.get("survey")),
                "calls": meta.get("calls_used", 0),
                "created_at": cfg.get("created_at"),
            }
        )
    return out


def expand_run_ids(runs_dir: Path, specs: list[str]) -> list[str]:
    """Ids de run a partir de ids sueltos, listas separadas por comas o patrones (``idea05-*``).

    Conserva el orden y quita duplicados; un patrón sin coincidencias o un id inexistente es un error.
    """
    out: list[str] = []
    available = (
        sorted(p.name for p in runs_dir.iterdir() if (p / "swarm.db").is_file()) if runs_dir.is_dir() else []
    )
    for spec in specs:
        for token in (t.strip() for t in spec.split(",")):
            if not token:
                continue
            if any(ch in token for ch in "*?["):
                matches = [r for r in available if fnmatch.fnmatchcase(r, token)]
                if not matches:
                    raise FileNotFoundError(f"ningún run coincide con {token!r} en {runs_dir}")
                out += matches
            elif token in available:
                out.append(token)
            else:
                raise FileNotFoundError(f"no existe el run {token!r} en {runs_dir}")
    return list(dict.fromkeys(out))
