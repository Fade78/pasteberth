"""Chargement et validation de la configuration TOML.

Le fichier par défaut respecte XDG : ``~/.config/pasteberth/config.toml``.
Un chemin explicite peut être fourni via ``--config`` ou ``$PASTEBERTH_CONFIG``.
"""
from __future__ import annotations

import ipaddress
import os
import re
import socket
from dataclasses import dataclass, field
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError as exc:  # Python < 3.11
    raise SystemExit("Pasteberth nécessite Python 3.11+ (module tomllib)") from exc


class ConfigError(Exception):
    """Erreur fatale de configuration (message destiné à l'utilisateur)."""


_ZONE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR"}

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
    enabled: bool = False
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


@dataclass(frozen=True)
class Config:
    listen_address: str
    port: int
    max_upload_bytes: int
    trusted_proxies: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]
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


def _warn_unknown(table: dict, known: set[str], where: str, warnings: list[str]) -> None:
    for key in sorted(set(table) - known):
        warnings.append(f"{where}: clé inconnue '{key}' ignorée")


def _parse_auth(raw: object, warnings: list[str]) -> AuthConfig:
    if raw is None:
        return AuthConfig()
    table = _expect_table(raw, "[auth]")
    _warn_unknown(table, {"enabled", "session_ttl_hours"}, "[auth]", warnings)
    enabled = _get_bool(table, "enabled", "[auth]", default=False)
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
         "color", "create_directory"},
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
    create_dir = _get_bool(table, "create_directory", where, default=True)
    return ZoneConfig(
        id=zid,
        label=label,
        directory=directory,
        retain=retain,
        reference_prefix=prefix,
        color=color.lower(),
        create_directory=create_dir,
    )


def _parse_trusted_proxies(raw: object, warnings: list[str]) -> tuple:
    if raw is None:
        return ("127.0.0.1", "::1")
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
         "allow_unauthenticated_remote", "auth", "zones", "log_level"},
        "config",
        warnings,
    )

    listen = _get_str(data, "listen_address", "config", default="127.0.0.1")
    port = data.get("port", 8765)
    if isinstance(port, bool) or not isinstance(port, int) or not (1 <= port <= 65535):
        raise ConfigError("'port' doit être un entier entre 1 et 65535")

    max_upload_bytes = parse_size(data.get("max_upload_size", "20MB"))
    if max_upload_bytes < 1024:
        raise ConfigError("'max_upload_size' est trop petit (minimum 1KB)")
    if max_upload_bytes > 1024**3:
        raise ConfigError("'max_upload_size' est trop grand (maximum 1GB)")

    trusted_proxies = _parse_trusted_proxies(data.get("trusted_proxies"), warnings)
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
        trusted_proxies=trusted_proxies,
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
    """Refuse les configurations dangereuses (exposition réseau sans auth)."""
    loopback_only = is_loopback_address(cfg.listen_address)
    if not loopback_only and not cfg.auth.enabled and not cfg.allow_unauthenticated_remote:
        raise ConfigError(
            f"refus de démarrer : écoute sur '{cfg.listen_address}' (non-loopback) "
            "avec authentification désactivée.\n"
            "Un service sans mot de passe ne doit pas être exposé au réseau.\n"
            "Solutions :\n"
            "  - activer l'authentification ([auth] enabled = true + `pasteberth passwd`) ;\n"
            "  - écouter en local (listen_address = \"127.0.0.1\") derrière un reverse proxy ;\n"
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
    for zone in cfg.zones.values():
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
                zone.directory.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise ConfigError(
                    f"zone '{zone.id}': impossible de créer {zone.directory} ({exc})"
                ) from exc
        else:
            raise ConfigError(
                f"zone '{zone.id}': répertoire inexistant et create_directory = false : "
                f"{zone.directory}"
            )
