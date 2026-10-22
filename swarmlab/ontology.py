"""Ontología del escenario: entidades, relaciones, hechos ancla y tensiones, más el brief.

Flujo: cada trozo del documento → ``ChunkExtraction`` (modelo pesado) → deduplicación por nombre
normalizado → una pasada de fusión con el LLM (``MergePlan``) → ``Ontology`` → ``ScenarioBrief``.
"""

from __future__ import annotations

import asyncio
import itertools
import re
import unicodedata
from collections import Counter
from collections.abc import Callable
from statistics import mean
from typing import Any, Literal

from pydantic import BaseModel, Field

from swarmlab import prompts
from swarmlab.config import Settings
from swarmlab.ingest import Chunk, Document
from swarmlab.llm import LLM, CallCache, FakeRequest, cached_complete, fake_responder

EntityType = Literal[
    "person", "group", "organization", "institution", "competitor", "media", "product", "other"
]
ENTITY_TYPE_HELP = (
    "person (persona concreta), group (colectivo de personas), organization (empresa u organización), "
    "institution (organismo público o regulador), competitor (competidor), media (medio o creador de "
    "contenido), product (producto o servicio), other"
)

# --------------------------------------------------------------------------- modelos de dominio


class Entity(BaseModel):
    id: str
    name: str
    type: EntityType
    description: str
    interests: list[str] = Field(default_factory=list)
    initial_stance: float = Field(0.0, ge=-1, le=1)
    influence: float = Field(0.5, ge=0, le=1)
    aliases: list[str] = Field(default_factory=list)
    chunk_ids: list[str] = Field(default_factory=list)


class Relation(BaseModel):
    source: str
    target: str
    type: str
    weight: float = Field(0.5, ge=0, le=1)
    description: str = ""


class Fact(BaseModel):
    id: str
    statement: str
    chunk_id: str
    importance: float = Field(0.5, ge=0, le=1)


class Tension(BaseModel):
    id: str
    description: str
    parties: list[str]
    stakes: str = ""
    intensity: float = Field(0.5, ge=0, le=1)


class Ontology(BaseModel):
    title: str
    language: str
    entities: list[Entity]
    relations: list[Relation]
    facts: list[Fact]
    tensions: list[Tension]

    def entity(self, entity_id: str) -> Entity | None:
        return next((e for e in self.entities if e.id == entity_id), None)


# --------------------------------------------------------------------------- esquemas de salida del LLM


class ExtractedEntity(BaseModel):
    name: str = Field(description="nombre canónico")
    type: EntityType
    description: str = Field(description="1-2 frases")
    interests: list[str] = Field(default_factory=list, description="2-5 intereses o motivaciones")
    initial_stance: float = Field(ge=-1, le=1, description="postura hacia la propuesta del documento")
    influence: float = Field(ge=0, le=1)
    aliases: list[str] = Field(default_factory=list, description="otros nombres o siglas")


class ExtractedRelation(BaseModel):
    source: str = Field(description="name de la entidad origen")
    target: str = Field(description="name de la entidad destino")
    type: str
    weight: float = Field(ge=0, le=1, description="intensidad de la relación")
    description: str = ""


class ExtractedFact(BaseModel):
    statement: str
    importance: float = Field(ge=0, le=1)


class ExtractedTension(BaseModel):
    description: str
    parties: list[str] = Field(description="names de las entidades enfrentadas")
    stakes: str = Field(description="qué está en juego")
    intensity: float = Field(ge=0, le=1)


class ChunkExtraction(BaseModel):
    entities: list[ExtractedEntity]
    relations: list[ExtractedRelation] = Field(default_factory=list)
    facts: list[ExtractedFact] = Field(default_factory=list)
    tensions: list[ExtractedTension] = Field(default_factory=list)


class MergeGroup(BaseModel):
    members: list[int] = Field(description="índices de la lista que son la misma entidad")
    canonical: str = Field(description="nombre canónico del grupo")


class MergePlan(BaseModel):
    groups: list[MergeGroup] = Field(default_factory=list, description="grupos de duplicados")


class AudienceSegment(BaseModel):
    name: str
    description: str
    weight: float = Field(ge=0, le=1)
    typical_stance: float = Field(ge=-1, le=1)
    entity_ids: list[str] = Field(default_factory=list, description="ids de entidades relacionadas")


class ScenarioBrief(BaseModel):
    title: str
    one_liner: str
    summary: str
    proposal: str
    key_facts: list[str]
    stakeholders: list[str]
    tensions: list[str]
    uncertainties: list[str]
    audience_segments: list[AudienceSegment]


# --------------------------------------------------------------------------- normalización


_ARTICLES = {"el", "la", "los", "las", "un", "una", "unos", "unas", "lo", "the", "a", "an", "il", "le", "les"}
_LEGAL = {"sl", "slu", "sa", "sau", "sll", "inc", "ltd", "llc", "gmbh", "srl", "spa"}


def normalize_name(name: str) -> str:
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"\b(s\.\s?l\.?\s?u?\.?|s\.\s?a\.?)(?=\s|$)", " ", s)
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    words = [w for w in s.split() if w]
    while words and words[0] in _ARTICLES:
        words = words[1:]
    words = [w for w in words if w not in _LEGAL] or words
    return " ".join(words)


def slug_id(name: str, taken: set[str]) -> str:
    base = normalize_name(name).replace(" ", "-")[:32].strip("-") or "entidad"
    slug, n = base, 2
    while slug in taken:
        slug = f"{base}-{n}"
        n += 1
    taken.add(slug)
    return slug


# --------------------------------------------------------------------------- extracción


async def extract_chunk(
    llm: LLM,
    chunk: Chunk,
    total: int,
    doc: Document,
    question: str,
    settings: Settings,
    cache: CallCache | None,
) -> ChunkExtraction:
    prompt = prompts.render(
        "ontology_chunk",
        doc_title=doc.title,
        question=question,
        language=doc.language_name,
        chunk_pos=chunk.index + 1,
        chunk_total=total,
        chunk_title=chunk.title,
        chunk_text=chunk.text,
        entity_types=ENTITY_TYPE_HELP,
        min_entities=10 if total == 1 else 4,
        max_entities=22 if total == 1 else 15,
    )
    return await cached_complete(
        cache,
        "ontology.chunk",
        llm,
        prompt,
        schema=ChunkExtraction,
        system=prompts.load("ontology_system"),
        model=settings.model_heavy,
        effort=settings.effort_heavy,
        purpose="ontology.chunk",
        meta={"chunk": chunk.id, "_chunk_text": chunk.text},
    )


class _Candidate(BaseModel):
    entity: ExtractedEntity
    chunk_id: str


class _UnionFind:
    def __init__(self, n: int) -> None:
        self.parent = list(range(n))

    def find(self, i: int) -> int:
        while self.parent[i] != i:
            self.parent[i] = self.parent[self.parent[i]]
            i = self.parent[i]
        return i

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb)] = min(ra, rb)


def _group_by_normalized(cands: list[_Candidate]) -> list[list[int]]:
    uf = _UnionFind(len(cands))
    key_owner: dict[str, int] = {}
    for i, cand in enumerate(cands):
        keys = {normalize_name(cand.entity.name)} | {normalize_name(a) for a in cand.entity.aliases}
        for key in keys:
            if not key:
                continue
            if key in key_owner:
                uf.union(i, key_owner[key])
            else:
                key_owner[key] = i
    groups: dict[int, list[int]] = {}
    for i in range(len(cands)):
        groups.setdefault(uf.find(i), []).append(i)
    return list(groups.values())


async def _llm_merge_plan(
    llm: LLM,
    reps: list[_Candidate],
    doc: Document,
    settings: Settings,
    cache: CallCache | None,
) -> MergePlan:
    lines = [
        f"{i}: {c.entity.name} | {c.entity.type} | {c.entity.description[:140]}" for i, c in enumerate(reps)
    ]
    prompt = prompts.render("ontology_merge", doc_title=doc.title, entity_list="\n".join(lines))
    return await cached_complete(
        cache,
        "ontology.merge",
        llm,
        prompt,
        schema=MergePlan,
        system=prompts.load("ontology_system"),
        model=settings.model_heavy,
        effort=settings.effort_heavy,
        purpose="ontology.merge",
        meta={"entities": len(reps), "_names": [c.entity.name for c in reps]},
    )


def _fuse(cands: list[_Candidate], canonical: str | None, taken: set[str]) -> Entity:
    names = [c.entity.name for c in cands]
    name = canonical or Counter(names).most_common(1)[0][0]
    types = Counter(c.entity.type for c in cands).most_common(1)[0][0]
    descs = sorted({c.entity.description.strip() for c in cands if c.entity.description.strip()}, key=len)
    interests: list[str] = []
    for c in cands:
        for it in c.entity.interests:
            if it.lower() not in {x.lower() for x in interests}:
                interests.append(it)
    aliases = sorted({n for c in cands for n in [c.entity.name, *c.entity.aliases]} - {name})
    return Entity(
        id=slug_id(name, taken),
        name=name,
        type=types,
        description=descs[-1] if descs else name,
        interests=interests[:6],
        initial_stance=round(mean(c.entity.initial_stance for c in cands), 2),
        influence=round(max(c.entity.influence for c in cands), 2),
        aliases=aliases[:8],
        chunk_ids=sorted({c.chunk_id for c in cands}),
    )


async def build_ontology(
    llm: LLM,
    doc: Document,
    chunks: list[Chunk],
    question: str,
    settings: Settings,
    cache: CallCache | None = None,
    on_step: Callable[[str], None] | None = None,
) -> tuple[Ontology, dict[str, Any]]:
    """Extrae por trozos (en paralelo, limitado por el semáforo del LLM), deduplica y fusiona."""
    extractions = await asyncio.gather(
        *(extract_chunk(llm, ch, len(chunks), doc, question, settings, cache) for ch in chunks)
    )
    if on_step:
        on_step(f"{len(chunks)} trozos extraídos")
    cands = [
        _Candidate(entity=e, chunk_id=ch.id)
        for ch, ex in zip(chunks, extractions, strict=True)
        for e in ex.entities
    ]
    groups = _group_by_normalized(cands)
    reps = [cands[g[0]] for g in groups]
    stats: dict[str, Any] = {"raw_entities": len(cands), "after_normalization": len(groups)}

    canonical: dict[int, str] = {}
    if len(reps) > 1:
        plan = await _llm_merge_plan(llm, reps, doc, settings, cache)
        uf = _UnionFind(len(reps))
        for grp in plan.groups:
            valid = [m for m in grp.members if 0 <= m < len(reps)]
            for m in valid[1:]:
                uf.union(valid[0], m)
            if len(valid) >= 2:
                canonical[uf.find(valid[0])] = grp.canonical.strip()
        merged: dict[int, list[int]] = {}
        for i in range(len(reps)):
            merged.setdefault(uf.find(i), []).append(i)
        final_groups = [
            ([j for i in members for j in groups[i]], canonical.get(root)) for root, members in merged.items()
        ]
    else:
        final_groups = [(g, None) for g in groups]
    stats["after_llm_merge"] = len(final_groups)

    taken: set[str] = set()
    entities = [_fuse([cands[i] for i in members], canon, taken) for members, canon in final_groups]
    lookup: dict[str, str] = {}
    for ent in entities:
        for n in [ent.name, *ent.aliases]:
            lookup.setdefault(normalize_name(n), ent.id)

    def resolve(name: str) -> str | None:
        return lookup.get(normalize_name(name))

    rel_acc: dict[tuple[str, str, str], list[ExtractedRelation]] = {}
    dropped = 0
    for ex in extractions:
        for r in ex.relations:
            s, t = resolve(r.source), resolve(r.target)
            if not s or not t or s == t:
                dropped += 1
                continue
            rel_acc.setdefault((s, t, r.type.strip().lower().replace(" ", "_")), []).append(r)
    relations = [
        Relation(
            source=s,
            target=t,
            type=typ,
            weight=round(mean(r.weight for r in rs), 2),
            description=max((r.description for r in rs), key=len),
        )
        for (s, t, typ), rs in rel_acc.items()
    ]
    stats["relations_dropped"] = dropped

    facts: list[Fact] = []
    seen_facts: set[str] = set()
    for ch, ex in zip(chunks, extractions, strict=True):
        for f in ex.facts:
            key = normalize_name(f.statement)
            if key in seen_facts:
                continue
            seen_facts.add(key)
            facts.append(
                Fact(
                    id=f"f{len(facts) + 1:02d}",
                    statement=f.statement,
                    chunk_id=ch.id,
                    importance=f.importance,
                )
            )
    facts.sort(key=lambda f: -f.importance)

    tensions: list[Tension] = []
    for ex in extractions:
        for t in ex.tensions:
            parties = [p for p in dict.fromkeys(resolve(n) for n in t.parties) if p]
            tensions.append(
                Tension(
                    id=f"t{len(tensions) + 1:02d}",
                    description=t.description,
                    parties=parties,
                    stakes=t.stakes,
                    intensity=t.intensity,
                )
            )
    ont = Ontology(
        title=doc.title,
        language=doc.language,
        entities=entities,
        relations=relations,
        facts=facts,
        tensions=tensions,
    )
    stats.update(entities=len(entities), relations=len(relations), facts=len(facts), tensions=len(tensions))
    return ont, stats


# --------------------------------------------------------------------------- brief


async def generate_brief(
    llm: LLM,
    ont: Ontology,
    doc: Document,
    question: str,
    settings: Settings,
    cache: CallCache | None = None,
) -> ScenarioBrief:
    entities = "\n".join(
        f"- {e.id} | {e.type} | {e.initial_stance:+.1f} | {e.influence:.1f} | {e.name}: {e.description[:160]}"
        for e in ont.entities
    )
    facts = "\n".join(f"- [{f.id}] {f.statement}" for f in ont.facts[:25])
    tensions = "\n".join(
        f"- [{t.id}] {t.description} (partes: {', '.join(t.parties)})" for t in ont.tensions[:12]
    )
    prompt = prompts.render(
        "brief_user",
        doc_title=doc.title,
        question=question,
        language=doc.language_name,
        entities=entities,
        facts=facts or "- (ninguno)",
        tensions=tensions or "- (ninguna)",
        excerpt=doc.text[:3500],
        entity_ids=", ".join(e.id for e in ont.entities),
    )
    brief = await cached_complete(
        cache,
        "ontology.brief",
        llm,
        prompt,
        schema=ScenarioBrief,
        system=prompts.load("brief_system"),
        model=settings.model_heavy,
        effort=settings.effort_heavy,
        purpose="ontology.brief",
        meta={"_entity_ids": [e.id for e in ont.entities], "_title": doc.title},
    )
    known = {e.id for e in ont.entities}
    for seg in brief.audience_segments:
        seg.entity_ids = [i for i in seg.entity_ids if i in known]
    total = sum(s.weight for s in brief.audience_segments) or 1.0
    for seg in brief.audience_segments:
        seg.weight = round(seg.weight / total, 3)
    return brief


def render_brief_md(
    brief: ScenarioBrief, ont: Ontology, question: str, stats: dict[str, Any] | None = None
) -> str:
    stance = {e.id: e.initial_stance for e in ont.entities}
    lines = [
        f"# {brief.title}",
        "",
        f"> {brief.one_liner}",
        "",
        f"**Pregunta de la simulación:** {question}",
        "",
        "## Resumen",
        brief.summary,
        "",
        "## Propuesta",
        brief.proposal,
        "",
        "## Hechos ancla",
        *[f"- {f}" for f in brief.key_facts],
        "",
        "## Actores",
        *[f"- {s}" for s in brief.stakeholders],
        "",
        "## Tensiones",
        *[f"- {t}" for t in brief.tensions],
        "",
        "## Incógnitas que debe iluminar la simulación",
        *[f"- {u}" for u in brief.uncertainties],
        "",
        "## Segmentos de audiencia",
        "| Segmento | Peso | Postura típica | Entidades |",
        "|---|---|---|---|",
        *[
            f"| {s.name} — {s.description} | {s.weight:.0%} | {s.typical_stance:+.1f} | {', '.join(s.entity_ids)} |"
            for s in brief.audience_segments
        ],
        "",
        f"## Grafo de entidades ({len(ont.entities)} entidades, {len(ont.relations)} relaciones)",
        "| id | Entidad | Tipo | Postura inicial | Influencia |",
        "|---|---|---|---|---|",
        *[
            f"| {e.id} | {e.name} | {e.type} | {stance[e.id]:+.2f} | {e.influence:.2f} |"
            for e in sorted(ont.entities, key=lambda e: -e.influence)
        ],
        "",
        "### Relaciones",
        *[f"- {r.source} —{r.type}→ {r.target} ({r.weight:.1f}) {r.description}" for r in ont.relations],
    ]
    if stats:
        lines += ["", "### Extracción", ", ".join(f"{k}={v}" for k, v in stats.items())]
    return "\n".join(lines) + "\n"


def compact_brief(brief: ScenarioBrief, max_chars: int = 1800) -> str:
    """Contexto corto del escenario para los prompts de agentes y personas."""
    text = "\n".join(
        [
            f"{brief.title}: {brief.one_liner}",
            brief.proposal,
            "Hechos: " + " | ".join(brief.key_facts[:6]),
            "Tensiones: " + " | ".join(brief.tensions[:4]),
        ]
    )
    return text if len(text) <= max_chars else text[: max_chars - 1] + "…"


# --------------------------------------------------------------------------- respuestas simuladas (dry-run)

_CAPS_RE = re.compile(
    r"\b([A-ZÁÉÍÓÚÑ][\wáéíóúñü]{2,}(?:\s+(?:de|del|la|las|los|y)?\s*[A-ZÁÉÍÓÚÑ][\wáéíóúñü]{2,})*)"
)
_GENERIC = [
    "Clientes potenciales",
    "Competidores tradicionales",
    "Regulador",
    "Medios especializados",
    "Inversores",
]


@fake_responder("ontology.chunk")
def _fake_chunk(req: FakeRequest) -> ChunkExtraction:
    text = str(req.meta.get("_chunk_text", req.prompt))
    words = [
        w for w in _CAPS_RE.findall(text) if len(w) > 3 and w.lower() not in {"para", "pero", "como", "este"}
    ]
    names = [n for n, _ in Counter(words).most_common(6)] or []
    names += [g for g in _GENERIC if g not in names][: max(0, 8 - len(names))]
    types: list[EntityType] = ["group", "organization", "institution", "competitor", "media", "person"]
    ents = [
        ExtractedEntity(
            name=n,
            type=types[i % len(types)],
            description=f"Actor «{n}» mencionado en el documento.",
            interests=["precio", "confianza"][: 1 + i % 2],
            initial_stance=round(req.rng.uniform(-0.8, 0.8), 2),
            influence=round(req.rng.uniform(0.2, 0.9), 2),
        )
        for i, n in enumerate(names)
    ]
    rels = [
        ExtractedRelation(source=a.name, target=b.name, type="influye_en", weight=0.5)
        for a, b in itertools.pairwise(ents)
    ]
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if 30 < len(s.strip()) < 220]
    facts = [ExtractedFact(statement=s, importance=0.6) for s in sentences[:4]]
    tension = ExtractedTension(
        description=f"{ents[0].name} frente a {ents[1].name}",
        parties=[ents[0].name, ents[1].name],
        stakes="cuota",
        intensity=0.6,
    )
    return ChunkExtraction(entities=ents, relations=rels, facts=facts, tensions=[tension])


@fake_responder("ontology.merge")
def _fake_merge(req: FakeRequest) -> MergePlan:
    names = [str(n) for n in req.meta.get("_names", [])]
    groups: list[MergeGroup] = []
    by_prefix: dict[str, list[int]] = {}
    for i, n in enumerate(names):
        by_prefix.setdefault(normalize_name(n)[:6], []).append(i)
    for members in by_prefix.values():
        if len(members) > 1:
            groups.append(MergeGroup(members=members, canonical=names[members[0]]))
    return MergePlan(groups=groups)


@fake_responder("ontology.brief")
def _fake_brief(req: FakeRequest) -> ScenarioBrief:
    ids = [str(i) for i in req.meta.get("_entity_ids", [])]
    title = str(req.meta.get("_title", "Escenario"))
    segs = [
        AudienceSegment(
            name="Público objetivo",
            description="clientes potenciales",
            weight=0.35,
            typical_stance=0.3,
            entity_ids=ids[:2],
        ),
        AudienceSegment(
            name="Competencia y gremio",
            description="afectados por la propuesta",
            weight=0.2,
            typical_stance=-0.6,
            entity_ids=ids[2:3],
        ),
        AudienceSegment(
            name="Prescriptores",
            description="expertos y creadores de contenido",
            weight=0.2,
            typical_stance=0.0,
            entity_ids=ids[3:4],
        ),
        AudienceSegment(
            name="Observadores", description="poco implicados", weight=0.25, typical_stance=0.0, entity_ids=[]
        ),
    ]
    return ScenarioBrief(
        title=title,
        one_liner=f"Lanzamiento de «{title}».",
        summary="Escenario simulado en modo dry-run.",
        proposal="Propuesta descrita en el documento semilla.",
        key_facts=["Hecho ancla simulado 1", "Hecho ancla simulado 2"],
        stakeholders=[f"{i}: gana o pierde" for i in ids[:5]],
        tensions=["Precio frente a confianza"],
        uncertainties=["¿Se fiará el público?"],
        audience_segments=segs,
    )
