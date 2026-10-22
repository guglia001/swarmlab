"""Métricas de la simulación: postura, polarización, actividad, influencers, narrativas y cambios de bando.

Export a CSV/JSON y gráficas PNG (matplotlib, backend Agg).
"""

from __future__ import annotations

import csv
import json
import math
import re
import unicodedata
from collections import Counter, defaultdict
from itertools import combinations
from pathlib import Path
from statistics import mean, median, pstdev
from typing import Any

from pydantic import BaseModel, Field

from swarmlab import prompts
from swarmlab.config import Settings
from swarmlab.llm import LLM, CallCache, FakeRequest, cached_complete, fake_responder
from swarmlab.store import RunStore

BINS: list[tuple[str, float, float]] = [
    ("muy en contra", -1.01, -0.6),
    ("en contra", -0.6, -0.2),
    ("neutral", -0.2, 0.2),
    ("a favor", 0.2, 0.6),
    ("muy a favor", 0.6, 1.01),
]
SIDE_THRESHOLD = 0.2


def distribution(stances: list[float]) -> dict[str, int]:
    out = dict.fromkeys((b[0] for b in BINS), 0)
    for s in stances:
        for name, lo, hi in BINS:
            if lo < s <= hi or (name == "muy en contra" and s <= hi):
                out[name] += 1
                break
    return out


def polarization(stances: list[float]) -> float:
    """Distancia media entre pares de agentes en la escala de postura, normalizada a 0..1."""
    if len(stances) < 2:
        return 0.0
    pairs = list(combinations(stances, 2))
    return round(sum(abs(a - b) for a, b in pairs) / len(pairs) / 2, 3)


def side(s: float) -> int:
    return 1 if s >= SIDE_THRESHOLD else (-1 if s <= -SIDE_THRESHOLD else 0)


def stance_stats(stances: dict[str, float]) -> dict[str, Any]:
    vals = list(stances.values())
    return {
        "mean": round(mean(vals), 3) if vals else 0.0,
        "median": round(median(vals), 3) if vals else 0.0,
        "std": round(pstdev(vals), 3) if len(vals) > 1 else 0.0,
        "distribution": distribution(vals),
        "polarization": polarization(vals),
        "extremes_share": round(sum(1 for v in vals if abs(v) >= 0.6) / max(len(vals), 1), 3),
        "positive_share": round(sum(1 for v in vals if v >= SIDE_THRESHOLD) / max(len(vals), 1), 3),
        "negative_share": round(sum(1 for v in vals if v <= -SIDE_THRESHOLD) / max(len(vals), 1), 3),
    }


def round_metrics(
    round_no: int,
    stances_before: dict[str, float],
    stances_after: dict[str, float],
    initial: dict[str, float],
    outcomes: list[dict[str, Any]],
    active: list[str],
    engagement_by_author: dict[str, float],
    events: list[str],
) -> dict[str, Any]:
    valid = [o for o in outcomes if o["valid"] and o["type"] != "do_nothing"]
    switchers = sorted(
        a for a, s in stances_after.items() if side(initial.get(a, 0.0)) != side(s) and side(s) != 0
    )
    return {
        "round": round_no,
        **stance_stats(stances_after),
        "mean_before": round(mean(stances_before.values()), 3) if stances_before else 0.0,
        "active_agents": len(active),
        "actions": dict(Counter(o["type"] for o in valid)),
        "invalid_actions": sum(1 for o in outcomes if not o["valid"]),
        "posts_created": sum(1 for o in valid if o["type"] in ("post", "reply", "quote", "repost")),
        "top_authors": sorted(engagement_by_author.items(), key=lambda kv: -kv[1])[:5],
        "switchers_so_far": switchers,
        "events": events,
    }


# --------------------------------------------------------------------------- narrativas


class Narrative(BaseModel):
    id: str = ""
    label: str
    description: str
    stance: str = Field(description="pro, contra o mixta respecto a la propuesta")
    post_ids: list[str]
    volume: int = 0
    engagement: float = 0.0
    first_round: int | None = None
    peak_round: int | None = None
    rounds: dict[str, int] = Field(default_factory=dict)
    top_authors: list[str] = Field(default_factory=list)


class NarrativeDraft(BaseModel):
    label: str = Field(description="etiqueta corta del tema o argumento")
    description: str = Field(description="1-2 frases")
    stance: str = Field(description="pro, contra o mixta")
    post_ids: list[str] = Field(description="ids de los posts que pertenecen a esta narrativa")


class NarrativeSet(BaseModel):
    narratives: list[NarrativeDraft]


def _post_rows(store: RunStore) -> list[dict[str, Any]]:
    return [dict(r) for r in store.load_posts()]


def _enrich(
    drafts: list[NarrativeDraft], posts: dict[str, dict[str, Any]], handles: dict[str, str]
) -> list[Narrative]:
    out: list[Narrative] = []
    for i, d in enumerate(drafts, start=1):
        ids = [p for p in dict.fromkeys(d.post_ids) if p in posts]
        if not ids:
            continue
        rounds = Counter(int(posts[p]["round"]) for p in ids)
        eng = sum(
            posts[p]["likes"] + 2 * posts[p]["reposts"] + 1.5 * posts[p]["quotes"] + posts[p]["replies"]
            for p in ids
        )
        authors = Counter(handles.get(posts[p]["author_id"], posts[p]["author_id"]) for p in ids)
        out.append(
            Narrative(
                id=f"n{i:02d}",
                label=d.label,
                description=d.description,
                stance=d.stance,
                post_ids=ids,
                volume=len(ids),
                engagement=round(eng, 1),
                first_round=min(rounds),
                peak_round=max(rounds.items(), key=lambda kv: (kv[1], -kv[0]))[0],
                rounds={str(k): v for k, v in sorted(rounds.items())},
                top_authors=[a for a, _ in authors.most_common(3)],
            )
        )
    return sorted(out, key=lambda n: -(n.volume + n.engagement / 5))


async def cluster_narratives(
    llm: LLM,
    store: RunStore,
    handles: dict[str, str],
    language: str,
    settings: Settings,
    cache: CallCache | None,
    max_posts: int = 90,
) -> list[Narrative]:
    """Agrupa los posts con texto en 3–8 narrativas (1 llamada al modelo medio)."""
    rows = [r for r in _post_rows(store) if r["text"] and r["kind"] in ("post", "reply", "quote")]
    if not rows:
        return []
    ranked = sorted(rows, key=lambda r: -(r["likes"] + 2 * r["reposts"] + 1.5 * r["quotes"] + r["replies"]))[
        :max_posts
    ]
    ranked.sort(key=lambda r: r["seq"])
    lines = [
        f"[{r['id']}] r{r['round']} @{handles.get(r['author_id'], r['author_id'])} "
        f"(postura {r['author_stance'] if r['author_stance'] is not None else 0:+.1f}, "
        f"♥{r['likes']} ↻{r['reposts']}): {' '.join(r['text'].split())[:170]}"
        for r in ranked
    ]
    prompt = prompts.render("narratives", language=language, posts="\n".join(lines))
    res = await cached_complete(
        cache,
        "narratives",
        llm,
        prompt,
        schema=NarrativeSet,
        system=prompts.load("narratives_system"),
        model=settings.model_medium,
        effort=settings.effort_medium,
        purpose="narratives",
        meta={"posts": len(ranked), "_post_ids": [r["id"] for r in ranked]},
    )
    return _enrich(res.narratives, {r["id"]: r for r in rows}, handles)


_STOP = set(
    [
        "a",
        "al",
        "algo",
        "ante",
        "como",
        "con",
        "contra",
        "cual",
        "cuando",
        "de",
        "del",
        "desde",
        "donde",
        "el",
        "ella",
        "en",
        "entre",
        "era",
        "es",
        "esa",
        "ese",
        "eso",
        "esta",
        "este",
        "esto",
        "ha",
        "hay",
        "la",
        "las",
        "le",
        "les",
        "lo",
        "los",
        "mas",
        "me",
        "mi",
        "mis",
        "muy",
        "no",
        "nos",
        "o",
        "para",
        "pero",
        "por",
        "porque",
        "que",
        "se",
        "si",
        "sin",
        "sobre",
        "su",
        "sus",
        "tambien",
        "te",
        "tu",
        "un",
        "una",
        "uno",
        "y",
        "ya",
        "yo",
        "eso",
        "vez",
        "solo",
        "asi",
        "the",
        "and",
        "of",
        "to",
        "in",
        "is",
        "for",
        "it",
        "that",
        "on",
        "with",
        "you",
        "this",
        "are",
    ]
)


def keyword_narratives(store: RunStore, handles: dict[str, str], top_n: int = 5) -> list[Narrative]:
    """Alternativa sin LLM: términos más frecuentes, separando posts a favor y en contra."""
    rows = [r for r in _post_rows(store) if r["text"]]
    out: list[Narrative] = []
    for label, cond in (("pro", lambda s: s >= SIDE_THRESHOLD), ("contra", lambda s: s <= -SIDE_THRESHOLD)):
        group = [r for r in rows if r["author_stance"] is not None and cond(r["author_stance"])]
        words = Counter()
        for r in group:
            norm = unicodedata.normalize("NFKD", r["text"].lower()).encode("ascii", "ignore").decode()
            words.update(w for w in re.findall(r"[a-z]{4,}", norm) if w not in _STOP)
        terms = [w for w, _ in words.most_common(top_n)]
        if group and terms:
            out.append(
                NarrativeDraft(
                    label=f"Términos {label}: {', '.join(terms[:3])}",
                    description=f"Palabras más repetidas en posts {label}: {', '.join(terms)}.",
                    stance=label,
                    post_ids=[r["id"] for r in group][:40],
                )
            )
    return _enrich(out, {r["id"]: r for r in rows}, handles)  # type: ignore[arg-type]


@fake_responder("narratives")
def _fake_narratives(req: FakeRequest) -> NarrativeSet:
    ids = [str(i) for i in req.meta.get("_post_ids", [])]
    half = len(ids) // 2
    return NarrativeSet(
        narratives=[
            NarrativeDraft(
                label="Dudas sobre el precio",
                description="Narrativa simulada.",
                stance="contra",
                post_ids=ids[:half],
            ),
            NarrativeDraft(
                label="Ahorro de tiempo", description="Narrativa simulada.", stance="pro", post_ids=ids[half:]
            ),
        ]
    )


# --------------------------------------------------------------------------- agregados finales


def trajectories(store: RunStore) -> dict[str, list[tuple[int, float]]]:
    out: dict[str, list[tuple[int, float]]] = defaultdict(list)
    for r in store.agent_states():
        out[r["agent_id"]].append((int(r["round"]), float(r["stance"])))
    return dict(out)


def influencers(store: RunStore, handles: dict[str, str], top: int = 10) -> list[dict[str, Any]]:
    """Ranking por engagement recibido + seguidores dentro de la simulación + volumen de posts."""
    eng: dict[str, float] = defaultdict(float)
    posts_by: Counter[str] = Counter()
    for r in store.load_posts():
        if r["kind"] == "event":
            continue
        eng[r["author_id"]] += r["likes"] + 2 * r["reposts"] + 1.5 * r["quotes"] + r["replies"]
        posts_by[r["author_id"]] += 1
    followers = Counter(r["followee_id"] for r in store.load_follows())
    scored = []
    for agent in set(eng) | set(followers):
        if agent.startswith("sys:"):
            continue
        score = eng.get(agent, 0.0) + 0.5 * followers.get(agent, 0) + 0.3 * posts_by.get(agent, 0)
        scored.append(
            {
                "agent_id": agent,
                "handle": handles.get(agent, agent),
                "engagement": round(eng.get(agent, 0.0), 1),
                "followers": followers.get(agent, 0),
                "posts": posts_by.get(agent, 0),
                "score": round(score, 1),
            }
        )
    return sorted(scored, key=lambda d: -d["score"])[:top]


def side_switchers(traj: dict[str, list[tuple[int, float]]]) -> list[dict[str, Any]]:
    out = []
    for agent, points in traj.items():
        if len(points) < 2:
            continue
        start, end = points[0][1], points[-1][1]
        if side(start) != side(end) and side(end) != 0:
            out.append(
                {
                    "agent_id": agent,
                    "from": round(start, 2),
                    "to": round(end, 2),
                    "delta": round(end - start, 2),
                }
            )
    return sorted(out, key=lambda d: -abs(d["delta"]))


def summary_metrics(store: RunStore) -> dict[str, Any]:
    personas = {p["id"]: p for p in store.load_persona_data()}
    handles = {pid: p["handle"] for pid, p in personas.items()}
    traj = trajectories(store)
    per_round = store.metrics()
    initial = {a: pts[0][1] for a, pts in traj.items() if pts}
    final = {a: pts[-1][1] for a, pts in traj.items() if pts}
    by_segment: dict[str, list[float]] = defaultdict(list)
    for agent, s in final.items():
        by_segment[personas.get(agent, {}).get("segment", "?")].append(s)
    actions_total: Counter[str] = Counter()
    for m in per_round:
        actions_total.update(m.get("actions", {}))
    return {
        "rounds_completed": len(per_round),
        "population": len(personas),
        "initial": stance_stats(initial),
        "final": stance_stats(final),
        "delta_mean": round((mean(final.values()) - mean(initial.values())) if final else 0.0, 3),
        "by_segment": {
            seg: {
                "n": len(v),
                "mean": round(mean(v), 3),
                "positive_share": round(sum(x >= SIDE_THRESHOLD for x in v) / len(v), 3),
            }
            for seg, v in sorted(by_segment.items())
        },
        "actions_total": dict(actions_total),
        "influencers": influencers(store, handles),
        "switchers": side_switchers(traj),
        "per_round": [
            {
                k: m.get(k)
                for k in (
                    "round",
                    "mean",
                    "std",
                    "polarization",
                    "active_agents",
                    "posts_created",
                    "positive_share",
                    "negative_share",
                    "events",
                )
            }
            for m in per_round
        ],
    }


# --------------------------------------------------------------------------- export


def export_metrics(store: RunStore) -> dict[str, Path]:
    """Escribe metrics.json, rounds.csv, agents.csv y posts.csv en ``runs/<id>/metrics``."""
    out_dir = store.paths.metrics_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    summary = summary_metrics(store)
    summary["narratives"] = store.narratives()
    paths = {
        "json": out_dir / "metrics.json",
        "rounds": out_dir / "rounds.csv",
        "agents": out_dir / "agents.csv",
        "posts": out_dir / "posts.csv",
    }
    paths["json"].write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    per_round = store.metrics()
    with paths["rounds"].open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(
            [
                "round",
                "mean",
                "median",
                "std",
                "polarization",
                "positive_share",
                "negative_share",
                "active_agents",
                "posts_created",
                "invalid_actions",
                *[b[0] for b in BINS],
                "events",
            ]
        )
        for m in per_round:
            dist = m.get("distribution", {})
            w.writerow(
                [
                    m["round"],
                    m["mean"],
                    m["median"],
                    m["std"],
                    m["polarization"],
                    m["positive_share"],
                    m["negative_share"],
                    m["active_agents"],
                    m["posts_created"],
                    m["invalid_actions"],
                    *[dist.get(b[0], 0) for b in BINS],
                    " | ".join(m.get("events", [])),
                ]
            )

    personas = store.load_persona_data()
    traj = trajectories(store)
    rounds = sorted({r for pts in traj.values() for r, _ in pts})
    with paths["agents"].open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(
            [
                "id",
                "handle",
                "name",
                "segment",
                "bucket",
                "involvement",
                "influencer",
                *[f"r{r}" for r in rounds],
            ]
        )
        for p in personas:
            pts = dict(traj.get(p["id"], []))
            w.writerow(
                [
                    p["id"],
                    p["handle"],
                    p["name"],
                    p["segment"],
                    p["stance_bucket"],
                    p["involvement"],
                    p["influencer"],
                    *[pts.get(r, "") for r in rounds],
                ]
            )

    handles = {p["id"]: p["handle"] for p in personas}
    handles.update({r["id"]: r["handle"] for r in store.load_accounts()})
    with paths["posts"].open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(
            [
                "id",
                "round",
                "author",
                "kind",
                "parent_id",
                "likes",
                "reposts",
                "quotes",
                "replies",
                "author_stance",
                "text",
            ]
        )
        for r in store.load_posts():
            w.writerow(
                [
                    r["id"],
                    r["round"],
                    handles.get(r["author_id"], r["author_id"]),
                    r["kind"],
                    r["parent_id"] or "",
                    r["likes"],
                    r["reposts"],
                    r["quotes"],
                    r["replies"],
                    r["author_stance"],
                    r["text"],
                ]
            )
    return paths


def make_charts(store: RunStore) -> list[Path]:
    """Gráficas PNG: postura media ± desviación, distribución por ronda, actividad, polarización e influencers."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    out = store.paths.charts_dir
    out.mkdir(parents=True, exist_ok=True)
    per_round = store.metrics()
    traj = trajectories(store)
    initial = {a: pts[0][1] for a, pts in traj.items() if pts and pts[0][0] == 0}
    rounds = [0] + [m["round"] for m in per_round]
    means = [mean(initial.values()) if initial else 0.0] + [m["mean"] for m in per_round]
    stds = [pstdev(initial.values()) if len(initial) > 1 else 0.0] + [m["std"] for m in per_round]
    event_rounds = [(m["round"], "; ".join(m.get("events", []))) for m in per_round if m.get("events")]
    paths: list[Path] = []

    def finish(fig: Any, name: str) -> None:
        fig.tight_layout()
        path = out / name
        fig.savefig(path, dpi=110)
        plt.close(fig)
        paths.append(path)

    fig, ax = plt.subplots(figsize=(9, 4.8))
    ax.plot(rounds, means, marker="o", color="#1f77b4", label="postura media")
    ax.fill_between(
        rounds,
        [m - s for m, s in zip(means, stds, strict=True)],
        [m + s for m, s in zip(means, stds, strict=True)],
        alpha=0.2,
        label="± desviación",
    )
    for _agent, pts in sorted(traj.items())[:60]:
        ax.plot([r for r, _ in pts], [s for _, s in pts], color="grey", alpha=0.12, linewidth=0.8)
    for r, label in event_rounds:
        ax.axvline(r, color="red", linestyle="--", alpha=0.6)
        ax.text(r, 0.92, label[:38], rotation=90, va="top", ha="right", fontsize=7, color="red")
    ax.axhline(0, color="black", linewidth=0.6)
    ax.set_ylim(-1.05, 1.05)
    ax.set_xlabel("ronda")
    ax.set_ylabel("postura (−1 en contra, +1 a favor)")
    ax.set_title("Evolución de la postura")
    ax.legend(loc="lower left", fontsize=8)
    finish(fig, "stance_mean.png")

    fig, ax = plt.subplots(figsize=(9, 4.8))
    dist_rows = [distribution(list(initial.values()))] + [m["distribution"] for m in per_round]
    bottom = [0.0] * len(rounds)
    colors = ["#b2182b", "#ef8a62", "#d9d9d9", "#67a9cf", "#2166ac"]
    for (name, _, _), color in zip(BINS, colors, strict=True):
        vals = [d.get(name, 0) for d in dist_rows]
        ax.bar(rounds, vals, bottom=bottom, color=color, label=name)
        bottom = [b + v for b, v in zip(bottom, vals, strict=True)]
    ax.set_xlabel("ronda")
    ax.set_ylabel("agentes")
    ax.set_title("Distribución de posturas por ronda")
    ax.legend(fontsize=8, ncol=5, loc="upper center", bbox_to_anchor=(0.5, -0.15))
    finish(fig, "stance_distribution.png")

    fig, ax = plt.subplots(figsize=(9, 4.8))
    kinds = ["post", "reply", "quote", "repost", "like", "follow", "unfollow"]
    bottom = [0.0] * len(per_round)
    for kind in kinds:
        vals = [m.get("actions", {}).get(kind, 0) for m in per_round]
        ax.bar([m["round"] for m in per_round], vals, bottom=bottom, label=kind)
        bottom = [b + v for b, v in zip(bottom, vals, strict=True)]
    ax2 = ax.twinx()
    ax2.plot(
        [m["round"] for m in per_round],
        [m["active_agents"] for m in per_round],
        color="black",
        marker="s",
        label="agentes activos",
    )
    ax.set_xlabel("ronda")
    ax.set_ylabel("acciones")
    ax2.set_ylabel("agentes activos")
    ax.set_title("Volumen de actividad")
    ax.legend(fontsize=8, loc="upper left")
    finish(fig, "activity.png")

    fig, ax = plt.subplots(figsize=(9, 4.2))
    ax.plot(
        [m["round"] for m in per_round],
        [m["polarization"] for m in per_round],
        marker="o",
        color="purple",
        label="polarización",
    )
    ax.plot(
        [m["round"] for m in per_round],
        [m["extremes_share"] for m in per_round],
        marker="^",
        color="orange",
        label="% en extremos",
    )
    ax.set_ylim(0, 1)
    ax.set_xlabel("ronda")
    ax.set_title("Polarización")
    ax.legend(fontsize=8)
    finish(fig, "polarization.png")

    personas = store.load_persona_data()
    handles = {p["id"]: p["handle"] for p in personas}
    top = influencers(store, handles, top=10)
    if top:
        fig, ax = plt.subplots(figsize=(9, 4.8))
        final = {a: pts[-1][1] for a, pts in traj.items() if pts}
        ax.barh(
            [f"@{d['handle']}" for d in reversed(top)],
            [d["score"] for d in reversed(top)],
            color=[plt.cm.RdYlGn((final.get(d["agent_id"], 0) + 1) / 2) for d in reversed(top)],
        )
        ax.set_xlabel("influencia (engagement + seguidores + posts)")
        ax.set_title("Top influencers (color = postura final)")
        finish(fig, "influencers.png")
    return paths


def _fmt(x: float | None) -> str:
    return "–" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x:+.2f}"
