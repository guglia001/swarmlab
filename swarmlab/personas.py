"""Generación de la población de agentes.

Dos modos (``STANCE_MODE``):

- ``impresion`` (por defecto): la postura inicial **se mide**. El LLM crea personas con su vida,
  su situación y lo que usan hoy, sin opinión sobre la propuesta (huecos por segmento, franja de
  edad y entorno). Después cada persona lee la propuesta y da su primera impresión en personaje
  (postura, implicación y reacción) antes de hablar con nadie. Así la distribución inicial depende
  de la idea y se puede comparar entre ideas.
- ``cuotas``: la postura inicial **se fija** con un plan de cuotas (postura × implicación ×
  segmento) con mínimos garantizados (≥15 % escépticos, ≥10 % poco interesados). Sirve como test
  de estrés para garantizar objeciones, pero entonces la distribución inicial es un dato de entrada
  y no dice nada de la idea.

En ambos, el LLM rellena cada lote de huecos recibiendo los arquetipos ya creados para no repetirlos,
Python normaliza (rangos, handles únicos, seguidores) y mide la diversidad; los duplicados se regeneran
una vez.
"""

from __future__ import annotations

import asyncio
import math
import random
import re
import unicodedata
from collections import Counter
from collections.abc import Callable
from itertools import combinations
from statistics import mean, pstdev
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from swarmlab import prompts
from swarmlab.config import STANCE_MODES, Settings
from swarmlab.llm import LLM, CallCache, FakeRequest, cached_complete, fake_responder, per_agent_schema
from swarmlab.ontology import AudienceSegment, Ontology, ScenarioBrief, compact_brief

Involvement = Literal["alta", "media", "baja"]

# nombre, etiqueta, rango de postura, peso por defecto
BUCKETS: list[tuple[str, str, tuple[float, float], float]] = [
    ("critico", "crítico duro", (-1.0, -0.6), 0.10),
    ("esceptico", "escéptico", (-0.6, -0.2), 0.16),
    ("neutral", "neutral o curioso", (-0.2, 0.2), 0.24),
    ("simpatizante", "simpatizante", (0.2, 0.6), 0.22),
    ("entusiasta", "entusiasta", (0.6, 1.0), 0.14),
    ("desinteresado", "poco interesado en el tema", (-0.15, 0.15), 0.14),
]
SKEPTIC_BUCKETS = ("critico", "esceptico")
MIN_SKEPTIC_SHARE = 0.15
MIN_DISENGAGED_SHARE = 0.10
ACTIVITY_RANGES: dict[Involvement, tuple[float, float]] = {
    "alta": (0.6, 0.95),
    "media": (0.35, 0.65),
    "baja": (0.08, 0.3),
}
INVOLVEMENT_BY_BUCKET: dict[str, list[tuple[Involvement, float]]] = {
    "critico": [("alta", 0.7), ("media", 0.3)],
    "esceptico": [("alta", 0.25), ("media", 0.6), ("baja", 0.15)],
    "neutral": [("media", 0.5), ("baja", 0.5)],
    "simpatizante": [("alta", 0.25), ("media", 0.6), ("baja", 0.15)],
    "entusiasta": [("alta", 0.7), ("media", 0.3)],
    "desinteresado": [("baja", 1.0)],
}
# modo impresión: diversidad demográfica sin tocar la opinión (pesos aproximados de la población
# adulta activa en redes en España)
AGE_BANDS: list[tuple[str, float]] = [("18–29", 0.22), ("30–44", 0.30), ("45–64", 0.32), ("65 o más", 0.16)]
ENVIRONMENTS: list[tuple[str, float]] = [
    ("gran ciudad", 0.45),
    ("ciudad mediana", 0.35),
    ("pueblo o zona rural", 0.20),
]
INFLUENCER_SHARE = 0.1


def activity_range(involvement: Involvement, influencer: bool) -> tuple[float, float]:
    lo, hi = ACTIVITY_RANGES[involvement]
    return (min(hi, lo + 0.15), 0.98) if influencer else (lo, hi)


def stance_bucket_for(stance: float, involvement: str) -> str:
    """Grupo de postura de una postura medida (mismos cortes que ``agent.stance_label``)."""
    if involvement == "baja" and abs(stance) < 0.2:
        return "desinteresado"
    if stance <= -0.6:
        return "critico"
    if stance <= -0.2:
        return "esceptico"
    if stance < 0.2:
        return "neutral"
    if stance < 0.6:
        return "simpatizante"
    return "entusiasta"


# --------------------------------------------------------------------------- modelos


class BigFive(BaseModel):
    openness: float = Field(ge=0, le=1)
    conscientiousness: float = Field(ge=0, le=1)
    extraversion: float = Field(ge=0, le=1)
    agreeableness: float = Field(ge=0, le=1)
    neuroticism: float = Field(ge=0, le=1)

    def short(self) -> str:
        return (
            f"O{self.openness:.1f} C{self.conscientiousness:.1f} E{self.extraversion:.1f} "
            f"A{self.agreeableness:.1f} N{self.neuroticism:.1f}"
        )


class PersonaSlot(BaseModel):
    """Hueco de la población. En modo ``impresion`` solo fija segmento, edad y entorno."""

    index: int
    segment: str
    segment_description: str
    bucket: str = "abierto"
    bucket_label: str = "sin fijar"
    stance_min: float = -1.0
    stance_max: float = 1.0
    involvement: Involvement = "media"
    influencer: bool = False
    age_band: str = ""
    environment: str = ""

    @property
    def activity_range(self) -> tuple[float, float]:
        return activity_range(self.involvement, self.influencer)


class PersonaDraft(BaseModel):
    slot: int
    name: str
    handle: str
    archetype: str
    bio: str
    age: int = Field(ge=14, le=95)
    occupation: str
    values: list[str]
    traits: BigFive
    initial_stance: float = Field(ge=-1, le=1)
    activity: float = Field(ge=0, le=1)
    writing_style: str
    entity_links: list[str] = Field(default_factory=list)


class PersonaBatch(BaseModel):
    personas: list[PersonaDraft]


class ProfileDraft(BaseModel):
    """Persona sin opinión sobre la propuesta (modo impresión): se medirá después."""

    slot: int
    name: str
    handle: str
    archetype: str = Field(description="4–9 palabras sobre su vida y situación, no sobre su opinión")
    bio: str
    age: int = Field(ge=14, le=95)
    occupation: str
    values: list[str]
    traits: BigFive
    current_solution: str = Field(description="qué hace o usa hoy para lo que la propuesta resuelve")
    writing_style: str
    entity_links: list[str] = Field(default_factory=list)


class ProfileBatch(BaseModel):
    personas: list[ProfileDraft]


class Impression(BaseModel):
    agent_id: str
    reaction: str = Field(description="primera reacción en primera persona, con su voz (máx. 200 caracteres)")
    reason: str = Field(description="motivo principal, en una frase")
    involvement: Involvement = Field(description="cuánto le toca o le importa el tema")
    stance: float = Field(ge=-1, le=1, description="−1 en contra … 0 indiferente … +1 a favor")


class ImpressionBatch(BaseModel):
    impressions: list[Impression]


class Persona(BaseModel):
    id: str
    name: str
    handle: str
    archetype: str
    bio: str
    age: int
    occupation: str
    values: list[str]
    traits: BigFive
    initial_stance: float = Field(ge=-1, le=1)
    activity: float = Field(ge=0, le=1)
    followers_initial: int = Field(ge=0)
    writing_style: str
    entity_links: list[str] = Field(default_factory=list)
    segment: str
    stance_bucket: str
    involvement: Involvement
    influencer: bool = False
    current_solution: str = ""
    first_impression: str = ""

    @field_validator("handle")
    @classmethod
    def _handle(cls, v: str) -> str:
        if not re.fullmatch(r"[a-z0-9_]{3,24}", v):
            raise ValueError(f"handle inválido {v!r}")
        return v

    def card(self, involvement: bool = True) -> str:
        """Perfil compacto para los prompts."""
        return (
            f"@{self.handle} ({self.name}, {self.age}, {self.occupation}). {self.bio} "
            f"Valores: {', '.join(self.values)}. Rasgos {self.traits.short()}. Estilo: {self.writing_style}."
            + (f" Hoy: {self.current_solution}." if self.current_solution else "")
            + (f" Implicación {self.involvement}." if involvement else "")
        )


class DiversityReport(BaseModel):
    n: int
    mean_bio_similarity: float
    max_bio_similarity: float
    most_similar_pair: tuple[str, str] | None
    duplicate_archetypes: list[str]
    unique_occupations: int
    age_mean: float
    age_std: float
    stance_buckets: dict[str, int]
    involvement: dict[str, int]
    skeptic_share: float
    disengaged_share: float
    score: float
    warnings: list[str]
    stance_mode: str = "cuotas"


# --------------------------------------------------------------------------- plan de huecos


def _largest_remainder(weights: list[float], total: int) -> list[int]:
    s = sum(weights) or 1.0
    raw = [w / s * total for w in weights]
    counts = [math.floor(x) for x in raw]
    order = sorted(range(len(raw)), key=lambda i: raw[i] - counts[i], reverse=True)
    for i in order[: total - sum(counts)]:
        counts[i] += 1
    return counts


def bucket_counts(n: int) -> dict[str, int]:
    names = [b[0] for b in BUCKETS]
    counts = dict(zip(names, _largest_remainder([b[3] for b in BUCKETS], n), strict=True))
    need_skeptic = math.ceil(MIN_SKEPTIC_SHARE * n)
    need_disengaged = math.ceil(MIN_DISENGAGED_SHARE * n)
    donors = ["neutral", "simpatizante", "entusiasta"]

    def take_from_donor() -> None:
        donor = max(donors, key=lambda d: counts[d])
        counts[donor] -= 1

    while counts["critico"] + counts["esceptico"] < need_skeptic:
        take_from_donor()
        counts["esceptico"] += 1
    while counts["desinteresado"] < need_disengaged:
        take_from_donor()
        counts["desinteresado"] += 1
    return counts


def _default_segments(segments: list[AudienceSegment]) -> list[AudienceSegment]:
    return segments or [
        AudienceSegment(
            name="Público general", description="población general", weight=1.0, typical_stance=0.0
        )
    ]


def _pick_influencers(items: list[Any], n_total: int, stance: Callable[[Any], float]) -> list[Any]:
    """~10 % de influencers entre los muy implicados, repartidos por todo el rango de posturas."""
    n_infl = max(1, round(INFLUENCER_SHARE * n_total))
    ordered = sorted(items, key=stance)
    step = max(1, len(ordered) // n_infl)
    return ordered[::step][:n_infl]


def plan_slots(n: int, segments: list[AudienceSegment], rng: random.Random) -> list[PersonaSlot]:
    """Modo cuotas: combina cuotas de postura y de segmento (ordenando por postura típica)."""
    segments = _default_segments(segments)
    seg_counts = _largest_remainder([max(s.weight, 0.01) for s in segments], n)
    seg_slots = [
        (s, s.typical_stance + rng.gauss(0, 0.35))
        for s, c in zip(segments, seg_counts, strict=True)
        for _ in range(c)
    ]
    seg_slots.sort(key=lambda x: x[1])
    counts = bucket_counts(n)
    centers = {name: (lo + hi) / 2 for name, _, (lo, hi), _ in BUCKETS}
    bucket_list = [name for name in counts for _ in range(counts[name])]
    # los desinteresados se reparten por todo el rango para no concentrarlos en un segmento
    noisy = [
        (b, centers[b] + (rng.uniform(-0.9, 0.9) if b == "desinteresado" else rng.gauss(0, 0.1)))
        for b in bucket_list
    ]
    noisy.sort(key=lambda x: x[1])
    labels = {name: (label, rng_range) for name, label, rng_range, _ in BUCKETS}
    slots: list[PersonaSlot] = []
    for i, ((seg, _), (bucket, _)) in enumerate(zip(seg_slots, noisy, strict=True)):
        options = INVOLVEMENT_BY_BUCKET[bucket]
        involvement: Involvement = rng.choices([o for o, _ in options], weights=[w for _, w in options])[0]
        label, (lo, hi) = labels[bucket]
        slots.append(
            PersonaSlot(
                index=i + 1,
                bucket=bucket,
                bucket_label=label,
                stance_min=lo,
                stance_max=hi,
                involvement=involvement,
                segment=seg.name,
                segment_description=seg.description,
            )
        )
    engaged = [s for s in slots if s.involvement == "alta"] or slots
    for s in _pick_influencers(sorted(engaged, key=lambda s: s.index), n, lambda s: s.stance_min):
        s.influencer = True
    rng.shuffle(slots)
    for i, s in enumerate(slots):
        s.index = i + 1
    return slots


def plan_open_slots(n: int, segments: list[AudienceSegment], rng: random.Random) -> list[PersonaSlot]:
    """Modo impresión: huecos por segmento (según su peso) con franja de edad y entorno, sin postura."""
    segments = _default_segments(segments)
    seg_counts = _largest_remainder([max(s.weight, 0.01) for s in segments], n)
    slots = [
        PersonaSlot(
            index=0,
            segment=s.name,
            segment_description=s.description,
            age_band=rng.choices([a for a, _ in AGE_BANDS], weights=[w for _, w in AGE_BANDS])[0],
            environment=rng.choices([e for e, _ in ENVIRONMENTS], weights=[w for _, w in ENVIRONMENTS])[0],
        )
        for s, c in zip(segments, seg_counts, strict=True)
        for _ in range(c)
    ]
    rng.shuffle(slots)
    for i, s in enumerate(slots):
        s.index = i + 1
    return slots


# --------------------------------------------------------------------------- diversidad

_STOP = set(
    [
        "a",
        "al",
        "algo",
        "con",
        "como",
        "de",
        "del",
        "el",
        "en",
        "es",
        "esta",
        "este",
        "la",
        "las",
        "lo",
        "los",
        "me",
        "mi",
        "mis",
        "muy",
        "no",
        "o",
        "para",
        "pero",
        "por",
        "que",
        "se",
        "sin",
        "su",
        "sus",
        "te",
        "tu",
        "un",
        "una",
        "y",
        "ya",
        "yo",
        "the",
        "and",
        "of",
        "to",
        "in",
        "is",
        "for",
        "with",
        "on",
        "my",
        "i",
    ]
)


def _tokens(text: str) -> list[str]:
    norm = unicodedata.normalize("NFKD", text.lower()).encode("ascii", "ignore").decode()
    return [w for w in re.findall(r"[a-z0-9]+", norm) if len(w) > 2 and w not in _STOP]


def _tfidf(docs: list[list[str]]) -> list[dict[str, float]]:
    df = Counter(w for d in docs for w in set(d))
    n = len(docs)
    vecs = []
    for d in docs:
        tf = Counter(d)
        vec = {w: (c / max(len(d), 1)) * math.log((1 + n) / (1 + df[w]) + 1) for w, c in tf.items()}
        norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
        vecs.append({w: v / norm for w, v in vec.items()})
    return vecs


def _cos(a: dict[str, float], b: dict[str, float]) -> float:
    if len(a) > len(b):
        a, b = b, a
    return sum(v * b.get(w, 0.0) for w, v in a.items())


def normalize_archetype(text: str) -> str:
    return " ".join(sorted(set(_tokens(text))))


def diversity_report(personas: list[Persona], stance_mode: str = "cuotas") -> DiversityReport:
    n = len(personas)
    vecs = _tfidf([_tokens(f"{p.bio} {p.archetype} {p.occupation}") for p in personas])
    sims: list[tuple[float, int, int]] = [
        (_cos(vecs[i], vecs[j]), i, j) for i, j in combinations(range(n), 2)
    ]
    mean_sim = mean(s for s, _, _ in sims) if sims else 0.0
    top = max(sims, default=(0.0, 0, 0))
    arche = Counter(normalize_archetype(p.archetype) for p in personas)
    dupes = [p.archetype for p in personas if arche[normalize_archetype(p.archetype)] > 1]
    buckets = Counter(p.stance_bucket for p in personas)
    involvement = Counter(p.involvement for p in personas)
    skeptic = sum(1 for p in personas if p.initial_stance <= -0.2) / max(n, 1)
    disengaged = sum(1 for p in personas if p.involvement == "baja") / max(n, 1)
    warnings: list[str] = []
    if mean_sim > 0.12:
        warnings.append(f"similitud media de bios alta ({mean_sim:.2f}): población poco diversa")
    if top[0] > 0.5 and sims:
        warnings.append(
            f"bios casi iguales: @{personas[top[1]].handle} y @{personas[top[2]].handle} ({top[0]:.2f})"
        )
    if dupes:
        warnings.append(f"{len(dupes)} arquetipos repetidos")
    if stance_mode == "impresion":
        # aquí la postura se mide: pocos escépticos es un resultado, no un fallo de generación
        if skeptic < MIN_SKEPTIC_SHARE:
            warnings.append(
                f"solo {skeptic:.0%} de escépticos en la primera impresión: habrá pocas objeciones "
                "(STANCE_MODE=cuotas fuerza un mínimo para un test de estrés)"
            )
    else:
        if skeptic < MIN_SKEPTIC_SHARE:
            warnings.append(f"solo {skeptic:.0%} de escépticos (mínimo {MIN_SKEPTIC_SHARE:.0%})")
        if disengaged < MIN_DISENGAGED_SHARE:
            warnings.append(f"solo {disengaged:.0%} poco interesados (mínimo {MIN_DISENGAGED_SHARE:.0%})")
    occupations = len({p.occupation.strip().lower() for p in personas})
    score = max(
        0.0, min(1.0, (1 - mean_sim) * (1 - len(dupes) / max(n, 1)) * min(1.0, occupations / max(n * 0.8, 1)))
    )
    return DiversityReport(
        n=n,
        mean_bio_similarity=round(mean_sim, 3),
        max_bio_similarity=round(top[0], 3),
        most_similar_pair=(personas[top[1]].handle, personas[top[2]].handle) if sims else None,
        duplicate_archetypes=sorted(set(dupes)),
        unique_occupations=occupations,
        age_mean=round(mean(p.age for p in personas), 1) if personas else 0.0,
        age_std=round(pstdev(p.age for p in personas), 1) if n > 1 else 0.0,
        stance_buckets=dict(buckets),
        involvement=dict(involvement),
        skeptic_share=round(skeptic, 3),
        disengaged_share=round(disengaged, 3),
        score=round(score, 3),
        warnings=warnings,
        stance_mode=stance_mode,
    )


# --------------------------------------------------------------------------- generación


def _clean_handle(raw: str, name: str, taken: set[str]) -> str:
    base = unicodedata.normalize("NFKD", (raw or name).lower()).encode("ascii", "ignore").decode()
    base = re.sub(r"[^a-z0-9_]+", "_", base).strip("_")[:20] or "usuario"
    if len(base) < 3:
        base = (base + "_usr")[:20]
    handle, n = base, 2
    while handle in taken:
        suffix = str(n)
        handle = f"{base[: 20 - len(suffix)]}{suffix}"
        n += 1
    taken.add(handle)
    return handle


def _followers(involvement: Involvement, influencer: bool, extraversion: float, rng: random.Random) -> int:
    base = {"alta": 900, "media": 250, "baja": 60}[involvement] * (0.5 + extraversion)
    value = rng.lognormvariate(math.log(base), 0.7)
    if influencer:
        value *= rng.uniform(15, 40)
    return int(min(value, 500_000))


def _finalize(
    draft: PersonaDraft | ProfileDraft,
    slot: PersonaSlot,
    pid: str,
    taken: set[str],
    known: set[str],
    rng: random.Random,
) -> Persona:
    common = {
        "id": pid,
        "name": draft.name.strip(),
        "handle": _clean_handle(draft.handle, draft.name, taken),
        "archetype": draft.archetype.strip(),
        "bio": draft.bio.strip()[:220],
        "age": draft.age,
        "occupation": draft.occupation.strip(),
        "values": [v.strip() for v in draft.values if v.strip()][:4],
        "traits": draft.traits,
        "writing_style": draft.writing_style.strip(),
        "entity_links": [e for e in draft.entity_links if e in known][:3],
        "segment": slot.segment,
    }
    if isinstance(draft, ProfileDraft):
        # postura, implicación, actividad y seguidores llegan con la primera impresión
        return Persona(
            **common,
            initial_stance=0.0,
            activity=0.5,
            followers_initial=0,
            stance_bucket="pendiente",
            involvement="media",
            current_solution=draft.current_solution.strip()[:200],
        )
    stance = min(max(draft.initial_stance, slot.stance_min), slot.stance_max)
    alo, ahi = slot.activity_range
    return Persona(
        **common,
        initial_stance=round(stance, 2),
        activity=round(min(max(draft.activity, alo), ahi), 2),
        followers_initial=_followers(slot.involvement, slot.influencer, draft.traits.extraversion, rng),
        stance_bucket=slot.bucket,
        involvement=slot.involvement,
        influencer=slot.influencer,
    )


def _slot_line(s: PersonaSlot) -> str:
    if s.bucket == "abierto":
        return (
            f"- hueco {s.index}: segmento «{s.segment}» ({s.segment_description}); "
            f"edad orientativa {s.age_band or 'libre'}; entorno: {s.environment or 'libre'}"
        )
    alo, ahi = s.activity_range
    return (
        f"- hueco {s.index}: segmento «{s.segment}» ({s.segment_description}); postura {s.bucket_label} "
        f"[{s.stance_min:+.2f}, {s.stance_max:+.2f}]; implicación {s.involvement}; actividad [{alo:.2f}, {ahi:.2f}]"
        + ("; INFLUENCER con mucha audiencia" if s.influencer else "")
    )


async def _generate_batch(
    llm: LLM,
    slots: list[PersonaSlot],
    existing: list[Persona],
    avoid: list[str],
    brief: ScenarioBrief,
    ont: Ontology,
    question: str,
    language: str,
    settings: Settings,
    cache: CallCache | None,
    attempt: int,
    stance_mode: str,
) -> PersonaBatch | ProfileBatch:
    open_mode = stance_mode == "impresion"
    lines = [f"- {p.archetype} — {p.occupation}, {p.age} — {p.name}" for p in existing] + [
        f"- {a}" for a in avoid
    ]
    prompt = prompts.render(
        "personas_open_batch" if open_mode else "personas_batch",
        brief=compact_brief(brief),
        question=question,
        language=language,
        segments="\n".join(
            f"- {s.name}: {s.description}"
            + ("" if open_mode else f" (postura típica {s.typical_stance:+.1f})")
            for s in brief.audience_segments
        ),
        entity_ids=", ".join(f"{e.id} ({e.name})" for e in ont.entities),
        existing="\n".join(lines) or "- (ninguno todavía)",
        count=len(slots),
        slots="\n".join(_slot_line(s) for s in slots),
    )
    schema: type[PersonaBatch] | type[ProfileBatch] = ProfileBatch if open_mode else PersonaBatch
    return await cached_complete(
        cache,
        f"personas.{'profile' if open_mode else 'batch'}.{attempt}",
        llm,
        prompt,
        schema=schema,
        system=prompts.load("personas_open_system" if open_mode else "personas_system"),
        model=settings.model_medium,
        effort=settings.effort_medium,
        purpose="personas.profile" if open_mode else "personas.batch",
        meta={"slots": len(slots), "_slots": [s.model_dump() for s in slots], "_existing": len(existing)},
    )


async def generate_personas(
    llm: LLM,
    brief: ScenarioBrief,
    ont: Ontology,
    n: int,
    question: str,
    language: str,
    settings: Settings,
    rng: random.Random,
    cache: CallCache | None = None,
    on_step: Callable[[str], None] | None = None,
    stance_mode: str = "cuotas",
) -> tuple[list[Persona], DiversityReport]:
    if not 10 <= n <= 200:
        raise ValueError("la población debe estar entre 10 y 200 agentes")
    if stance_mode not in STANCE_MODES:
        raise ValueError(f"STANCE_MODE debe ser uno de {STANCE_MODES}")
    open_mode = stance_mode == "impresion"
    slots = (
        plan_open_slots(n, brief.audience_segments, rng)
        if open_mode
        else plan_slots(n, brief.audience_segments, rng)
    )
    known = {e.id for e in ont.entities}
    taken: set[str] = set()
    by_slot: dict[int, Persona] = {}
    pending = list(slots)
    for attempt in range(2):  # 2.º intento: solo huecos que faltan o duplicados
        avoid: list[str] = []
        if attempt == 1:
            report = diversity_report(list(by_slot.values()), stance_mode)
            dup_norm = {normalize_archetype(a) for a in report.duplicate_archetypes}
            seen: set[str] = set()
            for idx in sorted(by_slot):
                key = normalize_archetype(by_slot[idx].archetype)
                if key in dup_norm and key in seen:
                    avoid.append(by_slot[idx].archetype)
                    taken.discard(by_slot[idx].handle)
                    del by_slot[idx]
                seen.add(key)
            pending = [s for s in slots if s.index not in by_slot]
            if not pending:
                break
        for start in range(0, len(pending), settings.persona_batch):
            batch = pending[start : start + settings.persona_batch]
            existing = [by_slot[i] for i in sorted(by_slot)]
            result = await _generate_batch(
                llm,
                batch,
                existing,
                avoid,
                brief,
                ont,
                question,
                language,
                settings,
                cache,
                attempt,
                stance_mode,
            )
            drafts = {d.slot: d for d in result.personas}
            leftovers = [d for d in result.personas if d.slot not in {s.index for s in batch}]
            for s in batch:
                draft = drafts.get(s.index) or (leftovers.pop(0) if leftovers else None)
                if draft is not None:
                    by_slot[s.index] = _finalize(draft, s, "", taken, known, rng)
            if on_step:
                on_step(f"personas {len(by_slot)}/{n}")
    missing = [s.index for s in slots if s.index not in by_slot]
    if missing:
        raise RuntimeError(f"el modelo no generó personas para los huecos {missing}")
    personas = [by_slot[s.index] for s in slots]
    for i, p in enumerate(personas):
        p.id = f"a{i + 1:02d}" if n < 100 else f"a{i + 1:03d}"
    if open_mode:
        personas = await first_impressions(
            llm, personas, brief, language, settings, rng, cache, on_step=on_step
        )
    return personas, diversity_report(personas, stance_mode)


# --------------------------------------------------------------------------- primera impresión


def impression_schema(agent_ids: list[str]) -> type[ImpressionBatch]:
    return per_agent_schema(ImpressionBatch, "impressions", Impression, agent_ids)


async def first_impressions(
    llm: LLM,
    personas: list[Persona],
    brief: ScenarioBrief,
    language: str,
    settings: Settings,
    rng: random.Random,
    cache: CallCache | None = None,
    *,
    keep_social: bool = False,
    on_step: Callable[[str], None] | None = None,
) -> list[Persona]:
    """Primera impresión en personaje, antes de cualquier conversación: fija postura e implicación.

    Es la medida «en frío» de cómo cae la propuesta en cada persona (una llamada al modelo medio por
    lote de ``MEMORY_BATCH``). La actividad sale de la implicación. Con ``keep_social`` (población
    reutilizada de otro run para un A/B) se conservan influencers y seguidores: cambia la opinión
    sobre la propuesta, no la posición social de cada persona.
    """
    proposal = compact_brief(brief, 1500)
    size = max(settings.memory_batch, 1)
    batches = [personas[i : i + size] for i in range(0, len(personas), size)]

    async def ask(batch: list[Persona], idx: int) -> ImpressionBatch:
        ids = [p.id for p in batch]
        people = "\n\n".join(
            f"### PERSONA {p.id} — @{p.handle}\nPerfil: {p.card(involvement=False)}\nSegmento: {p.segment}"
            for p in batch
        )
        prompt = prompts.render(
            "impression_batch", proposal=proposal, language=language, count=len(batch), people=people
        )
        return await cached_complete(
            cache,
            "personas.impression",
            llm,
            prompt,
            schema=impression_schema(ids),
            system=prompts.load("impression_system"),
            model=settings.model_medium,
            effort=settings.effort_medium,
            purpose="personas.impression",
            meta={"batch": idx, "_agent_ids": ids},
        )

    results = await asyncio.gather(*(ask(b, i) for i, b in enumerate(batches)))
    found = {imp.agent_id: imp for res in results for imp in res.impressions}
    out: list[Persona] = []
    for p in personas:
        imp = found[p.id]
        stance = round(imp.stance, 2)
        reaction = " ".join(imp.reaction.split())[:220]
        out.append(
            p.model_copy(
                update={
                    "initial_stance": stance,
                    "involvement": imp.involvement,
                    "stance_bucket": stance_bucket_for(stance, imp.involvement),
                    "first_impression": f"{reaction} (motivo: {' '.join(imp.reason.split())[:160]})",
                }
            )
        )
    if not keep_social:
        engaged = [p for p in out if p.involvement == "alta"] or out
        chosen = {p.id for p in _pick_influencers(engaged, len(out), lambda p: (p.initial_stance, p.id))}
        out = [p.model_copy(update={"influencer": p.id in chosen}) for p in out]
    final: list[Persona] = []
    for p in out:
        lo, hi = activity_range(p.involvement, p.influencer)
        update: dict[str, Any] = {"activity": round(rng.uniform(lo, hi), 2)}
        if not keep_social:
            update["followers_initial"] = _followers(p.involvement, p.influencer, p.traits.extraversion, rng)
        final.append(p.model_copy(update=update))
    if on_step:
        on_step(f"primera impresión de {len(final)} personas")
    return final


# --------------------------------------------------------------------------- respuestas simuladas (dry-run)

_FIRST = ["Lucía", "Javier", "Carmen", "Andrés", "Nuria", "Rafael", "Inés", "Tomás", "Marta", "Óscar", "Elena", "Iker",
          "Rocío", "Samuel", "Pilar", "Hugo", "Aitana", "Bernardo", "Yolanda", "Khalid", "Daniela", "Ferran", "Mónica", "Wei"]  # fmt: skip
_LAST = ["García", "Ortega", "Ruiz", "Navarro", "Sanz", "Iglesias", "Vidal", "Molina", "Castro", "Herrero", "Romero",
         "Pastor", "Delgado", "Cano", "Bravo", "Prieto"]  # fmt: skip
_JOBS = ["fontanero autónomo", "diseñadora freelance", "dueña de una cafetería", "asesor fiscal", "estudiante de ADE",
         "jubilado", "programadora", "camionero autónomo", "abogada laboralista", "fotógrafo de bodas", "gerente de pyme textil",
         "community manager", "profesora de instituto", "electricista", "consultora de marketing", "agricultor",
         "periodista económica", "fisioterapeuta", "traductora", "taxista", "influencer de finanzas", "arquitecta",
         "repartidor", "contable en gestoría"]  # fmt: skip


def _fake_identity(i: int, req: FakeRequest) -> dict[str, Any]:
    first, last = _FIRST[i % len(_FIRST)], _LAST[(i * 7 + req.rng.randint(0, 3)) % len(_LAST)]
    return {
        "name": f"{first} {last}",
        "handle": f"{first}_{last}{i}".lower(),
        "age": 22 + (i * 5) % 50,
        "occupation": _JOBS[i % len(_JOBS)],
        "values": ["autonomía", "precio justo", "confianza"][: 2 + i % 2],
        "traits": BigFive(
            openness=round(req.rng.random(), 2),
            conscientiousness=round(req.rng.random(), 2),
            extraversion=round(req.rng.random(), 2),
            agreeableness=round(req.rng.random(), 2),
            neuroticism=round(req.rng.random(), 2),
        ),
        "writing_style": ["directo y breve", "irónico", "formal", "con emojis"][i % 4],
    }


@fake_responder("personas.batch")
def _fake_personas(req: FakeRequest) -> PersonaBatch:
    slots = [PersonaSlot.model_validate(s) for s in req.meta.get("_slots", [])]
    offset = int(req.meta.get("_existing", 0))
    out = []
    for k, s in enumerate(slots):
        i = offset + k
        ident = _fake_identity(i, req)
        job = ident["occupation"]
        alo, ahi = s.activity_range
        out.append(
            PersonaDraft(
                slot=s.index,
                archetype=f"{job} {s.bucket_label} del segmento {s.segment} número {i}",
                bio=f"{job.capitalize()} ({s.segment}). Opino sin filtro sobre lo que me afecta, caso {i}.",
                initial_stance=round(req.rng.uniform(s.stance_min, s.stance_max), 2),
                activity=round(req.rng.uniform(alo, ahi), 2),
                **ident,
            )
        )
    return PersonaBatch(personas=out)


@fake_responder("personas.profile")
def _fake_profiles(req: FakeRequest) -> ProfileBatch:
    slots = [PersonaSlot.model_validate(s) for s in req.meta.get("_slots", [])]
    offset = int(req.meta.get("_existing", 0))
    out = []
    for k, s in enumerate(slots):
        i = offset + k
        ident = _fake_identity(i, req)
        job = ident["occupation"]
        out.append(
            ProfileDraft(
                slot=s.index,
                archetype=f"{job} de {s.environment or 'algún sitio'} del segmento {s.segment} número {i}",
                bio=f"{job.capitalize()} ({s.segment}). Cuento mi día a día sin filtro, caso {i}.",
                current_solution="lo resuelvo como siempre, a mano",
                **ident,
            )
        )
    return ProfileBatch(personas=out)


@fake_responder("personas.impression")
def _fake_impressions(req: FakeRequest) -> dict[str, Any]:
    levels: list[Involvement] = ["alta", "media", "baja"]
    return {
        "impressions": [
            {
                "agent_id": aid,
                "reaction": f"[dry-run] primera reacción de {aid}",
                "reason": "respuesta simulada",
                "involvement": levels[req.rng.randint(0, 2)],
                "stance": round(req.rng.uniform(-0.8, 0.8), 2),
            }
            for aid in req.meta.get("_agent_ids", [])
        ]
    }


def persona_table_rows(personas: list[Persona]) -> list[dict[str, Any]]:
    return [
        {
            "id": p.id,
            "handle": p.handle,
            "name": p.name,
            "age": p.age,
            "occupation": p.occupation,
            "segment": p.segment,
            "bucket": p.stance_bucket,
            "stance": p.initial_stance,
            "activity": p.activity,
            "followers": p.followers_initial,
            "influencer": p.influencer,
            "archetype": p.archetype,
        }
        for p in personas
    ]
