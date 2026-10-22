"""Resultados de varios runs: métricas por run, réplicas agregadas y calibración del método.

- ``run_metrics``: las métricas de un run en un diccionario plano (postura, encuesta, precio).
- ``scorecard_md``: tabla Markdown; las réplicas de una misma idea se agregan (media ± desviación).
- ``noise_rows`` / ``noise_md``: por métrica, ruido entre réplicas frente a diferencias entre ideas
  (ICC) y diferencia mínima detectable. Se guarda en ``runs/_calibration/ruido.json`` para que
  ``swarmlab compare`` pueda juzgar runs sueltos.
- ``outcome_rows`` / ``outcome_md``: concordancia de cada métrica con desenlaces reales (AUC, Spearman).

Una réplica es el mismo documento semilla, la misma pregunta y la misma configuración con otra
``--rng-seed`` (``swarmlab replicate``). Las réplicas se agrupan por ``replica_of``.
"""

from __future__ import annotations

import json
import math
import tomllib
from collections import defaultdict
from pathlib import Path
from statistics import mean
from typing import Any

from swarmlab.metrics import summary_metrics
from swarmlab.stats import auc, auc_permutation_p, icc, mean_sd, min_detectable_diff, spearman
from swarmlab.store import RunStore, now_iso
from swarmlab.survey import pct, summarize

# clave, etiqueta, es porcentaje
METRICS: list[tuple[str, str, bool]] = [
    ("initial_mean", "Postura inicial media", False),
    ("initial_positive", "% a favor al inicio", True),
    ("final_mean", "Postura final media", False),
    ("delta", "Cambio de postura", False),
    ("final_positive", "% a favor al final", True),
    ("final_negative", "% en contra al final", True),
    ("adoption", "Índice de adopción", False),
    ("choice_all", "Eligen la propuesta (todos)", True),
    ("firm_all", "Compradores firmes (todos)", True),
    ("juster_all", "Probabilidad de compra (Juster, todos)", True),
    ("yes_prob_customers", "Clientes: sí + probablemente", True),
    ("pay_customers", "Clientes: pagarían", True),
    ("pay_certain_customers", "Clientes: pagarían con seguridad ≥ 8", True),
    ("choice_customers", "Clientes: eligen la propuesta", True),
    ("firm_customers", "Clientes: compradores firmes", True),
    ("juster_customers", "Clientes: probabilidad de compra (Juster)", True),
    ("pay_weighted_customers", "Clientes: pago ponderado por seguridad", True),
    ("price_at_reference", "Clientes que pagan el precio de referencia", True),
]
LABELS = {k: label for k, label, _ in METRICS}
LOWER_IS_BETTER = {"final_negative"}
IS_PCT = {k: p for k, _, p in METRICS}
NOISE_FILE = Path("_calibration") / "ruido.json"


def _eng(r: Any) -> float:
    return r["likes"] + 2 * r["reposts"] + 1.5 * r["quotes"] + r["replies"]


def top_objection(store: RunStore) -> str:
    handles = {p["id"]: p["handle"] for p in store.load_persona_data()}
    rows = [
        r
        for r in store.load_posts()
        if r["text"] and r["author_stance"] is not None and r["author_stance"] <= -0.2
    ]
    if not rows:
        return "–"
    r = max(rows, key=_eng)
    return f"[{r['id']}] @{handles.get(r['author_id'], '?')}: {' '.join(r['text'].split())[:140]}"


def run_metrics(store: RunStore) -> dict[str, Any]:
    """Todas las métricas comparables de un run (``None`` si no aplican, p. ej. sin encuesta)."""
    meta = store.meta_all()
    cfg = meta.get("config", {})
    s = summary_metrics(store)
    row: dict[str, Any] = {
        "run_id": store.paths.run_id,
        "group": cfg.get("replica_of") or store.paths.run_id,
        "seed_name": cfg.get("seed_name", ""),
        "seed_stem": Path(cfg.get("seed_name", "")).stem,
        "one_liner": meta.get("brief", {}).get("one_liner", ""),
        "stance_mode": cfg.get("stance_mode", "cuotas"),
        "agents": s["population"],
        "rounds_completed": s["rounds_completed"],
        "rounds": cfg.get("rounds"),
        "status": meta.get("status"),
        "dry_run": bool(cfg.get("dry_run")),
        "rng_seed": cfg.get("rng_seed"),
        "personas_from": cfg.get("personas_from"),
        "initial_mean": s["initial"]["mean"],
        "initial_positive": s["initial"]["positive_share"],
        "final_mean": s["final"]["mean"],
        "delta": s["delta_mean"],
        "final_positive": s["final"]["positive_share"],
        "final_negative": s["final"]["negative_share"],
        "best_segment": None,
        "worst_segment": None,
        "customers_n": 0,
        "price": None,
    }
    segs = sorted(s["by_segment"].items(), key=lambda kv: -kv[1]["mean"])
    if segs:
        row["best_segment"] = f"{segs[0][0]} ({segs[0][1]['mean']:+.2f})"
        row["worst_segment"] = f"{segs[-1][0]} ({segs[-1][1]['mean']:+.2f})"
    survey = meta.get("survey")
    keys = [k for k, _, _ in METRICS[6:]]
    row |= dict.fromkeys(keys)
    if survey and survey.get("answers"):
        personas = {p["id"]: p for p in store.load_persona_data()}
        summ = summarize(survey["answers"], personas, survey.get("reference"))
        o, c = summ["overall"], summ.get("customers") or {}
        row |= {
            "adoption": o["adoption_index"],
            "choice_all": o["choice_share"],
            "firm_all": o["firm_share"],
            "juster_all": o["juster_mean"],
            "customers_n": c.get("n", 0),
            "customers": c or None,
            "overall": o,
            "yes_prob_customers": c.get("yes_or_probably"),
            "pay_customers": c.get("would_pay_share"),
            "pay_certain_customers": c.get("would_pay_certain_share"),
            "choice_customers": c.get("choice_share"),
            "firm_customers": c.get("firm_share"),
            "juster_customers": c.get("juster_mean"),
            "pay_weighted_customers": c.get("pay_weighted"),
            "price": summ.get("price"),
            "price_at_reference": (summ.get("price") or {}).get("at_reference_share"),
        }
    return row


def load_rows(runs_dir: Path, run_ids: list[str], with_objection: bool = False) -> list[dict[str, Any]]:
    rows = []
    for run_id in run_ids:
        store = RunStore.open_existing(runs_dir / run_id)
        try:
            row = run_metrics(store)
            if with_objection:
                row["top_objection"] = top_objection(store)
        finally:
            store.close()
        rows.append(row)
    return rows


def group_rows(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        groups[r["group"]].append(r)
    return dict(groups)


def values(rows: list[dict[str, Any]], key: str) -> list[float]:
    return [float(r[key]) for r in rows if r.get(key) is not None]


# --------------------------------------------------------------------------- tabla de resultados


def fmt_value(key: str, vals: list[float]) -> str:
    """Un valor (o media ± desviación entre réplicas) con el formato de la métrica."""
    if not vals:
        return "–"
    m, sd = mean_sd(vals)
    if IS_PCT.get(key):
        return f"{m:.0%}" if len(vals) == 1 else f"{m:.0%} ± {sd * 100:.0f}"
    return f"{m:+.2f}" if len(vals) == 1 else f"{m:+.2f} ± {sd:.2f}"


def _share_cell(rows: list[dict[str, Any]], group: str, key: str, metric: str) -> str:
    """Proporción con su IC de Wilson si es un run suelto; media ± desviación si son réplicas."""
    if len(rows) == 1:
        b = rows[0].get(group) or {}
        share, ci = b.get(key), b.get(key.replace("_share", "_ci"))
        if share is None:
            return "–"
        return f"{share:.0%} [{ci[0]:.0%}–{ci[1]:.0%}]" if ci else f"{share:.0%}"
    return fmt_value(metric, values(rows, metric))


def scorecard_md(rows: list[dict[str, Any]]) -> str:
    """Tabla de resultados; cada fila es una idea (sus réplicas, agregadas)."""
    lines = [
        "| Idea (runs) | Modo | Agentes × rondas | Postura inicial → final | Adopción | Eligen la propuesta | "
        "Prob. de compra (Juster) | Compradores firmes | Clientes (n) | Clientes que pagarían | Precio máx. mediano |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    details = []
    for group, rs in group_rows(rows).items():
        first = rs[0]
        label = group if len(rs) == 1 else f"{group} (×{len(rs)})"
        prices = [r["price"] for r in rs if r.get("price")]
        price_cell = "–"
        if prices:
            med = mean(p["median"] for p in prices)
            ref = prices[0].get("reference")
            price_cell = f"{'≈' if len(prices) > 1 else ''}{med:g} {prices[0].get('unit', '')}".strip() + (
                f" (ref. {ref:g})" if ref is not None else ""
            )
        cust_n = "/".join(str(r.get("customers_n", 0)) for r in rs)
        lines.append(
            f"| {label} | {first['stance_mode']} | {first['agents']} × {first['rounds_completed']} | "
            f"{fmt_value('initial_mean', values(rs, 'initial_mean'))} → {fmt_value('final_mean', values(rs, 'final_mean'))} | "
            f"{fmt_value('adoption', values(rs, 'adoption'))} | "
            f"{_share_cell(rs, 'overall', 'choice_share', 'choice_all')} | "
            f"{fmt_value('juster_all', values(rs, 'juster_all'))} | "
            f"{_share_cell(rs, 'overall', 'firm_share', 'firm_all')} | "
            f"{cust_n} | {_share_cell(rs, 'customers', 'would_pay_share', 'pay_customers')} | {price_cell} |"
        )
        objection = first.get("top_objection")
        details.append(
            f"- **{label}** — {first['one_liner']}"
            + (f"\n  - Objeción con más eco: {objection}" if objection else "")
            + (
                f"\n  - Mejor segmento: {first['best_segment']}; peor: {first['worst_segment']}"
                if first.get("best_segment")
                else ""
            )
        )
    notes = [
        "",
        "Adopción, elección forzada y probabilidad de compra (Juster) son de toda la población encuestada: en la "
        "calibración con casos reales fueron las más estables entre réplicas y las que mejor separaron éxitos de "
        "fracasos (calibracion/RESULTADOS.md); «clientes que pagarían» fue la más floja. Entre "
        "corchetes, intervalo de Wilson al 95 %; con réplicas, media ± desviación entre réplicas (en puntos "
        "para los %). «Compradores firmes»: eligen la propuesta, pagarían con seguridad ≥ 8/10 y pagarían ya. En "
        "modo `cuotas` la postura inicial la fija el plan de cuotas y no dice nada de la idea.",
    ]
    return "\n".join([*lines, *notes, "", *details]) + "\n"


# --------------------------------------------------------------------------- ruido entre réplicas


def noise_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Por métrica: desviación entre réplicas, entre ideas, ICC y diferencia mínima detectable."""
    groups = group_rows(rows)
    out = []
    for key, label, _ in METRICS:
        per_group = [values(rs, key) for rs in groups.values()]
        per_group = [g for g in per_group if g]
        res = icc(per_group)
        if res is None:
            continue
        out.append(
            {
                "metric": key,
                "label": label,
                "groups": res.groups,
                "runs": res.runs,
                "sd_within": res.sd_within,
                "sd_between": res.sd_between,
                "icc": res.icc,
                "mdd_1": min_detectable_diff(res.sd_within, 1),
                "mdd_3": min_detectable_diff(res.sd_within, 3),
            }
        )
    return out


def _num(key: str, x: float) -> str:
    return f"{x * 100:.0f} pp" if IS_PCT.get(key) else f"{x:.2f}"


def noise_md(noise: list[dict[str, Any]]) -> str:
    if not noise:
        return "No hay réplicas: para medir el ruido hacen falta al menos dos runs de una misma idea.\n"
    lines = [
        "| Métrica | Ideas | Runs | σ entre réplicas | σ entre ideas | ICC | Dif. mínima detectable (1 run / 3 réplicas por idea) |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in noise:
        k = r["metric"]
        lines.append(
            f"| {r['label']} | {r['groups']} | {r['runs']} | {_num(k, r['sd_within'])} | {_num(k, r['sd_between'])} | "
            f"{r['icc']:.2f} | {_num(k, r['mdd_1'])} / {_num(k, r['mdd_3'])} |"
        )
    lines += [
        "",
        "ICC = parte de la variación entre runs que se debe a la idea y no al azar de la simulación "
        "(≥ 0,5: la métrica distingue ideas; 0,2–0,5: solo con réplicas; < 0,2: casi todo es ruido). "
        "Diferencia mínima detectable: la que hay que ver entre dos ideas para tomarla en serio "
        "(α = 5 %, potencia 80 %).",
    ]
    return "\n".join(lines) + "\n"


def save_noise_reference(runs_dir: Path, noise: list[dict[str, Any]], rows: list[dict[str, Any]]) -> Path:
    first = rows[0] if rows else {}
    data = {
        "created_at": now_iso(),
        "runs": [r["run_id"] for r in rows],
        "config": {
            "agents": sorted({r["agents"] for r in rows}),
            "rounds": sorted({r["rounds_completed"] for r in rows}),
            "stance_mode": sorted({r["stance_mode"] for r in rows}),
        },
        "example_run": first.get("run_id"),
        "metrics": {
            r["metric"]: {k: r[k] for k in ("sd_within", "sd_between", "icc", "groups", "runs")}
            for r in noise
        },
    }
    path = runs_dir / NOISE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


def load_noise_reference(runs_dir: Path) -> dict[str, Any] | None:
    path = runs_dir / NOISE_FILE
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


# --------------------------------------------------------------------------- concordancia con la realidad


def load_outcomes(path: Path) -> dict[str, dict[str, Any]]:
    """Desenlaces reales por id de caso (= nombre del documento semilla sin extensión)."""
    with path.open("rb") as fh:
        data = tomllib.load(fh)
    out = {}
    for case, info in data.items():
        if not isinstance(info, dict) or info.get("resultado") not in ("exito", "fracaso"):
            raise ValueError(f"caso {case!r}: 'resultado' debe ser 'exito' o 'fracaso'")
        out[case] = info
    return out


def outcome_rows(rows: list[dict[str, Any]], outcomes: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Por métrica: AUC éxito frente a fracaso (con p de permutación) y Spearman con el % conseguido."""
    cases: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        if r["seed_stem"] in outcomes:
            cases[r["seed_stem"]].append(r)
    out = []
    for key, label, _ in METRICS:
        pos, neg, xs, ys = [], [], [], []
        for case, rs in cases.items():
            vals = values(rs, key)
            if not vals:
                continue
            v = mean(vals)
            info = outcomes[case]
            (pos if info["resultado"] == "exito" else neg).append(v)
            if info.get("porcentaje") is not None:
                xs.append(v)
                ys.append(float(info["porcentaje"]))
        if not pos or not neg:
            continue
        sign = -1.0 if key in LOWER_IS_BETTER else 1.0
        out.append(
            {
                "metric": key,
                "label": label + (" (menos es mejor)" if key in LOWER_IS_BETTER else ""),
                "n_exito": len(pos),
                "n_fracaso": len(neg),
                "mean_exito": mean(pos),
                "mean_fracaso": mean(neg),
                "auc": auc([sign * v for v in pos], [sign * v for v in neg]),
                "p": auc_permutation_p([sign * v for v in pos], [sign * v for v in neg]),
                "spearman": spearman(xs, ys),
            }
        )
    return out


def outcome_md(res: list[dict[str, Any]]) -> str:
    if not res:
        return "Ningún run coincide con los casos del fichero de desenlaces (se emparejan por nombre de la semilla).\n"
    lines = [
        "| Métrica | Éxitos / fracasos | Media éxitos | Media fracasos | AUC | p (permutación) | Spearman con % conseguido |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in res:
        k = r["metric"]
        fmt = pct if IS_PCT.get(k) else (lambda x: f"{x:+.2f}")
        p = "–" if math.isnan(r["p"]) else f"{r['p']:.2f}"
        rho = "–" if math.isnan(r["spearman"]) else f"{r['spearman']:+.2f}"
        lines.append(
            f"| {r['label']} | {r['n_exito']} / {r['n_fracaso']} | {fmt(r['mean_exito'])} | {fmt(r['mean_fracaso'])} | "
            f"{r['auc']:.2f} | {p} | {rho} |"
        )
    lines += [
        "",
        "AUC = probabilidad de que un caso de éxito puntúe mejor que uno de fracaso (0,5 = azar; 1 = los "
        "separa perfectamente; por debajo de 0,5, al revés de lo esperado). p = probabilidad de un AUC igual "
        "o mayor repartiendo las etiquetas al azar (unilateral). Spearman: correlación de rangos con el % del "
        "objetivo conseguido.",
    ]
    return "\n".join(lines) + "\n"
