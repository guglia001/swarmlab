"""Informe de predicción: dossier compacto en Python → modelo pesado → ``report.md`` con citas verificadas.

También ``ReportChat`` para conversar sobre el informe reanudando la sesión de ``claude``.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from statistics import mean
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from swarmlab import prompts
from swarmlab.llm import (
    LLM,
    CallBudgetExceeded,
    Conversation,
    FakeRequest,
    LLMCallFailed,
    LLMError,
    UsageLimitError,
    cached_text,
    fake_responder,
)
from swarmlab.metrics import (
    Narrative,
    cluster_narratives,
    export_metrics,
    influencers,
    keyword_narratives,
    make_charts,
    side_switchers,
    stance_stats,
    summary_metrics,
    trajectories,
)
from swarmlab.store import RunStore, now_iso

if TYPE_CHECKING:
    from swarmlab.config import Settings
    from swarmlab.engine import Engine

SECTION_HEADERS = [
    "## 1. Resumen ejecutivo",
    "## 2. Trayectoria prevista y puntos de inflexión",
    "## 3. Actores clave",
    "## 4. Narrativas dominantes y cómo se propagaron",
    "## 5. Riesgos",
    "## 6. Oportunidades",
    "## 7. Recomendaciones accionables",
    "## 8. Nivel de confianza y qué evidencia lo cambiaría",
    "## 9. Limitaciones del método",
]
SUBSECTIONS = ["El malentendido más caro", "Objeciones recurrentes"]
POST_ID_RE = re.compile(r"\bp\d{4}\b")
HANDLE_RE = re.compile(r"@([a-z0-9_]{3,24})")
MIN_POST_CITATIONS = 8


def _clip(text: str, n: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _sentence(text: str) -> str:
    text = (text or "").strip()
    return text if text.endswith((".", "?", "!")) else f"{text}."


def _eng(r: Any) -> float:
    return r["likes"] + 2 * r["reposts"] + 1.5 * r["quotes"] + r["replies"]


class RunData:
    """Vista de solo lectura de un run para el dossier, el chat y las entrevistas."""

    def __init__(self, store: RunStore) -> None:
        self.store = store
        self.meta = store.meta_all()
        self.cfg = self.meta.get("config", {})
        self.personas = {p["id"]: p for p in store.load_persona_data()}
        self.handles = {pid: p["handle"] for pid, p in self.personas.items()}
        self.handles.update({r["id"]: r["handle"] for r in store.load_accounts()})
        self.by_handle = {p["handle"]: pid for pid, p in self.personas.items()}
        self.posts = [dict(r) for r in store.load_posts()]
        self.post_by_id = {p["id"]: p for p in self.posts}
        self.traj = trajectories(store)

    def h(self, agent_id: str) -> str:
        return self.handles.get(agent_id, agent_id)

    def post_line(self, p: dict[str, Any], n: int = 240) -> str:
        parent = self.post_by_id.get(p["parent_id"] or "")
        ctx = ""
        if parent and p["kind"] in ("reply", "quote"):
            ctx = f" ({'responde' if p['kind'] == 'reply' else 'cita'} a [{parent['id']}] @{self.h(parent['author_id'])})"
        stance = f"{p['author_stance']:+.2f}" if p["author_stance"] is not None else "n/a"
        author = p["author_id"]
        who = f"@{self.h(author)} ({author})" if author in self.personas else f"@{self.h(author)}"
        return (
            f"[{p['id']}] R{p['round']} {who} postura {stance} ♥{p['likes']} ↻{p['reposts']} ❝{p['quotes']} "
            f"💬{p['replies']}{ctx}: {_clip(p['text'], n)}"
        )

    def trajectory_line(self, agent_id: str) -> str:
        pts = self.traj.get(agent_id, [])
        return " → ".join(f"R{r} {s:+.2f}" for r, s in pts)

    def agent_dossier(self, agent_id: str, max_posts: int = 6) -> str:
        p = self.personas[agent_id]
        own = [x for x in self.posts if x["author_id"] == agent_id and x["text"]]
        own.sort(key=lambda x: -_eng(x))
        decisions = self.store.decisions_for(agent_id)
        reasons = (
            "; ".join(f"R{d['round']}: {_clip(d['reasoning'], 120)}" for d in decisions[-3:])
            or "sin decisiones"
        )
        lines = [
            f"@{p['handle']} ({agent_id}) — {p['name']}, {p['age']} años, {p['occupation']}; segmento «{p['segment']}»; "
            f"arquetipo: {p['archetype']}; {'INFLUENCER; ' if p['influencer'] else ''}implicación {p['involvement']}",
            f"  Bio: {p['bio']}",
            f"  Postura: {self.trajectory_line(agent_id)}",
            f"  Motivos (decisiones recientes): {reasons}",
        ]
        lines += [f"  {self.post_line(x, 200)}" for x in own[:max_posts]]
        return "\n".join(lines)


# --------------------------------------------------------------------------- dossier


def build_dossier(store: RunStore, narratives: list[Narrative], max_chars: int = 36000) -> str:
    d = RunData(store)
    cfg, meta = d.cfg, d.meta
    brief = meta.get("brief", {})
    summary = summary_metrics(store)
    per_round = store.metrics()
    sections: list[tuple[str, str]] = []

    sections.append(
        (
            "Escenario",
            "\n".join(
                [
                    f"Run {cfg.get('run_id')} — semilla «{cfg.get('seed_name')}» — pregunta: {cfg.get('question')}",
                    f"Propuesta: {brief.get('one_liner', '')} {brief.get('proposal', '')}",
                    "Hechos ancla: " + " | ".join(brief.get("key_facts", [])[:8]),
                    "Tensiones: " + " | ".join(brief.get("tensions", [])[:5]),
                    f"Simulación: {summary['population']} agentes, {summary['rounds_completed']} de {cfg.get('rounds')} "
                    f"rondas ({cfg.get('round_interval')} por ronda), plataforma {cfg.get('platform')}, "
                    f"{cfg.get('agents_per_call')} agentes por llamada.",
                ]
            ),
        )
    )
    seg_counts = Counter(p["segment"] for p in d.personas.values())
    seg_lines = [
        f"- {seg}: {n} agentes; postura final media {summary['by_segment'].get(seg, {}).get('mean', 0):+.2f}, "
        f"{summary['by_segment'].get(seg, {}).get('positive_share', 0):.0%} a favor"
        for seg, n in seg_counts.most_common()
    ]
    sections.append(("Población y segmentos", "\n".join(seg_lines)))
    if cfg.get("stance_mode") == "impresion":
        sections.append(("Primera impresión (en frío, antes de conversar)", first_impression_section(d)))
    evs = [json.loads(r["data"]) | {"post_id": r["post_id"]} for r in store.load_events()]
    if evs:
        sections.append(
            (
                "Eventos inyectados",
                "\n".join(
                    f"- R{e['round']} [{e.get('post_id') or '-'}] {e['source']}: {e['title']}" for e in evs
                ),
            )
        )
    init, fin = summary["initial"], summary["final"]
    table = [
        "| Ronda | Media | Desv. | Polarización | % a favor | % en contra | Activos | Posts |",
        "|---|---|---|---|---|---|---|---|",
    ]
    table.append(
        f"| R0 | {init['mean']:+.2f} | {init['std']:.2f} | {init['polarization']:.2f} | {init['positive_share']:.0%} | {init['negative_share']:.0%} | – | – |"
    )
    for m in per_round:
        table.append(
            f"| R{m['round']} | {m['mean']:+.2f} | {m['std']:.2f} | {m['polarization']:.2f} | {m['positive_share']:.0%} | "
            f"{m['negative_share']:.0%} | {m['active_agents']} | {m['posts_created']} |"
        )
    sections.append(("Métricas por ronda", "\n".join(table)))
    deltas = [(m["round"], m["mean"] - m["mean_before"]) for m in per_round]
    last = deltas[-2:] if len(deltas) >= 2 else deltas
    sections.append(
        (
            "Resultado final y convergencia",
            "\n".join(
                [
                    f"Postura media inicial {init['mean']:+.2f} → final {fin['mean']:+.2f} (Δ {summary['delta_mean']:+.2f}).",
                    f"A favor: {init['positive_share']:.0%} → {fin['positive_share']:.0%}; en contra: {init['negative_share']:.0%} → {fin['negative_share']:.0%}.",
                    f"Polarización {init['polarization']:.2f} → {fin['polarization']:.2f}; en extremos {fin['extremes_share']:.0%}.",
                    "Mayores saltos de la media: "
                    + ", ".join(f"R{r} {x:+.2f}" for r, x in sorted(deltas, key=lambda t: -abs(t[1]))[:3]),
                    "Δ media en las últimas rondas: " + ", ".join(f"R{r} {x:+.2f}" for r, x in last),
                    f"Acciones totales: {summary['actions_total']}",
                ]
            ),
        )
    )
    rs = store.round_summaries()
    sections.append(
        (
            "Resúmenes de ronda (escritos al cerrar cada ronda: el engagement que citan es el de ese momento)",
            "\n".join(
                f"- R{s['round']} ({s.get('mood', '')}): {_clip(s['narrative'], 600)} Posts clave: {', '.join(s.get('key_post_ids', []))}."
                for s in rs
            ),
        )
    )
    if narratives:
        sections.append(
            (
                "Narrativas dominantes",
                "\n".join(
                    f"- {n.id} «{n.label}» ({n.stance}; {n.volume} posts, engagement {n.engagement:.0f}; aparece en R{n.first_round}, "
                    f"pico en R{n.peak_round}; por ronda {n.rounds}; voces {', '.join('@' + a for a in n.top_authors)}): "
                    f"{n.description} Ejemplos: {', '.join(n.post_ids[:6])}"
                    for n in narratives
                ),
            )
        )
    text_posts = [p for p in d.posts if p["text"] and p["kind"] != "event"]
    top = sorted(text_posts, key=lambda p: (-_eng(p), p["seq"]))[:30]
    sections.append(("Posts más relevantes (por engagement)", "\n".join(d.post_line(p) for p in top)))
    skeptic = [p for p in text_posts if p["author_stance"] is not None and p["author_stance"] <= -0.2]
    skeptic.sort(key=lambda p: (-_eng(p), p["seq"]))
    if skeptic:
        sections.append(
            (
                "Objeciones con más eco (autores en contra)",
                "\n".join(d.post_line(p, 200) for p in skeptic[:12]),
            )
        )
    fans = [p for p in text_posts if p["author_stance"] is not None and p["author_stance"] >= 0.2]
    fans.sort(key=lambda p: (-_eng(p), p["seq"]))
    if fans:
        sections.append(("Argumentos a favor con más eco", "\n".join(d.post_line(p, 200) for p in fans[:8])))
    infl = influencers(store, d.handles, top=8)
    switch = side_switchers(d.traj)
    final = {a: pts[-1][1] for a, pts in d.traj.items() if pts}
    extremes = sorted(final, key=lambda a: final[a])[:2] + sorted(final, key=lambda a: -final[a])[:2]
    key_agents = list(
        dict.fromkeys([x["agent_id"] for x in infl[:6]] + [x["agent_id"] for x in switch[:4]] + extremes)
    )
    key_agents = [a for a in key_agents if a in d.personas][:12]
    sections.append(
        (
            "Agentes clave (influencia, cambios de bando, extremos)",
            "\n".join(d.agent_dossier(a, 3) for a in key_agents),
        )
    )
    if switch:
        sections.append(
            (
                "Agentes que cambiaron de bando",
                "\n".join(
                    f"- @{d.h(x['agent_id'])} ({x['agent_id']}): {x['from']:+.2f} → {x['to']:+.2f}"
                    for x in switch
                ),
            )
        )
    survey = meta.get("survey")
    if survey:
        from swarmlab.survey import survey_dossier_section

        sections.insert(
            4,
            (
                "Encuesta final de intención (en personaje, tras la simulación)",
                survey_dossier_section(survey, d.handles),
            ),
        )
    div = meta.get("diversity", {})
    mode = (
        "postura inicial MEDIDA (primera impresión en personaje: es un resultado)"
        if cfg.get("stance_mode") == "impresion"
        else "postura inicial FIJADA por cuotas (≥15 % escépticos…: es un dato de entrada, no evidencia de demanda)"
    )
    sections.append(
        (
            "Datos del método",
            f"{mode}; modelos: {meta.get('models', '?')}; "
            f"diversidad de la población {div.get('score', '?')} (similitud media de bios {div.get('mean_bio_similarity', '?')}); "
            f"llamadas usadas {meta.get('calls_used', '?')}; dry-run={cfg.get('dry_run')}",
        )
    )
    out: list[str] = [f"# Dossier de la simulación {cfg.get('run_id')}"]
    budget = max_chars
    for title, body in sections:
        block = f"\n## {title}\n{body}\n"
        if len(block) > budget:
            block = block[: max(budget, 0)] + "\n…(recortado)\n"
        out.append(block)
        budget -= len(block)
        if budget <= 0:
            break
    return "".join(out)


def first_impression_section(d: RunData) -> str:
    """Cómo cayó la propuesta en frío: distribución, implicación y las reacciones más extremas."""
    ps = list(d.personas.values())
    if not ps:
        return "(sin personas)"
    stats = stance_stats({p["id"]: p["initial_stance"] for p in ps})
    involvement = Counter(p["involvement"] for p in ps)
    ordered = sorted(ps, key=lambda p: p["initial_stance"])
    picks = list(dict.fromkeys([p["id"] for p in ordered[:3]] + [p["id"] for p in ordered[-3:]]))
    lines = [
        f"Media {stats['mean']:+.2f}; a favor {stats['positive_share']:.0%}, en contra {stats['negative_share']:.0%}; "
        f"implicación alta {involvement.get('alta', 0)}, media {involvement.get('media', 0)}, baja {involvement.get('baja', 0)}.",
        "Reacciones más extremas:",
    ]
    for pid in picks:
        p = d.personas[pid]
        if p.get("first_impression"):
            lines.append(
                f"- @{p['handle']} ({pid}, {p['segment']}, {p['initial_stance']:+.2f}): {_clip(p['first_impression'], 220)}"
            )
    return "\n".join(lines)


# --------------------------------------------------------------------------- validación del informe


class ReportCheck(BaseModel):
    missing_sections: list[str]
    missing_subsections: list[str]
    post_citations: int
    unknown_posts: list[str]
    agent_citations: int
    unknown_handles: list[str]

    @property
    def ok(self) -> bool:
        return (
            not self.missing_sections and not self.unknown_posts and self.post_citations >= MIN_POST_CITATIONS
        )

    def problems(self) -> list[str]:
        out = []
        if self.missing_sections:
            out.append(f"Faltan secciones: {', '.join(self.missing_sections)}")
        if self.missing_subsections:
            out.append(f"Faltan subsecciones en Riesgos: {', '.join(self.missing_subsections)}")
        if self.unknown_posts:
            out.append(f"Ids de post inexistentes: {', '.join(self.unknown_posts[:15])}")
        if self.post_citations < MIN_POST_CITATIONS:
            out.append(f"Solo {self.post_citations} citas a posts reales; cita al menos {MIN_POST_CITATIONS}")
        if self.unknown_handles:
            out.append(f"Handles inexistentes: {', '.join('@' + h for h in self.unknown_handles[:10])}")
        return out


def _norm_header(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip().lower())


def check_report(text: str, post_ids: set[str], handles: set[str]) -> ReportCheck:
    headers = {_norm_header(line) for line in text.splitlines() if line.startswith("#")}
    missing = [h for h in SECTION_HEADERS if not any(x.startswith(_norm_header(h)[:6]) for x in headers)]
    missing_sub = [s for s in SUBSECTIONS if s.lower() not in text.lower()]
    cited = POST_ID_RE.findall(text)
    cited_handles = HANDLE_RE.findall(text)
    return ReportCheck(
        missing_sections=missing,
        missing_subsections=missing_sub,
        post_citations=sum(1 for c in cited if c in post_ids),
        unknown_posts=sorted({c for c in cited if c not in post_ids}),
        agent_citations=sum(1 for h in cited_handles if h in handles),
        unknown_handles=sorted({h for h in cited_handles if h not in handles}),
    )


# --------------------------------------------------------------------------- generación


def deterministic_report(
    store: RunStore,
    narratives: list[Narrative],
    provisional: bool,
    reason: str = "límite de uso o tope de llamadas",
) -> str:
    """Informe sin LLM (límite de uso, tope de llamadas o ``REPORT_LLM=false``): 9 secciones con datos y citas."""
    d = RunData(store)
    summary = summary_metrics(store)
    init, fin = summary["initial"], summary["final"]
    per_round = store.metrics()
    text_posts = [p for p in d.posts if p["text"] and p["kind"] != "event"]
    top = sorted(text_posts, key=lambda p: (-_eng(p), p["seq"]))
    skeptic = [p for p in top if (p["author_stance"] or 0) <= -0.2]
    fans = [p for p in top if (p["author_stance"] or 0) >= 0.2]
    infl = influencers(store, d.handles, top=6)
    verdict = (
        "favorable" if fin["mean"] >= 0.2 else ("desfavorable" if fin["mean"] <= -0.2 else "dividida o tibia")
    )
    deltas = sorted(((m["round"], m["mean"] - m["mean_before"]) for m in per_round), key=lambda t: -abs(t[1]))
    lines = [
        f"# Informe de predicción — {d.meta.get('brief', {}).get('title', d.cfg.get('seed_name'))}",
        "",
        f"> **Informe {'provisional ' if provisional else ''}generado sin LLM** ({reason}). "
        "Regenerar con `swarmlab report " + str(d.cfg.get("run_id")) + "` cuando haya cuota.",
        "",
        SECTION_HEADERS[0],
        f"Pregunta: {_sentence(d.cfg.get('question', ''))} Tras {summary['rounds_completed']} rondas la reacción es **{verdict}**: "
        f"postura media {init['mean']:+.2f} → {fin['mean']:+.2f}, {fin['positive_share']:.0%} a favor y "
        f"{fin['negative_share']:.0%} en contra.",
        "",
        SECTION_HEADERS[1],
        *[
            f"- R{m['round']}: media {m['mean']:+.2f}, {m['posts_created']} posts"
            + (f"; evento: {'; '.join(m['events'])}" if m.get("events") else "")
            for m in per_round
        ],
        "Puntos de inflexión: " + ", ".join(f"R{r} ({x:+.2f})" for r, x in deltas[:3]),
        "",
        SECTION_HEADERS[2],
        *[
            f"- @{x['handle']} ({x['agent_id']}): engagement {x['engagement']}, {x['followers']} seguidores"
            for x in infl
        ],
        "",
        SECTION_HEADERS[3],
        *[
            f"- {n.id} «{n.label}» ({n.stance}, {n.volume} posts): ejemplos {', '.join(f'[{i}]' for i in n.post_ids[:3])}"
            for n in narratives
        ],
        "",
        SECTION_HEADERS[4],
        "### El malentendido más caro",
        (d.post_line(skeptic[0]) if skeptic else "Sin objeciones destacadas."),
        "### Objeciones recurrentes",
        *[f"- {d.post_line(p, 160)}" for p in skeptic[1:6]],
        "",
        SECTION_HEADERS[5],
        *[f"- {d.post_line(p, 160)}" for p in fans[:5]],
        "",
        SECTION_HEADERS[6],
        "1. Responder de forma explícita a la objeción con más eco"
        + (f" [{skeptic[0]['id']}]" if skeptic else "")
        + ".",
        "2. Apoyarse en los segmentos más receptivos: "
        + ", ".join(
            f"{s} ({v['positive_share']:.0%} a favor)"
            for s, v in sorted(summary["by_segment"].items(), key=lambda kv: -kv[1]["positive_share"])[:2]
        ),
        "",
        SECTION_HEADERS[7],
        f"Confianza **baja**: informe sin síntesis del LLM y {summary['population']} agentes simulados.",
        "",
        SECTION_HEADERS[8],
        "- Población sintética generada por un LLM; sesgos del modelo; pocas rondas; sin datos reales de mercado.",
    ]
    return "\n".join(lines) + "\n"


def appendix(store: RunStore, check: ReportCheck | None, charts: list[Path], llm_used: bool) -> str:
    d = RunData(store)
    meta = d.meta
    lines = ["", "---", "## Anexo: datos de la simulación", ""]
    graph_png = store.paths.charts_dir / "knowledge_graph.png"
    for path in [*charts, *([graph_png] if graph_png.is_file() else [])]:
        lines.append(f"![{path.stem}](charts/{path.name})")
    lines.append("")
    summary = summary_metrics(store)
    lines.append(
        f"- Run `{d.cfg.get('run_id')}` · {summary['population']} agentes · {summary['rounds_completed']} rondas · "
        f"{d.cfg.get('agents_per_call')} agentes/llamada · llamadas usadas: {meta.get('calls_used')} · dry-run: {d.cfg.get('dry_run')}"
    )
    lines.append(
        f"- Postura media: {summary['initial']['mean']:+.2f} → {summary['final']['mean']:+.2f} (Δ {summary['delta_mean']:+.2f})"
    )
    if check:
        lines.append(
            f"- Verificación de citas: {check.post_citations} citas a posts reales, {check.agent_citations} a agentes"
            + (f"; ids desconocidos: {', '.join(check.unknown_posts)}" if check.unknown_posts else "")
        )
    lines.append(
        f"- Síntesis: {'simulada (dry-run, FakeClaudeCLI)' if d.cfg.get('dry_run') else ('modelo pesado (claude -p)' if llm_used else 'determinista (sin LLM)')}; generado {now_iso()}"
    )
    lines.append(
        "- Datos: `metrics/metrics.json`, `metrics/rounds.csv`, `metrics/agents.csv`, `metrics/posts.csv`, `dossier.md`"
    )
    return "\n".join(lines) + "\n"


async def generate_report(
    engine: Engine, *, provisional: bool, allow_llm: bool, llm_report: bool = True
) -> Path:
    """Encuesta (si toca), narrativas, métricas, gráficas, dossier e informe.

    ``allow_llm=False`` no hace ninguna llamada. ``llm_report=False`` solo hace la encuesta: las
    narrativas salen por palabras clave y el informe es el determinista (runs de calibración).
    """
    store, settings, llm = engine.store, engine.settings, engine.llm
    d = RunData(store)
    language = engine.language()
    narratives: list[Narrative] = []
    llm_used = False
    with engine.budget.reserve_unlocked():
        survey = store.get_meta("survey")
        headroom = engine.budget.remaining
        need = -(-len(d.personas) // max(settings.memory_batch, 1)) + (3 if llm_report else 0)
        if (
            allow_llm
            and engine.cfg.survey
            and (not survey or survey.get("round") != store.get_meta("current_round"))
            and (headroom is None or headroom >= need)
        ):
            from swarmlab.survey import run_survey

            try:
                data = await run_survey(llm, store, settings, cache=store)
                store.update_meta(survey=data | {"round": store.get_meta("current_round")})
                engine.emit("survey", summary=data["summary"])
            except LLMError as exc:
                engine.emit("log", message=f"Encuesta final omitida ({exc})")
        if allow_llm and llm_report:
            try:
                narratives = await cluster_narratives(llm, store, d.handles, language, settings, store)
            except (LLMError, ValueError) as exc:
                engine.emit("log", message=f"Narrativas sin LLM ({exc})")
        if not narratives:
            narratives = keyword_narratives(store, d.handles)
        store.save_narratives(narratives)
        export_metrics(store)
        charts = make_charts(store)
        dossier = build_dossier(store, narratives)
        store.paths.dossier.write_text(dossier, encoding="utf-8")
        post_ids = set(d.post_by_id)
        handles = set(d.by_handle) | {h for h in d.handles.values()}
        check: ReportCheck | None = None
        text = ""
        if allow_llm and llm_report:
            try:
                text, check = await _llm_report(
                    llm, store, dossier, language, post_ids, handles, settings, provisional
                )
                llm_used = True
            except (UsageLimitError, CallBudgetExceeded, LLMCallFailed) as exc:
                engine.emit("log", message=f"Informe sin LLM ({exc})")
        if not text:
            reason = "límite de uso o tope de llamadas" if llm_report else "REPORT_LLM=false: run de medición"
            text = deterministic_report(store, narratives, provisional, reason)
            check = check_report(text, post_ids, handles)
    store.update_meta(calls_used=engine.budget.used)
    text = text.rstrip() + "\n" + appendix(store, check, charts, llm_used)
    store.paths.report.write_text(text, encoding="utf-8")
    store.update_meta(
        report={
            "path": str(store.paths.report),
            "provisional": provisional,
            "llm": llm_used,
            "rounds": store.get_meta("current_round", 0),
            "check": check.model_dump() if check else None,
            "generated_at": now_iso(),
        },
        calls_used=engine.budget.used,
    )
    return store.paths.report


async def _llm_report(
    llm: LLM,
    store: RunStore,
    dossier: str,
    language: str,
    post_ids: set[str],
    handles: set[str],
    settings: Settings,
    provisional: bool,
) -> tuple[str, ReportCheck]:
    meta = store.meta_all()
    cfg = meta.get("config", {})
    extra = (
        f"\nATENCIÓN: la simulación se detuvo en la ronda {meta.get('current_round')} de {cfg.get('rounds')}; "
        "el informe es provisional y debe decirlo en el resumen ejecutivo y en la confianza."
        if provisional
        else ""
    )
    prompt = prompts.render(
        "report_user",
        language=language,
        question=cfg.get("question", ""),
        dossier=dossier,
        title=meta.get("brief", {}).get("title", cfg.get("seed_name", "")),
        extra=extra,
    )
    system = prompts.load("report_system")
    stage = f"report.{meta.get('current_round')}"
    text = await cached_text(
        store,
        stage,
        llm,
        prompt,
        system=system,
        model=settings.model_heavy,
        effort=settings.effort_heavy,
        purpose="report",
        meta={
            "_post_ids": sorted(post_ids)[:60],
            "_handles": sorted(handles)[:20],
            "_title": cfg.get("seed_name", ""),
        },
        timeout=max(settings.call_timeout, 600),
    )
    check = check_report(text, post_ids, handles)
    if check.ok:
        return text, check
    fix = prompts.render(
        "report_fix", problems="\n".join(f"- {p}" for p in check.problems()), draft=text[:20000]
    )
    text2 = await cached_text(
        store,
        stage + ".fix",
        llm,
        prompt.rstrip() + "\n\n" + fix,
        system=system,
        model=settings.model_heavy,
        effort=settings.effort_heavy,
        purpose="report",
        meta={
            "_post_ids": sorted(post_ids)[:60],
            "_handles": sorted(handles)[:20],
            "_title": cfg.get("seed_name", ""),
            "fix": True,
        },
        timeout=max(settings.call_timeout, 600),
    )
    check2 = check_report(text2, post_ids, handles)
    better = (len(check2.missing_sections), len(check2.unknown_posts), -check2.post_citations) <= (
        len(check.missing_sections),
        len(check.unknown_posts),
        -check.post_citations,
    )
    return (text2, check2) if better else (text, check)


@fake_responder("report")
def _fake_report(req: FakeRequest) -> str:
    ids = [str(i) for i in req.meta.get("_post_ids", [])][:12]
    handles = [str(h) for h in req.meta.get("_handles", [])][:3]
    cites = " ".join(f"[{i}]" for i in ids)
    who = " ".join(f"@{h}" for h in handles)
    body = [f"# Informe de predicción — {req.meta.get('_title', '')} (dry-run)"]
    for h in SECTION_HEADERS:
        body.append(h)
        body.append(f"Contenido simulado con citas {cites} y actores {who}.")
        if h.startswith("## 5."):
            body += [
                "### El malentendido más caro",
                f"Simulado {cites[:20]}",
                "### Objeciones recurrentes",
                "- Simuladas",
            ]
    return "\n\n".join(body)


# --------------------------------------------------------------------------- chat sobre el informe


class ReportChat:
    """Conversación con el analista del informe. ``/buscar``, ``/agente`` y ``/post`` consultan SQLite."""

    KIND = "chat"
    KEY = "report"

    def __init__(self, store: RunStore, settings: Settings, llm: LLM, *, new: bool = False) -> None:
        self.store = store
        self.settings = settings
        self.data = RunData(store)
        report = (
            store.paths.report.read_text(encoding="utf-8")
            if store.paths.report.is_file()
            else "(sin informe)"
        )
        dossier = (
            store.paths.dossier.read_text(encoding="utf-8")
            if store.paths.dossier.is_file()
            else build_dossier(store, [])
        )
        row = None if new else store.get_session(self.KIND, self.KEY)
        if new:
            store.delete_session(self.KIND, self.KEY)
        self.conv = Conversation(
            llm,
            system=prompts.load("chat_system"),
            model=settings.model_medium,
            purpose="chat",
            preamble=prompts.render(
                "chat_preamble", run_id=store.paths.run_id, dossier=dossier[:40000], report=report[:30000]
            ),
            session_id=row["session_id"] if row else None,
            started=row is not None,
            effort=settings.effort_medium,
        )
        self.pending: list[str] = []

    @property
    def resumed(self) -> bool:
        return self.conv.started

    def command(self, line: str) -> str | None:
        """Ejecuta un comando local. Devuelve el texto a mostrar (y lo encola para el siguiente mensaje)."""
        cmd, _, arg = line.strip().partition(" ")
        arg = arg.strip()
        if cmd == "/buscar" and arg:
            rows = self.store.search_posts(arg, limit=8)
            text = (
                "\n".join(self.data.post_line(dict(r), 220) for r in rows) or f"(sin resultados para «{arg}»)"
            )
            text = f"Búsqueda «{arg}»:\n{text}"
        elif cmd == "/agente" and arg:
            agent_id = self.data.by_handle.get(arg.lstrip("@").lower()) or (
                arg if arg in self.data.personas else None
            )
            text = self.data.agent_dossier(agent_id) if agent_id else f"(no existe el agente {arg})"
        elif cmd == "/post" and arg:
            p = self.data.post_by_id.get(arg.strip("[]"))
            if not p:
                text = f"(no existe el post {arg})"
            else:
                replies = [x for x in self.data.posts if x["parent_id"] == p["id"]]
                text = "\n".join(
                    [
                        self.data.post_line(p, 400),
                        *[f"  ↳ {self.data.post_line(x, 200)}" for x in replies[:8]],
                    ]
                )
        else:
            return None
        self.pending.append(text)
        return text

    async def ask(self, question: str) -> str:
        message = question
        if self.pending:
            message = prompts.render("chat_context", data="\n\n".join(self.pending), question=question)
            self.pending = []
        self.store.add_transcript(self.KIND, self.KEY, "user", message)
        res = await self.conv.send(message)
        self.store.add_transcript(self.KIND, self.KEY, "assistant", res.text)
        if self.conv.session_id:
            self.store.save_session(
                self.KIND, self.KEY, self.conv.session_id, self.conv.model, self.conv.turns
            )
        return res.text


@fake_responder("chat")
def _fake_chat(req: FakeRequest) -> str:
    return f"(dry-run) Respuesta del analista al mensaje {len(req.history) + 1}."


def stance_summary(store: RunStore) -> dict[str, Any]:
    traj = trajectories(store)
    final = {a: pts[-1][1] for a, pts in traj.items() if pts}
    return stance_stats(final) | {"mean_final": round(mean(final.values()), 3) if final else 0.0}
