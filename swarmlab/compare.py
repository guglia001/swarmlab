"""Comparación A/B de dos ideas o dos versiones de una idea.

Cada lado puede ser un run o varias réplicas (``swarmlab replicate``). La unidad de réplica es el
run: los agentes de un mismo run no son independientes (deciden por lotes en la misma llamada y se
influyen en la red), así que su error típico «por agente» subestima el ruido real. Por eso:

- con ≥ 2 réplicas por lado, cada métrica se compara con una t de Welch sobre las medias de los runs;
- con un solo run en algún lado, se usa el ruido entre réplicas medido con ``swarmlab calibrate``
  (``runs/_calibration/ruido.json``) si existe; si no, no hay veredicto, solo la diferencia observada.

Con la misma población (``--personas-from``) y primera impresión medida, se añade la diferencia
emparejada persona a persona de la primera impresión (mismas personas, otra propuesta).
"""

from __future__ import annotations

import math
from collections import defaultdict
from pathlib import Path
from statistics import mean
from typing import Any

from swarmlab.config import Settings
from swarmlab.metrics import summary_metrics, trajectories
from swarmlab.scorecard import IS_PCT, LABELS, load_noise_reference, load_rows, values
from swarmlab.stats import mean_sd, normal_two_sided_p, welch
from swarmlab.store import RunStore, expand_run_ids

# primero, las que mejor separaron éxitos de fracasos reales en la calibración
KEY_METRICS = [
    "adoption",
    "choice_all",
    "juster_all",
    "initial_mean",
    "final_mean",
    "final_positive",
    "firm_all",
    "juster_customers",
    "pay_weighted_customers",
    "yes_prob_customers",
    "pay_customers",
    "choice_customers",
]
NORMAL_P = 0.05


def _top_posts(store: RunStore, negative: bool, n: int = 3) -> list[str]:
    handles = {p["id"]: p["handle"] for p in store.load_persona_data()}
    rows = [
        r
        for r in store.load_posts()
        if r["text"]
        and r["author_stance"] is not None
        and (r["author_stance"] <= -0.2 if negative else r["author_stance"] >= 0.2)
    ]
    rows.sort(key=lambda r: -(r["likes"] + 2 * r["reposts"] + 1.5 * r["quotes"] + r["replies"]))
    return [
        f"[{r['id']}] @{handles.get(r['author_id'], r['author_id'])}: {' '.join(r['text'].split())[:160]}"
        for r in rows[:n]
    ]


def _qualitative(settings: Settings, run_id: str) -> dict[str, Any]:
    store = RunStore.open_existing(settings.runs_dir / run_id)
    try:
        meta = store.meta_all()
        summary = summary_metrics(store)
        traj = trajectories(store)
        personas = {p["id"]: p for p in store.load_persona_data()}
        segs: dict[str, list[float]] = defaultdict(list)
        for agent, pts in traj.items():
            if pts:
                segs[personas.get(agent, {}).get("segment", "?")].append(pts[-1][1])
        return {
            "meta": meta,
            "summary": summary,
            "segments": segs,
            "initial": {a: pts[0][1] for a, pts in traj.items() if pts},
            "narratives": store.narratives(),
            "objections": _top_posts(store, True),
            "support": _top_posts(store, False),
        }
    finally:
        store.close()


def _fmt(key: str, x: float) -> str:
    return f"{x:.0%}" if IS_PCT.get(key) else f"{x:+.2f}"


def _fmt_diff(key: str, x: float) -> str:
    return f"{x * 100:+.0f} pp" if IS_PCT.get(key) else f"{x:+.2f}"


def _side_cell(key: str, vals: list[float]) -> str:
    if not vals:
        return "–"
    m, sd = mean_sd(vals)
    return _fmt(key, m) if len(vals) == 1 else f"{_fmt(key, m)} ± {_fmt_diff(key, sd).lstrip('+')}"


def metric_test(
    key: str, a: list[float], b: list[float], noise: dict[str, Any] | None
) -> dict[str, Any] | None:
    """B − A de una métrica con su p, por réplicas (Welch) o con el ruido de referencia."""
    if not a or not b:
        return None
    diff = mean(b) - mean(a)
    if len(a) >= 2 and len(b) >= 2:
        w = welch(a, b)
        return {"diff": diff, "p": w.p, "ci": (w.ci_low, w.ci_high), "how": "réplicas (Welch)"}
    ref = (noise or {}).get("metrics", {}).get(key)
    if ref and ref.get("sd_within"):
        se = ref["sd_within"] * math.sqrt(1 / len(a) + 1 / len(b))
        p = normal_two_sided_p(diff / se)
        return {
            "diff": diff,
            "p": p,
            "ci": (diff - 1.96 * se, diff + 1.96 * se),
            "how": "ruido de referencia",
        }
    return {"diff": diff, "p": math.nan, "ci": None, "how": "sin réplicas"}


def compare_runs(
    settings: Settings, run_a: str | list[str], run_b: str | list[str], out: Path | None = None
) -> tuple[str, Path]:
    a_ids = expand_run_ids(settings.runs_dir, [run_a] if isinstance(run_a, str) else run_a)
    b_ids = expand_run_ids(settings.runs_dir, [run_b] if isinstance(run_b, str) else run_b)
    rows_a, rows_b = load_rows(settings.runs_dir, a_ids), load_rows(settings.runs_dir, b_ids)
    qa, qb = _qualitative(settings, a_ids[0]), _qualitative(settings, b_ids[0])
    noise = load_noise_reference(settings.runs_dir)
    tests = {k: metric_test(k, values(rows_a, k), values(rows_b, k), noise) for k in KEY_METRICS}
    tests = {k: t for k, t in tests.items() if t is not None}

    same_population = all(r.get("personas_from") in a_ids for r in rows_b) or all(
        r.get("personas_from") in b_ids for r in rows_a
    )
    replicated = len(a_ids) >= 2 and len(b_ids) >= 2
    better = [k for k, t in tests.items() if not math.isnan(t["p"]) and t["p"] < NORMAL_P and t["diff"] > 0]
    worse = [k for k, t in tests.items() if not math.isnan(t["p"]) and t["p"] < NORMAL_P and t["diff"] < 0]
    if all(math.isnan(t["p"]) for t in tests.values()):
        verdict = (
            "Sin réplicas no hay veredicto: la diferencia observada puede ser ruido de la simulación. "
            "Repite cada lado con `swarmlab replicate <run> -n 2` (o mide el ruido con `swarmlab calibrate`)."
        )
    elif not better and not worse:
        verdict = "Sin diferencia clara: todas las diferencias están dentro del ruido de la simulación."
    else:
        parts = []
        if better:
            parts.append("B supera a A en " + ", ".join(LABELS[k].lower() for k in better))
        if worse:
            parts.append("A supera a B en " + ", ".join(LABELS[k].lower() for k in worse))
        verdict = "; ".join(parts) + " (p < 0,05)."
    if not replicated and any(t["how"] == "ruido de referencia" for t in tests.values()):
        verdict += (
            " Aproximado: un lado tiene un solo run y se usa el ruido medido en la calibración "
            f"({', '.join(map(str, (noise or {}).get('config', {}).get('agents', [])))} agentes)."
        )

    def names(rows: list[dict[str, Any]], q: dict[str, Any]) -> str:
        ids = ", ".join(f"`{r['run_id']}`" for r in rows)
        return f"{q['meta'].get('config', {}).get('seed_name')} — {q['meta'].get('brief', {}).get('one_liner', '')} ({ids})"

    lines = [
        f"# Comparación A/B: `{', '.join(a_ids)}` vs `{', '.join(b_ids)}`",
        "",
        f"- **A**: {names(rows_a, qa)}",
        f"- **B**: {names(rows_b, qb)}",
        (
            "- Misma población de agentes en ambos lados (comparación limpia)."
            if same_population
            else "- Poblaciones generadas por separado: las diferencias incluyen el efecto de la muestra."
        ),
        f"- Réplicas: A = {len(a_ids)}, B = {len(b_ids)}.",
        "",
        f"**Veredicto:** {verdict}",
        "",
        "| Métrica | A | B | B − A | IC 95 % | p | Método |",
        "|---|---|---|---|---|---|---|",
    ]
    for k, t in tests.items():
        ci = f"{_fmt_diff(k, t['ci'][0])} … {_fmt_diff(k, t['ci'][1])}" if t["ci"] else "–"
        p = "–" if math.isnan(t["p"]) else f"{t['p']:.3f}"
        label = "Postura media final" if k == "final_mean" else LABELS[k]
        lines.append(
            f"| {label} | {_side_cell(k, values(rows_a, k))} | {_side_cell(k, values(rows_b, k))} | "
            f"{_fmt_diff(k, t['diff'])} | {ci} | {p} | {t['how']} |"
        )
    acts_a, acts_b = qa["summary"]["actions_total"], qb["summary"]["actions_total"]
    lines += [
        f"| Posts + respuestas + citas (1.er run) | {sum(acts_a.get(k, 0) for k in ('post', 'reply', 'quote'))} | "
        f"{sum(acts_b.get(k, 0) for k in ('post', 'reply', 'quote'))} | | | | |",
        f"| Cambios de bando (1.er run) | {len(qa['summary']['switchers'])} | {len(qb['summary']['switchers'])} | | | | |",
    ]
    paired = [qb["initial"][a] - qa["initial"][a] for a in qa["initial"] if a in qb["initial"]]
    if rows_b[0].get("personas_from") == a_ids[0] and paired and rows_b[0]["stance_mode"] == "impresion":
        m, sd = mean_sd(paired)
        lines += [
            "",
            f"Primera impresión emparejada (mismas {len(paired)} personas, propuesta B frente a A): "
            f"{m:+.2f} de media por persona (desviación {sd:.2f}); "
            f"{sum(d > 0.05 for d in paired)} mejoran, {sum(d < -0.05 for d in paired)} empeoran. "
            "Es la medida más limpia de la diferencia entre versiones, aunque las personas de un mismo lote "
            "no son del todo independientes.",
        ]
    lines += [
        "",
        "## Por segmento (postura media final, 1.er run de cada lado)",
        "| Segmento | A | B |",
        "|---|---|---|",
    ]
    for seg in sorted(set(qa["segments"]) | set(qb["segments"])):
        va, vb = qa["segments"].get(seg), qb["segments"].get(seg)
        lines.append(
            f"| {seg} | {f'{mean(va):+.2f} (n={len(va)})' if va else '–'} | {f'{mean(vb):+.2f} (n={len(vb)})' if vb else '–'} |"
        )
    for label, side in (("A", qa), ("B", qb)):
        lines += ["", f"## Narrativas y objeciones — {label}"]
        lines += [f"- {n['label']} ({n['stance']}, {n['volume']} posts)" for n in side["narratives"][:5]]
        lines += ["", "Objeciones con más eco:"] + [f"- {o}" for o in side["objections"]]
        lines += ["", "Apoyos con más eco:"] + [f"- {s}" for s in side["support"]]
    md = "\n".join(lines) + "\n"
    stem_a = a_ids[0] if len(a_ids) == 1 else f"{a_ids[0]}+{len(a_ids) - 1}"
    stem_b = b_ids[0] if len(b_ids) == 1 else f"{b_ids[0]}+{len(b_ids) - 1}"
    path = out or settings.runs_dir / "_compare" / f"{stem_a}__vs__{stem_b}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(md, encoding="utf-8")
    return md, path
