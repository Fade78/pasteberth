"""Classification du contenu : image, texte ou binaire.

Le serveur ne décode pas les pixels. Il vérifie les conteneurs complets et
impose un budget de pixels pour les images ; le texte est validé UTF-8 ;
tout le reste est traité comme binaire opaque (jamais interprété).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from pasteberth.images import (
    FORMATS,
    InvalidImageError,
    inspect_image,
    mime_for,
)

# Types texte déclarés -> extension (paste : identité du clipboard).
TEXT_MIMES: dict[str, str] = {
    "text/plain": ".txt",
    "text/markdown": ".md",
    "text/html": ".html",
    "text/css": ".css",
    "text/javascript": ".js",
    "application/json": ".json",
    "application/xml": ".xml",
    "text/csv": ".csv",
    "application/x-yaml": ".yaml",
    "application/x-sh": ".sh",
    "text/x-python": ".py",
    "text/x-shellscript": ".sh",
}
DEFAULT_TEXT_EXT = ".txt"
DEFAULT_BINARY_EXT = ".bin"

_SAFE_EXT_RE = re.compile(r"^[A-Za-z0-9]{1,10}$")

_SIGNATURES = (
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"\xff\xd8\xff", "jpeg"),
    (b"RIFF", "webp"),
)


@dataclass(frozen=True)
class ContentInfo:
    kind: str  # "image" | "text" | "binary"
    ext: str  # extension avec point, générée côté serveur
    mime: str  # MIME servi
    width: int | None = None
    height: int | None = None
    fmt: str | None = None  # clé FORMATS pour les images


def _is_text(data: bytes) -> bool:
    if b"\x00" in data:
        return False
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def safe_extension(name: str | None) -> str | None:
    """Extension d'origine assainie (drop) : [A-Za-z0-9]{1,10}."""
    if not name:
        return None
    ext = Path(name).suffix.lstrip(".")
    ext = re.sub(r"[^A-Za-z0-9]", "", ext)
    if not _SAFE_EXT_RE.fullmatch(ext):
        return None
    return "." + ext.lower()


def classify(
    data: bytes,
    declared_mime: str | None,
    filename_hint: str | None = None,
    *,
    max_pixels: int = 25_000_000,
) -> ContentInfo:
    """Identifie le contenu : image (contenu fait foi), texte UTF-8, sinon binaire."""
    # 1. Image : la signature du contenu fait foi.
    for sig, fmt in _SIGNATURES:
        if data.startswith(sig):
            info = inspect_image(data, max_pixels=max_pixels)
            return ContentInfo(
                kind="image",
                ext=FORMATS[info.fmt][0],
                mime=mime_for(info.fmt),
                width=info.width,
                height=info.height,
                fmt=info.fmt,
            )
    # 2. Texte : déclaré texte OU contenu UTF-8 valide.
    declared = (declared_mime or "").split(";")[0].strip().lower()
    if declared.startswith("text/") or declared in (
        "application/json",
        "application/xml",
        "application/x-yaml",
    ):
        if _is_text(data):
            ext = TEXT_MIMES.get(declared, DEFAULT_TEXT_EXT)
            return ContentInfo(kind="text", ext=ext, mime=declared or "text/plain")
    if _is_text(data):
        return ContentInfo(kind="text", ext=DEFAULT_TEXT_EXT, mime="text/plain")
    # 3. Binaire : extension d'origine si drop, sinon .bin.
    ext = safe_extension(filename_hint) or DEFAULT_BINARY_EXT
    return ContentInfo(kind="binary", ext=ext, mime="application/octet-stream")
