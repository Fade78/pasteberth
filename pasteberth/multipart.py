"""Parser ``multipart/form-data`` minimal et borné (bibliothèque standard).

Le corps est déjà plafonné en amont (max_upload_bytes) ; ce parser borne
en plus le nombre de parties et la taille des en-têtes. Le nom de fichier est
retourné comme indication ; la couche stockage le valide avant tout usage.
"""
from __future__ import annotations

import re

MAX_PARTS = 32
MAX_HEADER_BLOCK = 8 * 1024
_MAX_TOKEN = 256

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


def _boundary_at(body: bytes, delimiter: bytes, start: int) -> int:
    """Retourne le prochain délimiteur positionné en début de ligne."""
    cursor = start
    while True:
        position = body.find(delimiter, cursor)
        if position < 0:
            return -1
        if position == 0 or body[position - 2:position] == b"\r\n" or body[position - 1:position] == b"\n":
            after = position + len(delimiter)
            if body[after:after + 2] in (b"--", b"\r\n") or body[after:after + 1] == b"\n":
                return position
        cursor = position + len(delimiter)


def parse_multipart(body: bytes, boundary: str) -> dict[str, tuple[str | None, str | None, bytes]]:
    """Retourne {nom du champ: (filename_client|None, content_type|None, contenu)}."""
    delimiter = b"--" + boundary.encode("utf-8")
    fields: dict[str, tuple[str | None, str | None, bytes]] = {}
    part_count = 0
    position = _boundary_at(body, delimiter, 0)
    if position < 0:
        raise MultipartError("délimiteur multipart introuvable")

    while True:
        after_delimiter = position + len(delimiter)
        if body[after_delimiter:after_delimiter + 2] == b"--":
            break
        if body[after_delimiter:after_delimiter + 2] == b"\r\n":
            headers_start = after_delimiter + 2
        elif body[after_delimiter:after_delimiter + 1] == b"\n":
            headers_start = after_delimiter + 1
        else:
            raise MultipartError("délimiteur multipart mal terminé")

        part_count += 1
        if part_count > MAX_PARTS:
            raise MultipartError("trop de parties dans le corps multipart")

        header_end = body.find(b"\r\n\r\n", headers_start)
        sep_len = 4
        if header_end < headers_start:
            header_end = body.find(b"\n\n", headers_start)
            sep_len = 2
        if header_end < headers_start:
            raise MultipartError("séparation en-têtes/contenu introuvable")
        if header_end - headers_start > MAX_HEADER_BLOCK:
            raise MultipartError("bloc d'en-têtes trop grand")

        content_start = header_end + sep_len
        next_position = _boundary_at(body, delimiter, content_start)
        if next_position < 0:
            raise MultipartError("délimiteur multipart final absent")
        content_end = next_position
        if body[content_end - 2:content_end] == b"\r\n":
            content_end -= 2
        elif body[content_end - 1:content_end] == b"\n":
            content_end -= 1
        content = body[content_start:content_end]

        header_blob = body[headers_start:header_end].decode("utf-8", "replace")
        disposition = ""
        part_ctype: str | None = None
        for line in header_blob.splitlines():
            lower = line.lower()
            if not disposition and lower.startswith("content-disposition:"):
                disposition = line
            elif lower.startswith("content-type:"):
                part_ctype = line.split(":", 1)[1].strip() or None
        name_match = _PARAM_RE["name"].search(disposition)
        if name_match:
            name = _unescape(name_match.group(1))
            if not name or len(name) > _MAX_TOKEN:
                raise MultipartError("nom de champ invalide")
            file_match = _PARAM_RE["filename"].search(disposition)
            filename = _unescape(file_match.group(1)) if file_match else None
            fields[name] = (filename, part_ctype, content)

        position = next_position

    if not fields:
        raise MultipartError("aucun champ dans le corps multipart")
    return fields
