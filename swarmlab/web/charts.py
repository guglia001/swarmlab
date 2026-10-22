"""Gráficas SVG inline para el panel web, sin JavaScript.

Los colores van por clases CSS (``pro``/``con``/``mid``, ``line``, ``band``…) definidas en ``style.css``,
así que la misma gráfica se ve bien en modo claro y oscuro. La postura es una escala divergente
(−1 en contra, +1 a favor): azul a favor, rojo en contra, gris en medio.
"""

from __future__ import annotations

import math
from typing import Any

import networkx as nx
from markupsafe import Markup, escape

SIDE = 0.2
BIN_CLASSES = [
    ("muy en contra", "con", 1.0),
    ("en contra", "con", 0.55),
    ("neutral", "mid", 0.8),
    ("a favor", "pro", 0.55),
    ("muy a favor", "pro", 1.0),
]
INTENT_CLASSES = [
    ("si", "sí", "pro", 1.0),
    ("probablemente", "prob.", "pro", 0.55),
    ("quiza", "quizá", "mid", 0.8),
    ("no", "no", "con", 1.0),
]


def stance_class(s: float | None) -> str:
    if s is None:
        return "mid"
    return "pro" if s >= SIDE else "con" if s <= -SIDE else "mid"


def stance_opacity(s: float | None) -> float:
    return 0.9 if s is None else round(0.45 + 0.55 * min(1.0, abs(s)), 2)


def _t(x: float, y: float, text: Any, cls: str = "", anchor: str = "start", dy: str = "") -> str:
    extra = (f' class="{cls}"' if cls else "") + (f' dy="{dy}"' if dy else "")
    return f'<text x="{x:.1f}" y="{y:.1f}" text-anchor="{anchor}"{extra}>{escape(text)}</text>'


def _svg(width: int, height: int, body: list[str], title: str, cls: str = "chart") -> Markup:
    return Markup(
        f'<svg class="{cls}" viewBox="0 0 {width} {height}" width="{width}" height="{height}" role="img" '
        f'aria-label="{escape(title)}"><title>{escape(title)}</title>' + "".join(body) + "</svg>"
    )


def empty(text: str) -> Markup:
    return Markup(f'<p class="muted small">{escape(text)}</p>')


# --------------------------------------------------------------------------- postura por ronda


def stance_lines_svg(metrics: list[dict[str, Any]], width: int = 720, height: int = 240) -> Markup:
    """Postura media por ronda con la banda ± desviación y los eventos marcados."""
    if not metrics:
        return empty("Todavía no hay rondas.")
    pts = [(0, metrics[0].get("mean_before", metrics[0]["mean"]), 0.0, [])]
    pts += [(m["round"], m["mean"], m.get("std") or 0.0, m.get("events") or []) for m in metrics]
    left, right, top, bottom = 38, 14, 12, 28
    w, h = width - left - right, height - top - bottom
    n = max(pts[-1][0], 1)
    sx = lambda r: left + r / n * w  # noqa: E731
    sy = lambda v: top + (1 - v) / 2 * h  # noqa: E731
    body = []
    for v in (-1, -0.5, 0, 0.5, 1):
        cls = "axis" if v == 0 else "grid"
        body.append(f'<line class="{cls}" x1="{left}" x2="{left + w}" y1="{sy(v):.1f}" y2="{sy(v):.1f}"/>')
        body.append(_t(left - 6, sy(v) + 4, f"{v:+.1f}" if v else "0", anchor="end"))
    for r, _, _, events in pts:
        body.append(_t(sx(r), height - 10, str(r), anchor="middle"))
        if events:
            body.append(
                f'<line class="event" x1="{sx(r):.1f}" x2="{sx(r):.1f}" y1="{top}" y2="{top + h}">'
                f"<title>{escape(', '.join(str(e) for e in events))}</title></line>"
            )
    upper = " ".join(f"{sx(r):.1f},{sy(min(1, m + s)):.1f}" for r, m, s, _ in pts)
    lower = " ".join(f"{sx(r):.1f},{sy(max(-1, m - s)):.1f}" for r, m, s, _ in reversed(pts))
    body.append(f'<polygon class="band" points="{upper} {lower}"/>')
    body.append(
        f'<polyline class="line" points="{" ".join(f"{sx(r):.1f},{sy(m):.1f}" for r, m, _, _ in pts)}"/>'
    )
    for r, m, s, _ in pts:
        body.append(
            f'<circle cx="{sx(r):.1f}" cy="{sy(m):.1f}" r="3.5" class="{stance_class(m)}">'
            f"<title>Ronda {r}: postura media {m:+.2f} (± {s:.2f})</title></circle>"
        )
    body.append(_t(left, 10, "postura media ± desviación · línea discontinua: evento", "ink"))
    return _svg(width, height, body, "Evolución de la postura media por ronda")


def distribution_svg(metrics: list[dict[str, Any]], width: int = 720, height: int = 200) -> Markup:
    """Reparto de posturas por ronda (barras apiladas)."""
    rows = [m for m in metrics if m.get("distribution")]
    if not rows:
        return empty("Todavía no hay rondas.")
    left, right, top, bottom = 38, 14, 10, 28
    w, h = width - left - right, height - top - bottom
    gap = 6
    bw = min(48, (w - gap * (len(rows) - 1)) / len(rows))
    x0 = left + (w - (bw * len(rows) + gap * (len(rows) - 1))) / 2
    body = [f'<line class="axis" x1="{left}" x2="{left + w}" y1="{top + h}" y2="{top + h}"/>']
    for i, m in enumerate(rows):
        dist = m["distribution"]
        total = sum(dist.values()) or 1
        x = x0 + i * (bw + gap)
        y = top + h
        for name, cls, op in BIN_CLASSES:
            k = dist.get(name, 0)
            if not k:
                continue
            bh = k / total * h
            y -= bh
            body.append(
                f'<rect class="bar {cls}" opacity="{op}" x="{x:.1f}" y="{y + 1:.1f}" width="{bw:.1f}" '
                f'height="{max(bh - 2, 0.5):.1f}" rx="2"><title>Ronda {m["round"]}: {k} {name}</title></rect>'
            )
        body.append(_t(x + bw / 2, height - 10, str(m["round"]), anchor="middle"))
    for frac in (0.5, 1.0):
        body.append(_t(left - 6, top + h * (1 - frac) + 4, f"{frac:.0%}", anchor="end"))
    return _svg(width, height, body, "Distribución de posturas por ronda")


def segments_svg(by_segment: dict[str, dict[str, Any]], width: int = 720) -> Markup:
    """Postura final media por segmento: barras divergentes alrededor del 0."""
    if not by_segment:
        return empty("Sin segmentos.")
    rows = sorted(by_segment.items(), key=lambda kv: -kv[1]["mean"])
    rh, left, right = 26, 230, 70
    height = rh * len(rows) + 24
    w = width - left - right
    cx = left + w / 2
    body = [f'<line class="axis" x1="{cx:.1f}" x2="{cx:.1f}" y1="6" y2="{height - 18}"/>']
    for i, (name, d) in enumerate(rows):
        y = 6 + i * rh
        m = float(d["mean"])
        x1, x2 = (cx, cx + m * w / 2) if m >= 0 else (cx + m * w / 2, cx)
        label = name if len(name) <= 34 else name[:33] + "…"
        body.append(_t(left - 8, y + rh / 2 + 4, label, "ink", anchor="end"))
        body.append(
            f'<rect class="bar {stance_class(m)}" opacity="{stance_opacity(m)}" x="{x1:.1f}" y="{y + 5}" '
            f'width="{max(abs(x2 - x1), 1):.1f}" height="{rh - 10}" rx="3"><title>{escape(name)}: {m:+.2f} '
            f"(n = {d['n']}, {d.get('positive_share', 0):.0%} a favor)</title></rect>"
        )
        body.append(_t(left + w + 8, y + rh / 2 + 4, f"{m:+.2f} · n {d['n']}"))
    body.append(_t(cx, height - 4, "0", anchor="middle"))
    return _svg(width, height, body, "Postura final media por segmento")


# --------------------------------------------------------------------------- red social


def network_svg(
    run_id: str,
    personas: list[dict[str, Any]],
    follows: list[tuple[str, str]],
    stances: dict[str, float],
    size: int = 640,
) -> Markup:
    """Grafo de seguimiento: nodo = agente (tamaño por seguidores, color por postura final)."""
    if not personas:
        return empty("Todavía no hay población.")
    g = nx.DiGraph()
    g.add_nodes_from(p["id"] for p in personas)
    g.add_edges_from((a, b) for a, b in follows if a in g and b in g)
    pos = nx.spring_layout(g, seed=7, k=1.6 / math.sqrt(max(len(g), 1)), iterations=60)
    pad = 26
    xs = [p[0] for p in pos.values()] or [0]
    ys = [p[1] for p in pos.values()] or [0]
    sx = lambda x: pad + (x - min(xs)) / ((max(xs) - min(xs)) or 1) * (size - 2 * pad)  # noqa: E731
    sy = lambda y: pad + (y - min(ys)) / ((max(ys) - min(ys)) or 1) * (size - 2 * pad)  # noqa: E731
    indeg = dict(g.in_degree())
    body = []
    for a, b in g.edges():
        body.append(
            f'<line class="edge" x1="{sx(pos[a][0]):.1f}" y1="{sy(pos[a][1]):.1f}" '
            f'x2="{sx(pos[b][0]):.1f}" y2="{sy(pos[b][1]):.1f}"/>'
        )
    for p in personas:
        pid = p["id"]
        s = stances.get(pid, p.get("initial_stance"))
        r = 4 + min(10, 1.6 * math.sqrt(indeg.get(pid, 0)))
        x, y = sx(pos[pid][0]), sy(pos[pid][1])
        title = f"@{p['handle']} · {p.get('segment', '')} · postura {s:+.2f} · {indeg.get(pid, 0)} seguidores"
        body.append(
            f'<g hx-get="/runs/{escape(run_id)}/agents/{escape(p["handle"])}" hx-target="#drawer" '
            f'hx-swap="innerHTML"><circle class="node {stance_class(s)}" opacity="{stance_opacity(s)}" '
            f'cx="{x:.1f}" cy="{y:.1f}" r="{r:.1f}"><title>{escape(title)}</title></circle></g>'
        )
    return _svg(size, size, body, "Red de seguimiento entre agentes", cls="chart network")


def sparkline_svg(points: list[tuple[int, float]], width: int = 84, height: int = 22) -> Markup:
    """Trayectoria de postura de un agente, en miniatura."""
    if not points:
        return Markup("")
    n = max(len(points) - 1, 1)
    coords = [(4 + i / n * (width - 8), 3 + (1 - s) / 2 * (height - 6)) for i, (_, s) in enumerate(points)]
    last = points[-1][1]
    body = [
        f'<line class="grid" x1="4" x2="{width - 4}" y1="{height / 2:.1f}" y2="{height / 2:.1f}"/>',
        f'<polyline class="line" points="{" ".join(f"{x:.1f},{y:.1f}" for x, y in coords)}"/>',
        f'<circle class="{stance_class(last)}" cx="{coords[-1][0]:.1f}" cy="{coords[-1][1]:.1f}" r="2.6"/>',
    ]
    title = f"postura {points[0][1]:+.2f} → {last:+.2f}"
    return _svg(width, height, body, title, cls="spark")


# --------------------------------------------------------------------------- encuesta


def intent_bar_svg(block: dict[str, Any], width: int = 320, height: int = 18) -> Markup:
    """Reparto de intención (sí / probablemente / quizá / no) en una barra apilada al 100 %."""
    intent = block.get("intent") or {}
    total = sum(intent.values()) or 1
    x = 0.0
    body = []
    for key, label, cls, op in INTENT_CLASSES:
        k = intent.get(key, 0)
        if not k:
            continue
        bw = k / total * width
        body.append(
            f'<rect class="bar {cls}" opacity="{op}" x="{x + 1:.1f}" y="0" width="{max(bw - 2, 0.5):.1f}" '
            f'height="{height}" rx="3"><title>{label}: {k} de {total}</title></rect>'
        )
        if bw > 26:
            body.append(_t(x + bw / 2, height / 2 + 4, str(k), "ink", anchor="middle"))
        x += bw
    return _svg(width, height, body, "Intención declarada")


def price_curve_svg(price: dict[str, Any], width: int = 520, height: int = 220) -> Markup:
    """Curva de demanda declarada: % de clientes que pagaría cada precio, con el de referencia."""
    curve = [(float(p), s) for p, s in (price or {}).get("curve") or [] if s is not None]
    if len(curve) < 2:
        return empty(
            "Sin curva de precio (hace falta un precio de referencia y respuestas con precio máximo)."
        )
    left, right, top, bottom = 40, 16, 14, 34
    w, h = width - left - right, height - top - bottom
    xs = [p for p, _ in curve]
    lo, hi = 0.0, max(xs) * 1.05
    sx = lambda p: left + (p - lo) / (hi - lo or 1) * w  # noqa: E731
    sy = lambda s: top + (1 - s) * h  # noqa: E731
    body = []
    for frac in (0, 0.25, 0.5, 0.75, 1.0):
        body.append(
            f'<line class="grid" x1="{left}" x2="{left + w}" y1="{sy(frac):.1f}" y2="{sy(frac):.1f}"/>'
        )
        body.append(_t(left - 6, sy(frac) + 4, f"{frac:.0%}", anchor="end"))
    steps = []
    for i, (p, s) in enumerate(curve):
        if i:
            steps.append(f"{sx(p):.1f},{sy(curve[i - 1][1]):.1f}")
        steps.append(f"{sx(p):.1f},{sy(s):.1f}")
    body.append(f'<polyline class="curve" points="{" ".join(steps)}"/>')
    for p, s in curve:
        body.append(
            f'<circle class="pro" cx="{sx(p):.1f}" cy="{sy(s):.1f}" r="3.5"><title>{p:g}: {s:.0%}</title></circle>'
        )
    ref = price.get("reference")
    unit = price.get("unit") or ""
    if ref is not None:
        body.append(
            f'<line class="ref" x1="{sx(float(ref)):.1f}" x2="{sx(float(ref)):.1f}" y1="{top}" y2="{top + h}"/>'
        )
        body.append(_t(sx(float(ref)) + 4, top + 10, f"referencia {ref:g} {unit}".strip(), "ink"))
    for p in sorted({curve[0][0], curve[-1][0], float(ref) if ref is not None else curve[0][0]}):
        body.append(_t(sx(p), height - 12, f"{p:g}", anchor="middle"))
    body.append(_t(left + w / 2, height - 1, f"precio máximo declarado ({unit})".strip(), anchor="middle"))
    return _svg(width, height, body, "Curva de demanda declarada")
