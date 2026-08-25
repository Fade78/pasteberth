"""Interface en ligne de commande.

    pasteberth serve   [--config PATH] [--log-level LEVEL]
    pasteberth passwd  [--config PATH]
    pasteberth --version
"""
from __future__ import annotations

import argparse
import getpass
import logging
import sys

from pasteberth import __version__
from pasteberth.auth import LoginRateLimiter, SessionStore, hash_password, save_password_hash
from pasteberth.config import (
    ConfigError,
    check_startup_policy,
    load_config,
    prepare_directories,
    resolve_config_path,
)
from pasteberth.service import PasteService


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )


def _cmd_serve(args: argparse.Namespace) -> int:
    try:
        cfg = load_config(resolve_config_path(args.config))
    except ConfigError as exc:
        print(f"pasteberth : erreur de configuration\n  {exc}", file=sys.stderr)
        return 2
    _setup_logging(args.log_level or cfg.log_level)
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

    service = PasteService(cfg)
    sessions = SessionStore(cfg.auth.session_ttl_hours * 3600)
    limiter = LoginRateLimiter()
    handler = _build_handler(cfg, service, sessions, limiter)

    from pasteberth.server import serve_forever

    log.info(
        "Pasteberth %s démarre sur http://%s:%d (%d zone(s), auth=%s)",
        __version__,
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
        serve_forever(handler, cfg.listen_address, cfg.port)
    except OSError as exc:
        log.error("impossible d'écouter sur %s:%d : %s", cfg.listen_address, cfg.port, exc)
        return 1
    log.info("serveur arrêté")
    return 0


def _build_handler(cfg, service: PasteService, sessions: SessionStore,
                   limiter: LoginRateLimiter):
    from pasteberth.webapp import make_handler

    return make_handler(cfg, service, sessions, limiter)


def _cmd_passwd(args: argparse.Namespace) -> int:
    config_path = resolve_config_path(args.config)
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
    save_password_hash(password_file, hash_password(pw1))
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
    sub = parser.add_subparsers(dest="command", required=True)

    p_serve = sub.add_parser("serve", help="démarre le serveur")
    p_serve.add_argument("--config", help="chemin du config.toml")
    p_serve.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    p_serve.set_defaults(func=_cmd_serve)

    p_pw = sub.add_parser("passwd", help="définit ou change le mot de passe")
    p_pw.add_argument("--config", help="chemin du config.toml (le hash va à côté)")
    p_pw.set_defaults(func=_cmd_passwd)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
