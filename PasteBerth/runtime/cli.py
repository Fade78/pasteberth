"""Command-line interface.

    pasteberth                         # start with local default values
    pasteberth serve   [--config PATH] [--log-level LEVEL]
    pasteberth drop [--config PATH] [--server URL] [--zone ID] [--replace] [ZONE_DIRECTORY] FILE...
    pasteberth register [--config PATH] FILE
    pasteberth mcp  [--config PATH] [--server URL] [--insecure]
    pasteberth copy [--config PATH] SOURCE_DIRECTORY TARGET_DIRECTORY FILE...
    pasteberth move [--config PATH] SOURCE_DIRECTORY TARGET_DIRECTORY FILE...
    pasteberth rename [--config PATH] DIRECTORY SOURCE TARGET
    pasteberth delete [--config PATH] [--force] DIRECTORY FILE...
    pasteberth passwd  [--config PATH]
    pasteberth audit   [--config PATH]
    pasteberth completion [--shell bash]
    pasteberth [--config PATH] --generate-config [--force]
"""
from __future__ import annotations

import argparse
import base64
import binascii
import errno
import getpass
import hashlib
import logging
import mimetypes
import os
import re
import secrets
import ssl
import stat
import sys
import time
from pathlib import Path
import socket

from . import __version__
from .zone_collection import discover_zone_collections, resolve_collection_members
from .client import ClientError, PasteberthClient, api_error
from .auth import (
    LoginRateLimiter,
    SessionStore,
    hash_password,
    load_password_hash,
    save_password_hash,
    valid_password_hash,
)
from .tokens import TokenStore, TokenStoreError
from .config import (
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
    ensure_external_path,
)
from .content import classify
from .images import InvalidImageError, mime_syntax_allowed
from .platformfs import UnsafeLinkError, UnsupportedFilesystemError, platform_fs
from .mcp import McpToolError, run_stdio
from .service import PasteService, ServiceError
from .storage import DestinationError, LocalDestination


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
        print(f"pasteberth: configuration error\n  {exc}", file=sys.stderr)
        return 2
    if cfg.auth.enabled:
        try:
            stored_hash = load_password_hash(
                cfg.password_file(),
                max_bytes=cfg.limits.max_password_file_bytes,
            )
        except RuntimeError as exc:
            print(f"pasteberth: configuration error\n  {exc}", file=sys.stderr)
            return 2
        if not valid_password_hash(stored_hash):
            print(
                "pasteberth: configuration error\n"
                f"  [auth] enabled = true requires a valid scrypt hash in {cfg.password_file()}\n"
                "  run `pasteberth passwd` before starting the service",
                file=sys.stderr,
            )
            return 2
    tls_context = None
    if cfg.tls.enabled:
        from .server import create_tls_context

        try:
            tls_context = create_tls_context(cfg.tls.certificate, cfg.tls.private_key)
        except (OSError, ssl.SSLError, ValueError) as exc:
            print(
                f"pasteberth: TLS error\n  unreadable certificate or key: {exc}",
                file=sys.stderr,
            )
            return 2
    _setup_logging(args.log_level or cfg.log_level)
    uses_default_storage = any(
        zone.directory == default_storage_path() for zone in cfg.zones.values()
    )
    if cfg.using_default_config:
        logging.getLogger("pasteberth.config").warning(
            "no configuration found: using default storage %s "
            "(loopback only, authentication disabled); "
            "run `pasteberth --generate-config` to customize it",
            default_storage_path(),
        )
    elif uses_default_storage:
        logging.getLogger("pasteberth.config").warning(
            "using default storage %s; edit the selected configuration to move it",
            default_storage_path(),
        )
    for warning in cfg.warnings:
        logging.getLogger("pasteberth.config").warning("%s", warning)

    # Revalidating immediately before resolution and bind reduces the window
    # in which a hostname could resolve differently between policy and listen.
    log = logging.getLogger("pasteberth.cli")
    try:
        prepare_directories(cfg)
    except ConfigError as exc:
        log.error("%s", exc)
        return 2

    try:
        service = PasteService(cfg)
    except (DestinationError, OSError) as exc:
        log.error("pasteberth: destination error\n  %s", exc)
        return 2
    tokens = None
    if cfg.auth.enabled:
        try:
            tokens = TokenStore(cfg.token_file())
        except TokenStoreError as exc:
            log.error("pasteberth: token registry error\n  %s", exc)
            service.close()
            return 2
    sessions = SessionStore(
        cfg.auth.session_ttl_hours * 3600,
        password_file=cfg.password_file() if cfg.auth.enabled else None,
        max_sessions=cfg.auth.max_sessions,
    )
    limiter = LoginRateLimiter(
        max_concurrent_checks=cfg.limits.max_login_concurrent_checks,
        max_tracked_ips=cfg.limits.max_login_tracked_ips,
        max_delay=cfg.limits.max_login_delay_seconds,
        forget_after=cfg.limits.login_forget_after_seconds,
    )
    handler = _build_handler(cfg, service, sessions, limiter, tokens)

    from .server import serve_forever

    try:
        expected_loopback = check_startup_policy(cfg)
    except ConfigError as exc:
        log.error("pasteberth: invalid listener policy\n  %s", exc)
        return 2

    log.info(
        "Pasteberth %s listening on %s://%s:%d (%d zone(s), auth=%s)",
        __version__,
        "https" if cfg.tls.enabled else "http",
        cfg.listen_address,
        cfg.port,
        service.active_zone_count(),
        "enabled" if cfg.auth.enabled else "DISABLED",
    )
    if not cfg.auth.enabled and not cfg.allow_unauthenticated_remote:
        log.info(
            "local-only listener: use an HTTPS reverse proxy for network access"
        )
    try:
        serve_forever(
            handler,
            cfg.listen_address,
            cfg.port,
            tls_context=tls_context,
            expected_loopback=expected_loopback,
            limits=cfg.limits,
        )
    except OSError as exc:
        log.error("cannot listen on %s:%d: %s", cfg.listen_address, cfg.port, exc)
        return 1
    finally:
        service.close()
    log.info("server stopped")
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
        print(f"pasteberth: configuration error\n  {exc}", file=sys.stderr)
        return None
    except (DestinationError, OSError) as exc:
        print(f"pasteberth: destination error\n  {exc}", file=sys.stderr)
        return None
    return cfg, service


def _command_path(raw: str) -> Path:
    if "\x00" in raw:
        raise ValueError("path contains a NUL character")
    return Path(os.path.abspath(os.path.expanduser(raw))).resolve()


def _zone_for_directory(cfg, raw_directory: str, *, service: PasteService | None = None):
    try:
        directory = _command_path(raw_directory)
    except (OSError, ValueError) as exc:
        raise ConfigError(f"invalid zone directory: {raw_directory!r} ({exc})") from exc
    if service is not None:
        try:
            return service.zone_for_directory(directory)
        except ServiceError as exc:
            raise ConfigError(str(exc)) from exc
    normalized = Path(os.path.normpath(str(directory)))
    zones = dict(cfg.zones)
    candidates, _diagnostics = discover_zone_collections(
        cfg.zone_collections, cfg.zones
    )
    for candidate in candidates:
        zones.setdefault(candidate.zone.id, candidate.zone)
    for zone in zones.values():
        if normalized == Path(os.path.normpath(str(zone.directory.resolve()))):
            return zone
    raise ConfigError(
        f"directory does not match any configured zone: {directory}"
    )


def _zone_for_id(cfg, zone_id: str):
    zone = cfg.zones.get(zone_id)
    if zone is not None:
        return zone
    candidates, _diagnostics = discover_zone_collections(
        cfg.zone_collections, cfg.zones
    )
    for candidate in candidates:
        if candidate.zone.id == zone_id:
            return candidate.zone
    return None


def _read_drop_source(path: Path, max_bytes: int | None) -> bytes:
    path = Path(path).expanduser().resolve()
    fs = platform_fs()
    try:
        with fs.open_directory(path.parent) as parent:
            with fs.open_existing(parent, path.name, mode="rb") as stream:
                data = stream.read(max_bytes + 1) if max_bytes is not None else stream.read()
    except UnsafeLinkError as exc:
        raise ValueError("source is not a regular file") from exc
    except (OSError, ValueError, UnsupportedFilesystemError) as exc:
        raise ValueError(f"cannot read source: {exc}") from exc
    if max_bytes is not None and len(data) > max_bytes:
        raise ValueError(
            f"file is too large ({len(data)} > {max_bytes} bytes)"
        )
    return data


def _cmd_rename(args: argparse.Namespace) -> int:
    loaded = _load_command_service(args)
    if loaded is None:
        return 2
    cfg, service = loaded
    try:
        zone = _zone_for_directory(cfg, args.directory, service=service)
        item = service.rename(zone.id, args.source, args.target)
    except ConfigError as exc:
        print(f"pasteberth: configuration error\n  {exc}", file=sys.stderr)
        return 2
    except (OSError, ValueError, ServiceError) as exc:
        print(f"pasteberth: rename failed: {exc}", file=sys.stderr)
        return 1
    print(item["reference"])
    return 0


def _cmd_delete(args: argparse.Namespace) -> int:
    loaded = _load_command_service(args)
    if loaded is None:
        return 2
    cfg, service = loaded
    try:
        zone = _zone_for_directory(cfg, args.directory, service=service)
    except ConfigError as exc:
        print(f"pasteberth: configuration error\n  {exc}", file=sys.stderr)
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
            print(f"pasteberth: cannot delete {filename!r}: {exc}", file=sys.stderr)
            continue
        print(filename)
    return 1 if failures else 0


def _cmd_transfer(args: argparse.Namespace, mode: str) -> int:
    loaded = _load_command_service(args)
    if loaded is None:
        return 2
    cfg, service = loaded
    try:
        source_zone = _zone_for_directory(cfg, args.source_directory, service=service)
        target_zone = _zone_for_directory(cfg, args.target_directory, service=service)
        result = service.transfer(
            source_zone.id,
            target_zone.id,
            args.files,
            mode=mode,
        )
    except ConfigError as exc:
        print(f"pasteberth: configuration error\n  {exc}", file=sys.stderr)
        return 2
    except (OSError, ValueError, ServiceError) as exc:
        print(f"pasteberth: {mode} failed: {exc}", file=sys.stderr)
        return 1

    for item in result["items"]:
        print(item["reference"])
    for failure in result["failed"]:
        print(
            f"pasteberth: cannot {mode} {failure['filename']!r}: {failure['message']}",
            file=sys.stderr,
        )
    return 1 if result["failed"] else 0


def _cmd_copy(args: argparse.Namespace) -> int:
    return _cmd_transfer(args, "copy")


def _cmd_move(args: argparse.Namespace) -> int:
    return _cmd_transfer(args, "move")


_DROP_ZONE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_DEFAULT_DROP_SERVER_URL = "http://127.0.0.1:8765"


def _drop_server_url(cfg, explicit: str | None) -> str:
    if explicit:
        return explicit
    if cfg.using_default_config:
        return _DEFAULT_DROP_SERVER_URL
    host = cfg.listen_address
    if host in {"0.0.0.0", "::", ""}:
        host = "127.0.0.1"
    elif ":" in host and not host.startswith("["):
        host = f"[{host}]"
    scheme = "https" if cfg.tls.enabled else "http"
    return f"{scheme}://{host}:{cfg.port}{cfg.url_prefix}"


def _drop_password(args: argparse.Namespace) -> str:
    if args.password_stdin:
        return sys.stdin.read().rstrip("\r\n")
    from_environment = os.environ.get("PASTEBERTH_PASSWORD")
    if from_environment is not None:
        return from_environment
    return getpass.getpass("Pasteberth password: ")


def _drop_token(args: argparse.Namespace) -> str | None:
    if getattr(args, "token_stdin", False):
        value = sys.stdin.read().strip()
        if not value:
            raise ConfigError("--token-stdin received an empty bearer token")
        return value
    value = os.environ.get("PASTEBERTH_TOKEN")
    return value.strip() if value and value.strip() else None


def _try_direct_drop(
    destination: LocalDestination | None,
    client: PasteberthClient,
    zone_id: str,
    source: Path,
    data: bytes,
    declared_mime: str,
    *,
    replace: bool,
) -> dict | None:
    if destination is None:
        return None
    try:
        stage_name = destination.stage_direct_drop(data)
    except (DestinationError, OSError, UnsupportedFilesystemError):
        return None
    try:
        response = client.regularize(
            zone_id,
            stage_name,
            source.name,
            declared_mime,
            replace=replace,
        )
        if response.status in {401, 404, 405}:
            destination.discard_direct_drop(stage_name)
            return None
        if response.status != 201:
            raise api_error(response)
        payload = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get("reference"), str):
            raise ClientError(
                "server returned an invalid regularization response",
                status=response.status,
            )
        return payload
    except BaseException:
        try:
            destination.discard_direct_drop(stage_name)
        except (DestinationError, OSError):
            pass
        raise


def _resolve_drop_zone(
    client: PasteberthClient,
    directory: Path,
    *,
    cookie: str | None = None,
) -> str | None:
    response = client.resolve_directory(str(directory), cookie=cookie)
    if response.status == 401:
        return None
    if response.status != 200:
        raise api_error(response, "cannot resolve target zone")
    payload = response.json()
    if not isinstance(payload, dict) or not isinstance(payload.get("zone"), str):
        raise ClientError("server returned an invalid zone resolution response", status=response.status)
    return payload["zone"]


def _local_register_file_group(path: Path) -> str | None:
    if os.name == "nt":
        return None
    try:
        group_ids = {os.getgid(), *os.getgroups()}
        group_id = path.stat().st_gid
    except (AttributeError, OSError):
        return None
    return str(group_id) if group_id in group_ids else None


def _local_register_info(data: bytes, path: Path, declared_mime: str, cfg):
    if not data:
        raise ValueError("no data received")
    if cfg.max_upload_bytes is not None and len(data) > cfg.max_upload_bytes:
        raise ValueError(
            f"file is too large ({len(data)} > {cfg.max_upload_bytes} bytes)"
        )
    if not mime_syntax_allowed(
        declared_mime,
        max_length=cfg.limits.max_mime_length,
    ):
        raise ValueError(f"declared Content-Type is invalid: {declared_mime!r}")
    try:
        info = classify(
            data,
            declared_mime,
            path.name,
            max_pixels=cfg.max_image_pixels,
            max_dimension=cfg.limits.max_image_dimension,
            max_raw_bytes=cfg.limits.max_image_raw_bytes,
            max_png_chunks=cfg.limits.max_png_chunks,
            max_jpeg_segments=cfg.limits.max_jpeg_segments,
            max_webp_chunks=cfg.limits.max_webp_chunks,
        )
    except InvalidImageError as exc:
        raise ValueError(str(exc)) from exc
    accepted = {
        "image": cfg.accept_img,
        "text": cfg.accept_doc,
        "binary": cfg.accept_bin,
    }
    if not accepted[info.kind]:
        raise ValueError(
            f"{info.kind} content is rejected by configuration (accept_*)"
        )
    return info


def _cmd_register_path(args: argparse.Namespace, raw_path: str) -> int:
    config_path = find_config_path(_config_arg(args))
    try:
        cfg = build_default_config() if config_path is None else load_config(config_path)
        path = _command_path(raw_path)
        declared_mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        destination = LocalDestination(
            path.parent,
            create_directory=False,
            limits=cfg.limits,
            max_image_pixels=cfg.max_image_pixels,
            file_group=_local_register_file_group(path),
        )
    except ConfigError as exc:
        print(f"pasteberth: configuration error\n  {exc}", file=sys.stderr)
        return 2
    except (DestinationError, OSError, UnsupportedFilesystemError, ValueError) as exc:
        print(f"pasteberth: {raw_path}: {exc}", file=sys.stderr)
        return 1

    try:
        with destination.operation_lock(exclusive=True):
            data = destination.read_foreign(path.name, cfg.max_upload_bytes)
            info = _local_register_info(data, path, declared_mime, cfg)
            destination.save(
                data,
                info,
                filename=path.name,
                register_existing=True,
                sha256=hashlib.sha256(data).hexdigest(),
                creation_method="filesystem_register",
            )
    except (DestinationError, OSError, UnsupportedFilesystemError, ValueError) as exc:
        print(f"pasteberth: {raw_path}: {exc}", file=sys.stderr)
        return 1
    print(path)
    return 0


def _cmd_register(args: argparse.Namespace) -> int:
    return _cmd_register_path(args, args.file)


def _cmd_drop(args: argparse.Namespace) -> int:
    if args.password_stdin and args.token_stdin:
        print(
            "pasteberth: --password-stdin and --token-stdin are mutually exclusive",
            file=sys.stderr,
        )
        return 2
    if args.password_stdin and os.environ.get("PASTEBERTH_TOKEN", "").strip():
        print(
            "pasteberth: --password-stdin cannot be combined with PASTEBERTH_TOKEN",
            file=sys.stderr,
        )
        return 2
    if args.zone_id is None and args.directory is not None and not args.files:
        print(
            "pasteberth: drop requires a zone directory and at least one source file; "
            "use 'pasteberth register FILE' for an existing local file",
            file=sys.stderr,
        )
        return 2
    config_path = find_config_path(_config_arg(args))
    cookie = None
    password = None
    bearer_token = None
    try:
        cfg = build_default_config() if config_path is None else load_config(config_path)
        bearer_token = _drop_token(args)
        if bearer_token is not None and args.zone_id is None:
            raise ConfigError(
                "bearer drop requires --zone ID; target-directory resolution "
                "uses session or loopback authentication"
            )
        local_zone = None
        if args.zone_id is not None:
            if not _DROP_ZONE_RE.fullmatch(args.zone_id):
                raise ConfigError(f"invalid zone ID: {args.zone_id!r}")
            local_zone = _zone_for_id(cfg, args.zone_id)
            sources = ([args.directory] if args.directory is not None else []) + args.files
            zone_id = args.zone_id
            target_directory = None
        else:
            if args.directory is None or not args.files:
                raise ConfigError("drop requires a zone directory and at least one source file")
            target_directory = _command_path(args.directory)
            zone_id = None
            sources = args.files
        if not sources:
            raise ConfigError("drop requires at least one source file")
        client = PasteberthClient(
            _drop_server_url(cfg, args.server_url),
            timeout=cfg.limits.http_request_timeout_seconds or 60.0,
            insecure=args.insecure,
            bearer_token=bearer_token,
        )
        if target_directory is not None:
            zone_id = _resolve_drop_zone(client, target_directory, cookie=cookie)
            if zone_id is None:
                password = _drop_password(args)
                cookie = client.login(password)
                zone_id = _resolve_drop_zone(client, target_directory, cookie=cookie)
            if zone_id is None:
                raise ClientError("server did not resolve a target zone")
        local_destination = None
        destination_directory = target_directory
        create_directory = False
        if destination_directory is None and local_zone is not None:
            destination_directory = local_zone.directory
            create_directory = local_zone.create_directory
        local_destination_zone = local_zone
        if local_destination_zone is None and destination_directory is not None:
            try:
                local_destination_zone = _zone_for_directory(cfg, str(destination_directory))
            except ConfigError:
                pass
        if destination_directory is not None and bearer_token is None:
            try:
                local_endpoint = is_loopback_address(client.host)
            except ConfigError:
                local_endpoint = False
            if local_endpoint:
                local_destination = LocalDestination(
                    destination_directory,
                    create_directory=create_directory,
                    limits=cfg.limits,
                    max_image_pixels=cfg.max_image_pixels,
                    file_group=(
                        local_destination_zone.file_group
                        if local_destination_zone is not None
                        else None
                    ),
                )
        if zone_id is None:
            raise ClientError("server did not resolve a target zone")
    except (ConfigError, ClientError) as exc:
        print(f"pasteberth: configuration error\n  {exc}", file=sys.stderr)
        return 2

    failures = 0
    for raw_source in sources:
        try:
            source = _command_path(raw_source)
            data = _read_drop_source(source, cfg.max_upload_bytes)
            declared_mime = mimetypes.guess_type(source.name)[0] or "application/octet-stream"
            payload = _try_direct_drop(
                local_destination,
                client,
                zone_id,
                source,
                data,
                declared_mime,
                replace=args.replace,
            )
            if payload is not None:
                print(payload["reference"])
                continue
            response = client.upload(
                zone_id,
                data,
                source.name,
                declared_mime,
                replace=args.replace,
                cookie=cookie,
            )
            if response.status == 401:
                if bearer_token is not None:
                    raise api_error(response)
                if password is None:
                    password = _drop_password(args)
                cookie = client.login(password)
                response = client.upload(
                    zone_id,
                    data,
                    source.name,
                    declared_mime,
                    replace=args.replace,
                    cookie=cookie,
                )
            if response.status != 201:
                raise api_error(response)
            payload = response.json()
            if not isinstance(payload, dict) or not (
                isinstance(payload.get("reference"), str)
                or payload.get("accepted") is True
            ):
                raise ClientError("server returned an invalid upload response", status=response.status)
        except (EOFError, OSError, ValueError, ClientError) as exc:
            failures += 1
            print(f"pasteberth: {raw_source}: {exc}", file=sys.stderr)
            continue
        print(payload["reference"] if "reference" in payload else "accepted")
    return 1 if failures else 0


def _mcp_item(item: object, max_bytes: int | None, limits) -> tuple[bytes, str, str]:
    if not isinstance(item, dict):
        raise McpToolError("each drop item must be an object")
    allowed = {"path", "content", "content_base64", "filename", "mime"}
    unknown = set(item) - allowed
    if unknown:
        raise McpToolError(f"unknown drop item field: {sorted(unknown)[0]!r}")
    sources = [key for key in ("path", "content", "content_base64") if key in item]
    if len(sources) != 1:
        raise McpToolError("each drop item must contain exactly one source")
    source = sources[0]
    declared_mime = item.get("mime")
    if declared_mime is not None and (
        not isinstance(declared_mime, str) or not declared_mime
    ):
        raise McpToolError("drop item mime must be a non-empty string")

    if source == "path":
        raw_path = item["path"]
        if not isinstance(raw_path, str) or not raw_path:
            raise McpToolError("drop item path must be a non-empty string")
        if "filename" in item:
            raise McpToolError("filename is only valid for in-memory drop content")
        path = Path(raw_path).expanduser()
        data = _read_drop_source(path, max_bytes)
        filename = path.name
        fallback_mime = "application/octet-stream"
    else:
        filename = item.get("filename")
        if not isinstance(filename, str) or not filename:
            raise McpToolError("in-memory drop content requires filename")
        if source == "content":
            content = item["content"]
            if not isinstance(content, str):
                raise McpToolError("drop content must be a string")
            data = content.encode("utf-8")
            fallback_mime = "text/plain"
        else:
            encoded = item["content_base64"]
            if not isinstance(encoded, str) or not encoded:
                raise McpToolError("content_base64 must be a non-empty string")
            try:
                data = base64.b64decode(encoded, validate=True)
            except (ValueError, TypeError, binascii.Error) as exc:
                raise McpToolError("content_base64 is not valid base64") from exc
            fallback_mime = "application/octet-stream"

    if max_bytes is not None and len(data) > max_bytes:
        raise McpToolError(f"content is too large ({len(data)} > {max_bytes} bytes)")
    mime = declared_mime or mimetypes.guess_type(filename)[0] or fallback_mime
    if (
        limits.max_filename_length is not None
        and len(filename) > limits.max_filename_length
    ):
        raise McpToolError("drop filename is too long")
    if (
        limits.max_filename_bytes is not None
        and len(filename.encode("utf-8")) > limits.max_filename_bytes
    ):
        raise McpToolError("drop filename is too large")
    if (
        limits.max_mime_length is not None
        and len(mime) > limits.max_mime_length
    ):
        raise McpToolError("drop MIME type is too long")
    return data, filename, mime


def _mcp_password() -> str:
    password = os.environ.get("PASTEBERTH_PASSWORD")
    if password is None:
        raise McpToolError(
            "authentication required; set PASTEBERTH_PASSWORD for MCP stdio"
        )
    return password


def _mcp_token() -> str | None:
    value = os.environ.get("PASTEBERTH_TOKEN")
    return value.strip() if value and value.strip() else None


def _cmd_mcp(args: argparse.Namespace) -> int:
    config_path = find_config_path(_config_arg(args))
    try:
        cfg = build_default_config() if config_path is None else load_config(config_path)
        client = PasteberthClient(
            _drop_server_url(cfg, args.server_url),
            timeout=cfg.limits.http_request_timeout_seconds or 60.0,
            insecure=args.insecure,
            bearer_token=_mcp_token(),
        )
    except (ConfigError, ClientError) as exc:
        print(f"pasteberth: configuration error\n  {exc}", file=sys.stderr)
        return 2

    cookie = None

    def drop(arguments: dict) -> dict:
        nonlocal cookie
        unknown = set(arguments) - {"zone", "items", "replace"}
        if unknown:
            raise McpToolError(f"unknown drop field: {sorted(unknown)[0]!r}")
        zone_id = arguments.get("zone")
        if not isinstance(zone_id, str) or not _DROP_ZONE_RE.fullmatch(zone_id):
            raise McpToolError("drop zone must be a valid zone ID")
        items = arguments.get("items")
        if not isinstance(items, list) or not items:
            raise McpToolError("drop items must be a non-empty array")
        if len(items) > 128:
            raise McpToolError("drop accepts at most 128 items per call")
        replace = arguments.get("replace", False)
        if not isinstance(replace, bool):
            raise McpToolError("drop replace must be a boolean")

        uploaded = []
        errors = []
        for index, item in enumerate(items):
            try:
                data, filename, declared_mime = _mcp_item(
                    item, cfg.max_upload_bytes, cfg.limits
                )
                response = client.upload(
                    zone_id,
                    data,
                    filename,
                    declared_mime,
                    replace=replace,
                    cookie=cookie,
                )
                if response.status == 401:
                    if client.bearer_token:
                        raise api_error(response)
                    cookie = client.login(_mcp_password())
                    response = client.upload(
                        zone_id,
                        data,
                        filename,
                        declared_mime,
                        replace=replace,
                        cookie=cookie,
                    )
                if response.status != 201:
                    raise api_error(response)
                payload = response.json()
                if not isinstance(payload, dict) or not (
                    isinstance(payload.get("reference"), str)
                    or payload.get("accepted") is True
                ):
                    raise ClientError("server returned an invalid upload response")
                uploaded.append(payload)
            except (ClientError, McpToolError, OSError, ValueError) as exc:
                errors.append({"index": index, "message": str(exc)})
        return {"zone": zone_id, "items": uploaded, "errors": errors}

    return run_stdio(drop)


def _generated_config_text(storage_path: Path) -> str:
    storage = storage_path.as_posix()
    return f'''# Pasteberth local configuration.
# Keep this file, its password, and any TLS private key outside the read-only
# PasteBerth deployment bundle.

listen_address = "127.0.0.1"
port = 8765
max_upload_size = "20MiB"           # set to "unlimited" for no application cap
max_image_pixels = 25000000        # structural pixel budget; "unlimited" disables it
# Public path prefix: empty for root, or e.g. "/paste" behind a reverse proxy.
url_prefix = ""
# Display absolute file paths in the web UI. Set false when paths are sensitive.
show_full_path = true
# Trust no forwarded headers by default; list only the actual reverse proxy IP.
trusted_proxies = []
# With authentication enabled, an empty list accepts any public hostname.
# Anonymous configurations must list their controlled hostnames explicitly.
allowed_hosts = []
allow_unauthenticated_local = false
allow_unauthenticated_remote = false
# Loopback without auth also applies when a reverse proxy exposes this backend.
# Keep this listener on loopback when TLS terminates in a reverse proxy.
# A non-loopback listener requires direct TLS or the explicit private-network opt-in.
allow_insecure_http_remote = false
# Accepted content kinds (all true by default).
accept_bin = true
accept_img = true
accept_doc = true
log_level = "INFO"

# Most values below accept "unlimited"; request_queue_size is always positive
# because it configures the OS listen backlog. These are operational budgets,
# not additional upload ceilings.
[limits]
max_image_dimension = 16384
max_image_raw_size = "256MiB"
max_filename_length = 200
max_filename_size = 240
max_png_chunks = 100000
max_jpeg_segments = 100000
max_webp_chunks = 100000
max_mime_length = 120
max_multipart_boundary_length = 70
max_multipart_parts = 32
max_multipart_header_size = "8KiB"
max_multipart_field_name_length = 256
max_batch_names = 10000
max_batch_body_size = "2MiB"
max_comment_body_size = "8KiB"
max_http_header_size = "64KiB"
max_login_body_size = "4KiB"
max_login_fields = 8
max_login_delay_seconds = 900
max_login_concurrent_checks = 4
max_login_tracked_ips = 4096
login_forget_after_seconds = 3600
max_scrypt_memory_size = "64MiB"
max_password_file_size = "16KiB"
max_metadata_size = "64KiB"
max_comment_length = 280
max_comment_bytes = 1024
request_queue_size = 32
max_active_requests = 64
max_pending_requests = 8
http_header_timeout_seconds = 5
http_request_timeout_seconds = 60

[tls]
enabled = false
# certificate = "/absolute/path/to/cert.pem"
# private_key = "/absolute/path/to/key.pem"

[auth]
enabled = true
session_ttl_hours = 72
max_sessions = 4096  # use "unlimited" to disable FIFO eviction
# password_file = "/absolute/path/to/passwd"
# token_file = "/absolute/path/to/tokens.sqlite3"

[[zones]]
id = "default"
label = "Default"
type = "local"
directory = "{storage}"
retain = 10
reference_prefix = "@"
reference_suffix = ""
reference_list_prefix = ""
reference_list_suffix = ""
reference_separator = ","
allow_zip_download = true
color = "#304237"
create_directory = true
min_free_percent = 2.0

# Dynamic sidecar zones are optional. Discovery never creates directories.
# A collection ID starts with '@' and can be selected by a group pattern.
# [[zone_collection]]
# id = "@repositories"
# base_directory = "/absolute/path/to/parent"
# pattern = "^[^/]+/work/exchange$"
# max_depth = 4
# label_mode = "git-or-relative"  # also: "relative" or "first-directory"
# retain = 10
# file_group = "pasteberth"
# min_free_percent = 2.0

# Optional group views. Omit the whole section to show all zones without tabs.
# Patterns use Python regular expressions (re.search, case-sensitive).
# [[groups]]
# name = "LWP"
# selection = "pattern"
# pattern = ["^lightwebpres.*$"]
# layout = "tab"
#
# [[groups]]
# name = "Repositories"
# selection = "pattern"
# pattern = ["^@repositories$"]
# layout = "area"
#
# [[groups]]
# name = "Other"
# selection = "other"
# layout = "area"
'''


def _cmd_generate_config(args: argparse.Namespace) -> int:
    target = config_path_for_generation(_config_arg(args))
    target = target.expanduser()
    if not target.is_absolute():
        target = Path.cwd() / target
    if "\x00" in str(target):
        print(f"invalid configuration path: {target}", file=sys.stderr)
        return 2
    try:
        ensure_external_path(target, "configuration")
    except ConfigError as exc:
        print(f"invalid configuration path: {exc}", file=sys.stderr)
        return 2
    try:
        storage_path = ensure_external_path(default_storage_path(), "default storage")
    except ConfigError as exc:
        print(f"invalid storage path: {exc}", file=sys.stderr)
        return 2
    if target.exists() and not args.force:
        print(
            f"configuration already exists: {target}\n"
            "use --force only after checking the target",
            file=sys.stderr,
        )
        return 2
    try:
        fs = platform_fs()
        with fs.open_directory(target.parent, create=True, mode=0o700) as parent:
            temporary_name = f".{target.name}.{secrets.token_hex(12)}.tmp"
            temporary = fs.create_exclusive(
                parent,
                temporary_name,
                mode="w",
                permissions=0o600,
            )
            temporary_identity = temporary.identity
            try:
                with temporary as stream:
                    stream.write(_generated_config_text(storage_path))
                    stream.sync()
                fs.replace(
                    parent,
                    temporary_name,
                    target.name,
                    expected_source=temporary_identity,
                )
                fs.flush_directory(parent)
            except BaseException:
                if not temporary.closed:
                    temporary.close()
                try:
                    fs.remove_expected(parent, temporary_name, temporary_identity)
                except OSError:
                    pass
                raise
    except (OSError, ValueError, UnsupportedFilesystemError) as exc:
        print(f"cannot write {target}: {exc}", file=sys.stderr)
        return 1
    print(f"configuration generated: {target}")
    print("next step: pasteberth passwd")
    return 0


def _cmd_completion(args: argparse.Namespace) -> int:
    if args.shell != "bash":
        print(f"pasteberth: unsupported completion shell: {args.shell}", file=sys.stderr)
        return 2
    completion = (
        Path(__file__).resolve().parents[1]
        / "support"
        / "completions"
        / "pasteberth.bash"
    )
    try:
        sys.stdout.write(completion.read_text(encoding="utf-8"))
    except OSError as exc:
        print(f"pasteberth: cannot read completion script: {exc}", file=sys.stderr)
        return 1
    return 0


def _audit_zone(cfg, zone) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    configured_path = zone.directory
    path = configured_path.resolve()
    fs = platform_fs()
    if zone.file_group is not None:
        try:
            fs.validate_group(zone.file_group)
        except (OSError, UnsupportedFilesystemError) as exc:
            errors.append(
                f"zone {zone.id}: file_group {zone.file_group!r} is unusable ({exc})"
            )
    if path != configured_path:
        warnings.append(f"zone {zone.id}: symbolic link accepted: {configured_path}")
    if not path.exists():
        if zone.create_directory:
            warnings.append(f"zone {zone.id}: will be created at startup: {path}")
        else:
            errors.append(f"zone {zone.id}: directory is missing: {path}")
        return errors, warnings
    try:
        directory = fs.open_directory(path)
    except FileNotFoundError:
        errors.append(f"zone {zone.id}: directory is missing: {path}")
        return errors, warnings
    except NotADirectoryError:
        errors.append(f"zone {zone.id}: not a directory: {path}")
        return errors, warnings
    except (OSError, ValueError, UnsupportedFilesystemError) as exc:
        errors.append(f"zone {zone.id}: inspection failed ({exc})")
        return errors, warnings
    try:
        with directory:
            audit = fs.audit_permissions(path, directory=True)
            mode = audit.mode
        # This is a warning, not an error: shared zones are legitimate for
        # controlled multi-user sharing. Rejecting them would bypass protection.
        if (mode is not None and mode & 0o077) or (mode is None and not audit.private):
            permission_detail = oct(mode) if mode is not None else (audit.detail or "ACL")
            warnings.append(
                f"zone {zone.id}: permissions are not private ({permission_detail}): {path} "
                "(0700 recommended)"
            )
    except (OSError, UnsupportedFilesystemError) as exc:
        errors.append(f"zone {zone.id}: inspection failed ({exc})")
    if not fs.check_access(path, read=True, write=True, execute=True):
        errors.append(f"zone {zone.id}: directory is not readable and writable: {path}")
    lock_path = path / ".pasteberth.lock"
    try:
        with fs.open_directory(path) as directory:
            lock_info = fs.entry_info(directory, ".pasteberth.lock")
    except FileNotFoundError:
        lock_info = None
    except (OSError, UnsupportedFilesystemError) as exc:
        errors.append(f"zone {zone.id}: lock inspection failed ({exc})")
        lock_info = None
    if lock_info is not None:
        if lock_info.is_symlink:
            errors.append(f"zone {zone.id}: lock is a symbolic link: {lock_path}")
        elif not lock_info.is_regular:
            errors.append(f"zone {zone.id}: lock is not a regular file: {lock_path}")
        elif not fs.check_access(lock_path, read=True, write=True):
            errors.append(f"zone {zone.id}: lock is not readable/writable: {lock_path}")
    try:
        with fs.open_directory(path) as directory:
            usage = fs.volume_space(directory)
        free_percent = (
            usage.available_bytes * 100.0 / usage.total_bytes
            if usage.total_bytes
            else 0.0
        )
        if free_percent < zone.min_free_percent:
            errors.append(
                f"zone {zone.id}: free space {free_percent:.2f}% "
                f"< minimum {zone.min_free_percent:.2f}%"
            )
    except (OSError, UnsupportedFilesystemError) as exc:
        warnings.append(f"zone {zone.id}: free space cannot be measured ({exc})")
    return errors, warnings


def _audit_controlled_parents(fs, path: Path) -> str | None:
    """Reject parents that let a third party replace a target."""
    current = path
    while True:
        try:
            audit = fs.audit_permissions(current, directory=True)
        except (OSError, UnsupportedFilesystemError) as exc:
            return f"inaccessible parent ({current}: {exc})"
        if audit.directory_is_writable_by_other():
            return f"parent writable by a third party: {current}"
        if current == Path(current.anchor):
            return None
        current = current.parent


def _audit_regular_file(
    path: Path,
    label: str,
    *,
    require_owner: bool = False,
    require_private: bool = False,
    allow_symlink: bool = False,
    symlink_warnings: list[str] | None = None,
) -> tuple[list[str], Path | None]:
    """Audit a file and return the resolved target usable by OpenSSL."""
    fs = platform_fs()
    path = Path(path).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    errors: list[str] = []
    try:
        target = path.resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        return [f"{label}: inspection failed ({exc})"], None
    if target != path and symlink_warnings is not None:
        symlink_warnings.append(
            f"{label}: symbolic link accepted after target checks: {path}"
        )

    for parent in dict.fromkeys((path.parent, target.parent)):
        parent_error = _audit_controlled_parents(fs, parent)
        if parent_error:
            errors.append(f"{label} : {parent_error}")
    try:
        with fs.open_directory(target.parent) as parent:
            info = fs.entry_info(parent, target.name)
    except FileNotFoundError:
        return errors + [f"{label} missing: {path}"], None
    except (OSError, ValueError, UnsupportedFilesystemError) as exc:
        return errors + [f"{label}: inspection failed ({exc})"], None
    if info is None:
        return errors + [f"{label} missing: {path}"], None
    if info.is_symlink or not info.is_regular:
        return errors + [f"{label}: regular file required: {path}"], None
    mode = info.mode
    if mode is not None and mode & (stat.S_IWGRP | stat.S_IWOTH):
        errors.append(f"{label}: writable by a third party: {path}")
    if require_owner and not fs.is_owned(info):
        errors.append(f"{label}: file is not owned by the process: {path}")
    if require_private:
        try:
            audit = fs.audit_permissions(target, directory=False)
        except (OSError, UnsupportedFilesystemError) as exc:
            errors.append(f"{label}: permissions unreadable ({exc})")
        else:
            if not audit.private:
                detail = oct(audit.mode) if audit.mode is not None else (audit.detail or "ACL")
                errors.append(f"{label}: permissions too open ({detail}): {path}")
    if not fs.check_access(target, read=True):
        errors.append(f"{label}: file is not readable: {path}")
    return errors, target


def _audit_listener(cfg) -> tuple[str | None, str | None]:
    if cfg.port < 1024 and getattr(os, "geteuid", lambda: 1)() != 0:
        return f"privileged port requires root: {cfg.port}", None
    try:
        addresses = socket.getaddrinfo(
            cfg.listen_address,
            cfg.port,
            type=socket.SOCK_STREAM,
        )
    except OSError as exc:
        return f"invalid listener address: {exc}", None
    if not addresses:
        return f"listener address not found: {cfg.listen_address}", None

    family, socktype, protocol, _, sockaddr = addresses[0]
    try:
        with socket.socket(family, socktype, protocol) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(sockaddr)
    except OSError as exc:
        message = f"cannot bind {cfg.listen_address}:{cfg.port} ({exc})"
        if exc.errno in (errno.EADDRINUSE, errno.EACCES) or getattr(exc, "winerror", None) == 10013:
            return None, (
                f"port already in use on {cfg.listen_address}:{cfg.port}: "
                "another instance may already be running"
            )
        return message, None
    return None, None


def _audit_tls(cfg) -> list[str]:
    if not cfg.tls.enabled:
        return []
    from .server import create_tls_context

    errors: list[str] = []
    certificate_errors, certificate = _audit_regular_file(
        cfg.tls.certificate,
        "TLS certificate",
        allow_symlink=True,
    )
    key_errors, private_key = _audit_regular_file(
        cfg.tls.private_key,
        "TLS private key",
        require_owner=True,
        require_private=True,
    )
    errors.extend(certificate_errors)
    errors.extend(key_errors)
    if certificate is not None:
        try:
            decoded = ssl._ssl._test_decode_cert(str(certificate))
            not_before = ssl.cert_time_to_seconds(decoded["notBefore"])
            not_after = ssl.cert_time_to_seconds(decoded["notAfter"])
        except (KeyError, OSError, ssl.SSLError, ValueError) as exc:
            errors.append(f"TLS certificate: unreadable dates ({exc})")
        else:
            now = time.time()
            if now < not_before:
                errors.append("TLS certificate: certificate is not valid yet")
            if now >= not_after:
                errors.append("TLS certificate: certificate has expired")
    if certificate is not None and private_key is not None:
        try:
            create_tls_context(certificate, private_key)
        except (OSError, ssl.SSLError, ValueError) as exc:
            errors.append(f"invalid TLS configuration: {exc}")
    elif not any(error.startswith("invalid TLS configuration") for error in errors):
        errors.append("invalid TLS configuration: certificate or key unavailable")
    return errors


def _network_warning(cfg) -> str | None:
    if is_loopback_address(cfg.listen_address):
        return None
    if cfg.tls.enabled:
        if not cfg.auth.enabled:
            return "direct TLS network listener detected with authentication disabled"
        return None
    return (
        "unencrypted network HTTP listener despite explicit opt-in; "
        "prefer [tls] or an HTTPS reverse proxy"
    )


def _audit_groups(cfg, candidates=None, zones=None) -> list[str]:
    warnings: list[str] = []
    if candidates is None:
        candidates, diagnostics = discover_zone_collections(
            cfg.zone_collections, cfg.zones
        )
    else:
        diagnostics = ()
    if zones is None:
        zones = dict(cfg.zones)
        for candidate in candidates:
            zones.setdefault(candidate.zone.id, candidate.zone)
    warnings.extend(diagnostics)
    groups = cfg.groups

    all_groups = [group for group in groups if group.selection == "all"]
    other_groups = [group for group in groups if group.selection == "other"]
    if len(all_groups) > 1:
        warnings.append(
            "redundant selection='all' groups: "
            + ", ".join(group.name for group in all_groups)
        )
    if len(other_groups) > 1:
        warnings.append(
            "redundant selection='other' groups: "
            + ", ".join(group.name for group in other_groups)
        )
    if all_groups and other_groups:
        warnings.append(
            "selection='all' and selection='other' coexist; "
            "groups may overlap"
        )
    for group in groups:
        if group.selection in {"all", "other"} and group.pattern_defined:
            warnings.append(
                f"group {group.name}: 'pattern' ignored with "
                f"selection='{group.selection}'"
            )

    collection_members = resolve_collection_members(candidates, zones)
    memberships = resolve_group_zone_ids(groups, zones, collection_members)
    seen_pattern_memberships: dict[tuple[str, ...], str] = {}
    seen_effective_memberships: dict[tuple[str, ...], dict[str, str]] = {}
    for group in groups:
        zone_ids = memberships[group.name]
        if group.selection == "pattern":
            previous = seen_pattern_memberships.get(zone_ids)
            if previous is not None:
                warnings.append(
                    f"redundant pattern groups: {previous} and {group.name} "
                    f"select the same zones"
                )
            else:
                seen_pattern_memberships[zone_ids] = group.name

        groups_by_selection = seen_effective_memberships.setdefault(zone_ids, {})
        if group.selection not in groups_by_selection:
            for previous_selection, previous_name in groups_by_selection.items():
                if {previous_selection, group.selection} == {"all", "other"}:
                    continue
                warnings.append(
                    f"redundant groups: {previous_name} ({previous_selection}) and "
                    f"{group.name} ({group.selection}) select the same zones"
                )
            groups_by_selection[group.selection] = group.name
    return warnings


def _cmd_audit(args: argparse.Namespace) -> int:
    config_path = find_config_path(_config_arg(args))
    infos: list[str] = []
    errors: list[str] = []
    warnings: list[str] = []
    if config_path is None:
        try:
            cfg = build_default_config()
        except ConfigError as exc:
            print(f"ERROR configuration: {exc}", file=sys.stderr)
            return 2
        warnings.append(
            f"no config.toml found: default storage {default_storage_path()}"
        )
    else:
        try:
            cfg = load_config(config_path)
        except ConfigError as exc:
            print(f"ERROR configuration: {exc}", file=sys.stderr)
            return 2
    warnings.extend(cfg.warnings)

    if config_path is not None:
        config_errors, _ = _audit_regular_file(
            cfg.config_path,
            "configuration",
            require_owner=True,
            allow_symlink=True,
            symlink_warnings=warnings,
        )
        errors.extend(config_errors)

    fs = platform_fs()
    if sys.version_info < (3, 11):
        errors.append("Python 3.11 or newer is required")
    uses_default_storage = any(
        zone.directory == default_storage_path() for zone in cfg.zones.values()
    )
    if cfg.using_default_config:
        warnings.append("authentication disabled in default loopback mode")
    elif not cfg.auth.enabled:
        warnings.append("authentication disabled in configuration")
    elif cfg.auth.enabled:
        try:
            stored_hash = load_password_hash(
                cfg.password_file(),
                max_bytes=cfg.limits.max_password_file_bytes,
            )
        except RuntimeError as exc:
            errors.append(str(exc))
        else:
            if not valid_password_hash(stored_hash):
                errors.append(f"missing or invalid scrypt hash: {cfg.password_file()}")
        token_file = cfg.token_file()
        try:
            token_is_symlink = token_file.is_symlink()
        except OSError as exc:
            errors.append(f"token registry: inspection failed ({exc})")
            token_is_symlink = False
        if token_is_symlink:
            errors.append(f"token registry: symbolic link is not allowed: {token_file}")
        elif token_file.exists():
            token_errors, _ = _audit_regular_file(
                token_file,
                "token registry",
                require_owner=True,
                require_private=True,
            )
            errors.extend(token_errors)
        if token_file.parent.exists():
            if not token_file.parent.is_dir():
                errors.append(
                    f"token registry directory: regular directory required: "
                    f"{token_file.parent}"
                )
            else:
                try:
                    parent_audit = fs.audit_permissions(token_file.parent, directory=True)
                except (OSError, UnsupportedFilesystemError) as exc:
                    errors.append(f"token registry directory: permissions unreadable ({exc})")
                else:
                    if parent_audit.directory_is_writable_by_other():
                        detail = (
                            oct(parent_audit.mode)
                            if parent_audit.mode is not None
                            else (parent_audit.detail or "ACL")
                        )
                        errors.append(
                            f"token registry directory: permissions too open ({detail}): "
                            f"{token_file.parent}"
                        )
    if uses_default_storage and config_path is not None:
        warnings.append(
            f"default storage in use: {default_storage_path()}"
        )
    if not cfg.allowed_hosts:
        warnings.append(
            "allowed_hosts is empty: Host checking is disabled (wildcard); "
            "list exposed hostnames to enable it"
        )
    if cfg.show_full_path:
        warnings.append(
            "show_full_path = true exposes absolute paths in the Web UI; "
            "set it to false when those paths are sensitive"
        )
    broad_proxies = [str(network) for network in cfg.trusted_proxies if network.prefixlen == 0]
    if broad_proxies:
        errors.append(
            "trusted_proxies is too broad: "
            + ", ".join(broad_proxies)
            + " (global proxy is not allowed by the audit)"
        )
    if not (cfg.accept_bin or cfg.accept_img or cfg.accept_doc):
        warnings.append(
            "accept_bin, accept_img, and accept_doc are all false: "
            "the server will reject all content"
        )
    for index, rule in enumerate(cfg.zone_collections, start=1):
        if rule.file_group is None:
            continue
        try:
            fs.validate_group(rule.file_group)
        except (OSError, UnsupportedFilesystemError) as exc:
            errors.append(
                f"zone collection #{index}: file_group {rule.file_group!r} is unusable ({exc})"
            )
    discovery_started = time.perf_counter()
    collection_candidates, collection_diagnostics = discover_zone_collections(
        cfg.zone_collections, cfg.zones
    )
    discovery_duration = time.perf_counter() - discovery_started
    infos.append(
        f"zone collection discovery: {discovery_duration:.3f}s "
        f"({len(cfg.zone_collections)} rule(s), "
        f"{len(collection_candidates)} candidate(s))"
    )
    warnings.extend(collection_diagnostics)
    per_rule_counts = [0] * len(cfg.zone_collections)
    for candidate in collection_candidates:
        for rule_index in candidate.rule_indexes:
            if 0 <= rule_index < len(per_rule_counts):
                per_rule_counts[rule_index] += 1
    for index, count in enumerate(per_rule_counts):
        warnings.append(
            f"zone collection #{index + 1}: {count} candidate(s) currently discovered"
        )
    all_zones = dict(cfg.zones)
    for candidate in collection_candidates:
        all_zones.setdefault(candidate.zone.id, candidate.zone)
    warnings.extend(_audit_groups(cfg, collection_candidates, all_zones))

    for zone in cfg.zones.values():
        zone_errors, zone_warnings = _audit_zone(cfg, zone)
        errors.extend(zone_errors)
        warnings.extend(zone_warnings)
    for candidate in collection_candidates:
        zone_errors, zone_warnings = _audit_zone(cfg, candidate.zone)
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
    errors.extend(_audit_tls(cfg))

    for message in infos:
        print(f"INFO: {message}")
    for message in warnings:
        print(f"WARNING: {message}")
    for message in errors:
        print(f"ERROR: {message}")
    if errors:
        print(f"Audit failed: {len(errors)} error(s), {len(warnings)} warning(s)")
        return 2
    if warnings:
        print(f"Audit ready with {len(warnings)} warning(s)")
        return 1
    print("Audit passed: configuration is ready")
    return 0


def _build_handler(
    cfg,
    service: PasteService,
    sessions: SessionStore,
    limiter: LoginRateLimiter,
    tokens: TokenStore | None = None,
):
    from .webapp import make_handler

    return make_handler(cfg, service, sessions, limiter, tokens)


def _cmd_passwd(args: argparse.Namespace) -> int:
    config_path = find_config_path(_config_arg(args))
    if config_path is None:
        print(
            "configuration missing: run `pasteberth --generate-config` first",
            file=sys.stderr,
        )
        return 2
    if "\x00" in str(config_path):
        print(f"invalid configuration path: {config_path}", file=sys.stderr)
        return 2
    if not config_path.is_file():
        print(f"configuration not found: {config_path}", file=sys.stderr)
        return 2
    try:
        cfg = load_config(config_path)
    except ConfigError as exc:
        print(
            f"pasteberth: invalid configuration ({config_path})\n  {exc}\n"
            "no hash was written",
            file=sys.stderr,
        )
        return 2
    password_file = cfg.password_file()

    pw1 = getpass.getpass("New password: ")
    if len(pw1) < 8:
        print("rejected: at least 8 characters are required", file=sys.stderr)
        return 1
    pw2 = getpass.getpass("Confirmation: ")
    if pw1 != pw2:
        print("rejected: the two entries differ", file=sys.stderr)
        return 1
    try:
            save_password_hash(
                password_file,
                hash_password(pw1, maxmem=cfg.limits.max_scrypt_memory_bytes),
            )
    except OSError as exc:
        print(f"error: cannot write {password_file}: {exc}", file=sys.stderr)
        return 1
    try:
        mode = oct(password_file.stat().st_mode & 0o777)
    except OSError:
        mode = "?"
    print(f"scrypt hash written to {password_file} (mode {mode})")
    print(
        "remember to enable [auth] enabled = true in "
        f"{config_path if config_path.exists() else 'your config.toml'}"
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pasteberth",
        description="Bridge between a browser clipboard and the harness filesystem.",
    )
    parser.add_argument("--version", action="version", version=f"pasteberth {__version__}")
    parser.add_argument(
        "--config",
        dest="global_config",
        help="global configuration file path (before the command)",
    )
    parser.add_argument(
        "--generate-config",
        action="store_true",
        help="generate a configuration without starting the server",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="replace an existing target configuration",
    )
    sub = parser.add_subparsers(dest="command", required=False)

    p_serve = sub.add_parser("serve", help="start the server")
    p_serve.add_argument("--config", default=argparse.SUPPRESS, help="path to config.toml")
    p_serve.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="override log_level for this invocation",
    )
    p_serve.set_defaults(func=_cmd_serve)

    p_drop = sub.add_parser(
        "drop",
        help="drop files directly when possible, otherwise through the server",
        description=(
            "Publish one or more regular source files into a zone managed by the daemon.\n\n"
            "With --zone ID, positional arguments are source files only.\n"
            "Without --zone, the first positional argument is a target directory\n"
            "that the daemon resolves against its static zones and zone collections; the\n"
            "remaining arguments are source files. A single positional argument is\n"
            "invalid; use register FILE for an existing local file.\n\n"
            "For a loopback server with a writable target, Pasteberth stages\n"
            "the source locally and asks the daemon to regularize it. Remote or\n"
            "unavailable local staging falls back to the HTTP API.\n\n"
            "The daemon URL comes from the configuration, or defaults to\n"
            "http://127.0.0.1:8765 when no configuration is available.\n"
            "For a trusted self-signed HTTPS certificate, add --insecure; this\n"
            "disables certificate verification only.\n\n"
            "Examples:\n"
            "  pasteberth drop --config config.toml --zone project-alpha report.pdf\n"
            "  pasteberth drop --config config.toml "
            "/srv/pasteberth/project-alpha report.pdf\n"
            "  pasteberth drop --config config.toml --server \\\n"
            "      https://pasteberth.example.internal --zone project-alpha report.pdf\n"
            "  pasteberth register --config config.toml "
            "/srv/pasteberth/project-alpha/report.pdf"
        ),
        epilog=(
            "Sources are never modified. Use register for an existing regular file;\n"
            "it creates or refreshes only the sidecar and relies on the directory's\n"
            "filesystem permissions. Use --replace only for an upload to a managed\n"
            "filename. Use\n"
            "--password-stdin for non-interactive password authentication, or\n"
            "--token-stdin for a bearer credential. PASTEBERTH_TOKEN is also accepted."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_drop.add_argument(
        "directory",
        nargs="?",
        metavar="ZONE_DIRECTORY_OR_FIRST_SOURCE",
        help="zone directory, or the first source file when --zone is used",
    )
    p_drop.add_argument(
        "files",
        nargs="*",
        metavar="SOURCE_FILE",
        help="additional source files",
    )
    p_drop.add_argument(
        "--server",
        dest="server_url",
        help="Pasteberth server URL (derived from config or localhost default)",
    )
    p_drop.add_argument(
        "--zone",
        dest="zone_id",
        help="zone ID; positional arguments are source files and no target directory is required",
    )
    p_drop.add_argument("--replace", action="store_true",
                        help="allow explicit replacement of a managed name")
    p_drop.add_argument(
        "--password-stdin",
        action="store_true",
        help="read the password from stdin for an HTTP authentication challenge",
    )
    p_drop.add_argument(
        "--token-stdin",
        action="store_true",
        help="read a bearer token from stdin instead of using PASTEBERTH_TOKEN",
    )
    p_drop.add_argument(
        "--insecure",
        action="store_true",
        help="disable TLS certificate verification for a trusted self-signed server",
    )
    p_drop.add_argument("--config", default=argparse.SUPPRESS, help="path to config.toml")
    p_drop.set_defaults(func=_cmd_drop)

    p_register = sub.add_parser(
        "register",
        help="create or refresh a sidecar for an existing file",
        description=(
            "Create or refresh only the Pasteberth sidecar for an existing regular file.\n"
            "This is a filesystem-only operation: it reads the file and writes\n"
            "the sidecar in the same directory without contacting the daemon."
        ),
        epilog=(
            "Registration never changes the file data. An existing sidecar is\n"
            "refreshed from the current file, while a valid comment is retained.\n"
            "The daemon must be able to read the resulting sidecar."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_register.add_argument("file", metavar="FILE", help="existing file to register")
    p_register.add_argument("--config", default=argparse.SUPPRESS, help="path to config.toml")
    p_register.set_defaults(func=_cmd_register)

    p_mcp = sub.add_parser(
        "mcp",
        help="serve the optional MCP stdio adapter",
        description=(
            "Serve Pasteberth MCP tools over newline-delimited JSON-RPC on stdin/stdout.\n\n"
            "The stdio stream is reserved for MCP messages; authentication uses\n"
            "PASTEBERTH_TOKEN for a bearer token, or PASTEBERTH_PASSWORD for\n"
            "a session when the HTTP server requires authentication.\n"
            "The initial tool is drop, which accepts local paths or in-memory content."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_mcp.add_argument(
        "--server",
        dest="server_url",
        help="Pasteberth server URL (derived from config when omitted)",
    )
    p_mcp.add_argument(
        "--insecure",
        action="store_true",
        help="disable TLS certificate verification for a trusted self-signed server",
    )
    p_mcp.add_argument("--config", default=argparse.SUPPRESS, help="path to config.toml")
    p_mcp.set_defaults(func=_cmd_mcp)

    for transfer_mode, transfer_handler in (("copy", _cmd_copy), ("move", _cmd_move)):
        p_transfer = sub.add_parser(
            transfer_mode,
            help=f"{transfer_mode} managed files between configured zones",
        )
        p_transfer.add_argument(
            "source_directory",
            help="exact directory of the configured source zone",
        )
        p_transfer.add_argument(
            "target_directory",
            help="exact directory of the configured target zone",
        )
        p_transfer.add_argument(
            "files",
            nargs="+",
            help="managed names to transfer, without paths",
        )
        p_transfer.add_argument(
            "--config",
            default=argparse.SUPPRESS,
            help="path to config.toml",
        )
        p_transfer.set_defaults(func=transfer_handler)

    p_rename = sub.add_parser(
        "rename",
        help="rename a managed file and its sidecar",
    )
    p_rename.add_argument("directory", help="exact directory of the configured zone")
    p_rename.add_argument("source", help="current managed name, without a path")
    p_rename.add_argument("target", help="new name, without a path")
    p_rename.add_argument("--config", default=argparse.SUPPRESS, help="path to config.toml")
    p_rename.set_defaults(func=_cmd_rename)

    p_delete = sub.add_parser(
        "delete",
        help="delete one or more managed files and their sidecars",
    )
    p_delete.add_argument("directory", help="exact directory of the configured zone")
    p_delete.add_argument("files", nargs="+", help="managed names to delete, without paths")
    p_delete.add_argument(
        "--force",
        action="store_true",
        help="also delete a pair whose size no longer matches its sidecar",
    )
    p_delete.add_argument("--config", default=argparse.SUPPRESS, help="path to config.toml")
    p_delete.set_defaults(func=_cmd_delete)

    p_pw = sub.add_parser("passwd", help="set or change the password")
    p_pw.add_argument(
        "--config", default=argparse.SUPPRESS, help="path to config.toml (the hash follows [auth])"
    )
    p_pw.set_defaults(func=_cmd_passwd)

    p_audit = sub.add_parser("audit", help="check the environment without modifying it")
    p_audit.add_argument("--config", default=argparse.SUPPRESS, help="path to config.toml")
    p_audit.set_defaults(func=_cmd_audit)

    p_completion = sub.add_parser(
        "completion",
        help="print shell completion code",
    )
    p_completion.add_argument(
        "--shell",
        choices=["bash"],
        default="bash",
        help="completion shell (default: bash)",
    )
    p_completion.set_defaults(func=_cmd_completion)
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
