from __future__ import annotations

import asyncio
import random
from pathlib import Path

import pytest

from swarmlab.config import Settings
from swarmlab.ingest import chunk_document, load_document
from swarmlab.llm import FakeClaudeCLI, FakeRequest
from swarmlab.ontology import (
    ChunkExtraction,
    ExtractedEntity,
    ExtractedFact,
    ExtractedRelation,
    ExtractedTension,
    MergeGroup,
    MergePlan,
    build_ontology,
    generate_brief,
    normalize_name,
)
from swarmlab.personas import (
    BUCKETS,
    PersonaDraft,
    bucket_counts,
    diversity_report,
    generate_personas,
    plan_slots,
)


def test_normalize_name() -> None:
    assert normalize_name("La Agencia Tributaria") == normalize_name("agencia tributaria")
    assert normalize_name("Gestoría Lince, S.L.") == "gestoria lince"
    assert normalize_name("Los Autónomos") == "autonomos"


def _entity(name: str, stance: float, aliases: list[str] | None = None) -> ExtractedEntity:
    return ExtractedEntity(
        name=name,
        type="institution",
        description=f"desc {name}",
        initial_stance=stance,
        influence=0.5,
        aliases=aliases or [],
    )


def test_build_ontology_dedupes_by_name_and_llm_merge(example: Path, settings: Settings) -> None:
    doc = load_document(example)
    chunks = chunk_document(doc, 2500)[:2]

    def chunk(req: FakeRequest) -> ChunkExtraction:
        first = req.meta["chunk"] == "c01"
        ents = [
            _entity("Hacienda" if first else "la hacienda", -0.2),
            _entity("Agencia Tributaria" if first else "AEAT", 0.0),
            _entity("Autónomos", 0.4),
        ]
        return ChunkExtraction(
            entities=ents,
            relations=[ExtractedRelation(source=ents[0].name, target="Autónomos", type="regula", weight=0.8)],
            facts=[ExtractedFact(statement="Plan Autónomo a 29 € al mes.", importance=0.9)],
            tensions=[
                ExtractedTension(
                    description="sanciones",
                    parties=[ents[1].name, "Autónomos"],
                    stakes="multas",
                    intensity=0.7,
                )
            ],
        )

    def merge(req: FakeRequest) -> MergePlan:
        names = req.meta["_names"]
        idx = [i for i, n in enumerate(names) if n in ("Hacienda", "Agencia Tributaria", "AEAT")]
        return MergePlan(groups=[MergeGroup(members=idx, canonical="Agencia Tributaria")])

    llm = FakeClaudeCLI(responders={"ontology.chunk": chunk, "ontology.merge": merge})
    ont, stats = asyncio.run(build_ontology(llm, doc, chunks, "¿?", settings))
    names = sorted(e.name for e in ont.entities)
    assert names == ["Agencia Tributaria", "Autónomos"]
    assert stats == stats | {"raw_entities": 6, "after_normalization": 4, "after_llm_merge": 2}
    aeat = next(e for e in ont.entities if e.name == "Agencia Tributaria")
    assert set(aeat.chunk_ids) == {"c01", "c02"} and "AEAT" in aeat.aliases
    assert len(ont.relations) == 1 and ont.relations[0].source == aeat.id
    assert len(ont.facts) == 1  # hecho repetido en los dos trozos
    ids = {e.id for e in ont.entities}
    assert ont.tensions and all(t.parties and set(t.parties) <= ids for t in ont.tensions)


@pytest.mark.parametrize("n", [10, 13, 30, 77, 200])
def test_bucket_quotas_guarantee_minimums(n: int) -> None:
    counts = bucket_counts(n)
    assert sum(counts.values()) == n
    assert counts["critico"] + counts["esceptico"] >= 0.15 * n
    assert counts["desinteresado"] >= 0.10 * n
    assert set(counts) == {b[0] for b in BUCKETS}


def test_plan_slots_mixes_segments_and_marks_influencers(settings: Settings) -> None:
    from swarmlab.ontology import AudienceSegment

    segs = [
        AudienceSegment(name="Fans", description="d", weight=0.5, typical_stance=0.7),
        AudienceSegment(name="Gremio", description="d", weight=0.5, typical_stance=-0.7),
    ]
    slots = plan_slots(30, segs, random.Random(1))
    assert len(slots) == 30 and sorted(s.index for s in slots) == list(range(1, 31))
    gremio = [s for s in slots if s.segment == "Gremio"]
    fans = [s for s in slots if s.segment == "Fans"]
    assert sum(s.stance_max for s in gremio) < sum(s.stance_max for s in fans)
    assert 1 <= sum(s.influencer for s in slots) <= 4
    assert all(s.involvement == "baja" for s in slots if s.bucket == "desinteresado")


def test_generate_personas_respects_slots_and_is_diverse(example: Path, settings: Settings) -> None:
    llm = FakeClaudeCLI(seed=3)
    doc = load_document(example)
    ont, _ = asyncio.run(build_ontology(llm, doc, chunk_document(doc), "¿?", settings))
    brief = asyncio.run(generate_brief(llm, ont, doc, "¿?", settings))
    personas, div = asyncio.run(
        generate_personas(llm, brief, ont, 30, "¿?", "español", settings, random.Random(5))
    )
    assert len(personas) == 30
    assert len({p.handle for p in personas}) == 30
    assert [p.id for p in personas] == [f"a{i:02d}" for i in range(1, 31)]
    assert div.skeptic_share >= 0.15 and div.disengaged_share >= 0.10
    assert not div.duplicate_archetypes
    for p in personas:
        lo, hi = next(b[2] for b in BUCKETS if b[0] == p.stance_bucket)
        assert lo <= p.initial_stance <= hi


def test_duplicate_archetypes_are_regenerated_once(example: Path, settings: Settings) -> None:
    calls: list[int] = []

    def batch(req: FakeRequest) -> dict:
        calls.append(len(req.meta["_slots"]))
        out = []
        for s in req.meta["_slots"]:
            dup = len(calls) <= 2 and s["index"] % 2 == 0
            arche = "autónomo harto de su gestoría" if dup else f"perfil variante{s['index']}x{len(calls)}"
            out.append(
                PersonaDraft(
                    slot=s["index"],
                    name=f"N {s['index']}",
                    handle=f"user{s['index']}x{len(calls)}",
                    archetype=arche,
                    bio=f"bio {s['index']} {len(calls)}",
                    age=30,
                    occupation=f"oficio {s['index']}",
                    values=["a", "b"],
                    traits={
                        "openness": 0.5,
                        "conscientiousness": 0.5,
                        "extraversion": 0.5,
                        "agreeableness": 0.5,
                        "neuroticism": 0.5,
                    },
                    initial_stance=0.0,
                    activity=0.5,
                    writing_style="x",
                ).model_dump()
            )
        return {"personas": out}

    llm = FakeClaudeCLI(responders={"personas.batch": batch})
    doc = load_document(example)
    ont, _ = asyncio.run(build_ontology(llm, doc, chunk_document(doc), "¿?", settings))
    brief = asyncio.run(generate_brief(llm, ont, doc, "¿?", settings))
    personas, div = asyncio.run(
        generate_personas(llm, brief, ont, 12, "¿?", "español", settings, random.Random(2))
    )
    assert len(personas) == 12
    assert sum(1 for p in personas if p.archetype == "autónomo harto de su gestoría") == 1
    assert len(calls) == 3  # 2 lotes de 8+4 y 1 de regeneración
    assert diversity_report(personas).duplicate_archetypes == div.duplicate_archetypes == []
