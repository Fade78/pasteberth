"""Parser ``multipart/form-data`` minimal et borné (bibliothèque standard).

Le corps est déjà plafonné en amont (max_upload_bytes) ; ce parser borne
en plus le nombre de parties et la taille des en-têtes. Il n'utilise jamais
le nom de fichier fourni par le client pour nommer quoi que ce soit.
"""
from __future__ import annotations

import re

MAX_PARTS = 32
MAX_HEADER_BLOCK = 8 * 1024
_MAX_TOKEN = 256

_DISPOSITION_RE = re.compile(r"content-disposition\s*:", re.IGNORECASE)
_PARAM_RE = {
    "name": re.compile(r'\bname="((?:[^"\\]|\\.)*)"'),
    "filename": re.compile(r'\bfilename="((?:[^"\\]|\\.)*)"'),
}


class MultipartError(Exception):
    """Corps multipart malformé."""


def extract_boundary(content_type: str) -> str | None:
    match = re.search(r'boundary\s*=\s*(?:"([^"]+)"|([^;\s]+))', content_type or "", re.I)
    if not match:
        return None
    boundary = match.group(1) or match.group(2)
    boundary = boundary.rstrip()
    if not boundary or len(boundary) > 70 or '"' in boundary:
        return None
    return boundary


def _unescape(value: str) -> str:
    return re.sub(r"\\(.)", r"\1", value)


def parse_multipart(body: bytes, boundary: str) -> dict[str, tuple[str | None, str | None, bytes]]:
    """Retourne {nom du champ: (filename_client|None, content_type|None, contenu)}."""
    delimiter = b"--" + boundary.encode("utf-8")
    segments = body.split(delimiter)
    fields: dict[str, tuple[str | None, str | None, bytes]] = {}
    # segments[0] = préambule ; dernier segment = épilogue après "--".
    for segment in segments[1:]:
        if segment.startswith(b"--"):
            break  # marqueur final atteint
        if len(fields) >= MAX_PARTS:
            raise MultipartError("trop de parties dans le corps multipart")
        if segment.startswith(b"\r\n"):
            segment = segment[2:]
        elif segment.startswith(b"\n"):
            segment = segment[1:]
        header_end = segment.find(b"\r\n\r\n")
        sep_len = 4
        if header_end < 0:
            header_end = segment.find(b"\n\n")
            sep_len = 2
        if header_end < 0:
            raise MultipartError("séparation en-têtes/contenu introuvable")
        if header_end > MAX_HEADER_BLOCK:
            raise MultipartError("bloc d'en-têtes trop grand")
        header_blob = segment[:header_end].decode("utf-8", "replace")
        content = segment[header_end + sep_len:]
        if content.endswith(b"\r\n"):
            content = content[:-2]
        elif content.endswith(b"\n"):
            content = content[:-1]

        disposition = ""
        part_ctype: str | None = None
        for line in header_blob.splitlines():
            lower = line.lower()
            if not disposition and lower.startswith("content-disposition:"):
                disposition = line
            elif lower.startswith("content-type:"):
                part_ctype = line.split(":", 1)[1].strip() or None
        name_match = _PARAM_RE["name"].search(disposition)
        if not name_match:
            continue
        name = _unescape(name_match.group(1))
        if not name or len(name) > _MAX_TOKEN:
            raise MultipartError("nom de champ invalide")
        file_match = _PARAM_RE["filename"].search(disposition)
        filename = _unescape(file_match.group(1)) if file_match else None
        fields[name] = (filename, part_ctype, content)
    if not fields:
        raise MultipartError("aucun champ dans le corps multipart")
    return fields
