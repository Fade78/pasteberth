"""Validation structurelle des images : PNG, JPEG, WebP.

Le serveur ne décode pas les pixels. Il vérifie toutefois les conteneurs
complets et impose un budget de pixels afin de limiter les décodages côté
navigateur/harness lors de l'affichage des previews.
"""
from __future__ import annotations

import binascii
import struct
from dataclasses import dataclass

MAX_DIMENSION = 16_384
MAX_PIXELS = 25_000_000
HARD_MAX_PIXELS = 50_000_000

# Formats acceptés -> (extension, MIME)
FORMATS: dict[str, tuple[str, str]] = {
    "png": (".png", "image/png"),
    "jpeg": (".jpg", "image/jpeg"),
    "webp": (".webp", "image/webp"),
}

# MIME déclarés acceptés à l'upload (indicatif : le contenu fait foi).
ALLOWED_DECLARED_MIMES = {
    "image/png", "image/jpeg", "image/webp", "application/octet-stream",
    "text/plain", "text/markdown", "text/html", "text/css",
    "text/javascript", "application/json", "application/xml", "text/csv",
    "application/x-yaml", "application/x-sh", "text/x-python",
    "text/x-shellscript",
}


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
    kind: str = "image"
    mime: str = "image/png"
    ext: str = ".png"


def _check_dims(width: int, height: int, fmt: str, max_pixels: int) -> ImageInfo:
    if not (1 <= width <= MAX_DIMENSION and 1 <= height <= MAX_DIMENSION):
        raise InvalidImageError(
            "invalid_image",
            f"dimensions {fmt} irréalistes : {width}x{height}",
        )
    if width * height > max_pixels:
        raise InvalidImageError(
            "invalid_image",
            f"image {fmt} trop grande à décoder : {width}x{height} "
            f"(maximum {max_pixels} pixels)",
        )
    return ImageInfo(fmt=fmt, width=width, height=height, mime=mime_for(fmt), ext=FORMATS[fmt][0])


def _parse_png(data: bytes, max_pixels: int) -> ImageInfo:
    signature = b"\x89PNG\r\n\x1a\n"
    if len(data) < len(signature) or not data.startswith(signature):
        raise InvalidImageError("invalid_image", "signature PNG absente")

    pos = len(signature)
    saw_ihdr = False
    saw_idat = False
    saw_iend = False
    dimensions: tuple[int, int] | None = None
    while pos < len(data):
        if pos + 12 > len(data):
            raise InvalidImageError("invalid_image", "chunk PNG tronqué")
        chunk_len = struct.unpack(">I", data[pos:pos + 4])[0]
        chunk_type = data[pos + 4:pos + 8]
        chunk_start = pos + 8
        chunk_end = chunk_start + chunk_len
        if chunk_end + 4 > len(data):
            raise InvalidImageError("invalid_image", "chunk PNG tronqué")
        payload = data[chunk_start:chunk_end]
        expected_crc = struct.unpack(">I", data[chunk_end:chunk_end + 4])[0]
        actual_crc = binascii.crc32(chunk_type + payload) & 0xFFFFFFFF
        if actual_crc != expected_crc:
            raise InvalidImageError("invalid_image", "CRC PNG invalide")

        if not saw_ihdr:
            if chunk_type != b"IHDR" or chunk_len != 13:
                raise InvalidImageError("invalid_image", "premier chunk PNG != IHDR")
            width, height = struct.unpack(">II", payload[:8])
            dimensions = (width, height)
            saw_ihdr = True
        elif chunk_type == b"IDAT":
            saw_idat = True
        elif chunk_type == b"IEND":
            if chunk_len != 0 or not saw_idat:
                raise InvalidImageError("invalid_image", "PNG incomplet")
            saw_iend = True
            pos = chunk_end + 4
            if pos != len(data):
                raise InvalidImageError("invalid_image", "données après IEND PNG")
            break
        pos = chunk_end + 4

    if dimensions is None or not saw_idat or not saw_iend or pos != len(data):
        raise InvalidImageError("invalid_image", "PNG incomplet")
    return _check_dims(*dimensions, "png", max_pixels)


_SOF_MARKERS = {
    0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
    0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF,
}


def _parse_jpeg(data: bytes, max_pixels: int) -> ImageInfo:
    if len(data) < 4 or data[0:2] != b"\xff\xd8":
        raise InvalidImageError("invalid_image", "signature JPEG absente")
    pos = 2
    end = len(data)
    dimensions: tuple[int, int] | None = None
    while pos + 1 < end:
        if data[pos] != 0xFF:
            raise InvalidImageError("invalid_image", "flux JPEG désynchronisé")
        while pos < end and data[pos] == 0xFF:
            pos += 1
        if pos >= end:
            break
        marker = data[pos]
        pos += 1
        if marker == 0xD9:
            break
        if marker == 0xDA:
            if dimensions is None or pos + 2 > end:
                raise InvalidImageError("invalid_image", "trame JPEG incomplète")
            seg_len = struct.unpack(">H", data[pos:pos + 2])[0]
            if seg_len < 2 or pos + seg_len > end:
                raise InvalidImageError("invalid_image", "segment SOS JPEG tronqué")
            entropy_start = pos + seg_len
            eoi = data.find(b"\xff\xd9", entropy_start)
            if eoi < 0 or eoi + 2 != end:
                raise InvalidImageError("invalid_image", "fin JPEG absente ou incohérente")
            return _check_dims(*dimensions, "jpeg", max_pixels)
        if marker in (0x01,) or 0xD0 <= marker <= 0xD7:
            continue
        if pos + 2 > end:
            raise InvalidImageError("invalid_image", "segment JPEG tronqué")
        seg_len = struct.unpack(">H", data[pos:pos + 2])[0]
        if seg_len < 2 or pos + seg_len > end:
            raise InvalidImageError("invalid_image", "segment JPEG tronqué")
        if marker in _SOF_MARKERS:
            if seg_len < 7:
                raise InvalidImageError("invalid_image", "segment SOF JPEG tronqué")
            height, width = struct.unpack(">HH", data[pos + 3:pos + 7])
            dimensions = (width, height)
        pos += seg_len
    raise InvalidImageError("invalid_image", "JPEG incomplet ou en-tête de trame absent")


def _parse_webp(data: bytes, max_pixels: int) -> ImageInfo:
    if len(data) < 20 or data[0:4] != b"RIFF" or data[8:12] != b"WEBP":
        raise InvalidImageError("invalid_image", "signature WebP absente")
    riff_size = struct.unpack("<I", data[4:8])[0]
    if riff_size != len(data) - 8:
        raise InvalidImageError("invalid_image", "taille RIFF WebP incohérente")

    pos = 12
    end = len(data)
    dimensions: tuple[int, int] | None = None
    saw_image_payload = False
    while pos < end:
        if pos + 8 > end:
            raise InvalidImageError("invalid_image", "chunk WebP tronqué")
        fourcc = data[pos:pos + 4]
        size = struct.unpack("<I", data[pos + 4:pos + 8])[0]
        body = pos + 8
        chunk_end = body + size
        padded_end = chunk_end + (size & 1)
        if chunk_end > end or padded_end > end:
            raise InvalidImageError("invalid_image", "chunk WebP tronqué")
        if fourcc == b"VP8X":
            if size < 10:
                raise InvalidImageError("invalid_image", "chunk VP8X WebP tronqué")
            if dimensions is None:
                w = int.from_bytes(data[body + 4:body + 7], "little") + 1
                h = int.from_bytes(data[body + 7:body + 10], "little") + 1
                dimensions = (w, h)
        elif fourcc == b"VP8 ":
            if size < 10:
                raise InvalidImageError("invalid_image", "chunk VP8 WebP tronqué")
            if data[body + 3:body + 6] != b"\x9d\x01\x2a":
                raise InvalidImageError("invalid_image", "code de synchronisation VP8 invalide")
            saw_image_payload = True
            if dimensions is None:
                w, h = struct.unpack("<HH", data[body + 6:body + 10])
                dimensions = (w & 0x3FFF, h & 0x3FFF)
        elif fourcc == b"VP8L":
            if size < 5:
                raise InvalidImageError("invalid_image", "chunk VP8L WebP tronqué")
            if data[body] != 0x2F:
                raise InvalidImageError("invalid_image", "signature VP8L invalide")
            saw_image_payload = True
            if dimensions is None:
                bits = struct.unpack("<I", data[body + 1:body + 5])[0]
                w = (bits & 0x3FFF) + 1
                h = ((bits >> 14) & 0x3FFF) + 1
                dimensions = (w, h)
        pos = padded_end
    if dimensions is None:
        raise InvalidImageError("invalid_image", "chunk de dimension WebP introuvable")
    if not saw_image_payload:
        raise InvalidImageError("invalid_image", "payload image WebP introuvable")
    return _check_dims(*dimensions, "webp", max_pixels)


_PARSERS = {"png": _parse_png, "jpeg": _parse_jpeg, "webp": _parse_webp}
_SIGNATURES = (
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"\xff\xd8\xff", "jpeg"),
    (b"RIFF", "webp"),
)


def inspect_image(data: bytes, *, max_pixels: int = MAX_PIXELS) -> ImageInfo:
    """Identifie et valide une image à partir de son contenu."""
    if not (1 <= max_pixels <= HARD_MAX_PIXELS):
        raise ValueError(f"max_pixels doit être entre 1 et {HARD_MAX_PIXELS}")
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
    return _PARSERS[fmt](data, max_pixels)


def mime_allowed(declared: str | None) -> bool:
    """Vérifie le Content-Type déclaré (indicatif, jamais déterminant)."""
    if not declared:
        return True  # traité comme application/octet-stream
    return declared.split(";")[0].strip().lower() in ALLOWED_DECLARED_MIMES


def extension_for(fmt: str) -> str:
    return FORMATS[fmt][0]


def mime_for(fmt: str) -> str:
    return FORMATS[fmt][1]
