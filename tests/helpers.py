"""Utilitaires de test : fabrication d'images, serveur réel éphémère,
client HTTP minimal (bibliothèque standard uniquement)."""
from __future__ import annotations

import binascii
import json
import struct
import threading
import zlib
from http.client import HTTPConnection
from pathlib import Path

from pasteberth.auth import LoginRateLimiter, SessionStore, hash_password, save_password_hash
from pasteberth.config import load_config, prepare_directories
from pasteberth.server import PasteberthServer
from pasteberth.service import PasteService
from pasteberth.webapp import make_handler

REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------- images


def _png_chunk(ctype: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + ctype
        + payload
        + struct.pack(">I", binascii.crc32(ctype + payload) & 0xFFFFFFFF)
    )


def make_png(width: int = 4, height: int = 3) -> bytes:
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    row = b"\x00" + b"\x40\x80\xc0" * width
    idat = zlib.compress(row * height)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", idat)
        + _png_chunk(b"IEND", b"")
    )


def make_jpeg(
    width: int = 6,
    height: int = 4,
    progressive: bool = False,
    extra_segments: bool = False,
) -> bytes:
    sof_marker = 0xC2 if progressive else 0xC0
    out = bytearray(b"\xff\xd8")
    if extra_segments:
        app1_payload = b"Exif\x00\x00" + b"\x00" * 8
        out += b"\xff\xe1" + struct.pack(">H", len(app1_payload) + 2) + app1_payload
        comment = b"abcd"
        out += b"\xff\xfe" + struct.pack(">H", len(comment) + 2) + comment
    dqt = bytes([0x00]) + bytes(range(64))
    out += b"\xff\xdb" + struct.pack(">H", len(dqt) + 2) + dqt
    sof = struct.pack(">BHHB", 8, height, width, 1) + bytes([1, 0x11, 0])
    out += bytes([0xFF, sof_marker]) + struct.pack(">H", len(sof) + 2) + sof
    sos = bytes([1, 1, 0x00, 0, 63, 0])
    out += b"\xff\xda" + struct.pack(">H", len(sos) + 2) + sos + b"\x00" * 8
    out += b"\xff\xd9"
    return bytes(out)


def make_webp_vp8l(width: int = 5, height: int = 7) -> bytes:
    bits = (width - 1) | ((height - 1) << 14)
    payload = b"\x2f" + struct.pack("<I", bits) + b"\x00"
    chunk = b"VP8L" + struct.pack("<I", len(payload)) + payload
    riff_size = 4 + len(chunk)
    return b"RIFF" + struct.pack("<I", riff_size) + b"WEBP" + chunk


def make_webp_lossy(width: int = 8, height: int = 6) -> bytes:
    first_part_len = 2
    b0 = 0x10 | ((first_part_len & 7) << 5)   # keyframe, version 0, show=1
    b1 = (first_part_len >> 3) & 0xFF
    b2 = (first_part_len >> 11) & 0xFF
    frame_header = bytes([b0, b1, b2]) + b"\x9d\x01\x2a" + struct.pack("<HH", width, height)
    partition = b"\x00" * first_part_len
    payload = frame_header + partition
    chunk = b"VP8 " + struct.pack("<I", len(payload)) + payload + (b"\x00" if len(payload) % 2 else b"")
    riff_size = 4 + len(chunk)
    return b"RIFF" + struct.pack("<I", riff_size) + b"WEBP" + chunk


def make_webp_vp8x(width: int = 300, height: int = 200, with_payload: bool = True) -> bytes:
    payload = bytes([0x10, 0, 0, 0])  # flag alpha + octets réservés
    payload += (width - 1).to_bytes(3, "little") + (height - 1).to_bytes(3, "little")
    chunks = b"VP8X" + struct.pack("<I", len(payload)) + payload
    if with_payload:
        chunks += make_webp_lossy(width, height)[12:]
    return b"RIFF" + struct.pack("<I", 4 + len(chunks)) + b"WEBP" + chunks


# ------------------------------------------------------------- multipart


def build_multipart(
    field: str = "image",
    filename: str = "clipboard.png",
    data: bytes = b"",
    content_type: str = "image/png",
    boundary: str = "pasteberthtest7351029384756abc",
) -> tuple[bytes, str]:
    body = bytearray()
    body += f"--{boundary}\r\n".encode()
    body += f'Content-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'.encode()
    body += f"Content-Type: {content_type}\r\n\r\n".encode()
    body += data
    body += f"\r\n--{boundary}--\r\n".encode()
    return bytes(body), f"multipart/form-data; boundary={boundary}"


# ---------------------------------------------------------------- config


def write_config(
    tmp: Path,
    zones: list[dict] | None = None,
    *,
    auth_enabled: bool = False,
    listen_address: str = "127.0.0.1",
    port: int | None = None,
    max_upload_size: str = "20MiB",
    max_image_pixels: int | None = None,
    trusted_proxies: str | None = '["127.0.0.1", "::1"]',
    allowed_hosts: str | None = None,
    allow_unauthenticated_local: bool | None = True,
    allow_unauthenticated_remote: bool | None = None,
    allow_insecure_http_remote: bool | None = None,
    tls_enabled: bool = False,
    tls_certificate: str | None = None,
    tls_private_key: str | None = None,
    password_file: str | None = None,
    min_free_percent: float | None = None,
    extra: str = "",
    password: str | None = None,
) -> Path:
    if zones is None:
        zones = [
            {"id": zid, "label": zid.upper(), "retain": retain,
             "directory": str(tmp / f"{zid}-images")}
             for zid, retain in (("default", 3), ("secondary", 2))
        ]
    lines = [
        f'listen_address = "{listen_address}"',
    ]
    if port is not None:
        lines.append(f"port = {port}")
    lines.append(f'max_upload_size = "{max_upload_size}"')
    if max_image_pixels is not None:
        lines.append(f"max_image_pixels = {max_image_pixels}")
    if trusted_proxies is not None:
        lines.append(f"trusted_proxies = {trusted_proxies}")
    if allowed_hosts is not None:
        lines.append(f"allowed_hosts = {allowed_hosts}")
    if allow_unauthenticated_local is not None:
        lines.append(
            f"allow_unauthenticated_local = {str(allow_unauthenticated_local).lower()}"
        )
    if allow_unauthenticated_remote is not None:
        lines.append(f"allow_unauthenticated_remote = {str(allow_unauthenticated_remote).lower()}")
    if allow_insecure_http_remote is not None:
        lines.append(
            f"allow_insecure_http_remote = {str(allow_insecure_http_remote).lower()}"
        )
    lines.append('log_level = "WARNING"')
    if tls_enabled or tls_certificate is not None or tls_private_key is not None:
        lines.extend(
            [
                "",
                "[tls]",
                f"enabled = {str(tls_enabled).lower()}",
            ]
        )
        if tls_certificate is not None:
            lines.append(f'certificate = "{tls_certificate}"')
        if tls_private_key is not None:
            lines.append(f'private_key = "{tls_private_key}"')
    lines.append("")
    lines.append("[auth]")
    lines.append(f"enabled = {str(auth_enabled).lower()}")
    if password_file is not None:
        lines.append(f'password_file = "{password_file}"')
    lines.append("")
    for zone in zones:
        lines.append("[[zones]]")
        lines.append(f'id = "{zone["id"]}"')
        lines.append(f'label = "{zone.get("label", zone["id"])}"')
        lines.append(f'type = "{zone.get("type", "local")}"')
        lines.append(f'directory = "{zone["directory"]}"')
        lines.append(f'retain = {zone.get("retain", 3)}')
        lines.append(f'reference_prefix = "{zone.get("reference_prefix", "@")}"')
        lines.append(f'reference_suffix = "{zone.get("reference_suffix", "")}"')
        lines.append(f'color = "{zone.get("color", "#243447")}"')
        zone_min_free = zone.get("min_free_percent", min_free_percent)
        if zone_min_free is not None:
            lines.append(f"min_free_percent = {zone_min_free}")
        lines.append("")
    text = "\n".join(lines) + extra
    cfg_path = tmp / "config.toml"
    cfg_path.write_text(text, encoding="utf-8")
    if password is not None:
        password_path = Path(password_file) if password_file else tmp / "passwd"
        save_password_hash(password_path, hash_password(password))
    return cfg_path


class LiveServer:
    """Serveur réel sur port éphémère, pour des tests d'intégration socket."""

    def __init__(self, cfg_path: Path):
        self.cfg = load_config(cfg_path)
        prepare_directories(self.cfg)
        self.service = PasteService(self.cfg)
        self.sessions = SessionStore(
            self.cfg.auth.session_ttl_hours * 3600,
            password_file=self.cfg.password_file() if self.cfg.auth.enabled else None,
        )
        self.limiter = LoginRateLimiter()
        handler = make_handler(self.cfg, self.service, self.sessions, self.limiter)
        self.httpd = PasteberthServer(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)

    def restart(self) -> None:
        """Simule un redémarrage du service : nouvelles instances, mêmes disques."""
        self.stop()
        fresh = LiveServer.__new__(LiveServer)
        from pasteberth.config import load_config as _lc
        from pasteberth.auth import SessionStore as _SS, LoginRateLimiter as _LR

        fresh.cfg = _lc(self.cfg.config_path)
        prepare_directories(fresh.cfg)
        fresh.service = PasteService(fresh.cfg)
        fresh.sessions = _SS(
            fresh.cfg.auth.session_ttl_hours * 3600,
            password_file=fresh.cfg.password_file() if fresh.cfg.auth.enabled else None,
        )
        fresh.limiter = _LR()
        handler = make_handler(fresh.cfg, fresh.service, fresh.sessions, fresh.limiter)
        fresh.httpd = PasteberthServer(("127.0.0.1", 0), handler)
        fresh.port = fresh.httpd.server_address[1]
        fresh.thread = threading.Thread(target=fresh.httpd.serve_forever, daemon=True)
        fresh.thread.start()
        self.__dict__.update(fresh.__dict__)


# ----------------------------------------------------------------- client


def request(
    port: int,
    method: str,
    path: str,
    body: bytes | None = None,
    headers: dict | None = None,
    cookie: str | None = None,
    timeout: float = 15,
) -> tuple[int, dict, bytes]:
    conn = HTTPConnection("127.0.0.1", port, timeout=timeout)
    hdrs = dict(headers or {})
    if cookie:
        hdrs["Cookie"] = cookie
    try:
        conn.request(method, path, body=body, headers=hdrs)
        resp = conn.getresponse()
        data = resp.read()
        resp_headers = {}
        for key, value in resp.getheaders():
            resp_headers.setdefault(key.lower(), []).append(value)
        flat = {k: v[0] for k, v in resp_headers.items()}
        flat["__all__"] = resp_headers
        status = resp.status
    finally:
        conn.close()
    return status, flat, data


def json_of(body: bytes) -> dict:
    return json.loads(body.decode("utf-8"))


def login(port: int, password: str) -> str:
    """Retourne le cookie de session après login réussi."""
    status, headers, _ = request(
        port,
        "POST",
        "/login",
        body=f"password={password}".encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert status == 303, f"login échoué : {status}"
    set_cookie = headers["set-cookie"]
    return set_cookie.split(";", 1)[0]  # "pb_session=<token>"
