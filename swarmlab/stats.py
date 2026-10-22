"""Estadística mínima y sin dependencias para leer las simulaciones con honestidad.

- ``wilson``: intervalo de confianza de una proporción; funciona con n pequeño y con 0 % o 100 %.
- ``welch``: compara dos grupos de réplicas (t de Welch con la distribución t de verdad).
- ``icc``: qué parte de la variación entre runs es señal (la idea) y qué parte ruido (la réplica).
- ``auc`` / ``auc_permutation_p`` / ``spearman``: concordancia de una métrica con desenlaces reales.

La unidad de réplica es el run: los agentes de un run no son independientes (deciden por lotes en la
misma llamada y se influyen en la red), así que la variación que cuenta es la que hay entre runs.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import combinations
from statistics import mean, variance

Z95 = 1.959964


def wilson(k: int, n: int, z: float = Z95) -> tuple[float, float]:
    """Intervalo de Wilson al 95 % para ``k`` éxitos de ``n``; (0, 1) si no hay datos."""
    if n <= 0:
        return (0.0, 1.0)
    p = k / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, center - half), min(1.0, center + half))


def mean_sd(values: Sequence[float]) -> tuple[float, float]:
    """Media y desviación típica muestral (n − 1); desviación 0 con un solo valor."""
    if not values:
        return (math.nan, math.nan)
    return (mean(values), math.sqrt(variance(values)) if len(values) > 1 else 0.0)


# --------------------------------------------------------------------------- distribución t


def _betacf(a: float, b: float, x: float) -> float:
    """Fracción continua de la beta incompleta (Numerical Recipes, 6.4)."""
    tiny, eps = 1e-300, 3e-14
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c, d = 1.0, 1.0 - qab * x / qap
    d = 1.0 / (d if abs(d) > tiny else tiny)
    h = d
    for m in range(1, 300):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c
        c = c if abs(c) > tiny else tiny
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c
        c = c if abs(c) > tiny else tiny
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < eps:
            break
    return h


def betai(a: float, b: float, x: float) -> float:
    """Beta incompleta regularizada I_x(a, b)."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    log_front = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log1p(-x)
    front = math.exp(log_front)
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1.0 - x) / b


def t_two_sided_p(t: float, df: float) -> float:
    """p bilateral de una t con ``df`` grados de libertad."""
    if math.isnan(t) or df <= 0:
        return math.nan
    if math.isinf(t):
        return 0.0
    return betai(df / 2.0, 0.5, df / (df + t * t))


def normal_two_sided_p(z: float) -> float:
    """p bilateral de una normal estándar."""
    return math.erfc(abs(z) / math.sqrt(2.0))


def t_cdf(t: float, df: float) -> float:
    tail = 0.5 * t_two_sided_p(abs(t), df)
    return 1.0 - tail if t >= 0 else tail


def t_quantile(p: float, df: float) -> float:
    """Cuantil de la t (bisección sobre la CDF); suficiente para intervalos de confianza."""
    if not 0.0 < p < 1.0:
        raise ValueError("p debe estar entre 0 y 1")
    if p < 0.5:
        return -t_quantile(1.0 - p, df)
    lo, hi = 0.0, 1.0
    while t_cdf(hi, df) < p:
        hi *= 2.0
        if hi > 1e8:
            return math.inf
    for _ in range(200):
        mid = (lo + hi) / 2.0
        if t_cdf(mid, df) < p:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


# --------------------------------------------------------------------------- comparación de grupos


@dataclass
class Welch:
    """B − A con el error típico, la t de Welch, sus grados de libertad, p y el IC al 95 %."""

    n_a: int
    n_b: int
    mean_a: float
    mean_b: float
    diff: float
    se: float
    t: float
    df: float
    p: float
    ci_low: float
    ci_high: float

    @property
    def significant(self) -> bool:
        return not math.isnan(self.p) and self.p < 0.05


def welch(a: Sequence[float], b: Sequence[float]) -> Welch:
    """t de Welch sobre las medias de dos grupos de réplicas (al menos 2 por grupo)."""
    if len(a) < 2 or len(b) < 2:
        raise ValueError("hacen falta al menos 2 réplicas por grupo")
    ma, mb = mean(a), mean(b)
    va, vb = variance(a) / len(a), variance(b) / len(b)
    se = math.sqrt(va + vb)
    diff = mb - ma
    if se == 0.0:
        # réplicas idénticas: no hay ruido medible; no se puede afirmar nada con rigor
        return Welch(len(a), len(b), ma, mb, diff, 0.0, math.nan, math.nan, math.nan, diff, diff)
    df = (va + vb) ** 2 / ((va * va / (len(a) - 1) if va else 0.0) + (vb * vb / (len(b) - 1) if vb else 0.0))
    t = diff / se
    q = t_quantile(0.975, df)
    return Welch(len(a), len(b), ma, mb, diff, se, t, df, t_two_sided_p(t, df), diff - q * se, diff + q * se)


# --------------------------------------------------------------------------- ruido entre réplicas


@dataclass
class ICC:
    """Correlación intraclase ICC(1): parte de la varianza entre runs que se debe a la idea."""

    groups: int
    runs: int
    icc: float
    sd_within: float
    sd_between: float


def icc(groups: Sequence[Sequence[float]]) -> ICC | None:
    """ICC(1) de un ANOVA de un factor (grupos desequilibrados con el n₀ de Snedecor).

    Cada grupo son las réplicas de una misma idea. ``None`` si no hay al menos dos grupos y
    alguna réplica repetida (sin eso no se puede separar señal de ruido).
    """
    gs = [list(g) for g in groups if len(g) >= 1]
    n_total = sum(len(g) for g in gs)
    if len(gs) < 2 or n_total - len(gs) < 1:
        return None
    grand = sum(sum(g) for g in gs) / n_total
    ssb = sum(len(g) * (mean(g) - grand) ** 2 for g in gs)
    ssw = sum(sum((v - mean(g)) ** 2 for v in g) for g in gs)
    msb = ssb / (len(gs) - 1)
    msw = ssw / (n_total - len(gs))
    n0 = (n_total - sum(len(g) ** 2 for g in gs) / n_total) / (len(gs) - 1)
    var_between = max(0.0, (msb - msw) / n0)
    total = var_between + msw
    value = var_between / total if total > 0 else 0.0
    return ICC(len(gs), n_total, value, math.sqrt(msw), math.sqrt(var_between))


def min_detectable_diff(
    sd_within: float, replicas: int, z_alpha: float = Z95, z_power: float = 0.8416
) -> float:
    """Diferencia mínima entre dos ideas detectable con ``replicas`` runs por idea (α = 5 %, potencia 80 %)."""
    return (z_alpha + z_power) * sd_within * math.sqrt(2.0 / max(replicas, 1))


# --------------------------------------------------------------------------- concordancia con la realidad


def auc(positives: Sequence[float], negatives: Sequence[float]) -> float:
    """Área bajo la curva ROC (Mann-Whitney): probabilidad de que un éxito puntúe más que un fracaso."""
    if not positives or not negatives:
        return math.nan
    wins = sum(1.0 if p > q else 0.5 if p == q else 0.0 for p in positives for q in negatives)
    return wins / (len(positives) * len(negatives))


def auc_permutation_p(positives: Sequence[float], negatives: Sequence[float], max_exact: int = 16) -> float:
    """p exacto (unilateral) de obtener un AUC al menos tan alto reetiquetando éxitos y fracasos al azar."""
    values = [*positives, *negatives]
    k = len(positives)
    if not positives or not negatives or len(values) > max_exact:
        return math.nan
    observed = auc(positives, negatives)
    hits = total = 0
    for idx in combinations(range(len(values)), k):
        chosen = set(idx)
        pos = [values[i] for i in idx]
        neg = [values[i] for i in range(len(values)) if i not in chosen]
        total += 1
        hits += auc(pos, neg) >= observed - 1e-12
    return hits / total


def _ranks(values: Sequence[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2.0 + 1.0
        i = j + 1
    return ranks


def spearman(x: Sequence[float], y: Sequence[float]) -> float:
    """Correlación de rangos de Spearman (empates con rango medio)."""
    if len(x) != len(y) or len(x) < 3:
        return math.nan
    rx, ry = _ranks(x), _ranks(y)
    mx, my = mean(rx), mean(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry, strict=True))
    den = math.sqrt(sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry))
    return num / den if den else math.nan
