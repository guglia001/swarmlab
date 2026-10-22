"""Carga del documento semilla (PDF/MD/TXT) y troceado por secciones."""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

from pydantic import BaseModel

SUPPORTED_SUFFIXES = (".md", ".markdown", ".txt", ".pdf")

_STOPWORDS: dict[str, tuple[str, set[str]]] = {
    "es": ("español", {"el", "la", "los", "las", "de", "que", "y", "en", "un", "una", "por", "con", "para", "es", "del", "se", "su", "al", "como", "más", "pero"}),
    "en": ("English", {"the", "and", "of", "to", "in", "is", "that", "for", "with", "on", "as", "are", "it", "by", "this", "be", "from", "at", "or", "an"}),
    "fr": ("français", {"le", "la", "les", "de", "des", "et", "en", "un", "une", "du", "que", "pour", "dans", "est", "qui", "sur", "pas", "au", "avec", "par"}),
    "it": ("italiano", {"il", "la", "di", "che", "e", "in", "un", "una", "per", "del", "della", "con", "non", "sono", "gli", "le", "si", "al", "dei", "da"}),
    "pt": ("português", {"o", "a", "os", "as", "de", "que", "e", "em", "um", "uma", "para", "com", "não", "do", "da", "dos", "das", "se", "por", "mais"}),
    "de": ("Deutsch", {"der", "die", "das", "und", "in", "den", "von", "zu", "mit", "ist", "nicht", "ein", "eine", "für", "auf", "dem", "sich", "des", "auch", "es"}),
    "ca": ("català", {"el", "la", "els", "les", "de", "que", "i", "en", "un", "una", "per", "amb", "és", "del", "als", "dels", "més", "però", "aquest", "són"}),
}  # fmt: skip


class Chunk(BaseModel):
    id: str
    index: int
    title: str
    text: str


class Document(BaseModel):
    path: str
    name: str
    title: str
    text: str
    language: str
    language_name: str

    @property
    def chars(self) -> int:
        return len(self.text)


def detect_language(text: str) -> tuple[str, str]:
    """Heurística por palabras vacías; por defecto español."""
    words = re.findall(r"[a-zàâäáçéèêëíìîïñóòôöúùûüœ']+", text.lower()[:20000])
    best, best_hits = "es", 0
    for code, (_, stop) in _STOPWORDS.items():
        hits = sum(1 for w in words if w in stop)
        if hits > best_hits:
            best, best_hits = code, hits
    return best, _STOPWORDS[best][0]


def _read_pdf(path: Path) -> str:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    pages = [(page.extract_text() or "").strip() for page in reader.pages]
    return "\n\n".join(p for p in pages if p)


def _clean(text: str) -> str:
    text = unicodedata.normalize("NFC", text).replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+\n", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def load_document(path: Path) -> Document:
    """Lee el documento semilla; acepta ``.md``, ``.txt`` y ``.pdf``."""
    if not path.is_file():
        raise FileNotFoundError(f"no existe el documento semilla {path}")
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise ValueError(f"formato no soportado {suffix!r}; usa {', '.join(SUPPORTED_SUFFIXES)}")
    raw = _read_pdf(path) if suffix == ".pdf" else path.read_text(encoding="utf-8", errors="replace")
    text = _clean(raw)
    if len(text) < 50:
        raise ValueError(f"el documento {path.name} está vacío o no se pudo extraer texto")
    title = path.stem.replace("-", " ").replace("_", " ").strip()
    m = re.search(r"^#\s+(.+)$", text, flags=re.M)
    if m:
        title = m.group(1).strip()
    code, name = detect_language(text)
    return Document(path=str(path), name=path.name, title=title, text=text, language=code, language_name=name)


_HEADING_RE = re.compile(r"^(#{1,3})\s+(.+?)\s*#*\s*$", re.M)


def split_sections(text: str) -> list[tuple[str, str]]:
    """Secciones (título, cuerpo) por encabezados Markdown ``#``–``###``; sin encabezados, todo junto."""
    matches = list(_HEADING_RE.finditer(text))
    if not matches:
        return [("", text)]
    sections: list[tuple[str, str]] = []
    if matches[0].start() > 0 and text[: matches[0].start()].strip():
        sections.append(("", text[: matches[0].start()].strip()))
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[m.end() : end].strip()
        sections.append((m.group(2).strip(), f"{m.group(0).strip()}\n{body}".strip()))
    return sections


def _split_long(text: str, max_chars: int) -> list[str]:
    """Parte un bloque largo por párrafos y, si hace falta, por frases."""
    pieces: list[str] = []
    current = ""
    paragraphs = re.split(r"\n\s*\n", text)
    units: list[str] = []
    for para in paragraphs:
        if len(para) <= max_chars:
            units.append(para)
        else:
            units.extend(re.split(r"(?<=[.!?])\s+", para))
    for unit in units:
        if current and len(current) + len(unit) + 2 > max_chars:
            pieces.append(current.strip())
            current = ""
        current += unit + "\n\n"
    if current.strip():
        pieces.append(current.strip())
    return pieces


def chunk_document(doc: Document, max_chars: int = 7000) -> list[Chunk]:
    """Agrupa secciones consecutivas en trozos de hasta ``max_chars`` caracteres."""
    groups: list[tuple[str, str]] = []
    cur_title, cur_text = "", ""
    for title, body in split_sections(doc.text):
        parts = _split_long(body, max_chars) if len(body) > max_chars else [body]
        for j, part in enumerate(parts):
            part_title = title if j == 0 else f"{title} (cont.)"
            if cur_text and len(cur_text) + len(part) + 2 > max_chars:
                groups.append((cur_title, cur_text.strip()))
                cur_title, cur_text = "", ""
            if not cur_title:
                cur_title = part_title
            cur_text += part + "\n\n"
    if cur_text.strip():
        groups.append((cur_title, cur_text.strip()))
    return [
        Chunk(id=f"c{i + 1:02d}", index=i, title=title or f"Parte {i + 1}", text=text)
        for i, (title, text) in enumerate(groups)
    ]
