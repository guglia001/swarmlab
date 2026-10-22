from __future__ import annotations

from pathlib import Path

import pytest

from swarmlab.config import load_settings
from swarmlab.ingest import Document, chunk_document, detect_language, load_document


def test_settings_precedence(tmp_path: Path) -> None:
    (tmp_path / "config.toml").write_text(
        'MAX_CONCURRENCY = 2\nMODEL_LIGHT = "sonnet"\nAGENTS_PER_CALL = 4\n'
    )
    (tmp_path / ".env").write_text("AGENTS_PER_CALL=5  # comentario\nFEED_SIZE=6\n")
    env = {"SWARMLAB_FEED_SIZE": "7", "MAX_CONCURRENCY": "9"}  # sin prefijo no cuenta desde el entorno
    s = load_settings(None, {"call_timeout": 99}, cwd=tmp_path, environ=env)
    assert s.max_concurrency == 2
    assert s.model_light == "sonnet"
    assert s.agents_per_call == 5
    assert s.feed_size == 7
    assert s.call_timeout == 99
    assert s.runs_dir == (tmp_path / "runs").resolve()


def test_settings_reject_unknown_model(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        load_settings(None, {"model_heavy": "gpt-5"}, cwd=tmp_path, environ={})


def test_language_detection() -> None:
    assert detect_language("El autónomo paga la cuota de la Seguridad Social y el IVA trimestral.")[0] == "es"
    assert detect_language("The freelancer pays the quarterly tax and the social security fee.")[0] == "en"


def test_chunking_by_sections_respects_limit(example: Path) -> None:
    doc = load_document(example)
    assert doc.language == "es" and doc.title.startswith("Gestoría Lince")
    chunks = chunk_document(doc, max_chars=1500)
    assert len(chunks) > 3
    assert all(len(c.text) <= 1500 for c in chunks)
    assert chunks[0].id == "c01" and [c.index for c in chunks] == list(range(len(chunks)))
    joined = " ".join(c.text for c in chunks)
    for heading in ("Problema que resuelve", "Mercado y competencia", "Riesgos y dudas abiertas"):
        assert heading in joined


def test_long_section_is_split_by_paragraphs() -> None:
    body = "\n\n".join(f"Párrafo {i}. " + "texto " * 60 for i in range(10))
    doc = Document(
        path="x", name="x.md", title="X", text=f"# X\n\n{body}", language="es", language_name="español"
    )
    chunks = chunk_document(doc, max_chars=1000)
    assert len(chunks) >= 3 and all(len(c.text) <= 1000 for c in chunks)


def test_pdf_and_txt_are_supported(tmp_path: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig = plt.figure()
    fig.text(0.1, 0.5, "Plan de lanzamiento de una cooperativa de autónomos en Valencia con precios bajos")
    fig.savefig(tmp_path / "plan.pdf")
    plt.close(fig)
    doc = load_document(tmp_path / "plan.pdf")
    assert "cooperativa" in doc.text
    (tmp_path / "nota.txt").write_text(
        "Nota de lanzamiento: una app para pagar a medias entre compañeros de piso."
    )
    assert load_document(tmp_path / "nota.txt").text.startswith("Nota de lanzamiento")
    with pytest.raises(ValueError):
        (tmp_path / "x.docx").write_text("hola")
        load_document(tmp_path / "x.docx")
