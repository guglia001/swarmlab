"""Encuesta final en personaje: intención de uso, compra y pago de cada agente tras la simulación.

La postura (−1..1) dice si a alguien le gusta la idea; la encuesta pregunta si la usaría y si
pagaría, que es lo que importa para saber si un negocio puede ser rentable. Una llamada (modelo
medio) por cada lote de ``MEMORY_BATCH`` agentes, con su perfil, su memoria y su postura final.

Decir «sí» en una encuesta cuesta poco (sesgo hipotético), y más a un modelo de lenguaje. Por eso,
además de la intención clásica, se piden medidas más difíciles de inflar:

- **elección forzada** entre la propuesta a su precio, lo que la persona hace hoy y no hacer nada;
- **escala de Juster** (0–10), la probabilidad de compra mejor calibrada en encuestas reales
  (Juster, 1966);
- **seguridad del pago** (0–10): los «sí» con seguridad < 8 se cuentan como «no», como en la
  valoración contingente (Champ y Bishop, 2001);
- **compromiso inmediato**: ¿pagaría hoy una señal o la primera cuota?;
- **precio máximo**, para dibujar una curva de demanda frente al precio de la propuesta.

«Compradores firmes» = eligen la propuesta, pagarían con seguridad ≥ 8 y pagarían ya. Las
proporciones van con su intervalo de Wilson al 95 %: con 10 clientes potenciales, un 100 % es
compatible con un 72 %.

«Cliente» es quien forma parte del público al que se dirige la oferta, la compre o no (antes era
«quien la usaría», lo que inflaba el «% de clientes que pagarían» al elegir el grupo por su respuesta).
"""

from __future__ import annotations

import asyncio
from collections import Counter, defaultdict
from statistics import mean, median
from typing import Any, Literal

from pydantic import BaseModel, Field

from swarmlab import prompts
from swarmlab.agent import stance_label
from swarmlab.config import Settings
from swarmlab.llm import LLM, CallCache, FakeRequest, cached_complete, fake_responder, per_agent_schema
from swarmlab.memory import AgentMemory
from swarmlab.personas import Persona
from swarmlab.stats import wilson
from swarmlab.store import RunStore

Intent = Literal["si", "probablemente", "quiza", "no"]
Role = Literal["cliente", "socio", "prescriptor", "competidor", "observador"]
Choice = Literal["propuesta", "alternativa", "nada"]
Commitment = Literal["pagaria_ya", "se_apuntaria", "nada"]
INTENT_WEIGHT = {"si": 1.0, "probablemente": 0.6, "quiza": 0.25, "no": 0.0}
CERTAINTY_MIN = 8


class SurveyAnswer(BaseModel):
    agent_id: str
    role: Role = Field(
        description="cliente = forma parte del público al que se dirige la oferta, la compre o no"
    )
    current_alternative: str = Field(description="qué usa, hace o paga hoy en lugar de la propuesta")
    reason: str
    deal_breaker: str
    would_change_mind: str
    choice: Choice = Field(
        description="elección forzada: la propuesta a su precio, su alternativa actual o nada"
    )
    intent: Intent
    purchase_probability: int = Field(ge=0, le=10, description="escala de Juster: 0 ninguna … 10 segura")
    would_pay: Literal["si", "no", "no_aplica"]
    pay_certainty: int = Field(ge=0, le=10, description="seguridad de que pagaría de verdad; 0 si no pagaría")
    max_price: float | None = Field(
        ge=0, description="máximo que pagaría, en la unidad del precio de referencia"
    )
    fair_price: str = Field(description="precio o condición que le parece justo, o 'no aplica'")
    commitment: Commitment = Field(description="si mañana se abriera la reserva")


class SurveyBatch(BaseModel):
    reference_price: float | None = Field(
        description="precio principal que fija la propuesta; null si no fija precio"
    )
    price_unit: str = Field(description="unidad de ese precio, p. ej. '€/mes' o '€ por pieza'")
    answers: list[SurveyAnswer]


def survey_schema(agent_ids: list[str]) -> type[SurveyBatch]:
    return per_agent_schema(SurveyBatch, "answers", SurveyAnswer, agent_ids)


# --------------------------------------------------------------------------- resumen


def _share(k: int, n: int) -> float | None:
    return round(k / n, 3) if n else None


def _ci(k: int, n: int) -> list[float] | None:
    if not n:
        return None
    lo, hi = wilson(k, n)
    return [round(lo, 3), round(hi, 3)]


def is_firm_buyer(a: dict[str, Any]) -> bool:
    return (
        a.get("choice") == "propuesta"
        and a.get("would_pay") == "si"
        and (a.get("pay_certainty") or 0) >= CERTAINTY_MIN
        and a.get("commitment") == "pagaria_ya"
    )


def block(items: list[dict[str, Any]]) -> dict[str, Any]:
    """Métricas de un grupo de respuestas (admite respuestas antiguas sin los campos nuevos)."""
    n = len(items)
    intents = Counter(a["intent"] for a in items)
    yes_prob = intents.get("si", 0) + intents.get("probablemente", 0)
    pay = [a for a in items if a.get("would_pay") != "no_aplica"]
    payers = sum(a.get("would_pay") == "si" for a in pay)
    v2 = [a for a in items if a.get("choice") is not None]  # formato nuevo
    pay2 = [a for a in v2 if a.get("would_pay") != "no_aplica"]
    certain = sum(a.get("would_pay") == "si" and (a.get("pay_certainty") or 0) >= CERTAINTY_MIN for a in pay2)
    picks = sum(a["choice"] == "propuesta" for a in v2)
    pay_now = sum(a.get("commitment") == "pagaria_ya" for a in v2)
    signup = sum(a.get("commitment") in ("pagaria_ya", "se_apuntaria") for a in v2)
    firm = sum(is_firm_buyer(a) for a in v2)
    juster = [a["purchase_probability"] for a in v2 if a.get("purchase_probability") is not None]
    # continuo (sin efecto suelo): cada «sí» pesa su seguridad/10; «no» pesa 0
    weighted = [(a.get("pay_certainty") or 0) / 10 if a.get("would_pay") == "si" else 0.0 for a in pay2]
    return {
        "n": n,
        "intent": {k: intents.get(k, 0) for k in INTENT_WEIGHT},
        "yes_or_probably": _share(yes_prob, n) or 0.0,
        "yes_or_probably_ci": _ci(yes_prob, n),
        "adoption_index": round(sum(INTENT_WEIGHT[a["intent"]] for a in items) / n, 3) if n else 0.0,
        "would_pay_n": len(pay),
        "would_pay_share": _share(payers, len(pay)),
        "would_pay_ci": _ci(payers, len(pay)),
        "v2_n": len(v2),
        "would_pay_certain_share": _share(certain, len(pay2)),
        "would_pay_certain_ci": _ci(certain, len(pay2)),
        "choice_share": _share(picks, len(v2)),
        "choice_ci": _ci(picks, len(v2)),
        "pay_now_share": _share(pay_now, len(v2)),
        "signup_share": _share(signup, len(v2)),
        "firm_share": _share(firm, len(v2)),
        "firm_ci": _ci(firm, len(v2)),
        "firm_count": firm,
        "juster_mean": round(mean(juster) / 10, 3) if juster else None,
        "pay_weighted": round(mean(weighted), 3) if weighted else None,
    }


def price_summary(answers: list[dict[str, Any]], reference: dict[str, Any] | None) -> dict[str, Any] | None:
    """Precio máximo declarado por los clientes potenciales y curva de demanda frente al de referencia."""
    if not reference or reference.get("price") is None:
        return None  # sin precio de referencia (p. ej. precios por pieza) la curva no tiene unidad
    group = [a for a in answers if a.get("role") == "cliente"] or answers
    rows = [a for a in group if a.get("choice") is not None and a.get("would_pay") != "no_aplica"]
    if not rows or not any(a.get("max_price") for a in rows):
        return None
    values = sorted(float(a.get("max_price") or 0.0) for a in rows)
    n = len(values)
    ref = (reference or {}).get("price")
    points = sorted({v for v in values if v > 0} | ({float(ref)} if ref else set()))
    if len(points) > 8:
        step = (len(points) - 1) / 7
        points = sorted({points[round(i * step)] for i in range(8)} | ({float(ref)} if ref else set()))
    curve = [[p, _share(sum(v >= p for v in values), n)] for p in points]
    at_ref = sum(v >= float(ref) for v in values) if ref else None

    def q(p: float) -> float:
        return round(values[min(n - 1, max(0, round(p * (n - 1))))], 2)

    return {
        "unit": (reference or {}).get("unit", ""),
        "reference": ref,
        "n": n,
        "median": round(median(values), 2),
        "p25": q(0.25),
        "p75": q(0.75),
        "at_reference_share": _share(at_ref, n) if at_ref is not None else None,
        "at_reference_ci": _ci(at_ref, n) if at_ref is not None else None,
        "curve": curve,
    }


def summarize(
    answers: list[dict[str, Any]], personas: dict[str, Any], reference: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Distribución de intención y medidas de compra: global, por segmento, por papel y de los clientes."""

    def segment(agent_id: str) -> str:
        p = personas.get(agent_id)
        if p is None:
            return "?"
        return p.segment if isinstance(p, Persona) else p.get("segment", "?")

    by_segment: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_role: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for a in answers:
        by_segment[segment(a["agent_id"])].append(a)
        by_role[a["role"]].append(a)
    customers = by_role.get("cliente", [])
    return {
        "overall": block(answers),
        "customers": block(customers) if customers else None,
        "by_segment": {k: block(v) for k, v in sorted(by_segment.items())},
        "by_role": {k: block(v) for k, v in sorted(by_role.items())},
        "price": price_summary(answers, reference),
        "deal_breakers": [a["deal_breaker"] for a in answers if a["intent"] in ("quiza", "no")][:12],
        "change_mind": [a["would_change_mind"] for a in answers if a["intent"] in ("quiza", "probablemente")][
            :12
        ],
    }


def _reference(results: list[SurveyBatch]) -> dict[str, Any] | None:
    pairs = Counter(
        (r.reference_price, r.price_unit.strip()) for r in results if r.reference_price is not None
    )
    if not pairs:
        return None
    (price, unit), _ = pairs.most_common(1)[0]
    return {"price": price, "unit": unit}


# --------------------------------------------------------------------------- encuesta


async def run_survey(
    llm: LLM,
    store: RunStore,
    settings: Settings,
    question: str | None = None,
    cache: CallCache | None = None,
) -> dict[str, Any]:
    meta = store.meta_all()
    brief = meta.get("brief", {})
    personas = {p["id"]: Persona.model_validate(p) for p in store.load_persona_data()}
    memories = {m["agent_id"]: AgentMemory.model_validate(m) for m in store.load_memory_data()}
    stances = store.latest_stances()
    language = meta.get("document", {}).get("language_name", "español")
    question = question or meta.get("config", {}).get("question", "¿Lo usarías?")
    proposal = f"{brief.get('one_liner', '')}\n{brief.get('proposal', '')}".strip()
    ids = sorted(personas)
    size = settings.memory_batch
    batches = [ids[i : i + size] for i in range(0, len(ids), size)]

    async def ask(batch: list[str], idx: int) -> SurveyBatch:
        blocks = []
        for aid in batch:
            p, s = personas[aid], stances.get(aid, personas[aid].initial_stance)
            mem = memories.get(aid, AgentMemory(agent_id=aid))
            blocks.append(
                f"### AGENTE {aid} — @{p.handle}\nPerfil: {p.card()}\nSegmento: {p.segment}\n"
                f"Postura final: {s:+.2f} ({stance_label(s)})\n{mem.render()}"
            )
        prompt = prompts.render(
            "survey_batch",
            proposal=proposal,
            question=question,
            language=language,
            count=len(batch),
            agents="\n\n".join(blocks),
        )
        return await cached_complete(
            cache,
            "survey.v2",
            llm,
            prompt,
            schema=survey_schema(batch),
            system=prompts.load("survey_system"),
            model=settings.model_medium,
            effort=settings.effort_medium,
            purpose="survey",
            meta={"batch": idx, "_agent_ids": batch},
        )

    results = await asyncio.gather(*(ask(b, i) for i, b in enumerate(batches)))
    answers = [
        SurveyAnswer.model_validate(a.model_dump()).model_dump() for res in results for a in res.answers
    ]
    reference = _reference(results)
    data = {
        "version": 2,
        "question": question,
        "reference": reference,
        "answers": answers,
        "summary": summarize(answers, personas, reference),
    }
    store.update_meta(survey=data)
    return data


# --------------------------------------------------------------------------- presentación


def pct(x: float | None) -> str:
    return "–" if x is None else f"{x:.0%}"


def pct_ci(share: float | None, ci: list[float] | None) -> str:
    if share is None:
        return "–"
    return f"{share:.0%} (IC {ci[0]:.0%}–{ci[1]:.0%})" if ci else f"{share:.0%}"


def block_line(b: dict[str, Any]) -> str:
    """Una línea con las medidas de compra de un grupo (para el dossier, la CLI y el scorecard)."""
    parts = [
        f"sí+probablemente {pct_ci(b.get('yes_or_probably'), b.get('yes_or_probably_ci'))}",
        f"pagarían {pct_ci(b.get('would_pay_share'), b.get('would_pay_ci'))} (n={b.get('would_pay_n', '?')})",
    ]
    if b.get("v2_n"):
        parts += [
            f"pagarían con seguridad ≥{CERTAINTY_MIN} {pct_ci(b.get('would_pay_certain_share'), b.get('would_pay_certain_ci'))}",
            f"eligen la propuesta frente a su alternativa y a nada {pct_ci(b.get('choice_share'), b.get('choice_ci'))}",
            f"pagarían ya {pct(b.get('pay_now_share'))}",
            f"compradores firmes {pct_ci(b.get('firm_share'), b.get('firm_ci'))}",
            f"probabilidad de compra media (Juster) {pct(b.get('juster_mean'))}",
            f"pago ponderado por seguridad {pct(b.get('pay_weighted'))}",
        ]
    return "; ".join(parts)


def price_line(p: dict[str, Any] | None) -> str | None:
    if not p:
        return None
    unit = f" {p['unit']}" if p.get("unit") else ""
    curve = ", ".join(f"{price:g}{unit}: {pct(share)}" for price, share in p["curve"])
    ref = (
        f"; al precio de la propuesta ({p['reference']:g}{unit}) pagarían {pct_ci(p['at_reference_share'], p['at_reference_ci'])}"
        if p.get("reference") is not None
        else ""
    )
    return (
        f"Precio máximo declarado por {p['n']} clientes potenciales: mediana {p['median']:g}{unit} "
        f"(P25 {p['p25']:g}, P75 {p['p75']:g}){ref}. Curva de demanda (precio: % que pagaría): {curve}"
    )


def survey_dossier_section(data: dict[str, Any], handles: dict[str, str]) -> str:
    s = data["summary"]
    o = s["overall"]
    lines = [
        f"Pregunta: {data['question']}",
        f"Total {o['n']}: sí {o['intent']['si']}, probablemente {o['intent']['probablemente']}, quizá {o['intent']['quiza']}, "
        f"no {o['intent']['no']} → índice de adopción {o['adoption_index']:.2f}; {block_line(o)}",
    ]
    if s.get("customers"):
        c = s["customers"]
        lines.append(f"Clientes potenciales (público objetivo, lo compren o no; n={c['n']}): {block_line(c)}")
    if price := price_line(s.get("price")):
        lines.append(price)
    lines.append(
        "Lectura: la intención declarada no es una compra. En la calibración con casos reales, las medidas de "
        "TODA la población (índice de adopción, elección forzada y probabilidad de compra de Juster) fueron las "
        "más estables y las que mejor separaron éxitos de fracasos; las de clientes son ruidosas (n pequeño). "
        "Los IC son los de Wilson al 95 %."
    )
    lines += [
        f"- Segmento «{k}» (n={v['n']}): índice {v['adoption_index']:.2f}, sí+prob. {pct(v['yes_or_probably'])}, "
        f"eligen la propuesta {pct(v.get('choice_share'))}, firmes {pct(v.get('firm_share'))}"
        for k, v in s["by_segment"].items()
    ]
    lines.append("Respuestas individuales:")
    for a in data["answers"]:
        extra = (
            f", elige={a['choice']}, Juster {a['purchase_probability']}/10, seguridad {a['pay_certainty']}/10, "
            f"máx. {a['max_price'] if a.get('max_price') is not None else '–'}, compromiso={a['commitment']}"
            if a.get("choice") is not None
            else ""
        )
        lines.append(
            f"- @{handles.get(a['agent_id'], a['agent_id'])} ({a['agent_id']}, {a['role']}): {a['intent']}, paga={a['would_pay']}{extra} "
            f"[{a['fair_price'][:60]}] — {a['reason'][:150]} | hoy usa: {a.get('current_alternative', '')[:80]} | "
            f"freno: {a['deal_breaker'][:110]} | cambiaría si: {a['would_change_mind'][:110]}"
        )
    return "\n".join(lines)


@fake_responder("survey")
def _fake_survey(req: FakeRequest) -> dict[str, Any]:
    intents = ["si", "probablemente", "quiza", "no"]
    choices = ["propuesta", "alternativa", "nada"]
    commitments = ["pagaria_ya", "se_apuntaria", "nada"]
    answers = []
    for i, aid in enumerate(req.meta.get("_agent_ids", [])):
        pays = req.rng.random() < 0.4
        answers.append(
            {
                "agent_id": aid,
                "role": "cliente" if i % 2 == 0 else "observador",
                "current_alternative": "lo de siempre",
                "reason": "respuesta simulada",
                "deal_breaker": "precio",
                "would_change_mind": "ver casos reales",
                "choice": choices[req.rng.randint(0, 2)],
                "intent": intents[req.rng.randint(0, 3)],
                "purchase_probability": req.rng.randint(0, 10),
                "would_pay": "si" if pays else "no",
                "pay_certainty": req.rng.randint(5, 10) if pays else 0,
                "max_price": float(req.rng.choice([0, 9, 19, 29, 39])),
                "fair_price": "lo propuesto",
                "commitment": commitments[req.rng.randint(0, 2)],
            }
        )
    return {"reference_price": 29.0, "price_unit": "€/mes", "answers": answers}
