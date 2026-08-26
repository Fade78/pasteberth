"""Interface en ligne de commande.

    pasteberth                         # démarre avec les valeurs locales par défaut
    pasteberth serve   [--config PATH] [--log-level LEVEL]
    pasteberth passwd  [--config PATH]
    pasteberth audit   [--config PATH]
    pasteberth --generate-config
"""
from __future__ import annotations

import argparse
import getpass
import logging
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
    validate_directory_identities,
    repository_root,
)
from pasteberth.paths import first_symlink_component
from pasteberth.service import PasteService
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

    # Politique de sécurité déjà validée par load_config() ; on réaffiche
    # le contexte d'écoute pour journal clair.
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
        serve_forever(handler, cfg.listen_address, cfg.port, tls_context=tls_context)
    except OSError as exc:
        log.error("impossible d'écouter sur %s:%d : %s", cfg.listen_address, cfg.port, exc)
        return 1
    log.info("serveur arrêté")
    return 0


def _config_arg(args: argparse.Namespace) -> str | None:
    return getattr(args, "config", None) or getattr(args, "global_config", None)


def _generated_config_text(root: Path) -> str:
    storage = (root / "storage" / "default").as_posix()
    return f'''# Pasteberth local configuration.
# This file is intentionally outside Git and can be edited manually.

listen_address = "127.0.0.1"
port = 8765
max_upload_size = "20MiB"
max_image_pixels = 25000000
trusted_proxies = ["127.0.0.1", "::1"]
allowed_hosts = []
allow_unauthenticated_local = false
allow_unauthenticated_remote = false
# Non-loopback HTTP is refused by default; use an HTTPS reverse proxy.
allow_insecure_http_remote = false
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
'''


def _cmd_generate_config(args: argparse.Namespace) -> int:
    target = config_path_for_generation(_config_arg(args))
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
            os.chmod(temporary_path, 0o600)
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
    except OSError as exc:
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
        if mode & 0o022:
            errors.append(
                f"zone {zone.id}: permissions d'écriture non privées ({oct(mode)}) : {path} "
                "(0700 requis)"
            )
        elif mode & 0o077:
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


def _audit_listener(cfg) -> str | None:
    if cfg.port < 1024 and getattr(os, "geteuid", lambda: 1)() != 0:
        return f"port privilégié inaccessible sans root : {cfg.port}"
    try:
        addresses = socket.getaddrinfo(
            cfg.listen_address,
            cfg.port,
            type=socket.SOCK_STREAM,
        )
    except OSError as exc:
        return f"adresse d'écoute invalide : {exc}"
    if not addresses:
        return f"adresse d'écoute introuvable : {cfg.listen_address}"

    family, socktype, protocol, _, sockaddr = addresses[0]
    try:
        with socket.socket(family, socktype, protocol) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(sockaddr)
    except OSError as exc:
        return f"bind impossible sur {cfg.listen_address}:{cfg.port} ({exc})"
    return None


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
    listener_error = _audit_listener(cfg)
    if listener_error:
        errors.append(listener_error)
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
        description="Pont clipboard navigateur -> filesystem du harness.",
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
