"""Command-line interface.

    pasteberth                         # start with local default values
    pasteberth serve   [--config PATH] [--log-level LEVEL]
    pasteberth drop [--config PATH] [--replace] DIRECTORY FILE...
    pasteberth rename [--config PATH] DIRECTORY SOURCE TARGET
    pasteberth delete [--config PATH] [--force] DIRECTORY FILE...
    pasteberth passwd  [--config PATH]
    pasteberth audit   [--config PATH]
    pasteberth [--config PATH] --generate-config [--force]
"""
from __future__ import annotations

import argparse
import errno
import getpass
import logging
import mimetypes
import os
import secrets
import ssl
import stat
import sys
import time
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
from pasteberth.platformfs import UnsafeLinkError, UnsupportedFilesystemError, platform_fs
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
        print(f"pasteberth: configuration error\n  {exc}", file=sys.stderr)
        return 2
    if cfg.auth.enabled:
        try:
            stored_hash = load_password_hash(cfg.password_file())
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
        from pasteberth.server import create_tls_context

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
            "using default storage %s; edit config.toml to move it",
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
    sessions = SessionStore(
        cfg.auth.session_ttl_hours * 3600,
        password_file=cfg.password_file() if cfg.auth.enabled else None,
        max_sessions=cfg.auth.max_sessions,
    )
    limiter = LoginRateLimiter()
    handler = _build_handler(cfg, service, sessions, limiter)

    from pasteberth.server import serve_forever

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
        len(cfg.zones),
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
        )
    except OSError as exc:
        log.error("cannot listen on %s:%d: %s", cfg.listen_address, cfg.port, exc)
        return 1
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
    return Path(os.path.abspath(os.path.expanduser(raw)))


def _zone_for_directory(cfg, raw_directory: str):
    try:
        directory = _command_path(raw_directory)
        symlink = platform_fs().first_symlink_component(directory)
    except (OSError, ValueError) as exc:
        raise ConfigError(f"invalid zone directory: {raw_directory!r} ({exc})") from exc
    if symlink is not None:
        raise ConfigError(f"target directory contains a symbolic link: {symlink}")
    normalized = Path(os.path.normpath(str(directory)))
    for zone in cfg.zones.values():
        if normalized == Path(os.path.normpath(str(zone.directory))):
            return zone
    raise ConfigError(
        f"target directory does not match any configured zone: {directory}"
    )


def _read_drop_source(path: Path, max_bytes: int) -> bytes:
    fs = platform_fs()
    try:
        with fs.open_directory(path.parent) as parent:
            with fs.open_existing(parent, path.name, mode="rb") as stream:
                # The backend opens a regular file without following a link;
                # bounded reads keep the CLI from loading an oversized source.
                data = stream.read(max_bytes + 1)
    except UnsafeLinkError as exc:
        raise ValueError("source is not a regular file") from exc
    except (OSError, ValueError, UnsupportedFilesystemError) as exc:
        raise ValueError(f"cannot read source: {exc}") from exc
    if len(data) > max_bytes:
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
        zone = _zone_for_directory(cfg, args.directory)
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
        zone = _zone_for_directory(cfg, args.directory)
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


def _cmd_drop(args: argparse.Namespace) -> int:
    loaded = _load_command_service(args)
    if loaded is None:
        return 2
    cfg, service = loaded
    try:
        zone = _zone_for_directory(cfg, args.directory)
    except ConfigError as exc:
        print(f"pasteberth: configuration error\n  {exc}", file=sys.stderr)
        return 2

    failures = 0
    for raw_source in args.files:
        try:
            source = _command_path(raw_source)
            data = _read_drop_source(source, cfg.max_upload_bytes)
            declared_mime = mimetypes.guess_type(source.name)[0] or "application/octet-stream"
            source_directory = os.path.normcase(os.path.normpath(str(source.parent)))
            zone_directory = os.path.normcase(os.path.normpath(str(zone.directory)))
            source_is_target = source_directory == zone_directory
            item = service.upload(
                zone.id,
                data,
                declared_mime,
                source.name,
                preserve_filename=True,
                allow_replace=args.replace,
                adopt_existing=source_is_target,
            )
        except (OSError, ValueError, ServiceError) as exc:
            failures += 1
            print(f"pasteberth: {raw_source}: {exc}", file=sys.stderr)
            continue
        print(item["reference"])
    return 1 if failures else 0


def _generated_config_text(root: Path) -> str:
    storage = (root / "storage" / "default").as_posix()
    return f'''# Pasteberth local configuration.
# The default repository-root config.toml is ignored by Git. Custom config paths
# are not automatically ignored; keep this file and its password out of Git.

listen_address = "127.0.0.1"
port = 8765
max_upload_size = "20MiB"
max_image_pixels = 25000000        # structural pixel budget; maximum 50 MP
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

[tls]
enabled = false
# certificate = "/absolute/path/to/cert.pem"
# private_key = "/absolute/path/to/key.pem"

[auth]
enabled = true
session_ttl_hours = 72
max_sessions = 4096
# password_file = "/absolute/path/to/passwd"

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
    target = target.expanduser()
    if not target.is_absolute():
        target = Path.cwd() / target
    if "\x00" in str(target):
        print(f"invalid configuration path: {target}", file=sys.stderr)
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
                    stream.write(_generated_config_text(repository_root()))
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


def _audit_zone(cfg, zone) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    path = zone.directory
    fs = platform_fs()
    try:
        symlink = fs.first_symlink_component(path)
    except (OSError, ValueError, UnsupportedFilesystemError) as exc:
        errors.append(f"zone {zone.id}: inspection failed ({exc})")
        return errors, warnings
    if symlink is not None:
        errors.append(f"zone {zone.id}: symbolic link rejected: {symlink}")
        return errors, warnings
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
    if not fs.check_access(path, write=True, execute=True):
        errors.append(f"zone {zone.id}: directory is not writable: {path}")
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
        mode = audit.mode
        if mode is not None:
            world_writable_sticky = bool(mode & stat.S_IWOTH and mode & stat.S_ISVTX)
            group_writable_sticky = bool(mode & stat.S_IWGRP and mode & stat.S_ISVTX)
            if (mode & stat.S_IWGRP and not group_writable_sticky) or (
                mode & stat.S_IWOTH and not world_writable_sticky
            ):
                return f"parent writable by a third party: {current}"
        elif not audit.private:
            return f"non-private ACL on parent: {current}"
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
        symlink = fs.first_symlink_component(path)
    except (OSError, ValueError, UnsupportedFilesystemError) as exc:
        return [f"{label}: inspection failed ({exc})"], None
    target = path
    if symlink is not None:
        if not allow_symlink:
            return [f"{label}: symbolic link rejected: {symlink}"], None
        try:
            target = path.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            return [f"{label}: symbolic-link target unreadable ({exc})"], None
        if symlink_warnings is not None:
            symlink_warnings.append(
                f"{label}: symbolic link accepted after target checks: {symlink}"
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
    from pasteberth.server import create_tls_context

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


def _audit_groups(cfg) -> list[str]:
    warnings: list[str] = []
    all_groups = [group for group in cfg.groups if group.selection == "all"]
    other_groups = [group for group in cfg.groups if group.selection == "other"]
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
    for group in cfg.groups:
        if group.selection in {"all", "other"} and group.pattern_defined:
            warnings.append(
                f"group {group.name}: 'pattern' ignored with "
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
    errors: list[str] = []
    warnings: list[str] = []
    if config_path is None:
        cfg = build_default_config()
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
            stored_hash = load_password_hash(cfg.password_file())
        except RuntimeError as exc:
            errors.append(str(exc))
        else:
            if not valid_password_hash(stored_hash):
                errors.append(f"missing or invalid scrypt hash: {cfg.password_file()}")
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
    errors.extend(_audit_tls(cfg))

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


def _build_handler(cfg, service: PasteService, sessions: SessionStore,
                   limiter: LoginRateLimiter):
    from pasteberth.webapp import make_handler

    return make_handler(cfg, service, sessions, limiter)


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
        save_password_hash(password_file, hash_password(pw1))
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
        help="drop or adopt files into a zone and create their sidecars",
    )
    p_drop.add_argument("directory", help="exact directory of the configured zone")
    p_drop.add_argument("files", nargs="+", help="source files to copy")
    p_drop.add_argument("--replace", action="store_true",
                        help="allow explicit replacement of a managed name")
    p_drop.add_argument("--config", default=argparse.SUPPRESS, help="path to config.toml")
    p_drop.set_defaults(func=_cmd_drop)

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
