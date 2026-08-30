"""Interface en ligne de commande.

    pasteberth                         # démarre avec les valeurs locales par défaut
    pasteberth serve   [--config PATH] [--log-level LEVEL]
    pasteberth filesystem-drop [--config PATH] [--replace] DIRECTORY FILE...
    pasteberth filesystem-rename [--config PATH] DIRECTORY SOURCE TARGET
    pasteberth filesystem-delete [--config PATH] [--force] DIRECTORY FILE...
    pasteberth passwd  [--config PATH]
    pasteberth audit   [--config PATH]
    pasteberth --generate-config
"""
from __future__ import annotations

import argparse
import errno
import getpass
import logging
import mimetypes
import os
import shutil
import ssl
import stat
import sys
import tempfile
from pathlib import Path
import socket

from pasteberth import __version__
from pasteberth.auth import (
    LoginRateLimiter,
    SessionStore,
    hash_password,
    load_password_hash,
    save_password_hash,
    valid_password_hash,
)
from pasteberth.config import (
    ConfigError,
    check_startup_policy,
    build_default_config,
    config_path_for_generation,
    default_storage_path,
    find_config_path,
    is_loopback_address,
    load_config,
    prepare_directories,
    resolve_group_zone_ids,
    validate_directory_identities,
    repository_root,
)
from pasteberth.paths import first_symlink_component
from pasteberth.service import PasteService, ServiceError
from pasteberth.storage import DestinationError


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )


def _cmd_serve(args: argparse.Namespace) -> int:
    config_arg = _config_arg(args)
    config_path = find_config_path(config_arg)
    try:
        cfg = build_default_config() if config_path is None else load_config(config_path)
    except ConfigError as exc:
        print(f"pasteberth : erreur de configuration\n  {exc}", file=sys.stderr)
        return 2
    if cfg.auth.enabled:
        try:
            stored_hash = load_password_hash(cfg.password_file())
        except RuntimeError as exc:
            print(f"pasteberth : erreur de configuration\n  {exc}", file=sys.stderr)
            return 2
        if not valid_password_hash(stored_hash):
            print(
                "pasteberth : erreur de configuration\n"
                f"  [auth] enabled = true exige un hash scrypt valide dans {cfg.password_file()}\n"
                "  exécutez `pasteberth passwd` avant de démarrer le service",
                file=sys.stderr,
            )
            return 2
    tls_context = None
    if cfg.tls.enabled:
        from pasteberth.server import create_tls_context

        try:
            tls_context = create_tls_context(cfg.tls.certificate, cfg.tls.private_key)
        except (OSError, ssl.SSLError, ValueError) as exc:
            print(
                f"pasteberth : erreur TLS\n  certificat ou clé illisible : {exc}",
                file=sys.stderr,
            )
            return 2
    _setup_logging(args.log_level or cfg.log_level)
    uses_default_storage = any(
        zone.directory == default_storage_path() for zone in cfg.zones.values()
    )
    if cfg.using_default_config:
        logging.getLogger("pasteberth.config").warning(
            "aucune configuration trouvée : utilisation du stockage par défaut %s "
            "(loopback uniquement, authentification désactivée) ; "
            "exécutez `pasteberth --generate-config` pour le personnaliser",
            default_storage_path(),
        )
    elif uses_default_storage:
        logging.getLogger("pasteberth.config").warning(
            "utilisation du stockage par défaut %s ; modifiez config.toml pour le déplacer",
            default_storage_path(),
        )
    for warning in cfg.warnings:
        logging.getLogger("pasteberth.config").warning("%s", warning)

    # Revalider juste avant la résolution et le bind réduit la fenêtre où un
    # nom d'hôte pourrait changer de résolution entre la politique et l'écoute.
    log = logging.getLogger("pasteberth.cli")
    try:
        prepare_directories(cfg)
    except ConfigError as exc:
        log.error("%s", exc)
        return 2

    try:
        service = PasteService(cfg)
    except (DestinationError, OSError) as exc:
        log.error("pasteberth : erreur de destination\n  %s", exc)
        return 2
    sessions = SessionStore(
        cfg.auth.session_ttl_hours * 3600,
        password_file=cfg.password_file() if cfg.auth.enabled else None,
    )
    limiter = LoginRateLimiter()
    handler = _build_handler(cfg, service, sessions, limiter)

    from pasteberth.server import serve_forever

    try:
        expected_loopback = check_startup_policy(cfg)
    except ConfigError as exc:
        log.error("pasteberth : politique d'écoute invalide\n  %s", exc)
        return 2

    log.info(
        "Pasteberth %s démarre sur %s://%s:%d (%d zone(s), auth=%s)",
        __version__,
        "https" if cfg.tls.enabled else "http",
        cfg.listen_address,
        cfg.port,
        len(cfg.zones),
        "activée" if cfg.auth.enabled else "DÉSACTIVÉE",
    )
    if not cfg.auth.enabled and not cfg.allow_unauthenticated_remote:
        log.info(
            "écoute locale uniquement : prévoir un reverse proxy HTTPS pour un accès réseau"
        )
    try:
        serve_forever(
            handler,
            cfg.listen_address,
            cfg.port,
            tls_context=tls_context,
            expected_loopback=expected_loopback,
        )
    except OSError as exc:
        log.error("impossible d'écouter sur %s:%d : %s", cfg.listen_address, cfg.port, exc)
        return 1
    log.info("serveur arrêté")
    return 0


def _config_arg(args: argparse.Namespace) -> str | None:
    return getattr(args, "config", None) or getattr(args, "global_config", None)


def _load_command_service(args: argparse.Namespace):
    config_arg = _config_arg(args)
    config_path = find_config_path(config_arg)
    try:
        cfg = build_default_config() if config_path is None else load_config(config_path)
        prepare_directories(cfg)
        service = PasteService(cfg)
    except ConfigError as exc:
        print(f"pasteberth : erreur de configuration\n  {exc}", file=sys.stderr)
        return None
    except (DestinationError, OSError) as exc:
        print(f"pasteberth : erreur de destination\n  {exc}", file=sys.stderr)
        return None
    return cfg, service


def _command_path(raw: str) -> Path:
    if "\x00" in raw:
        raise ValueError("chemin contenant un caractère NUL")
    return Path(os.path.abspath(os.path.expanduser(raw)))


def _zone_for_directory(cfg, raw_directory: str):
    try:
        directory = _command_path(raw_directory)
        symlink = first_symlink_component(directory)
    except (OSError, ValueError) as exc:
        raise ConfigError(f"répertoire de zone invalide : {raw_directory!r} ({exc})") from exc
    if symlink is not None:
        raise ConfigError(f"le répertoire cible contient un lien symbolique : {symlink}")
    normalized = Path(os.path.normpath(str(directory)))
    for zone in cfg.zones.values():
        if normalized == Path(os.path.normpath(str(zone.directory))):
            return zone
    raise ConfigError(
        f"le répertoire cible ne correspond à aucune zone configurée : {directory}"
    )


def _read_drop_source(path: Path, max_bytes: int) -> bytes:
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    nonblock = getattr(os, "O_NONBLOCK", 0)
    fd = -1
    try:
        # Inspect before opening and keep O_NONBLOCK as a second line of
        # defence against a FIFO replacing the regular source in between.
        path_info = os.stat(path, follow_symlinks=False)
        if not stat.S_ISREG(path_info.st_mode):
            raise ValueError("la source n'est pas un fichier régulier")
        fd = os.open(path, os.O_RDONLY | nofollow | nonblock)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("la source n'est pas un fichier régulier")
        with os.fdopen(fd, "rb") as stream:
            fd = -1
            data = stream.read(max_bytes + 1)
    except (OSError, ValueError) as exc:
        raise ValueError(f"lecture impossible : {exc}") from exc
    finally:
        if fd >= 0:
            os.close(fd)
    if len(data) > max_bytes:
        raise ValueError(
            f"fichier trop grand ({len(data)} > {max_bytes} octets)"
        )
    return data


def _cmd_filesystem_rename(args: argparse.Namespace) -> int:
    loaded = _load_command_service(args)
    if loaded is None:
        return 2
    cfg, service = loaded
    try:
        zone = _zone_for_directory(cfg, args.directory)
        item = service.rename(zone.id, args.source, args.target)
    except ConfigError as exc:
        print(f"pasteberth : erreur de configuration\n  {exc}", file=sys.stderr)
        return 2
    except (OSError, ValueError, ServiceError) as exc:
        print(f"pasteberth : renommage impossible : {exc}", file=sys.stderr)
        return 1
    print(item["reference"])
    return 0


def _cmd_filesystem_delete(args: argparse.Namespace) -> int:
    loaded = _load_command_service(args)
    if loaded is None:
        return 2
    cfg, service = loaded
    try:
        zone = _zone_for_directory(cfg, args.directory)
    except ConfigError as exc:
        print(f"pasteberth : erreur de configuration\n  {exc}", file=sys.stderr)
        return 2

    failures = 0
    for filename in args.files:
        try:
            service.delete(
                zone.id,
                filename,
                allow_stale_sidecar=args.force,
            )
        except (OSError, ValueError, ServiceError) as exc:
            failures += 1
            print(f"pasteberth : suppression impossible {filename!r} : {exc}", file=sys.stderr)
            continue
        print(filename)
    return 1 if failures else 0


def _cmd_filesystem_drop(args: argparse.Namespace) -> int:
    loaded = _load_command_service(args)
    if loaded is None:
        return 2
    cfg, service = loaded
    try:
        zone = _zone_for_directory(cfg, args.directory)
    except ConfigError as exc:
        print(f"pasteberth : erreur de configuration\n  {exc}", file=sys.stderr)
        return 2

    failures = 0
    for raw_source in args.files:
        try:
            source = _command_path(raw_source)
            data = _read_drop_source(source, cfg.max_upload_bytes)
            declared_mime = mimetypes.guess_type(source.name)[0] or "application/octet-stream"
            item = service.upload(
                zone.id,
                data,
                declared_mime,
                source.name,
                preserve_filename=True,
                allow_replace=args.replace,
            )
        except (OSError, ValueError, ServiceError) as exc:
            failures += 1
            print(f"pasteberth : {raw_source} : {exc}", file=sys.stderr)
            continue
        print(item["reference"])
    return 1 if failures else 0


def _generated_config_text(root: Path) -> str:
    storage = (root / "storage" / "default").as_posix()
    return f'''# Pasteberth local configuration.
# This file is intentionally outside Git and can be edited manually.

listen_address = "127.0.0.1"
port = 8765
max_upload_size = "20MiB"
max_image_pixels = 25000000
# Trust no forwarded headers by default; list only the actual reverse proxy IP.
trusted_proxies = []
# Empty list = wildcard (Host check disabled); list the public hostnames to enforce it.
allowed_hosts = []
allow_unauthenticated_local = false
allow_unauthenticated_remote = false
# Non-loopback HTTP is refused by default; use an HTTPS reverse proxy.
allow_insecure_http_remote = false
# Accepted content kinds (all true by default).
accept_bin = true
accept_img = true
accept_doc = true
log_level = "INFO"

[tls]
enabled = false
# certificate = "/absolute/path/to/cert.pem"
# private_key = "/absolute/path/to/key.pem"

[auth]
enabled = true
session_ttl_hours = 72
# password_file = "/absolute/path/to/passwd"

[[zones]]
id = "default"
label = "Default"
type = "local"
directory = "{storage}"
retain = 10
reference_prefix = "@"
reference_suffix = ""
color = "#304237"
create_directory = true
min_free_percent = 2.0

# Optional group views. Omit the whole section to show all zones without tabs.
# Patterns use Python regular expressions (re.search, case-sensitive).
# [[groups]]
# name = "LWP"
# selection = "pattern"
# pattern = ["^lightwebpres.*$"]
# layout = "tab"
#
# [[groups]]
# name = "Other"
# selection = "other"
# layout = "area"
'''


def _cmd_generate_config(args: argparse.Namespace) -> int:
    target = config_path_for_generation(_config_arg(args))
    if "\x00" in str(target):
        print(f"chemin de configuration invalide : {target}", file=sys.stderr)
        return 2
    if target.exists() and not args.force:
        print(
            f"configuration déjà présente : {target}\n"
            "utilisez --force uniquement pour la remplacer",
            file=sys.stderr,
        )
        return 2
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
        )
        temporary_path = Path(temporary)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(_generated_config_text(repository_root()))
                stream.flush()
                os.fsync(stream.fileno())
                os.fchmod(stream.fileno(), 0o600)
            os.replace(temporary_path, target)
        finally:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass
    except OSError as exc:
        print(f"impossible d'écrire {target} : {exc}", file=sys.stderr)
        return 1
    print(f"configuration générée : {target}")
    print("prochaine étape : pasteberth passwd")
    return 0


def _audit_zone(cfg, zone) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    path = zone.directory
    try:
        symlink = first_symlink_component(path)
    except (OSError, ValueError) as exc:
        errors.append(f"zone {zone.id}: inspection impossible ({exc})")
        return errors, warnings
    if symlink is not None:
        errors.append(f"zone {zone.id}: lien symbolique refusé : {symlink}")
        return errors, warnings
    if not path.exists():
        if zone.create_directory:
            warnings.append(f"zone {zone.id}: sera créée au démarrage : {path}")
        else:
            errors.append(f"zone {zone.id}: répertoire absent : {path}")
        return errors, warnings
    if not path.is_dir():
        errors.append(f"zone {zone.id}: n'est pas un répertoire : {path}")
        return errors, warnings
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
        # Feature: avertissement seul, pas d'erreur — les zones partagées sont
        # légitimes (partage contrôlé entre utilisateurs). Refuser pousserait à
        # contourner la protection.
        if mode & 0o077:
            warnings.append(
                f"zone {zone.id}: permissions non privées ({oct(mode)}) : {path} "
                "(0700 recommandé)"
            )
    except OSError as exc:
        errors.append(f"zone {zone.id}: inspection impossible ({exc})")
    if not os.access(path, os.W_OK | os.X_OK):
        errors.append(f"zone {zone.id}: répertoire non accessible en écriture : {path}")
    lock_path = path / ".pasteberth.lock"
    try:
        lock_stat = lock_path.lstat()
    except FileNotFoundError:
        lock_stat = None
    except OSError as exc:
        errors.append(f"zone {zone.id}: inspection du verrou impossible ({exc})")
        lock_stat = None
    if lock_stat is not None:
        if stat.S_ISLNK(lock_stat.st_mode):
            errors.append(f"zone {zone.id}: le verrou est un lien symbolique : {lock_path}")
        elif not stat.S_ISREG(lock_stat.st_mode):
            errors.append(f"zone {zone.id}: le verrou n'est pas un fichier régulier : {lock_path}")
        elif not os.access(lock_path, os.R_OK | os.W_OK):
            errors.append(f"zone {zone.id}: verrou non accessible en lecture/écriture : {lock_path}")
    try:
        usage = shutil.disk_usage(path)
        free_percent = usage.free * 100.0 / usage.total if usage.total else 0.0
        if free_percent < zone.min_free_percent:
            errors.append(
                f"zone {zone.id}: espace libre {free_percent:.2f}% "
                f"< minimum {zone.min_free_percent:.2f}%"
            )
    except OSError as exc:
        warnings.append(f"zone {zone.id}: espace libre non mesurable ({exc})")
    return errors, warnings


def _audit_listener(cfg) -> tuple[str | None, str | None]:
    if cfg.port < 1024 and getattr(os, "geteuid", lambda: 1)() != 0:
        return f"port privilégié inaccessible sans root : {cfg.port}", None
    try:
        addresses = socket.getaddrinfo(
            cfg.listen_address,
            cfg.port,
            type=socket.SOCK_STREAM,
        )
    except OSError as exc:
        return f"adresse d'écoute invalide : {exc}", None
    if not addresses:
        return f"adresse d'écoute introuvable : {cfg.listen_address}", None

    family, socktype, protocol, _, sockaddr = addresses[0]
    try:
        with socket.socket(family, socktype, protocol) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(sockaddr)
    except OSError as exc:
        message = f"bind impossible sur {cfg.listen_address}:{cfg.port} ({exc})"
        if exc.errno == errno.EADDRINUSE:
            return None, (
                f"port déjà utilisé sur {cfg.listen_address}:{cfg.port} : "
                "une instance est peut-être déjà lancée"
            )
        return message, None
    return None, None


def _audit_tls(cfg) -> str | None:
    if not cfg.tls.enabled:
        return None
    from pasteberth.server import create_tls_context

    try:
        create_tls_context(cfg.tls.certificate, cfg.tls.private_key)
    except (OSError, ssl.SSLError, ValueError) as exc:
        return f"configuration TLS invalide : {exc}"
    return None


def _network_warning(cfg) -> str | None:
    if is_loopback_address(cfg.listen_address):
        return None
    if cfg.tls.enabled:
        if not cfg.auth.enabled:
            return "écoute réseau TLS directe détectée avec authentification désactivée"
        return None
    return (
        "écoute réseau HTTP non chiffrée malgré l'opt-in ; "
        "préférez [tls] ou un reverse proxy HTTPS"
    )


def _audit_groups(cfg) -> list[str]:
    warnings: list[str] = []
    all_groups = [group for group in cfg.groups if group.selection == "all"]
    other_groups = [group for group in cfg.groups if group.selection == "other"]
    if len(all_groups) > 1:
        warnings.append(
            "groupes selection='all' redondants : "
            + ", ".join(group.name for group in all_groups)
        )
    if len(other_groups) > 1:
        warnings.append(
            "groupes selection='other' redondants : "
            + ", ".join(group.name for group in other_groups)
        )
    if all_groups and other_groups:
        warnings.append(
            "selection='all' et selection='other' coexistent ; "
            "les groupes peuvent se recouvrir"
        )
    for group in cfg.groups:
        if group.selection in {"all", "other"} and group.pattern_defined:
            warnings.append(
                f"groupe {group.name}: 'pattern' ignoré avec "
                f"selection='{group.selection}'"
            )

    memberships = resolve_group_zone_ids(cfg.groups, cfg.zones)
    seen_pattern_memberships: dict[tuple[str, ...], str] = {}
    seen_effective_memberships: dict[tuple[str, ...], dict[str, str]] = {}
    for group in cfg.groups:
        zone_ids = memberships[group.name]
        if group.selection == "pattern":
            previous = seen_pattern_memberships.get(zone_ids)
            if previous is not None:
                warnings.append(
                    f"groupes pattern redondants : {previous} et {group.name} "
                    f"sélectionnent les mêmes zones"
                )
            else:
                seen_pattern_memberships[zone_ids] = group.name

        groups_by_selection = seen_effective_memberships.setdefault(zone_ids, {})
        if group.selection not in groups_by_selection:
            for previous_selection, previous_name in groups_by_selection.items():
                if {previous_selection, group.selection} == {"all", "other"}:
                    continue
                warnings.append(
                    f"groupes redondants : {previous_name} ({previous_selection}) et "
                    f"{group.name} ({group.selection}) sélectionnent les mêmes zones"
                )
            groups_by_selection[group.selection] = group.name
    return warnings


def _cmd_audit(args: argparse.Namespace) -> int:
    config_path = find_config_path(_config_arg(args))
    errors: list[str] = []
    warnings: list[str] = []
    if config_path is None:
        cfg = build_default_config()
        warnings.append(
            f"aucun config.toml trouvé : stockage par défaut {default_storage_path()}"
        )
    else:
        try:
            cfg = load_config(config_path)
        except ConfigError as exc:
            print(f"ERREUR configuration : {exc}", file=sys.stderr)
            return 2
    warnings.extend(cfg.warnings)

    if sys.version_info < (3, 11):
        errors.append("Python 3.11 ou plus récent est requis")
    uses_default_storage = any(
        zone.directory == default_storage_path() for zone in cfg.zones.values()
    )
    if cfg.using_default_config:
        warnings.append("authentification désactivée dans le mode par défaut loopback")
    elif not cfg.auth.enabled:
        warnings.append("authentification désactivée dans la configuration")
    elif cfg.auth.enabled:
        try:
            stored_hash = load_password_hash(cfg.password_file())
        except RuntimeError as exc:
            errors.append(str(exc))
        else:
            if not valid_password_hash(stored_hash):
                errors.append(f"hash scrypt absent ou invalide : {cfg.password_file()}")
    if uses_default_storage and config_path is not None:
        warnings.append(
            f"stockage par défaut utilisé : {default_storage_path()}"
        )
    if not cfg.allowed_hosts:
        warnings.append(
            "allowed_hosts vide : contrôle de Host désactivé (wildcard) ; "
            "listez vos noms d'hôte exposés pour le réactiver"
        )
    if not (cfg.accept_bin or cfg.accept_img or cfg.accept_doc):
        warnings.append(
            "accept_bin, accept_img et accept_doc sont tous à false : "
            "le serveur refusera tout contenu"
        )
    warnings.extend(_audit_groups(cfg))

    for zone in cfg.zones.values():
        zone_errors, zone_warnings = _audit_zone(cfg, zone)
        errors.extend(zone_errors)
        warnings.extend(zone_warnings)
    try:
        validate_directory_identities(cfg)
    except ConfigError as exc:
        errors.append(str(exc))

    network_warning = _network_warning(cfg)
    if network_warning:
        warnings.append(network_warning)
    listener_error, listener_warning = _audit_listener(cfg)
    if listener_error:
        errors.append(listener_error)
    if listener_warning:
        warnings.append(listener_warning)
    tls_error = _audit_tls(cfg)
    if tls_error:
        errors.append(tls_error)

    for message in warnings:
        print(f"AVERTISSEMENT : {message}")
    for message in errors:
        print(f"ERREUR : {message}")
    if errors:
        print(f"Audit échoué : {len(errors)} erreur(s), {len(warnings)} avertissement(s)")
        return 2
    if warnings:
        print(f"Audit prêt avec {len(warnings)} avertissement(s)")
        return 1
    print("Audit réussi : configuration prête")
    return 0


def _build_handler(cfg, service: PasteService, sessions: SessionStore,
                   limiter: LoginRateLimiter):
    from pasteberth.webapp import make_handler

    return make_handler(cfg, service, sessions, limiter)


def _cmd_passwd(args: argparse.Namespace) -> int:
    config_path = find_config_path(_config_arg(args))
    if config_path is None:
        print(
            "configuration absente : exécutez d'abord `pasteberth --generate-config`",
            file=sys.stderr,
        )
        return 2
    if "\x00" in str(config_path):
        print(f"chemin de configuration invalide : {config_path}", file=sys.stderr)
        return 2
    # La politique de sécurité ne bloque pas passwd : il doit rester possible
    # de préparer la configuration avant le premier démarrage.
    password_file = config_path.parent / "passwd"

    if config_path.exists():
        try:
            cfg = load_config(config_path)
            check_startup_policy(cfg)
            password_file = cfg.password_file()
        except ConfigError as exc:
            print(f"note : {config_path} comporte des problèmes ({exc})", file=sys.stderr)

    pw1 = getpass.getpass("Nouveau mot de passe : ")
    if len(pw1) < 8:
        print("refus : au moins 8 caractères requis", file=sys.stderr)
        return 1
    pw2 = getpass.getpass("Confirmation       : ")
    if pw1 != pw2:
        print("refus : les deux saisies diffèrent", file=sys.stderr)
        return 1
    try:
        save_password_hash(password_file, hash_password(pw1))
    except OSError as exc:
        print(f"erreur : impossible d'écrire {password_file} : {exc}", file=sys.stderr)
        return 1
    try:
        mode = oct(password_file.stat().st_mode & 0o777)
    except OSError:
        mode = "?"
    print(f"hash scrypt écrit dans {password_file} (mode {mode})")
    print(
        "pensez à activer [auth] enabled = true dans "
        f"{config_path if config_path.exists() else 'votre config.toml'}"
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pasteberth",
        description="Pont clipboard navigateur <-> filesystem du harness.",
    )
    parser.add_argument("--version", action="version", version=f"pasteberth {__version__}")
    parser.add_argument("--config", dest="global_config", help=argparse.SUPPRESS)
    parser.add_argument(
        "--generate-config",
        action="store_true",
        help="génère la configuration locale par défaut sans démarrer le serveur",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="autorise le remplacement d'une configuration générée",
    )
    sub = parser.add_subparsers(dest="command", required=False)

    p_serve = sub.add_parser("serve", help="démarre le serveur")
    p_serve.add_argument("--config", default=argparse.SUPPRESS, help="chemin du config.toml")
    p_serve.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    p_serve.set_defaults(func=_cmd_serve)

    p_drop = sub.add_parser(
        "filesystem-drop",
        help="dépose des fichiers dans une zone filesystem et crée leurs sidecars",
    )
    p_drop.add_argument("directory", help="répertoire exact de la zone configurée")
    p_drop.add_argument("files", nargs="+", help="fichiers sources à copier")
    p_drop.add_argument("--replace", action="store_true",
                        help="autorise le remplacement explicite d'un nom géré")
    p_drop.add_argument("--config", default=argparse.SUPPRESS, help="chemin du config.toml")
    p_drop.set_defaults(func=_cmd_filesystem_drop)

    p_rename = sub.add_parser(
        "filesystem-rename",
        help="renomme un fichier géré avec son sidecar",
    )
    p_rename.add_argument("directory", help="répertoire exact de la zone configurée")
    p_rename.add_argument("source", help="nom géré actuel, sans chemin")
    p_rename.add_argument("target", help="nouveau nom, sans chemin")
    p_rename.add_argument("--config", default=argparse.SUPPRESS, help="chemin du config.toml")
    p_rename.set_defaults(func=_cmd_filesystem_rename)

    p_delete = sub.add_parser(
        "filesystem-delete",
        help="supprime un ou plusieurs fichiers gérés avec leurs sidecars",
    )
    p_delete.add_argument("directory", help="répertoire exact de la zone configurée")
    p_delete.add_argument("files", nargs="+", help="noms gérés à supprimer, sans chemin")
    p_delete.add_argument(
        "--force",
        action="store_true",
        help="supprime aussi une paire dont la taille ne correspond plus au sidecar",
    )
    p_delete.add_argument("--config", default=argparse.SUPPRESS, help="chemin du config.toml")
    p_delete.set_defaults(func=_cmd_filesystem_delete)

    p_pw = sub.add_parser("passwd", help="définit ou change le mot de passe")
    p_pw.add_argument(
        "--config", default=argparse.SUPPRESS, help="chemin du config.toml (le hash va à côté)"
    )
    p_pw.set_defaults(func=_cmd_passwd)

    p_audit = sub.add_parser("audit", help="vérifie l'environnement sans le modifier")
    p_audit.add_argument("--config", default=argparse.SUPPRESS, help="chemin du config.toml")
    p_audit.set_defaults(func=_cmd_audit)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.generate_config:
        return _cmd_generate_config(args)
    if args.command is None:
        args.config = args.global_config
        args.log_level = None
        return _cmd_serve(args)
    if not hasattr(args, "config"):
        args.config = args.global_config
    elif args.config is None:
        args.config = args.global_config
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
