"""Validation structurelle des images : PNG, JPEG, WebP.

Principe : on ne décode jamais les pixels (pas de surface d'attaque de
décodeur). On vérifie la signature, la structure minimale des en-têtes et on
extrait les dimensions. Le format retenu est déterminé par le CONTENU,
jamais par le Content-Type déclaré par le client.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

MAX_DIMENSION = 100_000

# Formats acceptés -> (extension, MIME)
FORMATS: dict[str, tuple[str, str]] = {
    "png": (".png", "image/png"),
    "jpeg": (".jpg", "image/jpeg"),
    "webp": (".webp", "image/webp"),
}

# MIME déclarés acceptés à l'upload (indicatif : le contenu fait foi).
ALLOWED_DECLARED_MIMES = {"image/png", "image/jpeg", "image/webp", "application/octet-stream"}


class InvalidImageError(Exception):
    """Upload refusé : contenu vide, non reconnu ou corrompu."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ImageInfo:
    fmt: str  # clé de FORMATS
    width: int
    height: int


def _check_dims(width: int, height: int, fmt: str) -> ImageInfo:
    if not (1 <= width <= MAX_DIMENSION and 1 <= height <= MAX_DIMENSION):
        raise InvalidImageError(
            "invalid_image",
            f"dimensions {fmt} irréalistes : {width}x{height}",
        )
    return ImageInfo(fmt=fmt, width=width, height=height)


def _parse_png(data: bytes) -> ImageInfo:
    if len(data) < 24 or not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise InvalidImageError("invalid_image", "signature PNG absente")
    chunk_len = struct.unpack(">I", data[8:12])[0]
    if data[12:16] != b"IHDR" or chunk_len < 13:
        raise InvalidImageError("invalid_image", "premier chunk PNG != IHDR")
    width, height = struct.unpack(">II", data[16:24])
    return _check_dims(width, height, "png")


_SOF_MARKERS = {
    0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
    0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF,
}


def _parse_jpeg(data: bytes) -> ImageInfo:
    if len(data) < 4 or data[0] != 0xFF or data[1] != 0xD8:
        raise InvalidImageError("invalid_image", "signature JPEG absente")
    pos = 2
    end = len(data)
    while pos + 2 <= end:
        if data[pos] != 0xFF:  # désynchronisé
            raise InvalidImageError("invalid_image", "flux JPEG désynchronisé")
        marker = data[pos + 1]
        if marker == 0xFF:  # octets de bourrage
            pos += 1
            continue
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:  # sans longueur
            pos += 2
            continue
        if marker == 0xD9:  # EOI avant SOF
            break
        if pos + 4 > end:
            break
        seg_len = struct.unpack(">H", data[pos + 2:pos + 4])[0]
        if seg_len < 2:
            raise InvalidImageError("invalid_image", "longueur de segment JPEG invalide")
        if marker in _SOF_MARKERS:
            if pos + 9 > end:
                raise InvalidImageError("invalid_image", "segment SOF JPEG tronqué")
            height, width = struct.unpack(">HH", data[pos + 5:pos + 9])
            return _check_dims(width, height, "jpeg")
        if marker == 0xDA:  # SOS : données d'image, pas de SOF trouvé
            break
        pos += 2 + seg_len
    raise InvalidImageError("invalid_image", "en-tête de trame JPEG introuvable")


def _parse_webp(data: bytes) -> ImageInfo:
    if len(data) < 20 or data[0:4] != b"RIFF" or data[8:12] != b"WEBP":
        raise InvalidImageError("invalid_image", "signature WebP absente")
    pos = 12
    end = len(data)
    while pos + 8 <= end:
        fourcc = data[pos:pos + 4]
        size = struct.unpack("<I", data[pos + 4:pos + 8])[0]
        body = pos + 8
        if body + size > end:
            size = end - body  # tolère un RIFFSize incohérent, valide le contenu présent
        try:
            if fourcc == b"VP8X" and size >= 10:
                w = int.from_bytes(data[body + 4:body + 7], "little") + 1
                h = int.from_bytes(data[body + 7:body + 10], "little") + 1
                return _check_dims(w, h, "webp")
            if fourcc == b"VP8 " and size >= 10:
                if data[body + 3:body + 6] != b"\x9d\x01\x2a":
                    raise InvalidImageError("invalid_image", "code de synchronisation VP8 invalide")
                w, h = struct.unpack("<HH", data[body + 6:body + 10])
                return _check_dims(w & 0x3FFF, h & 0x3FFF, "webp")
            if fourcc == b"VP8L" and size >= 5:
                if data[body] != 0x2F:
                    raise InvalidImageError("invalid_image", "signature VP8L invalide")
                bits = struct.unpack("<I", data[body + 1:body + 5])[0]
                w = (bits & 0x3FFF) + 1
                h = ((bits >> 14) & 0x3FFF) + 1
                return _check_dims(w, h, "webp")
        finally:
            pass
        pos = body + size + (size & 1)  # chunks paddés sur octet pair
    raise InvalidImageError("invalid_image", "chunk de dimension WebP introuvable")


_PARSERS = {"png": _parse_png, "jpeg": _parse_jpeg, "webp": _parse_webp}
_SIGNATURES = (
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"\xff\xd8\xff", "jpeg"),
    (b"RIFF", "webp"),
)


def inspect_image(data: bytes) -> ImageInfo:
    """Identifie et valide une image à partir de son contenu."""
    if not data:
        raise InvalidImageError("empty_upload", "upload vide")
    fmt = None
    for sig, candidate in _SIGNATURES:
        if data.startswith(sig):
            fmt = candidate
            break
    if fmt is None:
        raise InvalidImageError(
            "unsupported_format",
            "contenu non reconnu (formats acceptés : PNG, JPEG, WebP)",
        )
    return _PARSERS[fmt](data)


def mime_allowed(declared: str | None) -> bool:
    """Vérifie le Content-Type déclaré (indicatif, jamais déterminant)."""
    if not declared:
        return True  # traité comme application/octet-stream
    return declared.split(";")[0].strip().lower() in ALLOWED_DECLARED_MIMES


def extension_for(fmt: str) -> str:
    return FORMATS[fmt][0]


def mime_for(fmt: str) -> str:
    return FORMATS[fmt][1]
