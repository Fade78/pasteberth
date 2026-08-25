"""Chargement et validation de la configuration TOML.

Le fichier par défaut respecte XDG : ``~/.config/pasteberth/config.toml``.
Un chemin explicite peut être fourni via ``--config`` ou ``$PASTEBERTH_CONFIG``.
"""
from __future__ import annotations

import ipaddress
import os
import re
import socket
import stat
from dataclasses import dataclass, field
from pathlib import Path

from pasteberth.images import HARD_MAX_PIXELS, MAX_PIXELS

try:
    import tomllib
except ModuleNotFoundError as exc:  # Python < 3.11
    raise SystemExit("Pasteberth nécessite Python 3.11+ (module tomllib)") from exc


class ConfigError(Exception):
    """Erreur fatale de configuration (message destiné à l'utilisateur)."""


_ZONE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR"}

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
        n = int(value)
    else:
        m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([A-Za-z]*)\s*", value)
        if not m:
            raise ConfigError(f"taille invalide : {value!r} (exemples : 20MB, 512KiB, 4096)")
        unit = m.group(2).upper()
        if unit not in _SIZE_UNITS:
            raise ConfigError(f"unité de taille inconnue : {m.group(2)!r}")
        n = int(float(m.group(1)) * _SIZE_UNITS[unit])
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


@dataclass(frozen=True)
class ZoneConfig:
    id: str
    label: str
    directory: Path
    retain: int
    reference_prefix: str = "@"
    color: str = "#243447"
    create_directory: bool = True
    min_free_percent: float = DEFAULT_MIN_FREE_PERCENT


@dataclass(frozen=True)
class Config:
    listen_address: str
    port: int
    max_upload_bytes: int
    max_image_pixels: int
    trusted_proxies: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]
    allow_unauthenticated_local: bool
    allow_unauthenticated_remote: bool
    auth: AuthConfig
    zones: dict[str, ZoneConfig]
    log_level: str
    config_path: Path
    warnings: list[str] = field(default_factory=list)

    def password_file(self) -> Path:
        """Emplacement du hash du mot de passe (à côté de la config, 0600)."""
        return self.config_path.parent / "passwd"


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
        warnings.append(f"{where}: clé inconnue '{key}' ignorée")


def _parse_auth(raw: object, warnings: list[str]) -> AuthConfig:
    if raw is None:
        return AuthConfig()
    table = _expect_table(raw, "[auth]")
    _warn_unknown(table, {"enabled", "session_ttl_hours"}, "[auth]", warnings)
    enabled = _get_bool(table, "enabled", "[auth]", default=True)
    ttl = table.get("session_ttl_hours", 72)
    if isinstance(ttl, bool) or not isinstance(ttl, int) or not (1 <= ttl <= 24 * 365):
        raise ConfigError("[auth]: 'session_ttl_hours' doit être un entier entre 1 et 8760")
    if enabled and "password_hash" in table:
        warnings.append(
            "[auth]: 'password_hash' dans config.toml est ignoré ; "
            "`pasteberth passwd` écrit le hash dans le fichier 'passwd' à côté de la config"
        )
    return AuthConfig(enabled=enabled, session_ttl_hours=ttl)


def _parse_zone(raw_zone: object, index: int, warnings: list[str]) -> ZoneConfig:
    where = f"[[zones]] #{index + 1}"
    table = _expect_table(raw_zone, where)
    _warn_unknown(
        table,
        {"id", "label", "type", "directory", "retain", "reference_prefix",
         "color", "create_directory", "min_free_percent"},
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
    retain = table.get("retain", 10)
    if isinstance(retain, bool) or not isinstance(retain, int) or not (1 <= retain <= 10_000):
        raise ConfigError(f"{where}: 'retain' doit être un entier entre 1 et 10000")
    prefix = _get_str(table, "reference_prefix", where, default="@")
    if len(prefix) > 16:
        raise ConfigError(f"{where}: 'reference_prefix' trop long (16 caractères max)")
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
        color=color,
        create_directory=create_dir,
        min_free_percent=min_free_percent,
    )


def _parse_trusted_proxies(raw: object, warnings: list[str]) -> tuple:
    if raw is None:
        return (
            ipaddress.ip_network("127.0.0.1/32"),
            ipaddress.ip_network("::1/128"),
        )
    if not isinstance(raw, list) or not all(isinstance(v, str) for v in raw):
        raise ConfigError("'trusted_proxies' doit être une liste de chaînes (IP ou CIDR)")
    networks = []
    for item in raw:
        try:
            networks.append(ipaddress.ip_network(item, strict=False))
        except ValueError as exc:
            raise ConfigError(f"'trusted_proxies': entrée invalide {item!r} ({exc})") from exc
    return tuple(networks)


def load_config(path: Path) -> Config:
    """Charge, valide et retourne la configuration."""
    path = Path(path)
    try:
        raw_bytes = path.read_bytes()
    except FileNotFoundError as exc:
        raise ConfigError(f"fichier de configuration introuvable : {path}") from exc
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
         "allow_unauthenticated_local", "allow_unauthenticated_remote", "auth",
         "zones", "log_level"},
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
    allow_unauth_local = _get_bool(
        data, "allow_unauthenticated_local", "config", default=False
    )
    allow_unauth_remote = _get_bool(
        data, "allow_unauthenticated_remote", "config", default=False
    )
    auth = _parse_auth(data.get("auth"), warnings)

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

    cfg = Config(
        listen_address=listen,
        port=port,
        max_upload_bytes=max_upload_bytes,
        max_image_pixels=max_image_pixels,
        trusted_proxies=trusted_proxies,
        allow_unauthenticated_local=allow_unauth_local,
        allow_unauthenticated_remote=allow_unauth_remote,
        auth=auth,
        zones=zones,
        log_level=log_level,
        config_path=path,
        warnings=warnings,
    )
    check_startup_policy(cfg)
    return cfg


def check_startup_policy(cfg: Config) -> None:
    """Refuse les configurations anonymes sans opt-in explicite."""
    loopback_only = is_loopback_address(cfg.listen_address)
    if cfg.auth.enabled:
        return
    if loopback_only and cfg.allow_unauthenticated_local:
        return
    if not loopback_only and cfg.allow_unauthenticated_remote:
        return
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


def resolve_config_path(explicit: str | None = None) -> Path:
    if explicit:
        return Path(explicit)
    env = os.environ.get("PASTEBERTH_CONFIG")
    if env:
        return Path(env)
    return default_config_path()


def prepare_directories(cfg: Config) -> None:
    """Crée/vérifie les répertoires des zones au démarrage (échec rapide)."""
    seen: dict[tuple[int, int], str] = {}
    for zone in cfg.zones.values():
        if zone.directory.is_symlink():
            raise ConfigError(
                f"zone '{zone.id}': le répertoire ne doit pas être un lien symbolique : "
                f"{zone.directory}"
            )
        if zone.directory.exists():
            if not zone.directory.is_dir():
                raise ConfigError(
                    f"zone '{zone.id}': '{zone.directory}' existe mais n'est pas un répertoire"
                )
            if not os.access(zone.directory, os.W_OK | os.X_OK):
                raise ConfigError(
                    f"zone '{zone.id}': répertoire non accessible en écriture : {zone.directory}"
                )
        elif zone.create_directory:
            try:
                zone.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            except OSError as exc:
                raise ConfigError(
                    f"zone '{zone.id}': impossible de créer {zone.directory} ({exc})"
                ) from exc
        else:
            raise ConfigError(
                f"zone '{zone.id}': répertoire inexistant et create_directory = false : "
                f"{zone.directory}"
            )
        try:
            mode = stat.S_IMODE(zone.directory.stat().st_mode)
            if mode & 0o077:
                raise ConfigError(
                    f"zone '{zone.id}': permissions trop ouvertes sur {zone.directory} "
                    f"({oct(mode)}), utilisez chmod 700"
                )
            identity = (zone.directory.stat().st_dev, zone.directory.stat().st_ino)
        except ConfigError:
            raise
        except OSError as exc:
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
