"""Utilitaires de test : fabrication d'images, serveur réel éphémère,
client HTTP minimal (bibliothèque standard uniquement)."""
from __future__ import annotations

import binascii
import ctypes
import json
import platform
import struct
import threading
import zlib
from http.client import HTTPConnection
from pathlib import Path

from PasteBerth.runtime.auth import LoginRateLimiter, SessionStore, hash_password, save_password_hash
from PasteBerth.runtime.config import load_config, prepare_directories
from PasteBerth.runtime.server import PasteberthServer
from PasteBerth.runtime.service import PasteService
from PasteBerth.runtime.webapp import make_handler

REPO_ROOT = Path(__file__).resolve().parent.parent


def running_under_wine() -> bool:
    if platform.system() != "Windows":
        return False
    try:
        ctypes.WinDLL("ntdll").wine_get_version
    except (AttributeError, OSError):
        return False
    return True


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
    payload = bytes([0x00, 0, 0, 0])  # image statique, octets réservés nuls
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
    extra_fields: dict[str, bytes | str] | None = None,
) -> tuple[bytes, str]:
    body = bytearray()
    body += f"--{boundary}\r\n".encode()
    body += f'Content-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'.encode()
    body += f"Content-Type: {content_type}\r\n\r\n".encode()
    body += data
    for name, value in (extra_fields or {}).items():
        raw = value.encode() if isinstance(value, str) else value
        body += f"\r\n--{boundary}\r\n".encode()
        body += f'Content-Disposition: form-data; name="{name}"\r\n'.encode()
        body += b"Content-Type: text/plain\r\n\r\n"
        body += raw
    body += f"\r\n--{boundary}--\r\n".encode()
    return bytes(body), f"multipart/form-data; boundary={boundary}"


# ---------------------------------------------------------------- config


def write_config(
    tmp: Path,
    zones: list[dict] | None = None,
    groups: list[dict] | None = None,
    *,
    auth_enabled: bool = False,
    max_sessions: int | str | None = None,
    listen_address: str = "127.0.0.1",
    port: int | None = None,
    max_upload_size: str = "20MiB",
    max_image_pixels: int | str | None = None,
    url_prefix: str | None = None,
    trusted_proxies: str | None = '["127.0.0.1", "::1"]',
    allowed_hosts: str | None = None,
    allow_unauthenticated_local: bool | None = True,
    allow_unauthenticated_remote: bool | None = None,
    allow_insecure_http_remote: bool | None = None,
    accept_bin: bool | None = None,
    accept_img: bool | None = None,
    accept_doc: bool | None = None,
    show_full_path: bool | None = None,
    tls_enabled: bool = False,
    tls_certificate: str | None = None,
    tls_private_key: str | None = None,
    password_file: str | None = None,
    min_free_percent: float | None = None,
    limits: dict[str, object] | None = None,
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
        f"listen_address = {json.dumps(listen_address)}",
    ]
    if port is not None:
        lines.append(f"port = {port}")
    lines.append(f"max_upload_size = {json.dumps(max_upload_size)}")
    if max_image_pixels is not None:
        rendered_pixels = (
            json.dumps(max_image_pixels)
            if isinstance(max_image_pixels, str)
            else str(max_image_pixels)
        )
        lines.append(f"max_image_pixels = {rendered_pixels}")
    if url_prefix is not None:
        lines.append(f"url_prefix = {json.dumps(url_prefix)}")
    if trusted_proxies is not None:
        lines.append(f"trusted_proxies = {trusted_proxies}")
    if allowed_hosts is None and not auth_enabled:
        allowed_hosts = '["localhost", "127.0.0.1", "::1"]'
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
    if accept_bin is not None:
        lines.append(f"accept_bin = {str(accept_bin).lower()}")
    if accept_img is not None:
        lines.append(f"accept_img = {str(accept_img).lower()}")
    if accept_doc is not None:
        lines.append(f"accept_doc = {str(accept_doc).lower()}")
    if show_full_path is not None:
        lines.append(f"show_full_path = {str(show_full_path).lower()}")
    lines.append('log_level = "WARNING"')
    if limits:
        lines.extend(["", "[limits]"])
        for key, value in limits.items():
            if isinstance(value, str):
                rendered = json.dumps(value)
            elif isinstance(value, bool):
                rendered = str(value).lower()
            else:
                rendered = str(value)
            lines.append(f"{key} = {rendered}")
    if tls_enabled or tls_certificate is not None or tls_private_key is not None:
        lines.extend(
            [
                "",
                "[tls]",
                f"enabled = {str(tls_enabled).lower()}",
            ]
        )
        if tls_certificate is not None:
            lines.append(f"certificate = {json.dumps(tls_certificate)}")
        if tls_private_key is not None:
            lines.append(f"private_key = {json.dumps(tls_private_key)}")
    lines.append("")
    lines.append("[auth]")
    lines.append(f"enabled = {str(auth_enabled).lower()}")
    if max_sessions is not None:
        rendered_sessions = (
            json.dumps(max_sessions) if isinstance(max_sessions, str) else str(max_sessions)
        )
        lines.append(f"max_sessions = {rendered_sessions}")
    if password_file is not None:
        lines.append(f"password_file = {json.dumps(password_file)}")
    lines.append("")
    for zone in zones:
        lines.append("[[zones]]")
        lines.append(f"id = {json.dumps(zone['id'])}")
        lines.append(f"label = {json.dumps(zone.get('label', zone['id']))}")
        lines.append(f"type = {json.dumps(zone.get('type', 'local'))}")
        lines.append(f"directory = {json.dumps(str(zone['directory']))}")
        lines.append(f'retain = {zone.get("retain", 3)}')
        if zone.get("storage_mode") is not None:
            lines.append(f"storage_mode = {json.dumps(zone['storage_mode'])}")
        if zone.get("max_items") is not None:
            lines.append(f"max_items = {zone['max_items']}")
        lines.append(
            f"reference_prefix = {json.dumps(zone.get('reference_prefix', '@'))}"
        )
        lines.append(
            f"reference_suffix = {json.dumps(zone.get('reference_suffix', ''))}"
        )
        lines.append(
            f"reference_list_prefix = {json.dumps(zone.get('reference_list_prefix', ''))}"
        )
        lines.append(
            f"reference_list_suffix = {json.dumps(zone.get('reference_list_suffix', ''))}"
        )
        lines.append(
            f"reference_separator = {json.dumps(zone.get('reference_separator', ','))}"
        )
        lines.append(
            f'allow_zip_download = {str(zone.get("allow_zip_download", True)).lower()}'
        )
        lines.append(f"color = {json.dumps(zone.get('color', '#243447'))}")
        zone_min_free = zone.get("min_free_percent", min_free_percent)
        if zone_min_free is not None:
            lines.append(f"min_free_percent = {zone_min_free}")
        lines.append("")
    if groups is not None:
        for group in groups:
            lines.append("[[groups]]")
            lines.append(f"name = {json.dumps(group['name'])}")
            if "selection" in group:
                lines.append(f"selection = {json.dumps(group['selection'])}")
            if "pattern" in group:
                lines.append(f"pattern = {json.dumps(group['pattern'])}")
            if "layout" in group:
                lines.append(f"layout = {json.dumps(group['layout'])}")
            if "hide_empty" in group:
                lines.append(f"hide_empty = {str(group['hide_empty']).lower()}")
            if "show_count" in group:
                lines.append(f"show_count = {str(group['show_count']).lower()}")
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
            max_sessions=self.cfg.auth.max_sessions,
        )
        self.limiter = LoginRateLimiter(
            max_concurrent_checks=self.cfg.limits.max_login_concurrent_checks,
            max_tracked_ips=self.cfg.limits.max_login_tracked_ips,
            max_delay=self.cfg.limits.max_login_delay_seconds,
            forget_after=self.cfg.limits.login_forget_after_seconds,
        )
        handler = make_handler(self.cfg, self.service, self.sessions, self.limiter)
        self.httpd = PasteberthServer(("127.0.0.1", 0), handler, limits=self.cfg.limits)
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
        from PasteBerth.runtime.config import load_config as _lc
        from PasteBerth.runtime.auth import SessionStore as _SS, LoginRateLimiter as _LR

        fresh.cfg = _lc(self.cfg.config_path)
        prepare_directories(fresh.cfg)
        fresh.service = PasteService(fresh.cfg)
        fresh.sessions = _SS(
            fresh.cfg.auth.session_ttl_hours * 3600,
            password_file=fresh.cfg.password_file() if fresh.cfg.auth.enabled else None,
            max_sessions=fresh.cfg.auth.max_sessions,
        )
        fresh.limiter = _LR(
            max_concurrent_checks=fresh.cfg.limits.max_login_concurrent_checks,
            max_tracked_ips=fresh.cfg.limits.max_login_tracked_ips,
            max_delay=fresh.cfg.limits.max_login_delay_seconds,
            forget_after=fresh.cfg.limits.login_forget_after_seconds,
        )
        handler = make_handler(fresh.cfg, fresh.service, fresh.sessions, fresh.limiter)
        fresh.httpd = PasteberthServer(
            ("127.0.0.1", 0), handler, limits=fresh.cfg.limits
        )
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
