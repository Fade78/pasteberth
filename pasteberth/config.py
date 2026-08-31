"""Chargement et validation de la configuration TOML.

Le dépôt peut fonctionner sans fichier utilisateur : une configuration locale
minimale est alors construite avec ``storage/default``. Un fichier ``config.toml``
au dépôt, ``$PASTEBERTH_CONFIG`` ou ``--config`` la remplacent ; l'ancien chemin
XDG reste accepté en dernier recours.
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

from pasteberth.images import HARD_MAX_PIXELS, MAX_PIXELS
from pasteberth.platformfs import UnsupportedFilesystemError, platform_fs


log = logging.getLogger("pasteberth.config")

try:
    import tomllib
except ModuleNotFoundError as exc:  # Python < 3.11
    raise SystemExit("Pasteberth nécessite Python 3.11+ (module tomllib)") from exc


class ConfigError(Exception):
    """Erreur fatale de configuration (message destiné à l'utilisateur)."""


_ZONE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR"}
_GROUP_SELECTIONS = {"all", "pattern", "other"}
_GROUP_LAYOUTS = {"area", "tab"}

DEFAULT_MAX_UPLOAD_BYTES = 20 * 1024**2
MAX_UPLOAD_BYTES = 50 * 1024**2
DEFAULT_MIN_FREE_PERCENT = 2.0
_SIZE_UNITS = {
    "": 1,
    "B": 1,
    "K": 1024, "KB": 1024, "KIB": 1024,
    "M": 1024**2, "MB": 1024**2, "MIB": 1024**2,
    "G": 1024**3, "GB": 1024**3, "GIB": 1024**3,
}


def parse_size(value: object) -> int:
    """Analyse une taille telle que ``20MB``, ``512KiB`` ou ``4096`` (octets)."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ConfigError(f"taille invalide : {value!r}")
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            raise ConfigError(f"taille invalide : {value!r}")
        try:
            n = int(value)
        except (ValueError, OverflowError) as exc:
            raise ConfigError(f"taille invalide : {value!r}") from exc
    else:
        m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([A-Za-z]*)\s*", value)
        if not m:
            raise ConfigError(f"taille invalide : {value!r} (exemples : 20MB, 512KiB, 4096)")
        unit = m.group(2).upper()
        if unit not in _SIZE_UNITS:
            raise ConfigError(f"unité de taille inconnue : {m.group(2)!r}")
        try:
            n = int(float(m.group(1)) * _SIZE_UNITS[unit])
        except (ValueError, OverflowError) as exc:
            raise ConfigError(f"taille invalide : {value!r}") from exc
    if n <= 0:
        raise ConfigError(f"la taille doit être positive : {value!r}")
    return n


def is_loopback_address(address: str) -> bool:
    """True si l'adresse d'écoute n'expose le service qu'à la machine locale."""
    address = address.strip()
    try:
        return ipaddress.ip_address(address).is_loopback
    except ValueError:
        pass
    # Nom d'hôte : boucler uniquement si TOUTES les résolutions sont locales.
    try:
        infos = socket.getaddrinfo(address, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise ConfigError(f"adresse d'écoute non résoluble : {address!r} ({exc})") from exc
    if not infos:
        raise ConfigError(f"adresse d'écoute non résoluble : {address!r}")
    return all(ipaddress.ip_address(info[4][0]).is_loopback for info in infos)


@dataclass(frozen=True)
class AuthConfig:
    enabled: bool = True
    session_ttl_hours: int = 72
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


@dataclass(frozen=True)
class GroupConfig:
    name: str
    selection: str = "pattern"
    pattern: tuple[str, ...] = ()
    pattern_defined: bool = False
    layout: str = "area"
    hide_empty: bool = False
    show_count: bool = True


@dataclass(frozen=True)
class Config:
    listen_address: str
    port: int
    max_upload_bytes: int
    max_image_pixels: int
    trusted_proxies: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]
    allowed_hosts: tuple[str, ...]
    allow_unauthenticated_local: bool
    allow_unauthenticated_remote: bool
    allow_insecure_http_remote: bool
    accept_bin: bool
    accept_img: bool
    accept_doc: bool
    auth: AuthConfig
    tls: TLSConfig
    zones: dict[str, ZoneConfig]
    groups: tuple[GroupConfig, ...]
    log_level: str
    config_path: Path
    warnings: list[str] = field(default_factory=list)
    using_default_config: bool = False

    def password_file(self) -> Path:
        """Emplacement du hash du mot de passe (0600, jamais un symlink)."""
        return self.auth.password_file or self.config_path.parent / "passwd"


def _expect_table(value: object, where: str) -> dict:
    if not isinstance(value, dict):
        raise ConfigError(f"{where} doit être une table")
    return value


def _get_str(table: dict, key: str, where: str, *, default: str | None = None,
             allow_empty: bool = False) -> str:
    if key not in table:
        if default is None:
            raise ConfigError(f"{where}: clé manquante '{key}'")
        return default
    value = table[key]
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise ConfigError(f"{where}: '{key}' doit être une chaîne non vide")
    return value


def _get_bool(table: dict, key: str, where: str, default: bool) -> bool:
    if key not in table:
        return default
    value = table[key]
    if not isinstance(value, bool):
        raise ConfigError(f"{where}: '{key}' doit être un booléen")
    return value


def _get_percent(table: dict, key: str, where: str, default: float) -> float:
    if key not in table:
        return default
    value = table[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{where}: '{key}' doit être un nombre entre 0 et 99,99")
    value = float(value)
    if not (0.0 <= value < 100.0):
        raise ConfigError(f"{where}: '{key}' doit être un nombre entre 0 et 99,99")
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
        suggestion = " ; utilisez 'groups'" if where == "config" and key == "groupes" else ""
        warnings.append(f"{where}: clé inconnue '{key}' ignorée{suggestion}")


def _parse_auth(raw: object, warnings: list[str]) -> AuthConfig:
    if raw is None:
        return AuthConfig()
    table = _expect_table(raw, "[auth]")
    _warn_unknown(
        table,
        {"enabled", "session_ttl_hours", "password_file"},
        "[auth]",
        warnings,
    )
    enabled = _get_bool(table, "enabled", "[auth]", default=True)
    ttl = table.get("session_ttl_hours", 72)
    if isinstance(ttl, bool) or not isinstance(ttl, int) or not (1 <= ttl <= 24 * 365):
        raise ConfigError("[auth]: 'session_ttl_hours' doit être un entier entre 1 et 8760")
    password_file_raw = table.get("password_file")
    password_file = None
    if password_file_raw is not None:
        if not isinstance(password_file_raw, str) or not password_file_raw.strip():
            raise ConfigError("[auth]: 'password_file' doit être un chemin absolu")
        if "\x00" in password_file_raw:
            raise ConfigError("[auth]: 'password_file' contient un caractère NUL")
        password_file = Path(os.path.expanduser(password_file_raw))
        if not password_file.is_absolute():
            raise ConfigError("[auth]: 'password_file' doit être un chemin absolu")
    if enabled and "password_hash" in table:
        warnings.append(
            "[auth]: 'password_hash' dans config.toml est ignoré ; "
            "`pasteberth passwd` écrit le hash dans le fichier 'passwd' configuré"
        )
    return AuthConfig(
        enabled=enabled,
        session_ttl_hours=ttl,
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
        raise ConfigError("[tls]: 'certificate' doit être un chemin absolu")
    if not isinstance(private_key_raw, str) or not private_key_raw.strip():
        raise ConfigError("[tls]: 'private_key' doit être un chemin absolu")
    if "\x00" in certificate_raw or "\x00" in private_key_raw:
        raise ConfigError("[tls]: les chemins ne peuvent pas contenir de caractère NUL")
    certificate = Path(os.path.expanduser(certificate_raw))
    private_key = Path(os.path.expanduser(private_key_raw))
    if not certificate.is_absolute() or not private_key.is_absolute():
        raise ConfigError("[tls]: 'certificate' et 'private_key' doivent être absolus")
    return TLSConfig(enabled=True, certificate=certificate, private_key=private_key)


def _parse_zone(raw_zone: object, index: int, warnings: list[str]) -> ZoneConfig:
    where = f"[[zones]] #{index + 1}"
    table = _expect_table(raw_zone, where)
    _warn_unknown(
        table,
        {"id", "label", "type", "directory", "retain", "reference_prefix", "reference_suffix",
         "reference_list_prefix", "reference_list_suffix", "reference_separator",
         "allow_zip_download", "color", "create_directory", "min_free_percent"},
        where,
        warnings,
    )
    ztype = _get_str(table, "type", where, default="local").lower()
    if ztype != "local":
        raise ConfigError(
            f"{where}: type de destination '{ztype}' non supporté en V1 "
            "(seul 'local' est implémenté)"
        )
    zid = _get_str(table, "id", where)
    if not _ZONE_ID_RE.fullmatch(zid):
        raise ConfigError(
            f"{where}: id de zone invalide : {zid!r} "
            "(caractères autorisés : a-z, chiffres, '-', '_')"
        )
    label = _get_str(table, "label", where, default=zid)
    directory_raw = _get_str(table, "directory", where)
    directory = Path(os.path.expanduser(directory_raw))
    if not directory.is_absolute():
        raise ConfigError(
            f"{where}: 'directory' doit être un chemin absolu "
            f"(relatif au serveur Pasteberth, pas au navigateur) : {directory_raw!r}"
        )
    if "\x00" in directory_raw:
        raise ConfigError(f"{where}: 'directory' contient un caractère NUL")
    retain = table.get("retain", 10)
    if isinstance(retain, bool) or not isinstance(retain, int) or not (1 <= retain <= 10_000):
        raise ConfigError(f"{where}: 'retain' doit être un entier entre 1 et 10000")
    prefix = _get_str(table, "reference_prefix", where, default="@", allow_empty=True)
    if len(prefix) > 16:
        raise ConfigError(f"{where}: 'reference_prefix' trop long (16 caractères max)")
    suffix = _get_str(table, "reference_suffix", where, default="", allow_empty=True)
    if len(suffix) > 16:
        raise ConfigError(f"{where}: 'reference_suffix' trop long (16 caractères max)")
    list_prefix = _get_str(
        table, "reference_list_prefix", where, default="", allow_empty=True
    )
    if len(list_prefix) > 16:
        raise ConfigError(
            f"{where}: 'reference_list_prefix' trop long (16 caractères max)"
        )
    list_suffix = _get_str(
        table, "reference_list_suffix", where, default="", allow_empty=True
    )
    if len(list_suffix) > 16:
        raise ConfigError(
            f"{where}: 'reference_list_suffix' trop long (16 caractères max)"
        )
    separator = _get_str(
        table, "reference_separator", where, default=",", allow_empty=True
    )
    if len(separator) > 16:
        raise ConfigError(
            f"{where}: 'reference_separator' trop long (16 caractères max)"
        )
    allow_zip_download = _get_bool(table, "allow_zip_download", where, default=True)
    color = _get_str(table, "color", where, default="#243447")
    if not _COLOR_RE.fullmatch(color):
        raise ConfigError(f"{where}: 'color' doit être au format #RRGGBB : {color!r}")
    color = color.lower()
    if _best_contrast(color) < 4.5:
        raise ConfigError(
            f"{where}: 'color' ne permet pas un contraste texte suffisant : {color!r}"
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
    )


def _parse_groups(raw: object, warnings: list[str]) -> tuple[GroupConfig, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ConfigError("'groups' doit être une liste de tables")
    groups = []
    seen_names = set()
    for index, item in enumerate(raw):
        where = f"[[groups]] #{index + 1}"
        if not isinstance(item, dict):
            raise ConfigError(f"{where}: doit être une table")
        _warn_unknown(
            item,
            {"name", "selection", "pattern", "layout", "hide_empty", "show_count"},
            where,
            warnings,
        )
        name = _get_str(item, "name", where)
        if name in seen_names:
            raise ConfigError(f"{where}: nom de groupe dupliqué : {name!r}")
        seen_names.add(name)
        selection = _get_str(item, "selection", where, default="pattern").lower()
        if selection not in _GROUP_SELECTIONS:
            raise ConfigError(
                f"{where}: 'selection' doit être parmi {sorted(_GROUP_SELECTIONS)}"
            )
        raw_pattern = item.get("pattern")
        pattern_defined = "pattern" in item
        if raw_pattern is not None and (
            not isinstance(raw_pattern, list)
            or not all(isinstance(pattern, str) for pattern in raw_pattern)
        ):
            raise ConfigError(f"{where}: 'pattern' doit être une liste de chaînes")
        pattern = tuple(raw_pattern or ())
        if selection == "pattern" and not pattern:
            raise ConfigError(f"{where}: 'pattern' ne peut pas être vide")
        if selection == "pattern":
            for expression in pattern:
                try:
                    re.compile(expression)
                except re.error as exc:
                    raise ConfigError(
                        f"{where}: expression régulière invalide dans 'pattern' "
                        f"{expression!r} ({exc})"
                    ) from exc
        layout = _get_str(item, "layout", where, default="area").lower()
        if layout not in _GROUP_LAYOUTS:
            raise ConfigError(
                f"{where}: 'layout' doit être parmi {sorted(_GROUP_LAYOUTS)}"
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
    """Calcule les zones effectives de chaque groupe sans accéder au stockage."""
    ordered_zone_ids = tuple(zone_ids)
    pattern_zone_ids: set[str] = set()
    resolved: dict[str, tuple[str, ...]] = {}
    for group in groups:
        if group.selection == "all":
            resolved[group.name] = ordered_zone_ids
        elif group.selection == "pattern":
            expressions = tuple(re.compile(pattern) for pattern in group.pattern)
            matching = tuple(
                zid
                for zid in ordered_zone_ids
                if any(expression.search(zid) for expression in expressions)
            )
            resolved[group.name] = matching
            pattern_zone_ids.update(matching)
    other_zone_ids = tuple(
        zid for zid in ordered_zone_ids if zid not in pattern_zone_ids
    )
    for group in groups:
        if group.selection == "other":
            resolved[group.name] = other_zone_ids
    return resolved


def _parse_trusted_proxies(raw: object, warnings: list[str]) -> tuple:
    if raw is None:
        return ()
    if not isinstance(raw, list) or not all(isinstance(v, str) for v in raw):
        raise ConfigError("'trusted_proxies' doit être une liste de chaînes (IP ou CIDR)")
    networks = []
    for item in raw:
        try:
            networks.append(ipaddress.ip_network(item, strict=False))
        except ValueError as exc:
            raise ConfigError(f"'trusted_proxies': entrée invalide {item!r} ({exc})") from exc
    return tuple(networks)


def _parse_allowed_hosts(raw: object) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list) or not all(isinstance(v, str) for v in raw):
        raise ConfigError("'allowed_hosts' doit être une liste de noms d'hôte")
    hosts: list[str] = []
    for item in raw:
        host = item.strip().lower().rstrip(".")
        if not host:
            raise ConfigError("'allowed_hosts' ne peut pas contenir de valeur vide")
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
                raise ConfigError(f"'allowed_hosts': nom d'hôte invalide {item!r}")
        if host not in hosts:
            hosts.append(host)
    return tuple(hosts)


def load_config(path: Path) -> Config:
    """Charge, valide et retourne la configuration."""
    path = Path(path)
    try:
        raw_bytes = path.read_bytes()
    except FileNotFoundError as exc:
        raise ConfigError(f"fichier de configuration introuvable : {path}") from exc
    except ValueError as exc:
        raise ConfigError(f"chemin de configuration invalide : {path}") from exc
    except OSError as exc:
        raise ConfigError(f"fichier de configuration illisible : {path} ({exc})") from exc
    try:
        data = tomllib.loads(raw_bytes.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise ConfigError(f"TOML invalide dans {path} : {exc}") from exc

    warnings: list[str] = []
    _warn_unknown(
        data,
        {"listen_address", "port", "max_upload_size", "trusted_proxies",
         "max_image_pixels",
         "allowed_hosts",
         "allow_unauthenticated_local", "allow_unauthenticated_remote",
         "allow_insecure_http_remote", "accept_bin", "accept_img", "accept_doc",
         "auth", "tls",
         "zones", "groups", "log_level"},
        "config",
        warnings,
    )

    listen = _get_str(data, "listen_address", "config", default="127.0.0.1")
    port = data.get("port", 8765)
    if isinstance(port, bool) or not isinstance(port, int) or not (1 <= port <= 65535):
        raise ConfigError("'port' doit être un entier entre 1 et 65535")

    max_upload_bytes = parse_size(
        data.get("max_upload_size", DEFAULT_MAX_UPLOAD_BYTES)
    )
    if max_upload_bytes < 1024:
        raise ConfigError("'max_upload_size' est trop petit (minimum 1KB)")
    if max_upload_bytes > MAX_UPLOAD_BYTES:
        raise ConfigError("'max_upload_size' est trop grand (maximum 50MiB)")

    max_image_pixels = data.get("max_image_pixels", MAX_PIXELS)
    if (
        isinstance(max_image_pixels, bool)
        or not isinstance(max_image_pixels, int)
        or not (1 <= max_image_pixels <= HARD_MAX_PIXELS)
    ):
        raise ConfigError(
            f"'max_image_pixels' doit être un entier entre 1 et {HARD_MAX_PIXELS}"
        )

    trusted_proxies = _parse_trusted_proxies(data.get("trusted_proxies"), warnings)
    # The public hostname is deployment-specific; an absent key preserves the
    # multi-station wildcard compatibility, while a non-empty list is strict.
    allowed_hosts = _parse_allowed_hosts(data.get("allowed_hosts"))
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
        raise ConfigError(f"'log_level' doit être parmi {sorted(_LOG_LEVELS)}")

    raw_zones = data.get("zones")
    if not isinstance(raw_zones, list) or not raw_zones:
        raise ConfigError("'zones' doit être une liste non vide ([[zones]])")
    zones: dict[str, ZoneConfig] = {}
    for i, raw_zone in enumerate(raw_zones):
        zone = _parse_zone(raw_zone, i, warnings)
        if zone.id in zones:
            raise ConfigError(f"id de zone dupliqué : {zone.id!r}")
        zones[zone.id] = zone

    groups = _parse_groups(data.get("groups"), warnings)

    cfg = Config(
        listen_address=listen,
        port=port,
        max_upload_bytes=max_upload_bytes,
        max_image_pixels=max_image_pixels,
        trusted_proxies=trusted_proxies,
        allowed_hosts=allowed_hosts,
        allow_unauthenticated_local=allow_unauth_local,
        allow_unauthenticated_remote=allow_unauth_remote,
        allow_insecure_http_remote=allow_insecure_http_remote,
        accept_bin=accept_bin,
        accept_img=accept_img,
        accept_doc=accept_doc,
        auth=auth,
        tls=tls,
        zones=zones,
        groups=groups,
        log_level=log_level,
        config_path=path,
        warnings=warnings,
    )
    check_startup_policy(cfg)
    return cfg


def check_startup_policy(cfg: Config) -> bool:
    """Refuse les expositions distantes non chiffrées ou anonymes."""
    loopback_only = is_loopback_address(cfg.listen_address)
    if not loopback_only and not cfg.tls.enabled and not cfg.allow_insecure_http_remote:
        raise ConfigError(
            f"refus de démarrer : écoute HTTP non chiffrée sur '{cfg.listen_address}' "
            "(non-loopback) ; activez [tls] pour cette adresse ou liez Pasteberth "
            "à loopback derrière un reverse proxy HTTPS.\n"
            "Pour forcer une écoute HTTP sur un réseau privé, ajoutez explicitement :\n"
            "  allow_insecure_http_remote = true"
        )
    if cfg.auth.enabled:
        return loopback_only
    if loopback_only and cfg.allow_unauthenticated_local:
        return loopback_only
    if not loopback_only and cfg.allow_unauthenticated_remote:
        return loopback_only
    if loopback_only:
        raise ConfigError(
            "refus de démarrer : authentification désactivée sur une écoute locale "
            "sans opt-in explicite.\n"
            "Un backend loopback peut être exposé par un reverse proxy.\n"
            "Solutions :\n"
            "  - activer l'authentification ([auth] enabled = true + `pasteberth passwd`) ;\n"
            "  - en connaissance de cause : allow_unauthenticated_local = true."
        )
    if not cfg.auth.enabled and not cfg.allow_unauthenticated_remote:
        raise ConfigError(
            f"refus de démarrer : écoute sur '{cfg.listen_address}' (non-loopback) "
            "avec authentification désactivée.\n"
            "Un service sans mot de passe ne doit pas être exposé au réseau.\n"
            "Solutions :\n"
            "  - activer l'authentification ([auth] enabled = true + `pasteberth passwd`) ;\n"
            "  - en connaissance de cause : allow_unauthenticated_remote = true."
        )


def default_config_path() -> Path:
    """Chemin XDG par défaut : $XDG_CONFIG_HOME/pasteberth/config.toml."""
    xdg = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(xdg) / "pasteberth" / "config.toml"


def repository_root() -> Path:
    """Racine du dépôt qui contient le code Pasteberth."""
    configured = os.environ.get("PASTEBERTH_REPO_ROOT")
    if configured:
        return Path(configured).expanduser()
    return Path(__file__).resolve().parent.parent


def repository_config_path() -> Path:
    return repository_root() / "config.toml"


def default_storage_path() -> Path:
    return repository_root() / "storage" / "default"


def build_default_config() -> Config:
    """Configuration minimale, locale et sans fichier utilisateur."""
    return Config(
        listen_address="127.0.0.1",
        port=8765,
        max_upload_bytes=DEFAULT_MAX_UPLOAD_BYTES,
        max_image_pixels=MAX_PIXELS,
        trusted_proxies=(),
        allowed_hosts=(),
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
                directory=default_storage_path(),
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
        groups=(),
        log_level="INFO",
        config_path=repository_config_path(),
        using_default_config=True,
    )


def find_config_path(explicit: str | None = None) -> Path | None:
    """Trouve une configuration existante sans créer de fichier."""
    if explicit:
        return Path(explicit)
    env = os.environ.get("PASTEBERTH_CONFIG")
    if env:
        return Path(env)
    repo_path = repository_config_path()
    if repo_path.is_file():
        return repo_path
    xdg_path = default_config_path()
    if xdg_path.is_file():
        return xdg_path
    return None


def config_path_for_generation(explicit: str | None = None) -> Path:
    """Chemin cible de ``--generate-config`` (dépôt par défaut)."""
    if explicit:
        return Path(explicit)
    env = os.environ.get("PASTEBERTH_CONFIG")
    if env:
        return Path(env)
    return repository_config_path()


def resolve_config_path(explicit: str | None = None) -> Path:
    if explicit:
        return Path(explicit)
    env = os.environ.get("PASTEBERTH_CONFIG")
    if env:
        return Path(env)
    return default_config_path()


def prepare_directories(cfg: Config) -> None:
    """Crée/vérifie les répertoires des zones au démarrage (échec rapide)."""
    fs = platform_fs()
    seen: dict[tuple[int, int], str] = {}
    for zone in cfg.zones.values():
        try:
            symlink = fs.first_symlink_component(zone.directory)
        except (OSError, ValueError) as exc:
            raise ConfigError(
                f"zone '{zone.id}': impossible d'inspecter {zone.directory} ({exc})"
            ) from exc
        if symlink is not None:
            raise ConfigError(
                f"zone '{zone.id}': le chemin contient un lien symbolique : {symlink}"
            )
        try:
            directory = fs.open_directory(
                zone.directory,
                create=zone.create_directory,
                mode=0o700,
            )
        except FileNotFoundError as exc:
            if not zone.create_directory:
                raise ConfigError(
                    f"zone '{zone.id}': répertoire inexistant et create_directory = false : "
                    f"{zone.directory}"
                ) from exc
            raise ConfigError(
                f"zone '{zone.id}': impossible de créer {zone.directory} ({exc})"
            ) from exc
        except NotADirectoryError as exc:
            raise ConfigError(
                f"zone '{zone.id}': '{zone.directory}' existe mais n'est pas un répertoire"
            ) from exc
        except (OSError, ValueError, UnsupportedFilesystemError) as exc:
            raise ConfigError(
                f"zone '{zone.id}': impossible d'inspecter {zone.directory} ({exc})"
            ) from exc
        try:
            with directory:
                audit = fs.audit_permissions(zone.directory, directory=True)
                mode = audit.mode
            # Feature: les répertoires partagés (group/other read ou write) sont
            # acceptés avec un avertissement, pas refusés. Refuser pousserait les
            # opérateurs à contourner la protection (chmod 777, désactivation du
            # service, stockage hors zone). Le 0700 reste recommandé.
            if (mode is not None and mode & 0o077) or (mode is None and not audit.private):
                permission_detail = oct(mode) if mode is not None else (audit.detail or "ACL")
                log.warning(
                    "zone '%s': permissions non privées sur %s (%s) ; "
                    "0700 est recommandé",
                    zone.id,
                    zone.directory,
                    permission_detail,
                )
            identity = directory.identity
        except (OSError, UnsupportedFilesystemError) as exc:
            raise ConfigError(
                f"zone '{zone.id}': impossible d'inspecter {zone.directory} ({exc})"
            ) from exc
        if not fs.check_access(zone.directory, write=True, execute=True):
            raise ConfigError(
                f"zone '{zone.id}': répertoire non accessible en écriture : {zone.directory}"
            )
        previous = seen.get(identity)
        if previous is not None:
            raise ConfigError(
                f"zones '{previous}' et '{zone.id}' ciblent le même répertoire "
                f"({zone.directory})"
            )
        seen[identity] = zone.id


def validate_directory_identities(cfg: Config) -> None:
    """Vérifie les collisions de répertoires sans créer les destinations."""
    fs = platform_fs()
    seen: dict[tuple[int, int], str] = {}
    configured: dict[Path, str] = {}
    for zone in cfg.zones.values():
        normalized = Path(os.path.normpath(str(zone.directory)))
        previous_path = configured.get(normalized)
        if previous_path is not None:
            raise ConfigError(
                f"zones '{previous_path}' et '{zone.id}' ciblent le même répertoire "
                f"({zone.directory})"
            )
        configured[normalized] = zone.id
        try:
            with fs.open_directory(zone.directory) as directory:
                identity = directory.identity
        except FileNotFoundError:
            continue
        except (NotADirectoryError, OSError, UnsupportedFilesystemError) as exc:
            raise ConfigError(
                f"zone '{zone.id}': impossible d'inspecter {zone.directory} ({exc})"
            ) from exc
        previous = seen.get(identity)
        if previous is not None:
            raise ConfigError(
                f"zones '{previous}' et '{zone.id}' ciblent le même répertoire "
                f"({zone.directory})"
            )
        seen[identity] = zone.id
