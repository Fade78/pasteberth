"""TOML configuration loading and validation.

The deployment bundle is read-only. Configuration and default storage live in
the user's XDG locations unless an explicit path is supplied.
"""
from __future__ import annotations

import ipaddress
import logging
import math
import os
import re
import socket
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from .auth import DEFAULT_MAX_SESSIONS
from .platformfs import UnsupportedFilesystemError, platform_fs


log = logging.getLogger("pasteberth.config")

try:
    import tomllib
except ModuleNotFoundError as exc:  # Python < 3.11
    raise SystemExit("Pasteberth requires Python 3.11+ (tomllib module)") from exc


class ConfigError(Exception):
    """Fatal configuration error (message intended for the user)."""


_ZONE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR"}
_GROUP_SELECTIONS = {"all", "pattern", "other"}
_GROUP_LAYOUTS = {"area", "tab"}
_STORAGE_MODES = {"directory", "sidecar"}
_AUTOZONE_LABEL_MODES = {"git-or-relative", "relative"}
_URL_PREFIX_RE = re.compile(r"/(?:[A-Za-z0-9._~-]+)(?:/[A-Za-z0-9._~-]+)*")

DEFAULT_MAX_UPLOAD_BYTES = 20 * 1024**2
DEFAULT_MAX_IMAGE_PIXELS = 25_000_000
DEFAULT_MIN_FREE_PERCENT = 2.0
_SIZE_UNITS = {
    "": 1,
    "B": 1,
    "K": 1024, "KB": 1024, "KIB": 1024,
    "M": 1024**2, "MB": 1024**2, "MIB": 1024**2,
    "G": 1024**3, "GB": 1024**3, "GIB": 1024**3,
}


def parse_size(value: object) -> int:
    """Parse a size such as ``20MB``, ``512KiB``, or ``4096`` (bytes)."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ConfigError(f"invalid size: {value!r}")
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            raise ConfigError(f"invalid size: {value!r}")
        try:
            n = int(value)
        except (ValueError, OverflowError) as exc:
            raise ConfigError(f"invalid size: {value!r}") from exc
    else:
        m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([A-Za-z]*)\s*", value)
        if not m:
            raise ConfigError(f"invalid size: {value!r} (examples: 20MB, 512KiB, 4096)")
        unit = m.group(2).upper()
        if unit not in _SIZE_UNITS:
            raise ConfigError(f"unknown size unit: {m.group(2)!r}")
        try:
            n = int(float(m.group(1)) * _SIZE_UNITS[unit])
        except (ValueError, OverflowError) as exc:
            raise ConfigError(f"invalid size: {value!r}") from exc
    if n <= 0:
        raise ConfigError(f"size must be positive: {value!r}")
    return n


def _parse_limit(value: object, label: str, *, size: bool = False) -> int | None:
    """Parse a positive configured limit; ``unlimited`` disables it."""
    if value is None or (
        isinstance(value, str) and value.strip().lower() in {"none", "unlimited"}
    ):
        return None
    try:
        parsed = parse_size(value) if size else value
    except ConfigError as exc:
        raise ConfigError(f"'{label}' must be a positive integer or 'unlimited'") from exc
    if isinstance(parsed, bool) or not isinstance(parsed, int) or parsed <= 0:
        raise ConfigError(f"'{label}' must be a positive integer or 'unlimited'")
    return parsed


def is_loopback_address(address: str) -> bool:
    """Return true when the listener exposes the service only locally."""
    address = address.strip()
    try:
        return ipaddress.ip_address(address).is_loopback
    except ValueError:
        pass
    # A hostname is loopback-only only when ALL resolutions are local.
    try:
        infos = socket.getaddrinfo(address, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise ConfigError(f"listener address cannot be resolved: {address!r} ({exc})") from exc
    if not infos:
        raise ConfigError(f"listener address cannot be resolved: {address!r}")
    return all(ipaddress.ip_address(info[4][0]).is_loopback for info in infos)


@dataclass(frozen=True)
class AuthConfig:
    enabled: bool = True
    session_ttl_hours: int = 72
    max_sessions: int | None = DEFAULT_MAX_SESSIONS
    password_file: Path | None = None


@dataclass(frozen=True)
class TLSConfig:
    enabled: bool = False
    certificate: Path | None = None
    private_key: Path | None = None


@dataclass(frozen=True)
class ZoneConfig:
    id: str
    label: str
    directory: Path
    retain: int
    reference_prefix: str = "@"
    reference_suffix: str = ""
    reference_list_prefix: str = ""
    reference_list_suffix: str = ""
    reference_separator: str = ","
    allow_zip_download: bool = True
    color: str = "#243447"
    create_directory: bool = True
    min_free_percent: float = DEFAULT_MIN_FREE_PERCENT
    storage_mode: str = "sidecar"
    max_items: int | None = None


@dataclass(frozen=True)
class AutoZoneConfig:
    base_directory: Path
    pattern: str
    max_depth: int
    group: str
    label_mode: str = "git-or-relative"
    storage_mode: str = "directory"
    max_items: int = 1
    min_free_percent: float = DEFAULT_MIN_FREE_PERCENT
    reference_prefix: str = "@"
    reference_suffix: str = ""
    reference_list_prefix: str = ""
    reference_list_suffix: str = ""
    reference_separator: str = ","
    allow_zip_download: bool = True
    color: str = "#243447"
    group_layout: str = "area"
    group_hide_empty: bool = False
    group_show_count: bool = True


@dataclass(frozen=True)
class GroupConfig:
    name: str
    selection: str = "pattern"
    pattern: tuple[str, ...] = ()
    pattern_defined: bool = False
    layout: str = "area"
    hide_empty: bool = False
    show_count: bool = True
    members: tuple[str, ...] = ()


@dataclass(frozen=True)
class LimitsConfig:
    """Resource budgets that are intentionally controlled by the operator."""

    max_image_dimension: int | None = 16_384
    max_image_raw_bytes: int | None = 256 * 1024**2
    max_filename_length: int | None = 200
    max_filename_bytes: int | None = 240
    max_png_chunks: int | None = 100_000
    max_jpeg_segments: int | None = 100_000
    max_webp_chunks: int | None = 100_000
    max_mime_length: int | None = 120
    max_multipart_boundary_length: int | None = 70
    max_multipart_parts: int | None = 32
    max_multipart_header_bytes: int | None = 8 * 1024
    max_multipart_field_name_length: int | None = 256
    max_batch_names: int | None = 10_000
    max_batch_body_bytes: int | None = 2 * 1024**2
    max_comment_body_bytes: int | None = 8 * 1024
    max_http_header_bytes: int | None = 64 * 1024
    max_login_body_bytes: int | None = 4 * 1024
    max_login_fields: int | None = 8
    max_login_delay_seconds: float | None = 900.0
    max_login_concurrent_checks: int | None = 4
    max_login_tracked_ips: int | None = 4096
    login_forget_after_seconds: float | None = 3600.0
    max_scrypt_memory_bytes: int | None = 64 * 1024**2
    max_password_file_bytes: int | None = 16 * 1024
    max_metadata_bytes: int | None = 64 * 1024
    max_comment_length: int | None = 280
    max_comment_bytes: int | None = 1024
    request_queue_size: int = 32
    max_active_requests: int | None = 64
    max_pending_requests: int | None = 8
    http_header_timeout_seconds: float | None = 5.0
    http_request_timeout_seconds: float | None = 60.0


@dataclass(frozen=True)
class Config:
    listen_address: str
    port: int
    max_upload_bytes: int | None
    max_image_pixels: int | None
    limits: LimitsConfig
    trusted_proxies: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]
    allowed_hosts: tuple[str, ...]
    url_prefix: str
    show_full_path: bool
    allow_unauthenticated_local: bool
    allow_unauthenticated_remote: bool
    allow_insecure_http_remote: bool
    accept_bin: bool
    accept_img: bool
    accept_doc: bool
    auth: AuthConfig
    tls: TLSConfig
    zones: dict[str, ZoneConfig]
    autozones: tuple[AutoZoneConfig, ...]
    groups: tuple[GroupConfig, ...]
    log_level: str
    config_path: Path
    warnings: list[str] = field(default_factory=list)
    using_default_config: bool = False

    def password_file(self) -> Path:
        """Return the password hash location (0600, never a symlink)."""
        return self.auth.password_file or self.config_path.parent / "passwd"


def _expect_table(value: object, where: str) -> dict:
    if not isinstance(value, dict):
        raise ConfigError(f"{where} must be a table")
    return value


def _get_str(table: dict, key: str, where: str, *, default: str | None = None,
             allow_empty: bool = False) -> str:
    if key not in table:
        if default is None:
            raise ConfigError(f"{where}: missing key '{key}'")
        return default
    value = table[key]
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise ConfigError(f"{where}: '{key}' must be a non-empty string")
    return value


def _get_bool(table: dict, key: str, where: str, default: bool) -> bool:
    if key not in table:
        return default
    value = table[key]
    if not isinstance(value, bool):
        raise ConfigError(f"{where}: '{key}' must be a boolean")
    return value


def _get_percent(table: dict, key: str, where: str, default: float) -> float:
    if key not in table:
        return default
    value = table[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{where}: '{key}' must be a number between 0 and 99.99")
    value = float(value)
    if not (0.0 <= value < 100.0):
        raise ConfigError(f"{where}: '{key}' must be a number between 0 and 99.99")
    return value


def _relative_luminance(color: str) -> float:
    channels = []
    for offset in (1, 3, 5):
        value = int(color[offset:offset + 2], 16) / 255.0
        channels.append(
            value / 12.92 if value <= 0.04045
            else ((value + 0.055) / 1.055) ** 2.4
        )
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]


def _best_contrast(color: str) -> float:
    background = _relative_luminance(color)
    candidates = (_relative_luminance("#12161b"), _relative_luminance("#f3f6fa"))
    return max(
        (max(background, foreground) + 0.05) / (min(background, foreground) + 0.05)
        for foreground in candidates
    )


def _warn_unknown(table: dict, known: set[str], where: str, warnings: list[str]) -> None:
    for key in sorted(set(table) - known):
        suggestion = "; use 'groups'" if where == "config" and key == "groupes" else ""
        warnings.append(f"{where}: unknown key '{key}' ignored {suggestion}".rstrip())


def _parse_limits(raw: object, warnings: list[str]) -> LimitsConfig:
    if raw is None:
        return LimitsConfig()
    table = _expect_table(raw, "[limits]")
    keys = {
        "max_image_dimension",
        "max_image_raw_size",
        "max_filename_length",
        "max_filename_size",
        "max_png_chunks",
        "max_jpeg_segments",
        "max_webp_chunks",
        "max_mime_length",
        "max_multipart_boundary_length",
        "max_multipart_parts",
        "max_multipart_header_size",
        "max_multipart_field_name_length",
        "max_batch_names",
        "max_batch_body_size",
        "max_comment_body_size",
        "max_http_header_size",
        "max_login_body_size",
        "max_login_fields",
        "max_login_delay_seconds",
        "max_login_concurrent_checks",
        "max_login_tracked_ips",
        "login_forget_after_seconds",
        "max_scrypt_memory_size",
        "max_password_file_size",
        "max_metadata_size",
        "max_comment_length",
        "max_comment_bytes",
        "request_queue_size",
        "max_active_requests",
        "max_pending_requests",
        "http_header_timeout_seconds",
        "http_request_timeout_seconds",
    }
    _warn_unknown(table, keys, "[limits]", warnings)

    def integer(key: str, default: int | None) -> int | None:
        return _parse_limit(table.get(key, default), f"[limits] '{key}'")

    def size(key: str, default: int | None) -> int | None:
        return _parse_limit(table.get(key, default), f"[limits] '{key}'", size=True)

    def seconds(key: str, default: float | None) -> float | None:
        value = table.get(key, default)
        if value is None or (
            isinstance(value, str) and value.strip().lower() in {"none", "unlimited"}
        ):
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError(f"[limits] '{key}' must be a positive number or 'unlimited'")
        value = float(value)
        if value <= 0 or not math.isfinite(value):
            raise ConfigError(f"[limits] '{key}' must be a positive number or 'unlimited'")
        return value

    request_queue_size = integer("request_queue_size", 32)
    if request_queue_size is None:
        raise ConfigError("[limits] 'request_queue_size' cannot be unlimited")

    return LimitsConfig(
        max_image_dimension=integer("max_image_dimension", 16_384),
        max_image_raw_bytes=size("max_image_raw_size", 256 * 1024**2),
        max_filename_length=integer("max_filename_length", 200),
        max_filename_bytes=size("max_filename_size", 240),
        max_png_chunks=integer("max_png_chunks", 100_000),
        max_jpeg_segments=integer("max_jpeg_segments", 100_000),
        max_webp_chunks=integer("max_webp_chunks", 100_000),
        max_mime_length=integer("max_mime_length", 120),
        max_multipart_boundary_length=integer("max_multipart_boundary_length", 70),
        max_multipart_parts=integer("max_multipart_parts", 32),
        max_multipart_header_bytes=size("max_multipart_header_size", 8 * 1024),
        max_multipart_field_name_length=integer("max_multipart_field_name_length", 256),
        max_batch_names=integer("max_batch_names", 10_000),
        max_batch_body_bytes=size("max_batch_body_size", 2 * 1024**2),
        max_comment_body_bytes=size("max_comment_body_size", 8 * 1024),
        max_http_header_bytes=size("max_http_header_size", 64 * 1024),
        max_login_body_bytes=size("max_login_body_size", 4 * 1024),
        max_login_fields=integer("max_login_fields", 8),
        max_login_delay_seconds=seconds("max_login_delay_seconds", 900.0),
        max_login_concurrent_checks=integer("max_login_concurrent_checks", 4),
        max_login_tracked_ips=integer("max_login_tracked_ips", 4096),
        login_forget_after_seconds=seconds("login_forget_after_seconds", 3600.0),
        max_scrypt_memory_bytes=size("max_scrypt_memory_size", 64 * 1024**2),
        max_password_file_bytes=size("max_password_file_size", 16 * 1024),
        max_metadata_bytes=size("max_metadata_size", 64 * 1024),
        max_comment_length=integer("max_comment_length", 280),
        max_comment_bytes=size("max_comment_bytes", 1024),
        request_queue_size=request_queue_size,
        max_active_requests=integer("max_active_requests", 64),
        max_pending_requests=integer("max_pending_requests", 8),
        http_header_timeout_seconds=seconds("http_header_timeout_seconds", 5.0),
        http_request_timeout_seconds=seconds("http_request_timeout_seconds", 60.0),
    )


def _parse_auth(raw: object, warnings: list[str]) -> AuthConfig:
    if raw is None:
        return AuthConfig()
    table = _expect_table(raw, "[auth]")
    _warn_unknown(
        table,
        {"enabled", "session_ttl_hours", "max_sessions", "password_file"},
        "[auth]",
        warnings,
    )
    enabled = _get_bool(table, "enabled", "[auth]", default=True)
    ttl = table.get("session_ttl_hours", 72)
    if isinstance(ttl, bool) or not isinstance(ttl, int) or ttl < 1:
        raise ConfigError("[auth]: 'session_ttl_hours' must be a positive integer")
    max_sessions = _parse_limit(
        table.get("max_sessions", DEFAULT_MAX_SESSIONS),
        "[auth] 'max_sessions'",
    )
    password_file_raw = table.get("password_file")
    password_file = None
    if password_file_raw is not None:
        if not isinstance(password_file_raw, str) or not password_file_raw.strip():
            raise ConfigError("[auth]: 'password_file' must be an absolute path")
        if "\x00" in password_file_raw:
            raise ConfigError("[auth]: 'password_file' contains a NUL character")
        password_file = Path(os.path.expanduser(password_file_raw))
        if not password_file.is_absolute():
            raise ConfigError("[auth]: 'password_file' must be an absolute path")
        password_file = ensure_external_path(password_file, "[auth] 'password_file'")
    if enabled and "password_hash" in table:
        warnings.append(
            "[auth]: 'password_hash' in config.toml is ignored; "
            "`pasteberth passwd` writes the hash to the configured 'passwd' file"
        )
    return AuthConfig(
        enabled=enabled,
        session_ttl_hours=ttl,
        max_sessions=max_sessions,
        password_file=password_file,
    )


def _parse_tls(raw: object, warnings: list[str]) -> TLSConfig:
    if raw is None:
        return TLSConfig()
    table = _expect_table(raw, "[tls]")
    _warn_unknown(table, {"enabled", "certificate", "private_key"}, "[tls]", warnings)
    enabled = _get_bool(table, "enabled", "[tls]", default=False)
    certificate_raw = table.get("certificate")
    private_key_raw = table.get("private_key")
    if not enabled:
        return TLSConfig(enabled=False)
    if not isinstance(certificate_raw, str) or not certificate_raw.strip():
        raise ConfigError("[tls]: 'certificate' must be an absolute path")
    if not isinstance(private_key_raw, str) or not private_key_raw.strip():
        raise ConfigError("[tls]: 'private_key' must be an absolute path")
    if "\x00" in certificate_raw or "\x00" in private_key_raw:
        raise ConfigError("[tls]: paths cannot contain a NUL character")
    certificate = Path(os.path.expanduser(certificate_raw))
    private_key = Path(os.path.expanduser(private_key_raw))
    if not certificate.is_absolute() or not private_key.is_absolute():
        raise ConfigError("[tls]: 'certificate' and 'private_key' must be absolute")
    certificate = ensure_external_path(certificate, "[tls] 'certificate'")
    private_key = ensure_external_path(private_key, "[tls] 'private_key'")
    return TLSConfig(enabled=True, certificate=certificate, private_key=private_key)


def _parse_zone(raw_zone: object, index: int, warnings: list[str]) -> ZoneConfig:
    where = f"[[zones]] #{index + 1}"
    table = _expect_table(raw_zone, where)
    _warn_unknown(
        table,
        {"id", "label", "type", "directory", "retain", "storage_mode", "max_items",
         "reference_prefix", "reference_suffix",
         "reference_list_prefix", "reference_list_suffix", "reference_separator",
         "allow_zip_download", "color", "create_directory", "min_free_percent"},
        where,
        warnings,
    )
    ztype = _get_str(table, "type", where, default="local").lower()
    if ztype != "local":
        raise ConfigError(
            f"{where}: destination type '{ztype}' is not supported in V1 "
            "(only 'local' is implemented)"
        )
    zid = _get_str(table, "id", where)
    if not _ZONE_ID_RE.fullmatch(zid):
        raise ConfigError(
            f"{where}: invalid zone ID: {zid!r} "
            "(allowed characters: a-z, digits, '-', '_')"
        )
    label = _get_str(table, "label", where, default=zid)
    directory_raw = _get_str(table, "directory", where)
    directory = Path(os.path.expanduser(directory_raw))
    if not directory.is_absolute():
        raise ConfigError(
            f"{where}: 'directory' must be an absolute path "
            f"(relative to the Pasteberth server, not the browser): {directory_raw!r}"
        )
    if "\x00" in directory_raw:
        raise ConfigError(f"{where}: 'directory' contains a NUL character")
    directory = ensure_external_path(directory, f"{where} 'directory'")
    retain = table.get("retain", 10)
    if isinstance(retain, bool) or not isinstance(retain, int) or retain < 1:
        raise ConfigError(f"{where}: 'retain' must be a positive integer")
    storage_mode = _get_str(table, "storage_mode", where, default="sidecar").lower()
    if storage_mode not in _STORAGE_MODES:
        raise ConfigError(
            f"{where}: 'storage_mode' must be one of {sorted(_STORAGE_MODES)}"
        )
    max_items = table.get("max_items")
    if max_items is not None and (
        isinstance(max_items, bool) or not isinstance(max_items, int) or max_items < 1
    ):
        raise ConfigError(f"{where}: 'max_items' must be a positive integer")
    if storage_mode == "directory" and max_items is None:
        raise ConfigError(f"{where}: 'max_items' is required for directory storage")
    if storage_mode == "directory" and "retain" in table:
        warnings.append(f"{where}: 'retain' is ignored for directory storage")
    if storage_mode == "sidecar" and max_items is not None:
        warnings.append(f"{where}: 'max_items' is ignored for sidecar storage")
    prefix = _get_str(table, "reference_prefix", where, default="@", allow_empty=True)
    suffix = _get_str(table, "reference_suffix", where, default="", allow_empty=True)
    list_prefix = _get_str(
        table, "reference_list_prefix", where, default="", allow_empty=True
    )
    list_suffix = _get_str(
        table, "reference_list_suffix", where, default="", allow_empty=True
    )
    separator = _get_str(
        table, "reference_separator", where, default=",", allow_empty=True
    )
    allow_zip_download = _get_bool(table, "allow_zip_download", where, default=True)
    color = _get_str(table, "color", where, default="#243447")
    if not _COLOR_RE.fullmatch(color):
        raise ConfigError(f"{where}: 'color' must use #RRGGBB format: {color!r}")
    color = color.lower()
    if _best_contrast(color) < 4.5:
        raise ConfigError(
            f"{where}: 'color' does not provide sufficient text contrast: {color!r}"
        )
    create_dir = _get_bool(table, "create_directory", where, default=True)
    min_free_percent = _get_percent(
        table, "min_free_percent", where, DEFAULT_MIN_FREE_PERCENT
    )
    return ZoneConfig(
        id=zid,
        label=label,
        directory=directory,
        retain=retain,
        reference_prefix=prefix,
        reference_suffix=suffix,
        reference_list_prefix=list_prefix,
        reference_list_suffix=list_suffix,
        reference_separator=separator,
        allow_zip_download=allow_zip_download,
        color=color,
        create_directory=create_dir,
        min_free_percent=min_free_percent,
        storage_mode=storage_mode,
        max_items=max_items,
    )


def _parse_autozone(raw_autozone: object, index: int, warnings: list[str]) -> AutoZoneConfig:
    where = f"[[autozone]] #{index + 1}"
    table = _expect_table(raw_autozone, where)
    _warn_unknown(
        table,
        {
            "base_directory", "pattern", "max_depth", "group", "label_mode",
            "storage_mode", "max_items", "min_free_percent", "retain",
            "reference_prefix", "reference_suffix", "reference_list_prefix",
            "reference_list_suffix", "reference_separator", "allow_zip_download",
            "color", "group_layout", "group_hide_empty", "group_show_count",
        },
        where,
        warnings,
    )
    base_raw = _get_str(table, "base_directory", where)
    if "\x00" in base_raw:
        raise ConfigError(f"{where}: 'base_directory' contains a NUL character")
    base_directory = Path(os.path.expanduser(base_raw))
    if not base_directory.is_absolute():
        raise ConfigError(
            f"{where}: 'base_directory' must be an absolute path: {base_raw!r}"
        )
    base_directory = ensure_external_path(base_directory, f"{where} 'base_directory'")

    pattern = _get_str(table, "pattern", where)
    try:
        re.compile(pattern)
    except re.error as exc:
        raise ConfigError(
            f"{where}: invalid regular expression in 'pattern' {pattern!r} ({exc})"
        ) from exc

    max_depth = table.get("max_depth", 4)
    if isinstance(max_depth, bool) or not isinstance(max_depth, int) or max_depth < 1:
        raise ConfigError(f"{where}: 'max_depth' must be a positive integer")
    group = _get_str(table, "group", where)
    label_mode = _get_str(table, "label_mode", where, default="git-or-relative").lower()
    if label_mode not in _AUTOZONE_LABEL_MODES:
        raise ConfigError(
            f"{where}: 'label_mode' must be one of {sorted(_AUTOZONE_LABEL_MODES)}"
        )
    storage_mode = _get_str(table, "storage_mode", where, default="directory").lower()
    if storage_mode != "directory":
        raise ConfigError(f"{where}: 'storage_mode' must be 'directory'")
    max_items = table.get("max_items")
    if isinstance(max_items, bool) or not isinstance(max_items, int) or max_items < 1:
        raise ConfigError(
            f"{where}: 'max_items' must be a positive integer for directory storage"
        )
    if "retain" in table:
        warnings.append(f"{where}: 'retain' is ignored for directory storage")

    min_free_percent = _get_percent(
        table, "min_free_percent", where, DEFAULT_MIN_FREE_PERCENT
    )
    prefix = _get_str(table, "reference_prefix", where, default="@", allow_empty=True)
    suffix = _get_str(table, "reference_suffix", where, default="", allow_empty=True)
    list_prefix = _get_str(
        table, "reference_list_prefix", where, default="", allow_empty=True
    )
    list_suffix = _get_str(
        table, "reference_list_suffix", where, default="", allow_empty=True
    )
    separator = _get_str(
        table, "reference_separator", where, default=",", allow_empty=True
    )
    allow_zip_download = _get_bool(table, "allow_zip_download", where, default=True)
    color = _get_str(table, "color", where, default="#243447")
    if not _COLOR_RE.fullmatch(color):
        raise ConfigError(f"{where}: 'color' must use #RRGGBB format: {color!r}")
    color = color.lower()
    if _best_contrast(color) < 4.5:
        raise ConfigError(
            f"{where}: 'color' does not provide sufficient text contrast: {color!r}"
        )
    group_layout = _get_str(table, "group_layout", where, default="area").lower()
    if group_layout not in _GROUP_LAYOUTS:
        raise ConfigError(
            f"{where}: 'group_layout' must be one of {sorted(_GROUP_LAYOUTS)}"
        )
    group_hide_empty = _get_bool(table, "group_hide_empty", where, default=False)
    group_show_count = _get_bool(table, "group_show_count", where, default=True)
    return AutoZoneConfig(
        base_directory=base_directory,
        pattern=pattern,
        max_depth=max_depth,
        group=group,
        label_mode=label_mode,
        storage_mode=storage_mode,
        max_items=max_items,
        min_free_percent=min_free_percent,
        reference_prefix=prefix,
        reference_suffix=suffix,
        reference_list_prefix=list_prefix,
        reference_list_suffix=list_suffix,
        reference_separator=separator,
        allow_zip_download=allow_zip_download,
        color=color,
        group_layout=group_layout,
        group_hide_empty=group_hide_empty,
        group_show_count=group_show_count,
    )


def _parse_groups(raw: object, warnings: list[str]) -> tuple[GroupConfig, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ConfigError("'groups' must be a list of tables")
    groups = []
    seen_names = set()
    for index, item in enumerate(raw):
        where = f"[[groups]] #{index + 1}"
        if not isinstance(item, dict):
            raise ConfigError(f"{where}: must be a table")
        _warn_unknown(
            item,
            {"name", "selection", "pattern", "layout", "hide_empty", "show_count"},
            where,
            warnings,
        )
        name = _get_str(item, "name", where)
        if name in seen_names:
            raise ConfigError(f"{where}: duplicate group name: {name!r}")
        seen_names.add(name)
        selection = _get_str(item, "selection", where, default="pattern").lower()
        if selection not in _GROUP_SELECTIONS:
            raise ConfigError(
                f"{where}: 'selection' must be one of {sorted(_GROUP_SELECTIONS)}"
            )
        raw_pattern = item.get("pattern")
        pattern_defined = "pattern" in item
        if raw_pattern is not None and (
            not isinstance(raw_pattern, list)
            or not all(isinstance(pattern, str) for pattern in raw_pattern)
        ):
            raise ConfigError(f"{where}: 'pattern' must be a list of strings")
        pattern = tuple(raw_pattern or ())
        if selection == "pattern" and not pattern:
            raise ConfigError(f"{where}: 'pattern' cannot be empty")
        if selection == "pattern":
            for expression in pattern:
                try:
                    re.compile(expression)
                except re.error as exc:
                    raise ConfigError(
                        f"{where}: invalid regular expression in 'pattern' "
                        f"{expression!r} ({exc})"
                    ) from exc
        layout = _get_str(item, "layout", where, default="area").lower()
        if layout not in _GROUP_LAYOUTS:
            raise ConfigError(
                f"{where}: 'layout' must be one of {sorted(_GROUP_LAYOUTS)}"
            )
        hide_empty = _get_bool(item, "hide_empty", where, default=False)
        show_count = _get_bool(item, "show_count", where, default=True)
        groups.append(GroupConfig(
            name=name,
            selection=selection,
            pattern=pattern,
            pattern_defined=pattern_defined,
            layout=layout,
            hide_empty=hide_empty,
            show_count=show_count,
        ))
    return tuple(groups)


def resolve_group_zone_ids(
    groups: tuple[GroupConfig, ...], zone_ids: Iterable[str],
) -> dict[str, tuple[str, ...]]:
    """Compute effective group zones without accessing storage."""
    ordered_zone_ids = tuple(zone_ids)
    pattern_zone_ids: set[str] = set()
    resolved: dict[str, tuple[str, ...]] = {}
    for group in groups:
        explicit_zone_ids = set(group.members)
        if group.selection == "all":
            resolved[group.name] = ordered_zone_ids
        elif group.selection == "autozone":
            resolved[group.name] = tuple(
                zid for zid in ordered_zone_ids if zid in explicit_zone_ids
            )
        elif group.selection == "pattern":
            expressions = tuple(re.compile(pattern) for pattern in group.pattern)
            matching = tuple(
                zid
                for zid in ordered_zone_ids
                if zid in explicit_zone_ids
                or any(expression.search(zid) for expression in expressions)
            )
            resolved[group.name] = matching
            pattern_zone_ids.update(
                zid
                for zid in matching
                if any(expression.search(zid) for expression in expressions)
            )
    for group in groups:
        if group.selection == "other":
            explicit_zone_ids = set(group.members)
            resolved[group.name] = tuple(
                zid for zid in ordered_zone_ids
                if zid in explicit_zone_ids or zid not in pattern_zone_ids
            )
    return resolved


def _parse_trusted_proxies(raw: object, warnings: list[str]) -> tuple:
    if raw is None:
        return ()
    if not isinstance(raw, list) or not all(isinstance(v, str) for v in raw):
        raise ConfigError("'trusted_proxies' must be a list of strings (IP or CIDR)")
    networks = []
    for item in raw:
        try:
            networks.append(ipaddress.ip_network(item, strict=False))
        except ValueError as exc:
            raise ConfigError(f"'trusted_proxies': invalid entry {item!r} ({exc})") from exc
    return tuple(networks)


def _parse_allowed_hosts(raw: object) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list) or not all(isinstance(v, str) for v in raw):
        raise ConfigError("'allowed_hosts' must be a list of hostnames")
    hosts: list[str] = []
    for item in raw:
        host = item.strip().lower().rstrip(".")
        if not host:
            raise ConfigError("'allowed_hosts' cannot contain an empty value")
        if host.startswith("[") and host.endswith("]"):
            host = host[1:-1]
        try:
            host = str(ipaddress.ip_address(host)).lower()
        except ValueError:
            if (
                len(host) > 253
                or not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", host)
                or ".." in host
            ):
                raise ConfigError(f"'allowed_hosts': invalid hostname {item!r}")
        if host not in hosts:
            hosts.append(host)
    return tuple(hosts)


def _parse_url_prefix(raw: object) -> str:
    """Validate a publication prefix without ambiguous path syntax."""
    if raw is None or raw == "":
        return ""
    if not isinstance(raw, str) or not _URL_PREFIX_RE.fullmatch(raw):
        raise ConfigError(
            "'url_prefix' must be empty or a path such as '/paste' "
            "without a trailing slash, query, or fragment"
        )
    if any(segment in {".", ".."} for segment in raw.split("/")[1:]):
        raise ConfigError("'url_prefix' cannot contain a '.' or '..' segment")
    return raw


def public_path(prefix: str, path: str) -> str:
    """Build a public path from an absolute application path."""
    if not path.startswith("/"):
        raise ValueError("a public path must start with '/'")
    if not prefix:
        return path
    if path == prefix or path.startswith(prefix + "/"):
        return path
    return prefix + ("/" if path == "/" else path)


def load_config(path: Path) -> Config:
    """Load, validate, and return the configuration."""
    path = ensure_external_path(Path(path), "configuration")
    try:
        raw_bytes = path.read_bytes()
    except FileNotFoundError as exc:
        raise ConfigError(f"configuration file not found: {path}") from exc
    except ValueError as exc:
        raise ConfigError(f"invalid configuration path: {path}") from exc
    except OSError as exc:
        raise ConfigError(f"configuration file cannot be read: {path} ({exc})") from exc
    try:
        data = tomllib.loads(raw_bytes.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise ConfigError(f"invalid TOML in {path}: {exc}") from exc

    warnings: list[str] = []
    _warn_unknown(
        data,
        {"listen_address", "port", "max_upload_size", "trusted_proxies",
           "max_image_pixels",
          "limits",
          "allowed_hosts", "url_prefix",
          "show_full_path", "allow_unauthenticated_local", "allow_unauthenticated_remote",
         "allow_insecure_http_remote", "accept_bin", "accept_img", "accept_doc",
         "auth", "tls",
         "zones", "autozone", "groups", "log_level"},
        "config",
        warnings,
    )

    listen = _get_str(data, "listen_address", "config", default="127.0.0.1")
    port = data.get("port", 8765)
    if isinstance(port, bool) or not isinstance(port, int) or not (1 <= port <= 65535):
        raise ConfigError("'port' must be an integer between 1 and 65535")

    max_upload_bytes = _parse_limit(
        data.get("max_upload_size", DEFAULT_MAX_UPLOAD_BYTES),
        "max_upload_size",
        size=True,
    )
    max_image_pixels = _parse_limit(
        data.get("max_image_pixels", DEFAULT_MAX_IMAGE_PIXELS),
        "max_image_pixels",
    )
    limits = _parse_limits(data.get("limits"), warnings)

    trusted_proxies = _parse_trusted_proxies(data.get("trusted_proxies"), warnings)
    # The public hostname is deployment-specific; an absent key preserves the
    # multi-station wildcard compatibility, while a non-empty list is strict.
    allowed_hosts = _parse_allowed_hosts(data.get("allowed_hosts"))
    url_prefix = _parse_url_prefix(data.get("url_prefix"))
    show_full_path = _get_bool(data, "show_full_path", "config", default=True)
    allow_unauth_local = _get_bool(
        data, "allow_unauthenticated_local", "config", default=False
    )
    allow_unauth_remote = _get_bool(
        data, "allow_unauthenticated_remote", "config", default=False
    )
    allow_insecure_http_remote = _get_bool(
        data, "allow_insecure_http_remote", "config", default=False
    )
    accept_bin = _get_bool(data, "accept_bin", "config", default=True)
    accept_img = _get_bool(data, "accept_img", "config", default=True)
    accept_doc = _get_bool(data, "accept_doc", "config", default=True)
    auth = _parse_auth(data.get("auth"), warnings)
    tls = _parse_tls(data.get("tls"), warnings)

    log_level = _get_str(data, "log_level", "config", default="INFO").upper()
    if log_level not in _LOG_LEVELS:
        raise ConfigError(f"'log_level' must be one of {sorted(_LOG_LEVELS)}")

    raw_zones = data.get("zones", [])
    if not isinstance(raw_zones, list):
        raise ConfigError("'zones' must be a list of tables")
    zones: dict[str, ZoneConfig] = {}
    for i, raw_zone in enumerate(raw_zones):
        zone = _parse_zone(raw_zone, i, warnings)
        if zone.id in zones:
            raise ConfigError(f"duplicate zone ID: {zone.id!r}")
        zones[zone.id] = zone

    raw_autozones = data.get("autozone", [])
    if not isinstance(raw_autozones, list):
        raise ConfigError("'autozone' must be a list of tables")
    autozones = tuple(
        _parse_autozone(raw_autozone, index, warnings)
        for index, raw_autozone in enumerate(raw_autozones)
    )
    if not zones and not autozones:
        raise ConfigError("configuration must define [[zones]] or [[autozone]]")

    groups = _parse_groups(data.get("groups"), warnings)

    cfg = Config(
        listen_address=listen,
        port=port,
        max_upload_bytes=max_upload_bytes,
        max_image_pixels=max_image_pixels,
        limits=limits,
        trusted_proxies=trusted_proxies,
        allowed_hosts=allowed_hosts,
        url_prefix=url_prefix,
        show_full_path=show_full_path,
        allow_unauthenticated_local=allow_unauth_local,
        allow_unauthenticated_remote=allow_unauth_remote,
        allow_insecure_http_remote=allow_insecure_http_remote,
        accept_bin=accept_bin,
        accept_img=accept_img,
        accept_doc=accept_doc,
        auth=auth,
        tls=tls,
        zones=zones,
        autozones=autozones,
        groups=groups,
        log_level=log_level,
        config_path=path,
        warnings=warnings,
    )
    check_startup_policy(cfg)
    return cfg


def check_startup_policy(cfg: Config) -> bool:
    """Reject unencrypted or anonymous remote exposure."""
    loopback_only = is_loopback_address(cfg.listen_address)
    if not loopback_only and not cfg.tls.enabled and not cfg.allow_insecure_http_remote:
        raise ConfigError(
            f"refusing to start: unencrypted HTTP listener on '{cfg.listen_address}' "
            "(non-loopback); enable [tls] for this address or bind Pasteberth "
            "to loopback behind an HTTPS reverse proxy.\n"
            "To force an HTTP listener on a private network, explicitly add:\n"
            "  allow_insecure_http_remote = true"
        )
    if cfg.auth.enabled:
        return loopback_only
    if not cfg.allowed_hosts:
        raise ConfigError(
            "refusing to start: authentication is disabled with empty 'allowed_hosts'. "
            "List allowed hosts or enable [auth]."
        )
    if loopback_only and cfg.allow_unauthenticated_local:
        return loopback_only
    if not loopback_only and cfg.allow_unauthenticated_remote:
        return loopback_only
    if loopback_only:
        raise ConfigError(
            "refusing to start: authentication is disabled on a local listener "
            "without explicit opt-in.\n"
            "A loopback backend can be exposed through a reverse proxy.\n"
            "Options:\n"
            "  - enable authentication ([auth] enabled = true + `pasteberth passwd`);\n"
            "  - knowingly set allow_unauthenticated_local = true."
        )
    if not cfg.auth.enabled and not cfg.allow_unauthenticated_remote:
        raise ConfigError(
            f"refusing to start: listener on '{cfg.listen_address}' (non-loopback) "
            "with authentication disabled.\n"
            "A passwordless service must not be exposed to the network.\n"
            "Options:\n"
            "  - enable authentication ([auth] enabled = true + `pasteberth passwd`);\n"
            "  - knowingly set allow_unauthenticated_remote = true."
        )
    return loopback_only


def default_config_path() -> Path:
    """Return the default XDG path: $XDG_CONFIG_HOME/pasteberth/config.toml."""
    xdg = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    path = Path(xdg).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return path / "pasteberth" / "config.toml"


def deployment_root() -> Path:
    """Return the physical root of the read-only PasteBerth deployment."""
    configured = os.environ.get("PASTEBERTH_HOME")
    if configured:
        return Path(configured).expanduser().resolve()
    return Path(__file__).resolve().parent.parent


def default_data_path() -> Path:
    """Return the XDG data directory used for mutable Pasteberth state."""
    xdg = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    path = Path(xdg).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return path / "pasteberth"


def default_storage_path() -> Path:
    return default_data_path() / "storage" / "default"


def ensure_external_path(path: Path, label: str) -> Path:
    """Reject mutable paths that resolve inside the read-only deployment."""
    path = Path(path).expanduser()
    try:
        resolved = path.resolve()
        root = deployment_root()
    except (OSError, RuntimeError, ValueError) as exc:
        raise ConfigError(f"{label} cannot be resolved: {path} ({exc})") from exc
    try:
        resolved.relative_to(root)
    except ValueError:
        # Keep the configured spelling for diagnostics. Callers resolve it
        # immediately before opening it, so descriptor backends accept linked
        # parents without allowing writes inside the read-only bundle.
        return path
    raise ConfigError(f"{label} must be outside the PasteBerth deployment: {path}")


def build_default_config() -> Config:
    """Build a minimal local configuration without a user file."""
    storage = ensure_external_path(default_storage_path(), "default storage")
    config_path = ensure_external_path(default_config_path(), "default configuration")
    return Config(
        listen_address="127.0.0.1",
        port=8765,
        max_upload_bytes=DEFAULT_MAX_UPLOAD_BYTES,
        max_image_pixels=DEFAULT_MAX_IMAGE_PIXELS,
        limits=LimitsConfig(),
        trusted_proxies=(),
        allowed_hosts=("localhost", "127.0.0.1", "::1"),
        url_prefix="",
        show_full_path=True,
        allow_unauthenticated_local=True,
        allow_unauthenticated_remote=False,
        allow_insecure_http_remote=False,
        accept_bin=True,
        accept_img=True,
        accept_doc=True,
        auth=AuthConfig(enabled=False),
        tls=TLSConfig(),
        zones={
            "default": ZoneConfig(
                id="default",
                label="Default",
                directory=storage,
                retain=10,
                reference_prefix="@",
                reference_suffix="",
                reference_list_prefix="",
                reference_list_suffix="",
                reference_separator=",",
                allow_zip_download=True,
                color="#304237",
            )
        },
        autozones=(),
        groups=(),
        log_level="INFO",
        config_path=config_path,
        using_default_config=True,
    )


def find_config_path(explicit: str | None = None) -> Path | None:
    """Find an existing configuration without creating a file."""
    if explicit:
        return Path(explicit)
    env = os.environ.get("PASTEBERTH_CONFIG")
    if env:
        return Path(env)
    xdg_path = default_config_path()
    if xdg_path.is_file():
        return xdg_path
    return None


def config_path_for_generation(explicit: str | None = None) -> Path:
    """Return the ``--generate-config`` target (XDG by default)."""
    if explicit:
        return Path(explicit)
    env = os.environ.get("PASTEBERTH_CONFIG")
    if env:
        return Path(env)
    return default_config_path()


def resolve_config_path(explicit: str | None = None) -> Path:
    if explicit:
        return Path(explicit)
    env = os.environ.get("PASTEBERTH_CONFIG")
    if env:
        return Path(env)
    return default_config_path()


def prepare_directories(cfg: Config) -> None:
    """Create/check zone directories at startup (fail fast)."""
    fs = platform_fs()
    seen: dict[tuple[int, int], str] = {}
    for zone in cfg.zones.values():
        try:
            directory_path = zone.directory.resolve()
        except (OSError, RuntimeError, ValueError) as exc:
            raise ConfigError(
                f"zone '{zone.id}': cannot inspect {zone.directory} ({exc})"
            ) from exc
        try:
            directory = fs.open_directory(
                directory_path,
                create=zone.create_directory,
                mode=0o700,
            )
        except FileNotFoundError as exc:
            if not zone.create_directory:
                raise ConfigError(
                    f"zone '{zone.id}': directory is missing and create_directory = false: "
                    f"{zone.directory}"
                ) from exc
            raise ConfigError(
                f"zone '{zone.id}': cannot create {zone.directory} ({exc})"
            ) from exc
        except NotADirectoryError as exc:
            raise ConfigError(
                f"zone '{zone.id}': '{zone.directory}' exists but is not a directory"
            ) from exc
        except (OSError, ValueError, UnsupportedFilesystemError) as exc:
            raise ConfigError(
                f"zone '{zone.id}': cannot inspect {zone.directory} ({exc})"
            ) from exc
        try:
            with directory:
                audit = fs.audit_permissions(directory_path, directory=True)
                mode = audit.mode
            # Shared directories (group/other read or write) are accepted with a
            # warning, not rejected. Rejection would push operators to bypass
            # the protection (chmod 777, disabling the service, or off-zone
            # storage). 0700 remains recommended.
            if (mode is not None and mode & 0o077) or (mode is None and not audit.private):
                permission_detail = oct(mode) if mode is not None else (audit.detail or "ACL")
                log.warning(
                    "zone '%s': permissions are not private on %s (%s); "
                    "0700 is recommended",
                    zone.id,
                    directory_path,
                    permission_detail,
                )
            identity = directory.identity
        except (OSError, UnsupportedFilesystemError) as exc:
            raise ConfigError(
                f"zone '{zone.id}': cannot inspect {zone.directory} ({exc})"
            ) from exc
        if not fs.check_access(directory_path, write=True, execute=True):
            raise ConfigError(
                f"zone '{zone.id}': directory is not writable: {directory_path}"
            )
        previous = seen.get(identity)
        if previous is not None:
            raise ConfigError(
                f"zones '{previous}' and '{zone.id}' target the same directory "
                f"({zone.directory})"
            )
        seen[identity] = zone.id


def validate_directory_identities(cfg: Config) -> None:
    """Check directory collisions without creating destinations."""
    fs = platform_fs()
    seen: dict[tuple[int, int], str] = {}
    configured: dict[Path, str] = {}
    for zone in cfg.zones.values():
        directory_path = zone.directory.resolve()
        normalized = Path(os.path.normpath(str(directory_path)))
        previous_path = configured.get(normalized)
        if previous_path is not None:
            raise ConfigError(
                f"zones '{previous_path}' and '{zone.id}' target the same directory "
                f"({zone.directory})"
            )
        configured[normalized] = zone.id
        try:
            with fs.open_directory(directory_path) as directory:
                identity = directory.identity
        except FileNotFoundError:
            continue
        except (NotADirectoryError, OSError, UnsupportedFilesystemError) as exc:
            raise ConfigError(
                f"zone '{zone.id}': cannot inspect {zone.directory} ({exc})"
            ) from exc
        previous = seen.get(identity)
        if previous is not None:
            raise ConfigError(
                f"zones '{previous}' and '{zone.id}' target the same directory "
                f"({zone.directory})"
            )
        seen[identity] = zone.id
