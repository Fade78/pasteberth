"""Storage destinations and circular per-zone retention.

The local destination assumes a directory private to the Pasteberth process.
File access goes through a directory descriptor so sidecar ownership checks
cannot become an arbitrary read or delete primitive.
"""
from __future__ import annotations

import json
import hashlib
import logging
import os  # Compatibility seam for tests that patch the process-wide os module.
import re
import secrets
import unicodedata
from abc import ABC, abstractmethod
from contextvars import ContextVar
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

from .config import LimitsConfig
from .content import ContentInfo, classify
from .images import (
    FORMATS,
    mime_for,
)
from .platformfs import (
    BusyError,
    DirectoryHandle,
    EntryChangedError,
    EntryExistsError,
    FileIdentity,
    FileHandle,
    PermissionSecurityError,
    UnsafeLinkError,
    UnsupportedFilesystemError,
    platform_fs,
)

_DEFAULT_LIMITS = LimitsConfig()

log = logging.getLogger("pasteberth.storage")

_GENERATED_FILENAME_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}_[0-9a-f]{6}\.[a-z0-9]{1,10}$"
)
_CLIENT_FILENAME_RE = re.compile(r"^[^/\\\x00\r\n]+$")
_META_KEYS = {"filename", "created_at", "width", "height", "size", "format"}
# kind/mime were added in v1.0.3; v1.0.1/v1.0.2 sidecars (6 keys) remain valid.
_META_KEYS_NEW = _META_KEYS | {"kind", "mime"}
_META_KEYS_WITH_COMMENT = _META_KEYS | {"comment"}
_META_KEYS_NEW_WITH_COMMENT = _META_KEYS_NEW | {"comment"}
_MIME_RE = re.compile(r"^[A-Za-z0-9!#$%&'*+.^_`|~-]+/[A-Za-z0-9!#$%&'*+.^_`|~-]+$")
_TEXT_MIMES = {"application/json", "application/xml", "application/x-yaml"}
def _meta_keys_ok(raw: dict) -> bool:
    return set(raw) in (
        _META_KEYS,
        _META_KEYS_NEW,
        _META_KEYS_WITH_COMMENT,
        _META_KEYS_NEW_WITH_COMMENT,
    )


def validate_comment(
    value: object,
    *,
    max_length: int | None = _DEFAULT_LIMITS.max_comment_length,
    max_bytes: int | None = _DEFAULT_LIMITS.max_comment_bytes,
) -> str:
    """Validate a short, safe Unicode comment suitable for a JSON sidecar."""
    if not isinstance(value, str):
        raise ValueError("comment must be a string")
    if max_length is not None and len(value) > max_length:
        raise ValueError(f"comment must be at most {max_length} characters")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("comment contains invalid Unicode") from exc
    if max_bytes is not None and len(encoded) > max_bytes:
        raise ValueError(f"comment must be at most {max_bytes} UTF-8 bytes")
    for char in value:
        category = unicodedata.category(char)
        if category.startswith("C") and char not in {"\n", "\u200c", "\u200d"}:
            raise ValueError("comment contains unsupported control characters")
        codepoint = ord(char)
        if 0xFDD0 <= codepoint <= 0xFDEF or codepoint & 0xFFFF in {0xFFFE, 0xFFFF}:
            raise ValueError("comment contains a noncharacter")
    return value
_SPACE_MARGIN_BYTES = 64 * 1024
_TXN_MARKER_RE = re.compile(r"^\.pbtxn-([0-9a-f]{24})\.json$")
_TXN_COMMIT_RE = re.compile(r"^\.pbtxn-([0-9a-f]{24})\.commit$")
_DATA_TEMP_RE = re.compile(r"^\.pbdata-[0-9a-f]{24}\.tmp$")
_META_TEMP_RE = re.compile(r"^\.pbmeta-[0-9a-f]{24}\.tmp$")
_TXN_TEMP_RE = re.compile(r"^\.pbtxn-[0-9a-f]{24}\.tmp$")
_TXN_DATA_GUARD_RE = re.compile(r"^\.pbtxn-guard-[0-9a-f]{24}\.data$")
_TXN_META_GUARD_RE = re.compile(r"^\.pbtxn-guard-[0-9a-f]{24}\.json$")
_DELETE_MARKER_RE = re.compile(r"^\.pbdel-([0-9a-f]{24})\.json$")
_DELETE_TEMP_RE = re.compile(r"^\.pbdel-[0-9a-f]{24}\.tmp$")
_RENAME_MARKER_RE = re.compile(r"^\.pbrename-([0-9a-f]{24})\.json$")
_RENAME_COMMIT_RE = re.compile(r"^\.pbrename-([0-9a-f]{24})\.commit$")
_RENAME_TEMP_RE = re.compile(r"^\.pbrename-[0-9a-f]{24}\.tmp$")
_RENAME_BACKUP_RE = re.compile(r"^\.pbrename-backup-[0-9a-f]{24}\.json$")
_RENAME_DATA_BACKUP_RE = re.compile(r"^\.pbrename-backup-[0-9a-f]{24}\.data$")
_RENAME_DATA_GUARD_RE = re.compile(r"^\.pbrename-guard-[0-9a-f]{24}\.data$")
_RENAME_META_BACKUP_GUARD_RE = re.compile(
    r"^\.pbrename-backup-[0-9a-f]{24}\.guard\.json$"
)
_RENAME_META_GUARD_RE = re.compile(r"^\.pbrename-guard-[0-9a-f]{24}\.json$")
_TRASH_RE = re.compile(r"^\.pbtrash-[0-9a-f]{24}\.(?:data|json)$")
_INTERNAL_RESERVED_PREFIXES = (
    ".pbmeta-",
    ".pbdata-",
    ".pbbackup-",
    ".pbtxn-",
    ".pbtrash-",
    ".pbrename-",
    ".pbdel-",
)
_TXN_KEYS = {
    "version",
    "state",
    "target",
    "data_temp",
    "meta_temp",
    "data_backup",
    "meta_backup",
    "data_guard",
    "meta_guard",
    "target_identity",
    "meta_identity",
    "new_data_identity",
    "new_meta_identity",
}
_TXN_KEYS_WITHOUT_GUARDS = _TXN_KEYS - {"data_guard", "meta_guard"}
_TXN_KEYS_WITHOUT_META_GUARD = _TXN_KEYS - {"meta_guard"}
_DELETE_KEYS = {
    "version",
    "target",
    "data_trash",
    "meta_trash",
    "target_identity",
    "meta_identity",
}
_RENAME_KEYS = {
    "version",
    "state",
    "source",
    "target",
    "meta_temp",
    "meta_backup",
    "data_backup",
    "data_guard",
    "meta_guard",
    "meta_backup_guard",
    "source_identity",
    "source_meta_identity",
    "new_meta_identity",
}
_RENAME_KEYS_WITHOUT_GUARDS = _RENAME_KEYS - {
    "data_guard",
    "meta_guard",
    "meta_backup_guard",
}
_RENAME_KEYS_WITHOUT_META_GUARDS = _RENAME_KEYS - {
    "meta_guard",
    "meta_backup_guard",
}
_LEGACY_RENAME_KEYS = _RENAME_KEYS - {
    "data_backup",
    "data_guard",
    "meta_guard",
    "meta_backup_guard",
}


def _txn_token(name: str) -> str | None:
    match = _TXN_MARKER_RE.fullmatch(name) or _TXN_COMMIT_RE.fullmatch(name)
    return match.group(1) if match else None


def _delete_token(name: str) -> str | None:
    match = _DELETE_MARKER_RE.fullmatch(name)
    return match.group(1) if match else None


def _rename_token(name: str) -> str | None:
    match = _RENAME_MARKER_RE.fullmatch(name) or _RENAME_COMMIT_RE.fullmatch(name)
    return match.group(1) if match else None


def _internal_transaction_name(name: object) -> bool:
    return isinstance(name, str) and (
        bool(_TXN_MARKER_RE.fullmatch(name))
        or bool(_TXN_COMMIT_RE.fullmatch(name))
    )


def _internal_marker_name(name: object) -> bool:
    return _internal_transaction_name(name) or (
        isinstance(name, str)
        and (
            bool(_DELETE_MARKER_RE.fullmatch(name))
            or bool(_RENAME_MARKER_RE.fullmatch(name))
            or bool(_RENAME_COMMIT_RE.fullmatch(name))
        )
    )


def _filename_shape_ok(
    name: object,
    *,
    max_length: int | None = _DEFAULT_LIMITS.max_filename_length,
    max_bytes: int | None = _DEFAULT_LIMITS.max_filename_bytes,
) -> bool:
    if not isinstance(name, str) or not name.strip():
        return False
    if name in {".", "..", ".pasteberth.lock"}:
        return False
    try:
        if max_bytes is not None and len(name.encode("utf-8")) > max_bytes:
            return False
    except UnicodeEncodeError:
        return False
    if any(unicodedata.category(char).startswith("C") for char in name):
        return False
    return (max_length is None or len(name) <= max_length) and bool(
        _CLIENT_FILENAME_RE.fullmatch(name)
    )


def _legacy_pbdel_filename(name: object) -> bool:
    """Recognize names that older clients could already have stored."""
    return _filename_shape_ok(name) and name.startswith(".pbdel-")


def _persisted_filename(name: object) -> bool:
    """Accept current names and legacy .pbdel names in V1 journals."""
    return valid_filename(name) or _legacy_pbdel_filename(name)


def valid_filename(
    name: object,
    *,
    max_length: int | None = _DEFAULT_LIMITS.max_filename_length,
    max_bytes: int | None = _DEFAULT_LIMITS.max_filename_bytes,
) -> bool:
    if not _filename_shape_ok(name, max_length=max_length, max_bytes=max_bytes):
        return False
    return not name.startswith(_INTERNAL_RESERVED_PREFIXES)


def generated_filename(name: object) -> bool:
    """Return whether a name comes from the historical internal generator."""
    return isinstance(name, str) and bool(_GENERATED_FILENAME_RE.fullmatch(name))


def _rename_noreplace(
    directory: DirectoryHandle,
    source: str,
    target: str,
) -> None:
    """Delegate no-replace rename to the selected platform backend.

    The helper remains as a narrow seam for the v1.5 transaction tests that
    inject failures around publication.
    """
    platform_fs().rename_noreplace(directory, source, target)


@dataclass(frozen=True)
class StoredImage:
    filename: str
    created_at: datetime  # timezone-aware UTC
    width: int | None
    height: int | None
    size: int
    fmt: str | None
    kind: str = "image"  # "image" | "text" | "binary"
    mime: str = "image/png"
    comment: str = ""
    changed_at: datetime | None = None  # filesystem modification time when known


@dataclass(frozen=True)
class SpaceInfo:
    total_bytes: int
    available_bytes: int

    @property
    def available_percent(self) -> float:
        if self.total_bytes <= 0:
            return 0.0
        return self.available_bytes * 100.0 / self.total_bytes


class DestinationError(Exception):
    """Destination I/O error (missing directory, permissions, etc.)."""


class UnknownImageError(DestinationError):
    """The file is no longer a known Pasteberth object."""


class DestinationBusyError(DestinationError):
    """The destination is already locked by another operation."""


class StorageLowError(DestinationError):
    """The write would cross the configured free-space threshold."""

    def __init__(self, info: SpaceInfo, minimum_percent: float):
        self.info = info
        self.minimum_percent = minimum_percent
        super().__init__(
            f"insufficient free space ({info.available_percent:.2f}% available, "
            f"minimum {minimum_percent:.2f}%)"
        )


class StorageConflictError(DestinationError):
    """The target exists without a coherent sidecar (foreign file)."""


class ReplacementRequiredError(DestinationError):
    """An existing Pasteberth pair requires explicit replacement."""


class RetentionError(DestinationError):
    """At least one retention deletion failed."""

    def __init__(self, failures: list[str]):
        self.failures = failures
        super().__init__(f"{len(failures)} retention deletion(s) failed")


class Destination(ABC):
    """Pragmatic interface: a future SshDestination uses the same methods."""

    @abstractmethod
    def save(
        self,
        data: bytes,
        info: ContentInfo,
        filename: str | None = None,
        *,
        allow_replace: bool = False,
        adopt_existing: bool = False,
    ) -> StoredImage: ...

    @abstractmethod
    def list(self) -> list[StoredImage]:
        """History, newest first."""

    @abstractmethod
    def delete(self, filename: str, *, allow_stale_sidecar: bool = False) -> None: ...

    @abstractmethod
    def rename(
        self,
        source: str,
        target: str,
    ) -> StoredImage: ...

    @abstractmethod
    def update_comment(self, filename: str, comment: str) -> StoredImage: ...

    @abstractmethod
    def read(self, filename: str) -> bytes: ...

    @abstractmethod
    def open_read(self, filename: str):
        """Open a managed file for a read held under the destination lock."""
        ...

    @abstractmethod
    def reference_path(self, filename: str) -> str:
        """Path as seen by the harness (reference base)."""


class LocalDestination(Destination):
    def __init__(
        self,
        directory: Path,
        *,
        create_directory: bool = True,
        limits: LimitsConfig | None = None,
        max_image_pixels: int | None = None,
        file_group: str | None = None,
    ):
        self.directory = Path(directory).expanduser().resolve()
        self.create_directory = create_directory
        self.limits = limits or LimitsConfig()
        self.max_image_pixels = max_image_pixels
        self.file_group = file_group
        self._created_file_permissions = 0o660 if file_group else 0o600
        self._fs = platform_fs()
        lock_key = os.path.normcase(os.path.abspath(os.fspath(self.directory)))
        self._stable_lock_name = (
            ".pasteberth-zone-"
            + hashlib.sha256(lock_key.encode("utf-8")).hexdigest()
            + ".lock"
        )
        self._directory_identity = None
        self._operation_directory = ContextVar(
            "pasteberth_operation_directory",
            default=None,
        )
        self._cleanup_pair_check = ContextVar(
            "pasteberth_cleanup_pair_check",
            default=None,
        )
        self._cleanup_pair_unguarded = ContextVar(
            "pasteberth_cleanup_pair_unguarded",
            default=frozenset(),
        )
        self._cleanup_recovery_handles = ContextVar(
            "pasteberth_cleanup_recovery_handles",
            default=None,
        )
        self._journal_identities: dict[str, tuple[int, int]] = {}
        self._ensure_dir()
        with self.operation_lock(exclusive=True):
            self.reconcile()

    def _valid_filename(self, name: object) -> bool:
        return valid_filename(
            name,
            max_length=self.limits.max_filename_length,
            max_bytes=self.limits.max_filename_bytes,
        )

    def _legacy_pbdel_filename(self, name: object) -> bool:
        return (
            _filename_shape_ok(
                name,
                max_length=self.limits.max_filename_length,
                max_bytes=self.limits.max_filename_bytes,
            )
            and name.startswith(".pbdel-")
        )

    def _persisted_filename(self, name: object) -> bool:
        return self._valid_filename(name) or self._legacy_pbdel_filename(name)

    def _ensure_dir(self) -> None:
        bound = self._operation_directory.get()
        if bound is not None:
            if bound.closed:
                raise DestinationError("bound directory handle is already closed")
            return
        # Permissions are not inspected here: shared directories are accepted
        # with a startup warning from config.py. Rejecting them at runtime would
        # break legitimate shared zones and encourage bypassing protection.
        directory = None
        try:
            directory = self._fs.open_directory(
                self.directory,
                create=self.create_directory and self._directory_identity is None,
                mode=0o700,
            )
            self._check_directory_identity(directory)
        except FileNotFoundError as exc:
            if self._directory_identity is not None:
                raise DestinationError(
                    f"zone directory was replaced: {self.directory}"
                ) from exc
            if not self.create_directory:
                raise DestinationError(f"zone directory is missing: {self.directory}") from exc
            raise DestinationError(
                f"cannot create {self.directory}: {exc}"
            ) from exc
        except (OSError, ValueError, UnsupportedFilesystemError) as exc:
            raise DestinationError(
                f"cannot open {self.directory}: {exc}"
            ) from exc
        finally:
            if directory is not None:
                directory.close()

    def _check_directory_identity(self, directory: DirectoryHandle) -> None:
        expected = self._directory_identity
        if expected is None:
            self._directory_identity = directory.identity
            return
        if directory.identity != expected:
            raise DestinationError(
                f"zone directory was replaced: {self.directory}"
            )

    @contextmanager
    def _stable_operation_lock(self, *, exclusive: bool, blocking: bool):
        lock_root = self._fs.runtime_directory() / "pasteberth" / "zones"
        try:
            with self._fs.open_directory(lock_root, create=True, mode=0o700) as root:
                with self._fs.acquire_lock(
                    root,
                    name=self._stable_lock_name,
                    exclusive=exclusive,
                    blocking=blocking,
                ):
                    yield
        except BusyError:
            raise
        except DestinationError:
            raise
        except (OSError, UnsupportedFilesystemError) as exc:
            raise DestinationError(
                f"cannot acquire stable lock for {self.directory}: {exc}"
            ) from exc

    @contextmanager
    def _directory_fd(self):
        bound = self._operation_directory.get()
        if bound is not None:
            if bound.closed:
                raise DestinationError("bound directory handle is already closed")
            yield bound
            return
        self._ensure_dir()
        directory = None
        try:
            directory = self._fs.open_directory(self.directory)
            self._check_directory_identity(directory)
        except OSError as exc:
            if directory is not None:
                directory.close()
            raise DestinationError(f"cannot open {self.directory}: {exc}") from exc
        except BaseException:
            if directory is not None:
                directory.close()
            raise
        try:
            yield directory
        finally:
            if directory is not None:
                directory.close()

    @contextmanager
    def operation_lock(self, *, exclusive: bool, blocking: bool = True):
        """Lock operations even between processes belonging to the same user."""
        try:
            with self._stable_operation_lock(
                exclusive=exclusive,
                blocking=blocking,
            ):
                with self._directory_fd() as directory_fd:
                    with self._fs.acquire_lock(
                        directory_fd,
                        exclusive=exclusive,
                        blocking=blocking,
                    ):
                        token = self._operation_directory.set(directory_fd)
                        try:
                            yield
                        finally:
                            self._operation_directory.reset(token)
        except BusyError as exc:
            raise DestinationBusyError(
                f"destination is busy: {self.directory}"
            ) from exc
        except DestinationError:
            raise
        except (OSError, UnsupportedFilesystemError) as exc:
            raise DestinationError(
                f"cannot acquire lock for {self.directory}: {exc}"
            ) from exc

    def _open_file(self, directory: DirectoryHandle, name: str, mode: str = "rb") -> FileHandle:
        is_sidecar_name = (
            isinstance(name, str)
            and name.endswith(".json")
            and self._valid_filename(name[:-5])
        )
        if not self._valid_filename(name) and not is_sidecar_name and not _internal_marker_name(name):
            raise DestinationError(f"invalid filename: {name!r}")
        try:
            return self._fs.open_existing(directory, name, mode=mode)
        except FileNotFoundError:
            raise
        except (DestinationError, UnsafeLinkError):
            raise
        except (OSError, UnsupportedFilesystemError) as exc:
            raise DestinationError(f"cannot open {name!r}: {exc}") from exc

    def _create_file(
        self,
        directory: DirectoryHandle,
        name: str,
        *,
        mode: str,
        permissions: int | None = None,
    ) -> FileHandle:
        """Create a server-owned file with the configured filesystem group."""
        handle = None
        identity = None
        try:
            handle = self._fs.create_exclusive(
                directory,
                name,
                mode=mode,
                permissions=self._created_file_permissions if permissions is None else permissions,
            )
            identity = handle.identity
            if self.file_group is not None:
                self._fs.set_group(handle, self.file_group)
            return handle
        except BaseException as exc:
            if handle is not None and not handle.closed:
                try:
                    handle.close()
                except OSError:
                    pass
            if identity is not None:
                try:
                    self._fs.remove_expected(directory, name, identity)
                except (OSError, UnsupportedFilesystemError):
                    pass
            if isinstance(exc, (PermissionSecurityError, UnsupportedFilesystemError, OSError)):
                raise DestinationError(
                    f"cannot create {name!r} with group {self.file_group!r}: {exc}"
                ) from exc
            raise

    def _write_transaction_file(
        self,
        directory_fd: DirectoryHandle,
        name: str,
        transaction: dict,
    ) -> None:
        """Publish a transaction marker without replacing an existing name."""
        if not _internal_marker_name(name):
            raise ValueError(f"invalid transaction name: {name!r}")
        temp_name = name.rsplit(".", 1)[0] + ".tmp"
        file_handle = None
        temp_identity = None
        try:
            file_handle = self._create_file(
                directory_fd,
                temp_name,
                mode="w",
            )
            temp_identity = file_handle.identity
            with file_handle as fh:
                json.dump(transaction, fh, ensure_ascii=False, separators=(",", ":"))
                fh.sync()
            self._move_expected(directory_fd, temp_name, name, temp_identity)
            self._journal_identities[name] = temp_identity
            self._fsync_directory(directory_fd)
        except BaseException:
            if file_handle is not None and not file_handle.closed:
                try:
                    file_handle.close()
                except OSError:
                    pass
            if temp_identity is not None:
                try:
                    self._remove_expected(directory_fd, name, temp_identity)
                except (DestinationError, OSError):
                    pass
                try:
                    self._remove_expected(directory_fd, temp_name, temp_identity)
                except (DestinationError, OSError):
                    pass
            raise

    @staticmethod
    def _transaction_identity(raw: dict, key: str) -> tuple[int, int] | None:
        value = raw.get(key)
        if value is None:
            return None
        if not isinstance(value, list) or len(value) != 2:
            raise ValueError(f"invalid transaction identity: {key}")
        if any(isinstance(part, bool) or not isinstance(part, int) for part in value):
            raise ValueError(f"invalid transaction identity: {key}")
        return (value[0], value[1])

    def _parse_transaction(self, marker_name: str, raw: object) -> dict:
        if not isinstance(raw, dict) or set(raw) not in (
            _TXN_KEYS,
            _TXN_KEYS_WITHOUT_GUARDS,
            _TXN_KEYS_WITHOUT_META_GUARD,
        ):
            raise ValueError("invalid transaction marker")
        raw = dict(raw)
        token = _txn_token(marker_name)
        if token is None or raw["version"] != 1 or raw["state"] not in ("prepared", "committed"):
            raise ValueError("invalid transaction marker")
        if (
            (_TXN_MARKER_RE.fullmatch(marker_name) and raw["state"] != "prepared")
            or (_TXN_COMMIT_RE.fullmatch(marker_name) and raw["state"] != "committed")
        ):
            raise ValueError("inconsistent transaction state")
        target = raw["target"]
        if not self._persisted_filename(target):
            raise ValueError("invalid transaction target")
        expected_names = {
            "data_backup": f".pbbackup-{token}.data",
            "meta_backup": f".pbbackup-{token}.json",
        }
        if any(raw[key] != value for key, value in expected_names.items()):
            raise ValueError("inconsistent transaction files")
        if not _DATA_TEMP_RE.fullmatch(raw["data_temp"]):
            raise ValueError("invalid data temporary file")
        if not _META_TEMP_RE.fullmatch(raw["meta_temp"]):
            raise ValueError("invalid sidecar temporary file")
        if raw.get("data_guard") is not None:
            if raw["data_guard"] != f".pbtxn-guard-{token}.data":
                raise ValueError("inconsistent data guard")
        if raw.get("meta_guard") is not None:
            if raw["meta_guard"] != f".pbtxn-guard-{token}.json":
                raise ValueError("inconsistent sidecar guard")
        raw.setdefault("data_guard", None)
        raw.setdefault("meta_guard", None)
        for key in ("target_identity", "meta_identity"):
            LocalDestination._transaction_identity(raw, key)
        for key in ("new_data_identity", "new_meta_identity"):
            if LocalDestination._transaction_identity(raw, key) is None:
                raise ValueError(f"missing transaction identity: {key}")
        return raw

    def _parse_delete_transaction(self, marker_name: str, raw: object) -> dict:
        if not isinstance(raw, dict) or set(raw) != _DELETE_KEYS:
            raise ValueError("invalid deletion marker")
        token = _delete_token(marker_name)
        if token is None or raw["version"] != 1:
            raise ValueError("invalid deletion marker")
        target = raw["target"]
        if not self._persisted_filename(target):
            raise ValueError("invalid deletion target")
        if (
            raw["data_trash"] != f".pbtrash-{token}.data"
            or raw["meta_trash"] != f".pbtrash-{token}.json"
        ):
            raise ValueError("inconsistent deletion files")
        for key in ("target_identity", "meta_identity"):
            if LocalDestination._transaction_identity(raw, key) is None:
                raise ValueError(f"missing deletion identity: {key}")
        return raw

    def _parse_rename_transaction(self, marker_name: str, raw: object) -> dict:
        if not isinstance(raw, dict) or set(raw) not in (
            _RENAME_KEYS,
            _RENAME_KEYS_WITHOUT_GUARDS,
            _RENAME_KEYS_WITHOUT_META_GUARDS,
            _LEGACY_RENAME_KEYS,
        ):
            raise ValueError("invalid rename marker")
        raw = dict(raw)
        token = _rename_token(marker_name)
        if token is None or raw["version"] != 1 or raw["state"] not in (
            "prepared",
            "committed",
        ):
            raise ValueError("invalid rename marker")
        if (
            (_RENAME_MARKER_RE.fullmatch(marker_name) and raw["state"] != "prepared")
            or (_RENAME_COMMIT_RE.fullmatch(marker_name) and raw["state"] != "committed")
        ):
            raise ValueError("inconsistent rename state")
        source = raw["source"]
        target = raw["target"]
        if (
            not self._persisted_filename(source)
            or not self._persisted_filename(target)
            or source == target
        ):
            raise ValueError("invalid rename names")
        if raw["meta_backup"] != f".pbrename-backup-{token}.json":
            raise ValueError("inconsistent rename backup")
        if "data_backup" in raw:
            if raw["data_backup"] != f".pbrename-backup-{token}.data":
                raise ValueError("inconsistent data backup")
        else:
            # Markers created before the data backup was added remain
            # recoverable; they simply retain the old best-effort semantics.
            raw["data_backup"] = None
        if raw.get("data_guard") is not None:
            if raw["data_guard"] != f".pbrename-guard-{token}.data":
                raise ValueError("inconsistent data guard")
        if raw.get("meta_guard") is not None:
            if raw["meta_guard"] != f".pbrename-guard-{token}.json":
                raise ValueError("inconsistent sidecar guard")
        if raw.get("meta_backup_guard") is not None:
            if raw["meta_backup_guard"] != f".pbrename-backup-{token}.guard.json":
                raise ValueError("inconsistent sidecar backup guard")
        raw.setdefault("data_guard", None)
        raw.setdefault("meta_guard", None)
        raw.setdefault("meta_backup_guard", None)
        if not _META_TEMP_RE.fullmatch(raw["meta_temp"]):
            raise ValueError("invalid rename temporary file")
        for key in ("source_identity", "source_meta_identity", "new_meta_identity"):
            if LocalDestination._transaction_identity(raw, key) is None:
                raise ValueError(f"missing rename identity: {key}")
        return raw

    def _active_transaction_names(self, directory_fd: DirectoryHandle) -> set[str]:
        names: set[str] = set()
        try:
            entries = self._fs.entries(directory_fd)
        except OSError as exc:
            raise DestinationError(
                f"cannot read {self.directory}: {exc}"
            ) from exc
        for entry in entries:
            try:
                if self._historical_pbdel_pair(directory_fd, entry.name):
                    continue
                if _txn_token(entry.name) is not None:
                    transaction = self._parse_transaction(
                        entry.name,
                        self._read_meta(directory_fd, entry.name),
                    )
                    names.add(transaction["target"])
                elif _delete_token(entry.name) is not None:
                    transaction = self._parse_delete_transaction(
                        entry.name,
                        self._read_meta(directory_fd, entry.name),
                    )
                    names.add(transaction["target"])
                elif _rename_token(entry.name) is not None:
                    transaction = self._parse_rename_transaction(
                        entry.name,
                        self._read_meta(directory_fd, entry.name),
                    )
                    names.update((transaction["source"], transaction["target"]))
            except (DestinationError, ValueError, TypeError, KeyError):
                continue
        return names

    def _historical_pbdel_pair(
        self,
        directory_fd: DirectoryHandle,
        name: str,
    ) -> bool:
        """Do not execute an old client upload as a deletion marker."""
        if not self._legacy_pbdel_filename(name):
            return False
        sidecar_name = name + ".json"
        try:
            sidecar = self._fs.entry_info(directory_fd, sidecar_name)
        except (OSError, UnsupportedFilesystemError):
            # Ambiguous recovery must be non-destructive.
            return True
        return sidecar is not None and sidecar.is_regular and not sidecar.is_symlink

    def _remember_journal_entry(self, directory_fd: DirectoryHandle, entry) -> None:
        try:
            self._journal_identities[entry.name] = entry.identity
        except OSError:
            pass

    def _journal_identity(
        self,
        directory_fd: DirectoryHandle,
        name: str,
    ) -> tuple[int, int] | None:
        expected = self._journal_identities.get(name)
        if expected is not None:
            return expected
        return self._entry_identity(directory_fd, name)

    def _unlink_expected(
        self,
        directory_fd: DirectoryHandle,
        name: str,
        expected: tuple[int, int] | None,
    ) -> bool:
        try:
            info = self._fs.entry_info(directory_fd, name)
        except FileNotFoundError:
            return True
        if info is None:
            return True
        if not info.is_regular:
            raise DestinationError(f"temporary file is not regular: {name!r}")
        if not self._fs.is_owned(info):
            return False
        actual = info.identity
        if expected is not None and actual != expected:
            return False
        quarantine_name = f".pbtrash-{secrets.token_hex(12)}.data"
        try:
            self._move_expected(directory_fd, name, quarantine_name, actual)
        except (DestinationError, OSError):
            return False
        try:
            quarantine_info = self._fs.entry_info(directory_fd, quarantine_name)
        except FileNotFoundError:
            return True
        if quarantine_info is None:
            return True
        if not quarantine_info.is_regular:
            return False
        if quarantine_info.identity != actual:
            return False
        try:
            return self._fs.remove_expected(directory_fd, quarantine_name, actual)
        except FileNotFoundError:
            return True
        except (OSError, UnsupportedFilesystemError):
            return False

    def _restore_noreplace(
        self,
        directory_fd: DirectoryHandle,
        source: str,
        target: str,
        expected: tuple[int, int],
    ) -> bool:
        if self._entry_identity(directory_fd, source) != expected:
            return False
        try:
            _rename_noreplace(directory_fd, source, target)
        except FileExistsError:
            return False
        return self._entry_identity(directory_fd, target) == expected

    def _entry_identity_any(
        self,
        directory_fd: DirectoryHandle,
        name: str,
    ) -> tuple[int, int] | None:
        try:
            return self._fs.identity(directory_fd, name, require_regular=False)
        except FileNotFoundError:
            return None
        except UnsafeLinkError:
            return None

    def _move_expected(
        self,
        directory_fd: DirectoryHandle,
        source: str,
        target: str,
        expected: tuple[int, int],
    ) -> None:
        if self._entry_identity(directory_fd, source) != expected:
            raise StorageConflictError(f"file changed during operation: {source!r}")
        try:
            _rename_noreplace(directory_fd, source, target)
        except FileExistsError as exc:
            raise StorageConflictError(f"target appeared during operation: {target!r}") from exc
        actual = self._entry_identity_any(directory_fd, target)
        if actual == expected:
            return
        if actual is not None:
            self._restore_any(directory_fd, target, source, actual)
        raise StorageConflictError(f"foreign file appeared during operation: {source!r}")

    def _link_expected(
        self,
        directory_fd: DirectoryHandle,
        source: str,
        target: str,
        expected: tuple[int, int],
    ) -> None:
        """Create a backup link without replacing a concurrent entry."""
        if self._entry_identity(directory_fd, source) != expected:
            raise StorageConflictError(f"file changed during operation: {source!r}")
        try:
            self._fs.link_expected(directory_fd, source, target, expected)
        except (EntryExistsError, FileExistsError) as exc:
            raise StorageConflictError(f"target appeared during operation: {target!r}") from exc
        except (EntryChangedError, UnsafeLinkError) as exc:
            raise StorageConflictError(f"file changed during operation: {source!r}") from exc
        if self._entry_identity_any(directory_fd, target) != expected:
            raise StorageConflictError(f"foreign file appeared during operation: {target!r}")

    def _restore_any(
        self,
        directory_fd: DirectoryHandle,
        source: str,
        target: str,
        expected: tuple[int, int],
    ) -> bool:
        if self._entry_identity_any(directory_fd, source) != expected:
            return False
        try:
            _rename_noreplace(directory_fd, source, target)
        except FileExistsError:
            return False
        return self._entry_identity_any(directory_fd, target) == expected

    def _remove_expected(
        self,
        directory_fd: DirectoryHandle,
        name: str,
        expected: tuple[int, int] | None,
    ) -> bool:
        """Remove an entry by first moving it away from its public name."""
        try:
            actual = self._entry_identity(directory_fd, name)
        except DestinationError:
            return False
        if actual is None:
            return True
        if expected is None or actual != expected:
            return False
        expected = actual
        pair_check = self._cleanup_pair_check.get()
        is_guarded = pair_check is not None and name not in self._cleanup_pair_unguarded.get()
        if is_guarded:
            try:
                if not pair_check():
                    return False
            except (DestinationError, OSError):
                return False
        recovery_handles = self._cleanup_recovery_handles.get()
        if is_guarded and recovery_handles is not None:
            try:
                recovery_name = f".pbtrash-{secrets.token_hex(12)}.data"
                self._link_expected(
                    directory_fd,
                    name,
                    recovery_name,
                    expected,
                )
                recovery_handles.append((name, expected, recovery_name))
            except (DestinationError, OSError, StorageConflictError):
                recovery_name = None
        trash_name = f".pbtrash-{secrets.token_hex(12)}.data"
        try:
            self._move_expected(directory_fd, name, trash_name, expected)
            if is_guarded:
                try:
                    if not pair_check():
                        self._restore_noreplace(directory_fd, trash_name, name, expected)
                        return False
                except (DestinationError, OSError):
                    return False
            return self._unlink_expected(directory_fd, trash_name, expected)
        except (DestinationError, OSError, StorageConflictError):
            return False

    @contextmanager
    def _cleanup_pair_guard(self, pair_check, *, unguarded_names=()):
        check_token = self._cleanup_pair_check.set(pair_check)
        unguarded_token = self._cleanup_pair_unguarded.set(frozenset(unguarded_names))
        handles = []
        handles_token = self._cleanup_recovery_handles.set(handles)
        try:
            yield handles
        finally:
            self._cleanup_recovery_handles.reset(handles_token)
            self._cleanup_pair_unguarded.reset(unguarded_token)
            self._cleanup_pair_check.reset(check_token)

    def _close_recovery_handles(
        self,
        directory_fd: int,
        handles,
        *,
        keep=(),
        pair_check=None,
    ) -> bool:
        if pair_check is not None:
            try:
                if not pair_check():
                    return False
            except (DestinationError, OSError):
                return False
        for _name, expected, recovery_name in handles:
            if recovery_name in keep:
                continue
            if pair_check is not None:
                try:
                    if not pair_check():
                        return False
                except (DestinationError, OSError):
                    return False
            if self._entry_identity_any(directory_fd, recovery_name) is None:
                continue
            if self._entry_identity_any(directory_fd, recovery_name) != expected:
                return False
            safety_name = None
            if pair_check is not None:
                safety_name = f".pbtrash-{secrets.token_hex(12)}.data"
                try:
                    self._link_expected(
                        directory_fd,
                        recovery_name,
                        safety_name,
                        expected,
                    )
                except (DestinationError, OSError):
                    return False
                try:
                    if not pair_check():
                        return False
                except (DestinationError, OSError):
                    return False
            try:
                if not self._unlink_expected(directory_fd, recovery_name, expected):
                    return False
            except (DestinationError, OSError):
                return False
            if pair_check is not None:
                try:
                    if not pair_check():
                        return False
                except (DestinationError, OSError):
                    return False
            if safety_name is not None:
                try:
                    if not self._unlink_expected(directory_fd, safety_name, expected):
                        return False
                except (DestinationError, OSError):
                    return False
                try:
                    if not pair_check():
                        return False
                except (DestinationError, OSError):
                    return False
        return True

    def _restore_from_recovery_handle(
        self,
        directory_fd: int,
        handles,
        names: tuple[str | None, ...],
        target: str,
        expected: tuple[int, int] | None,
    ) -> bool:
        current = self._entry_identity(directory_fd, target)
        if current is not None:
            return current == expected
        if expected is None:
            return False
        for name in names:
            if name is None:
                continue
            for handle_name, handle_expected, recovery_name in reversed(handles):
                if handle_name != name or handle_expected != expected:
                    continue
                try:
                    self._link_expected(
                        directory_fd,
                        recovery_name,
                        target,
                        expected,
                    )
                except (DestinationError, OSError):
                    continue
                if self._entry_identity(directory_fd, target) == expected:
                    return True
                if self._remove_expected(directory_fd, target, expected):
                    continue
                return False
        return False

    def _retain_recovery_candidates(
        self,
        directory_fd: int,
        handles,
        candidates: tuple[tuple[str | None, tuple[int, int] | None], ...],
    ) -> set[str]:
        """Keep a private copy in the journal if a foreign target blocks recovery."""
        keep: set[str] = set()
        for candidate_name, expected in candidates:
            if candidate_name is None or expected is None:
                continue
            current = None
            try:
                current = self._entry_identity_any(directory_fd, candidate_name)
                if current == expected:
                    continue
                for handle_name, handle_expected, recovery_name in reversed(handles):
                    if handle_expected != expected:
                        continue
                    if self._entry_identity_any(directory_fd, recovery_name) != expected:
                        continue
                    if current is not None:
                        keep.add(recovery_name)
                        continue
                    if self._restore_noreplace(
                        directory_fd,
                        recovery_name,
                        candidate_name,
                        expected,
                    ):
                        break
                    keep.add(recovery_name)
            except (DestinationError, OSError):
                for handle_name, handle_expected, recovery_name in handles:
                    if handle_name == candidate_name and handle_expected == expected:
                        keep.add(recovery_name)
                continue
        return keep

    def _recovery_names_for_identity(
        self,
        directory_fd: int,
        expected: tuple[int, int] | None,
    ) -> tuple[str, ...]:
        if expected is None:
            return ()
        try:
            return tuple(
                entry.name
                for entry in self._fs.entries(directory_fd)
                if _TRASH_RE.fullmatch(entry.name)
                and self._entry_identity_any(directory_fd, entry.name) == expected
            )
        except OSError:
            return ()

    def _remove_anonymous_recovery(
        self,
        directory_fd: int,
        *identities: tuple[int, int] | None,
    ) -> None:
        """Remove anonymous recovery copies that are no longer needed."""
        for identity in identities:
            if identity is None:
                continue
            for name in self._recovery_names_for_identity(directory_fd, identity):
                try:
                    self._remove_expected(directory_fd, name, identity)
                except (DestinationError, OSError):
                    pass

    def _restore_transaction_public_pair_from_handles(
        self,
        directory_fd: int,
        transaction: dict,
        handles,
    ) -> bool:
        return self._restore_from_recovery_handle(
            directory_fd,
            handles,
            (transaction.get("data_temp"), transaction.get("data_guard")),
            transaction["target"],
            self._transaction_identity(transaction, "new_data_identity"),
        ) and self._restore_from_recovery_handle(
            directory_fd,
            handles,
            (transaction.get("meta_temp"), transaction.get("meta_guard")),
            transaction["target"] + ".json",
            self._transaction_identity(transaction, "new_meta_identity"),
        )

    def _transaction_recovery_candidates(
        self,
        transaction: dict,
    ) -> tuple[tuple[str | None, tuple[int, int] | None], ...]:
        data_expected = self._transaction_identity(transaction, "new_data_identity")
        meta_expected = self._transaction_identity(transaction, "new_meta_identity")
        return (
            (transaction.get("data_temp"), data_expected),
            (transaction.get("data_guard"), data_expected),
            (transaction.get("meta_temp"), meta_expected),
            (transaction.get("meta_guard"), meta_expected),
        )

    def _restore_rename_public_pair_from_handles(
        self,
        directory_fd: int,
        transaction: dict,
        handles,
    ) -> bool:
        return self._restore_from_recovery_handle(
            directory_fd,
            handles,
            (transaction.get("data_backup"), transaction.get("data_guard")),
            transaction["target"],
            self._transaction_identity(transaction, "source_identity"),
        ) and self._restore_from_recovery_handle(
            directory_fd,
            handles,
            (transaction.get("meta_temp"), transaction.get("meta_guard")),
            transaction["target"] + ".json",
            self._transaction_identity(transaction, "new_meta_identity"),
        )

    def _rename_recovery_candidates(
        self,
        transaction: dict,
    ) -> tuple[tuple[str | None, tuple[int, int] | None], ...]:
        source_expected = self._transaction_identity(transaction, "source_identity")
        meta_expected = self._transaction_identity(transaction, "new_meta_identity")
        return (
            (transaction.get("data_guard"), source_expected),
            (transaction.get("data_backup"), source_expected),
            (transaction.get("meta_temp"), meta_expected),
            (transaction.get("meta_guard"), meta_expected),
        )

    def _restore_rename_source_pair_from_handles(
        self,
        directory_fd: int,
        transaction: dict,
        handles,
    ) -> bool:
        return self._restore_from_recovery_handle(
            directory_fd,
            handles,
            (transaction.get("data_backup"), transaction.get("data_guard")),
            transaction["source"],
            self._transaction_identity(transaction, "source_identity"),
        ) and self._restore_from_recovery_handle(
            directory_fd,
            handles,
            (transaction.get("meta_backup"), transaction.get("meta_backup_guard")),
            transaction["source"] + ".json",
            self._transaction_identity(transaction, "source_meta_identity"),
        )

    def _preserve_committed_transaction(
        self,
        directory_fd: int,
        transaction: dict,
        commit_name: str,
    ) -> None:
        """Keep a journal if the public pair disappears during cleanup."""
        if self._entry_identity(directory_fd, commit_name) is not None:
            return
        committed = dict(transaction)
        committed["state"] = "committed"
        self._write_transaction_file(directory_fd, commit_name, committed)

    def _preserve_prepared_transaction(
        self,
        directory_fd: int,
        transaction: dict,
        marker_name: str,
    ) -> None:
        """Keep a prepared journal while an old pair remains hidden."""
        if self._entry_identity(directory_fd, marker_name) is not None:
            return
        for name, key in (
            (transaction.get("data_backup"), "target_identity"),
            (transaction.get("meta_backup"), "meta_identity"),
        ):
            expected = self._transaction_identity(transaction, key)
            candidates = () if name is None else (name,)
            candidates += self._recovery_names_for_identity(directory_fd, expected)
            for candidate in candidates:
                if expected is not None and self._entry_identity_any(directory_fd, candidate) == expected:
                    self._write_transaction_file(directory_fd, marker_name, transaction)
                    return

    def _preserve_delete_transaction(
        self,
        directory_fd: int,
        transaction: dict,
        marker_name: str,
    ) -> None:
        """Keep a tombstone if a public pair became foreign."""
        if self._entry_identity(directory_fd, marker_name) is not None:
            return
        for name, key in (
            (transaction.get("data_trash"), "target_identity"),
            (transaction.get("meta_trash"), "meta_identity"),
        ):
            expected = self._transaction_identity(transaction, key)
            candidates = () if name is None else (name,)
            candidates += self._recovery_names_for_identity(directory_fd, expected)
            for candidate in candidates:
                if expected is not None and self._entry_identity_any(directory_fd, candidate) == expected:
                    self._write_transaction_file(directory_fd, marker_name, transaction)
                    return

    def _preserve_prepared_rename(
        self,
        directory_fd: int,
        transaction: dict,
        marker_name: str,
    ) -> None:
        """Keep the rollback journal with a copy of the source."""
        if self._entry_identity(directory_fd, marker_name) is not None:
            return
        source_identity = self._transaction_identity(transaction, "source_identity")
        source_meta_identity = self._transaction_identity(
            transaction, "source_meta_identity"
        )
        candidates = (
            (transaction.get("data_backup"), source_identity),
            (transaction.get("data_guard"), source_identity),
            (transaction.get("meta_backup"), source_meta_identity),
            (transaction.get("meta_backup_guard"), source_meta_identity),
        )
        for name, expected in candidates:
            names = () if name is None else (name,)
            names += self._recovery_names_for_identity(directory_fd, expected)
            if expected is not None and any(
                self._entry_identity_any(directory_fd, candidate) == expected
                for candidate in names
            ):
                self._write_transaction_file(directory_fd, marker_name, transaction)
                return

    def _commit_file_matches(
        self,
        directory_fd: int,
        commit_name: str,
        transaction: dict,
    ) -> bool:
        try:
            committed = dict(transaction)
            committed["state"] = "committed"
            parser = (
                self._parse_rename_transaction
                if _RENAME_COMMIT_RE.fullmatch(commit_name)
                else self._parse_transaction
            )
            return parser(commit_name, self._read_meta(directory_fd, commit_name)) == committed
        except (DestinationError, ValueError, TypeError, KeyError):
            return False

    def _rollback_delete_transaction(
        self,
        directory_fd: int,
        transaction: dict,
        marker_name: str,
    ) -> bool:
        complete = True
        for trash, target, key in (
            (transaction["data_trash"], transaction["target"], "target_identity"),
            (transaction["meta_trash"], transaction["target"] + ".json", "meta_identity"),
        ):
            expected = self._transaction_identity(transaction, key)
            if expected is None:
                complete = False
                continue
            trash_identity = self._entry_identity(directory_fd, trash)
            if trash_identity is None:
                # The named trash may have been consumed by an interrupted
                # finish; look for an anonymous recovery copy by identity.
                for candidate in self._recovery_names_for_identity(directory_fd, expected):
                    if self._entry_identity(directory_fd, candidate) != expected:
                        continue
                    trash = candidate
                    trash_identity = expected
                    break
            if trash_identity is None:
                if self._entry_identity(directory_fd, target) != expected:
                    return False
                continue
            if trash_identity != expected:
                return False
            current_identity = self._entry_identity(directory_fd, target)
            if current_identity is None:
                try:
                    self._link_expected(directory_fd, trash, target, expected)
                except (DestinationError, OSError):
                    complete = False
            elif current_identity == expected:
                # Keep the trash until the marker has been retired; it is the
                # last durable copy during that final race window.
                pass
            else:
                # A foreign public entry appeared. Preserve it and leave the
                # old object hidden for reconciliation rather than overwrite it.
                return False

        if not complete:
            return False

        def pair_check() -> bool:
            try:
                return all(
                    self._entry_identity(directory_fd, target) == expected
                    for target, expected in (
                        (
                            transaction["target"],
                            self._transaction_identity(transaction, "target_identity"),
                        ),
                        (
                            transaction["target"] + ".json",
                            self._transaction_identity(transaction, "meta_identity"),
                        ),
                    )
                )
            except (DestinationError, OSError):
                return False

        recovery_handles = []
        recovery_to_keep: set[str] = set()
        with self._cleanup_pair_guard(pair_check) as recovery_handles:
            if not pair_check():
                complete = False
            if complete:
                for trash, key in (
                    (transaction["data_trash"], "target_identity"),
                    (transaction["meta_trash"], "meta_identity"),
                ):
                    if not pair_check():
                        complete = False
                        break
                    expected = self._transaction_identity(transaction, key)
                    if not self._remove_expected(directory_fd, trash, expected):
                        complete = False
                        break
                    if not pair_check():
                        complete = False
                        break
            if complete and not self._remove_expected(
                directory_fd,
                marker_name,
                self._journal_identity(directory_fd, marker_name),
            ):
                complete = False
            if complete and not pair_check():
                complete = False
        if not pair_check():
            recovery_to_keep.update(
                self._retain_recovery_candidates(
                    directory_fd,
                    recovery_handles,
                    (
                        (
                            transaction.get("data_trash"),
                            self._transaction_identity(transaction, "target_identity"),
                        ),
                        (
                            transaction.get("meta_trash"),
                            self._transaction_identity(transaction, "meta_identity"),
                        ),
                    ),
                )
            )
        if not complete:
            try:
                self._preserve_delete_transaction(directory_fd, transaction, marker_name)
            except (DestinationError, OSError):
                log.warning("cannot preserve deletion tombstone: %s", marker_name)
        closed = self._close_recovery_handles(
            directory_fd,
            recovery_handles,
            keep=recovery_to_keep,
            pair_check=pair_check,
        )
        if not closed:
            complete = False
            recovery_to_keep.update(
                self._retain_recovery_candidates(
                    directory_fd,
                    recovery_handles,
                    (
                        (
                            transaction.get("data_trash"),
                            self._transaction_identity(transaction, "target_identity"),
                        ),
                        (
                            transaction.get("meta_trash"),
                            self._transaction_identity(transaction, "meta_identity"),
                        ),
                    ),
                )
            )
            try:
                self._preserve_delete_transaction(directory_fd, transaction, marker_name)
            except (DestinationError, OSError):
                log.warning("cannot preserve deletion tombstone: %s", marker_name)
        if complete:
            self._remove_anonymous_recovery(
                directory_fd,
                self._transaction_identity(transaction, "target_identity"),
                self._transaction_identity(transaction, "meta_identity"),
            )
            self._fsync_directory(directory_fd)
        return complete

    def _finish_delete_transaction(
        self,
        directory_fd: int,
        transaction: dict,
        marker_name: str,
    ) -> bool:
        """Complete a marked deletion without touching foreign entries."""
        complete = True
        for target, trash, key in (
            (transaction["target"], transaction["data_trash"], "target_identity"),
            (transaction["target"] + ".json", transaction["meta_trash"], "meta_identity"),
        ):
            expected = self._transaction_identity(transaction, key)
            if expected is None:
                complete = False
                continue
            public_identity = self._entry_identity(directory_fd, target)
            trash_identity = self._entry_identity(directory_fd, trash)
            if public_identity is not None and public_identity != expected:
                return False
            elif trash_identity is None and public_identity == expected:
                try:
                    self._move_expected(directory_fd, target, trash, expected)
                except (DestinationError, OSError):
                    complete = False
            elif trash_identity is not None and trash_identity != expected:
                return False

        if not complete:
            return False

        for trash, key in (
            (transaction["data_trash"], "target_identity"),
            (transaction["meta_trash"], "meta_identity"),
        ):
            expected = self._transaction_identity(transaction, key)
            if expected is not None and not self._remove_expected(directory_fd, trash, expected):
                complete = False

        for target, key in (
            (transaction["target"], "target_identity"),
            (transaction["target"] + ".json", "meta_identity"),
        ):
            expected = self._transaction_identity(transaction, key)
            if expected is not None and self._entry_identity(directory_fd, target) == expected:
                complete = False

        if complete:
            marker_identity = self._journal_identity(directory_fd, marker_name)
            if not self._remove_expected(directory_fd, marker_name, marker_identity):
                complete = False
            else:
                self._fsync_directory(directory_fd)
        return complete

    def _recover_deletions(self, directory_fd: DirectoryHandle, entries) -> set[str]:
        protected: set[str] = set()
        for entry in entries:
            if _delete_token(entry.name) is None:
                continue
            if self._historical_pbdel_pair(directory_fd, entry.name):
                continue
            protected.add(entry.name)
            self._remember_journal_entry(directory_fd, entry)
            try:
                transaction = self._parse_delete_transaction(
                    entry.name,
                    self._read_meta(directory_fd, entry.name),
                )
            except (DestinationError, ValueError, TypeError, KeyError):
                log.warning("invalid deletion marker, preserved: %s", entry.name)
                continue
            protected.update(
                {
                    transaction["target"],
                    transaction["target"] + ".json",
                    transaction["data_trash"],
                    transaction["meta_trash"],
                }
            )
            try:
                if not self._finish_delete_transaction(directory_fd, transaction, entry.name):
                    log.warning("deferred deletion recovery: %s", entry.name)
            except (DestinationError, OSError):
                log.warning("deletion recovery failed: %s", entry.name)
        return protected

    def _rollback_rename_transaction(
        self,
        directory_fd: int,
        transaction: dict,
        marker_name: str,
    ) -> bool:
        """Roll back a prepared rename without overwriting a foreign entry."""
        source = transaction["source"]
        target = transaction["target"]
        source_meta = source + ".json"
        target_meta = target + ".json"
        source_identity = self._transaction_identity(transaction, "source_identity")
        source_meta_identity = self._transaction_identity(
            transaction, "source_meta_identity"
        )
        new_meta_identity = self._transaction_identity(transaction, "new_meta_identity")
        data_backup = transaction.get("data_backup")
        data_guard = transaction.get("data_guard")
        meta_guard = transaction.get("meta_guard")
        meta_backup_guard = transaction.get("meta_backup_guard")
        data_backups = (data_backup, data_guard)
        complete = True

        current_source = self._entry_identity(directory_fd, source)
        current_target = self._entry_identity(directory_fd, target)
        data_is_recovered = False
        if current_source == source_identity:
            data_is_recovered = True
            if current_target == source_identity:
                # The move created an alias instead of changing the source;
                # keep the public source and leave the alias untouched.
                pass
            elif current_target is not None:
                # A foreign target appeared before the move. The source is
                # still intact, so the marker can be retired safely without
                # ever deleting or overwriting that target.
                pass
        elif current_source is None:
            if current_target == source_identity:
                if not self._restore_noreplace(
                    directory_fd, target, source, source_identity
                ):
                    complete = False
                else:
                    data_is_recovered = True
            else:
                data_candidates: tuple[str, ...] = ()
                for backup_name in data_backups:
                    if backup_name is not None:
                        data_candidates += (backup_name,)
                data_candidates += self._recovery_names_for_identity(
                    directory_fd, source_identity
                )
                for backup_name in data_candidates:
                    if self._entry_identity(directory_fd, backup_name) != source_identity:
                        continue
                    if self._restore_noreplace(
                        directory_fd,
                        backup_name,
                        source,
                        source_identity,
                    ):
                        data_is_recovered = True
                        break
                if not data_is_recovered:
                    complete = False
        else:
            complete = False

        current_source_meta = self._entry_identity(directory_fd, source_meta)
        current_target_meta = self._entry_identity(directory_fd, target_meta)
        if current_target_meta == new_meta_identity:
            if not self._remove_expected(directory_fd, target_meta, new_meta_identity):
                complete = False
        current_source_meta = self._entry_identity(directory_fd, source_meta)
        if current_source_meta is None:
            meta_recovered = False
            meta_candidates: tuple[str, ...] = ()
            for backup_name in (transaction["meta_backup"], meta_backup_guard):
                if backup_name is not None:
                    meta_candidates += (backup_name,)
            meta_candidates += self._recovery_names_for_identity(
                directory_fd, source_meta_identity
            )
            for backup_name in meta_candidates:
                if self._entry_identity(directory_fd, backup_name) != source_meta_identity:
                    continue
                if self._restore_noreplace(
                    directory_fd,
                    backup_name,
                    source_meta,
                    source_meta_identity,
                ):
                    meta_recovered = True
                    break
            if not meta_recovered:
                complete = False
        elif current_source_meta != source_meta_identity:
            complete = False
        for backup_name in (transaction["meta_backup"], meta_backup_guard):
            if backup_name is None:
                continue
            backup_identity = self._entry_identity(directory_fd, backup_name)
            if backup_identity is not None and backup_identity != source_meta_identity:
                complete = False

        for temp_name in (transaction["meta_temp"], meta_guard):
            if temp_name is None:
                continue
            current_temp = self._entry_identity(directory_fd, temp_name)
            if current_temp == new_meta_identity:
                if not self._remove_expected(directory_fd, temp_name, new_meta_identity):
                    complete = False
            elif current_temp is not None:
                complete = False

        current_source_meta = self._entry_identity(directory_fd, source_meta)
        if current_source_meta != source_meta_identity:
            complete = False
        # A foreign target sidecar is intentionally left untouched. It is not
        # part of the transaction and will remain an ignored orphan if needed.

        recovery_handles = []
        recovery_to_keep: set[str] = set()
        source_pair_check = lambda: self._rename_source_pair_is_intact(
            directory_fd, transaction
        )
        if complete:
            with self._cleanup_pair_guard(
                source_pair_check
            ) as recovery_handles:
                if data_guard is not None:
                    if not self._rename_source_pair_is_intact(directory_fd, transaction):
                        complete = False
                    elif not self._remove_expected(directory_fd, data_guard, source_identity):
                        complete = False
                    elif not self._rename_source_pair_is_intact(directory_fd, transaction):
                        complete = False
                if complete and meta_guard is not None:
                    if not self._rename_source_pair_is_intact(directory_fd, transaction):
                        complete = False
                    elif not self._remove_expected(
                        directory_fd, meta_guard, new_meta_identity
                    ):
                        complete = False
                    elif not self._rename_source_pair_is_intact(directory_fd, transaction):
                        complete = False
                if complete and data_backup is not None:
                    if not self._rename_source_pair_is_intact(directory_fd, transaction):
                        complete = False
                    else:
                        current_backup = self._entry_identity(directory_fd, data_backup)
                        if current_backup == source_identity:
                            if not self._remove_expected(
                                directory_fd, data_backup, source_identity
                            ):
                                complete = False
                            elif not self._rename_source_pair_is_intact(
                                directory_fd, transaction
                            ):
                                complete = False
                        elif current_backup is not None:
                            complete = False
                if complete:
                    for backup_name in (transaction["meta_backup"], meta_backup_guard):
                        if backup_name is None:
                            continue
                        if not self._rename_source_pair_is_intact(directory_fd, transaction):
                            complete = False
                            break
                        current_backup = self._entry_identity(directory_fd, backup_name)
                        if current_backup == source_meta_identity:
                            if not self._remove_expected(
                                directory_fd, backup_name, source_meta_identity
                            ):
                                complete = False
                                break
                        elif current_backup is not None:
                            complete = False
                            break
                if complete:
                    marker_identity = self._journal_identity(directory_fd, marker_name)
                    if not self._remove_expected(directory_fd, marker_name, marker_identity):
                        complete = False
                    elif not self._rename_source_pair_is_intact(directory_fd, transaction):
                        complete = False
        if not complete and not self._rename_source_pair_is_intact(directory_fd, transaction):
            restored = self._restore_rename_source_pair_from_handles(
                directory_fd,
                transaction,
                recovery_handles,
            )
            if restored and self._rename_source_pair_is_intact(directory_fd, transaction):
                complete = self._rollback_rename_transaction(
                    directory_fd,
                    transaction,
                    marker_name,
                )
        if not complete and not self._rename_source_pair_is_intact(directory_fd, transaction):
            try:
                self._preserve_prepared_rename(directory_fd, transaction, marker_name)
            except (DestinationError, OSError):
                log.warning("cannot preserve rename journal: %s", marker_name)
        if not self._rename_source_pair_is_intact(directory_fd, transaction):
            recovery_to_keep.update(
                self._retain_recovery_candidates(
                    directory_fd,
                    recovery_handles,
                    (
                        (data_backup, source_identity),
                        (data_guard, source_identity),
                        (transaction.get("meta_backup"), source_meta_identity),
                        (meta_backup_guard, source_meta_identity),
                    ),
                )
            )
        closed = self._close_recovery_handles(
            directory_fd,
            recovery_handles,
            keep=recovery_to_keep,
            pair_check=source_pair_check,
        )
        if not closed:
            complete = False
            recovery_to_keep.update(
                self._retain_recovery_candidates(
                    directory_fd,
                    recovery_handles,
                    (
                        (data_backup, source_identity),
                        (data_guard, source_identity),
                        (transaction.get("meta_backup"), source_meta_identity),
                        (meta_backup_guard, source_meta_identity),
                    ),
                )
            )
            try:
                self._preserve_prepared_rename(directory_fd, transaction, marker_name)
            except (DestinationError, OSError):
                log.warning("cannot preserve rename journal: %s", marker_name)
        if complete:
            self._remove_anonymous_recovery(
                directory_fd,
                source_identity,
                source_meta_identity,
            )
            self._fsync_directory(directory_fd)
        return complete

    def _cleanup_committed_rename(
        self,
        directory_fd: int,
        transaction: dict,
        marker_name: str,
        commit_name: str,
    ) -> bool:
        pair_check = lambda: self._rename_public_pair_is_intact(
            directory_fd, transaction
        )
        recovery_handles = []
        recovery_to_keep: set[str] = set()
        result = False
        try:
            with self._cleanup_pair_guard(
                pair_check,
                unguarded_names=(transaction.get("data_backup"), marker_name, commit_name),
            ) as recovery_handles:
                result = self._cleanup_committed_rename_body(
                    directory_fd,
                    transaction,
                    marker_name,
                    commit_name,
                )
        finally:
            if not pair_check():
                restored = self._restore_rename_public_pair_from_handles(
                    directory_fd,
                    transaction,
                    recovery_handles,
                )
                if restored:
                    extra_handles = []
                    try:
                        with self._cleanup_pair_guard(
                            pair_check,
                            unguarded_names=(
                                transaction.get("data_backup"),
                                marker_name,
                                commit_name,
                            ),
                        ) as extra_handles:
                            result = self._cleanup_committed_rename_body(
                                directory_fd,
                                transaction,
                                marker_name,
                                commit_name,
                            )
                    except (DestinationError, OSError):
                        pass
                    finally:
                        recovery_handles.extend(extra_handles)
            if not pair_check():
                result = False
                recovery_to_keep.update(
                    self._retain_recovery_candidates(
                        directory_fd,
                        recovery_handles,
                        self._rename_recovery_candidates(transaction),
                    )
                )
            if not result or not pair_check():
                try:
                    self._preserve_committed_transaction(
                        directory_fd,
                        transaction,
                        commit_name,
                    )
                except (DestinationError, OSError):
                    log.warning("cannot preserve rename journal: %s", commit_name)
            closed = self._close_recovery_handles(
                directory_fd,
                recovery_handles,
                keep=recovery_to_keep,
                pair_check=pair_check,
            )
            if not closed:
                result = False
                recovery_to_keep.update(
                    self._retain_recovery_candidates(
                        directory_fd,
                        recovery_handles,
                        self._rename_recovery_candidates(transaction),
                    )
                )
                try:
                    self._preserve_committed_transaction(
                        directory_fd,
                        transaction,
                        commit_name,
                    )
                except (DestinationError, OSError):
                    log.warning("cannot preserve rename journal: %s", commit_name)
        return result

    def _cleanup_committed_rename_body(
        self,
        directory_fd: int,
        transaction: dict,
        marker_name: str,
        commit_name: str,
    ) -> bool:
        """Remove artifacts from a durably published rename."""
        target = transaction["target"]
        source = transaction["source"]
        data_backup = transaction.get("data_backup")
        source_identity = self._transaction_identity(transaction, "source_identity")
        source_meta_identity = self._transaction_identity(
            transaction, "source_meta_identity"
        )
        new_meta_identity = self._transaction_identity(transaction, "new_meta_identity")
        recovery_cleanup: list[tuple[str, tuple[int, int]]] = []
        recovery_cleanup_names: set[str] = set()
        recovery_names_by_key: dict[str, tuple[str, ...]] = {}
        for key in ("source_identity", "source_meta_identity", "new_meta_identity"):
            expected = self._transaction_identity(transaction, key)
            names = self._recovery_names_for_identity(directory_fd, expected)
            recovery_names_by_key[key] = names
            for name in names:
                if name not in recovery_cleanup_names and expected is not None:
                    recovery_cleanup_names.add(name)
                    recovery_cleanup.append((name, expected))
        for public_name, backups, key in (
            (
                target,
                (data_backup, transaction.get("data_guard")),
                "source_identity",
            ),
            (
                target + ".json",
                (transaction["meta_temp"], transaction.get("meta_guard")),
                "new_meta_identity",
            ),
        ):
            if self._entry_identity(directory_fd, public_name) is not None:
                continue
            expected = self._transaction_identity(transaction, key)
            candidates = list(backups)
            candidates.extend(recovery_names_by_key[key])
            for backup_name in candidates:
                if backup_name is None:
                    continue
                if self._entry_identity_any(directory_fd, backup_name) != expected:
                    continue
                try:
                    self._link_expected(
                        directory_fd,
                        backup_name,
                        public_name,
                        expected,
                    )
                except (DestinationError, OSError):
                    return False
                break
        if not self._rename_public_pair_is_intact(
            directory_fd,
            transaction,
        ):
            # Do not discard the old sidecar or backups while the public pair
            # does not exactly describe the committed rename.
            return False
        cleanup_items = [
            (transaction["meta_backup"], source_meta_identity),
            (transaction["meta_temp"], new_meta_identity),
        ]
        meta_backup_guard = transaction.get("meta_backup_guard")
        if meta_backup_guard is not None:
            cleanup_items.append((meta_backup_guard, source_meta_identity))
        if data_backup is not None:
            cleanup_items.append((data_backup, source_identity))
        cleanup_items.extend(recovery_cleanup)
        for name, expected in cleanup_items:
            if not self._rename_public_pair_is_intact(directory_fd, transaction):
                return False
            if not self._remove_expected(directory_fd, name, expected):
                return False
            if not self._rename_public_pair_is_intact(directory_fd, transaction):
                return False
        for name, expected in (
            (transaction.get("meta_guard"), new_meta_identity),
            (transaction.get("data_guard"), source_identity),
        ):
            if name is None:
                continue
            if not self._remove_expected(directory_fd, name, expected):
                return False
            if not self._rename_public_pair_is_intact(directory_fd, transaction):
                return False
        if not self._rename_public_pair_is_intact(directory_fd, transaction):
            return False
        if not self._remove_expected(
            directory_fd,
            marker_name,
            self._journal_identity(directory_fd, marker_name),
        ):
            return False
        if not self._rename_public_pair_is_intact(directory_fd, transaction):
            return False
        if not self._remove_expected(
            directory_fd,
            commit_name,
            self._journal_identity(directory_fd, commit_name),
        ):
            return False
        if not self._rename_public_pair_is_intact(directory_fd, transaction):
            return False
        self._fsync_directory(directory_fd)
        return True

    def _rename_public_pair_is_intact(
        self,
        directory_fd: int,
        transaction: dict,
    ) -> bool:
        try:
            return (
                self._entry_identity(directory_fd, transaction["target"])
                == self._transaction_identity(transaction, "source_identity")
                and self._entry_identity(directory_fd, transaction["target"] + ".json")
                == self._transaction_identity(transaction, "new_meta_identity")
                and self._entry_identity(directory_fd, transaction["source"]) is None
                and self._entry_identity(directory_fd, transaction["source"] + ".json") is None
            )
        except (DestinationError, OSError):
            return False

    def _rename_source_pair_is_intact(
        self,
        directory_fd: int,
        transaction: dict,
    ) -> bool:
        try:
            return (
                self._entry_identity(directory_fd, transaction["source"])
                == self._transaction_identity(transaction, "source_identity")
                and self._entry_identity(directory_fd, transaction["source"] + ".json")
                == self._transaction_identity(transaction, "source_meta_identity")
            )
        except (DestinationError, OSError):
            return False

    def _recover_renames(self, directory_fd: DirectoryHandle, entries) -> set[str]:
        markers: dict[str, tuple[str, dict]] = {}
        commits: dict[str, tuple[str, dict]] = {}
        protected: set[str] = set()
        for entry in entries:
            token = _rename_token(entry.name)
            if token is None:
                continue
            protected.add(entry.name)
            self._remember_journal_entry(directory_fd, entry)
            try:
                transaction = self._parse_rename_transaction(
                    entry.name,
                    self._read_meta(directory_fd, entry.name),
                )
            except (DestinationError, ValueError, TypeError, KeyError):
                log.warning("invalid rename marker, preserved: %s", entry.name)
                continue
            protected.update(
                {
                    transaction["source"],
                    transaction["source"] + ".json",
                    transaction["target"],
                    transaction["target"] + ".json",
                    transaction["meta_temp"],
                    transaction["meta_backup"],
                }
            )
            if transaction.get("data_backup") is not None:
                protected.add(transaction["data_backup"])
            if transaction.get("data_guard") is not None:
                protected.add(transaction["data_guard"])
            if transaction.get("meta_guard") is not None:
                protected.add(transaction["meta_guard"])
            if transaction.get("meta_backup_guard") is not None:
                protected.add(transaction["meta_backup_guard"])
            if _RENAME_MARKER_RE.fullmatch(entry.name):
                markers[token] = (entry.name, transaction)
            else:
                commits[token] = (entry.name, transaction)

        for token, (commit_name, transaction) in commits.items():
            marker_name = f".pbrename-{token}.json"
            try:
                if not self._cleanup_committed_rename(
                    directory_fd,
                    transaction,
                    marker_name,
                    commit_name,
                ):
                    log.warning("deferred rename cleanup: %s", commit_name)
            except (DestinationError, OSError):
                log.warning("committed rename recovery failed: %s", commit_name)

        for token, (marker_name, transaction) in markers.items():
            if token in commits:
                continue
            try:
                if not self._rollback_rename_transaction(
                    directory_fd, transaction, marker_name
                ):
                    log.warning("deferred rename rollback: %s", marker_name)
            except (DestinationError, OSError):
                log.warning("rename rollback failed: %s", marker_name)
        return protected

    def _rollback_transaction(self, directory_fd: int, transaction: dict, marker_name: str) -> bool:
        target = transaction["target"]
        meta_name = target + ".json"
        target_identity = self._transaction_identity(transaction, "target_identity")
        meta_identity = self._transaction_identity(transaction, "meta_identity")
        new_data_identity = self._transaction_identity(transaction, "new_data_identity")
        new_meta_identity = self._transaction_identity(transaction, "new_meta_identity")
        complete = True

        for name, expected in (
            (target, new_data_identity),
            (meta_name, new_meta_identity),
        ):
            actual = self._entry_identity(directory_fd, name)
            if actual == expected and not self._remove_expected(directory_fd, name, expected):
                complete = False

        for backup, name, expected in (
            (transaction["data_backup"], target, target_identity),
            (transaction["meta_backup"], meta_name, meta_identity),
        ):
            current_identity = self._entry_identity(directory_fd, name)
            if expected is None:
                # A fresh install has no old pair; a backup present with no
                # recorded identity is a foreign leftover and must block
                # marker retirement.
                if backup is not None and self._entry_identity(directory_fd, backup) is not None:
                    complete = False
                continue
            backup_candidates: tuple[str, ...] = ()
            if backup is not None:
                backup_candidates = (backup,)
            backup_candidates += self._recovery_names_for_identity(directory_fd, expected)
            restored = False
            for backup_name in backup_candidates:
                backup_identity = self._entry_identity(directory_fd, backup_name)
                if backup_identity is None:
                    continue
                if backup_identity != expected:
                    complete = False
                    continue
                if current_identity is None:
                    try:
                        self._link_expected(directory_fd, backup_name, name, expected)
                    except (DestinationError, OSError):
                        complete = False
                    restored = True
                    break
                elif current_identity == expected:
                    # Keep the backup until the prepared marker has been retired;
                    # it is the last durable copy during that final race window.
                    restored = True
                    break
                else:
                    complete = False
                    break
            if not restored and current_identity != expected:
                complete = False

        for name, expected in (
            (transaction["data_temp"], new_data_identity),
            (transaction.get("data_guard"), new_data_identity),
            (transaction["meta_temp"], new_meta_identity),
            (transaction.get("meta_guard"), new_meta_identity),
        ):
            if name is None:
                continue
            actual = self._entry_identity(directory_fd, name)
            if actual is None:
                continue
            if actual == expected:
                if not self._remove_expected(directory_fd, name, expected):
                    complete = False
            else:
                complete = False

        current_target = self._entry_identity(directory_fd, target)
        current_meta = self._entry_identity(directory_fd, meta_name)
        if target_identity is None and meta_identity is None:
            # A foreign entry may have appeared before either new entry was
            # installed. It is not part of this transaction and must not keep
            # a permanent marker or block future use of the name.
            pass
        elif current_target != target_identity or current_meta != meta_identity:
            # A foreign public entry or a missing old pair needs manual or
            # later reconciliation; never retire the marker for that state.
            complete = False

        if target_identity is None and meta_identity is None:
            if complete:
                if not self._remove_expected(
                    directory_fd,
                    marker_name,
                    self._journal_identity(directory_fd, marker_name),
                ):
                    complete = False
                else:
                    self._fsync_directory(directory_fd)
            return complete

        old_pair_check = lambda: (
            self._entry_identity(directory_fd, target) == target_identity
            and self._entry_identity(directory_fd, meta_name) == meta_identity
        )
        recovery_handles = []
        recovery_to_keep: set[str] = set()
        if complete:
            with self._cleanup_pair_guard(old_pair_check) as recovery_handles:
                if not old_pair_check():
                    complete = False
                if complete:
                    for backup, expected in (
                        (transaction["data_backup"], target_identity),
                        (transaction["meta_backup"], meta_identity),
                    ):
                        if not old_pair_check():
                            complete = False
                            break
                        if not self._remove_expected(directory_fd, backup, expected):
                            complete = False
                            break
                        if not old_pair_check():
                            complete = False
                            break
                if complete and not self._remove_expected(
                    directory_fd,
                    marker_name,
                    self._journal_identity(directory_fd, marker_name),
                ):
                    complete = False
                if complete and not old_pair_check():
                    complete = False
        if not old_pair_check():
            recovery_to_keep.update(
                self._retain_recovery_candidates(
                    directory_fd,
                    recovery_handles,
                    (
                        (transaction.get("data_backup"), target_identity),
                        (transaction.get("meta_backup"), meta_identity),
                    ),
                )
            )
        if not complete:
            try:
                self._preserve_prepared_transaction(directory_fd, transaction, marker_name)
            except (DestinationError, OSError):
                log.warning("cannot preserve transaction journal: %s", marker_name)
        closed = self._close_recovery_handles(
            directory_fd,
            recovery_handles,
            keep=recovery_to_keep,
            pair_check=old_pair_check,
        )
        if not closed:
            complete = False
            recovery_to_keep.update(
                self._retain_recovery_candidates(
                    directory_fd,
                    recovery_handles,
                    (
                        (transaction.get("data_backup"), target_identity),
                        (transaction.get("meta_backup"), meta_identity),
                    ),
                )
            )
            try:
                self._preserve_prepared_transaction(directory_fd, transaction, marker_name)
            except (DestinationError, OSError):
                log.warning("cannot preserve transaction journal: %s", marker_name)
        if complete:
            self._remove_anonymous_recovery(
                directory_fd,
                target_identity,
                meta_identity,
            )
            self._fsync_directory(directory_fd)
        return complete

    def _cleanup_committed_transaction(
        self,
        directory_fd: DirectoryHandle,
        transaction: dict,
        marker_name: str,
        commit_name: str,
    ) -> bool:
        pair_check = lambda: self._transaction_public_pair_is_intact(
            directory_fd, transaction
        )
        recovery_handles = []
        recovery_to_keep: set[str] = set()
        result = False
        try:
            with self._cleanup_pair_guard(
                pair_check,
                unguarded_names=(transaction.get("data_backup"), marker_name, commit_name),
            ) as recovery_handles:
                result = self._cleanup_committed_transaction_body(
                    directory_fd,
                    transaction,
                    marker_name,
                    commit_name,
                )
        finally:
            if not pair_check():
                restored = self._restore_transaction_public_pair_from_handles(
                    directory_fd,
                    transaction,
                    recovery_handles,
                )
                if restored:
                    extra_handles = []
                    try:
                        with self._cleanup_pair_guard(
                            pair_check,
                            unguarded_names=(
                                transaction.get("data_backup"),
                                marker_name,
                                commit_name,
                            ),
                        ) as extra_handles:
                            result = self._cleanup_committed_transaction_body(
                                directory_fd,
                                transaction,
                                marker_name,
                                commit_name,
                            )
                    except (DestinationError, OSError):
                        pass
                    finally:
                        recovery_handles.extend(extra_handles)
            if not pair_check():
                result = False
                recovery_to_keep.update(
                    self._retain_recovery_candidates(
                        directory_fd,
                        recovery_handles,
                        self._transaction_recovery_candidates(transaction),
                    )
                )
            if not result or not pair_check():
                try:
                    self._preserve_committed_transaction(
                        directory_fd,
                        transaction,
                        commit_name,
                    )
                except (DestinationError, OSError):
                    log.warning("cannot preserve transaction journal: %s", commit_name)
            closed = self._close_recovery_handles(
                directory_fd,
                recovery_handles,
                keep=recovery_to_keep,
                pair_check=pair_check,
            )
            if not closed:
                result = False
                recovery_to_keep.update(
                    self._retain_recovery_candidates(
                        directory_fd,
                        recovery_handles,
                        self._transaction_recovery_candidates(transaction),
                    )
                )
                try:
                    self._preserve_committed_transaction(
                        directory_fd,
                        transaction,
                        commit_name,
                    )
                except (DestinationError, OSError):
                    log.warning("cannot preserve transaction journal: %s", commit_name)
        return result

    def _cleanup_committed_transaction_body(
        self,
        directory_fd: int,
        transaction: dict,
        marker_name: str,
        commit_name: str,
    ) -> bool:
        recovery_cleanup: list[tuple[str, str]] = []
        recovery_cleanup_names: set[str] = set()
        recovery_names_by_key: dict[str, tuple[str, ...]] = {}
        for key in (
            "target_identity",
            "meta_identity",
            "new_data_identity",
            "new_meta_identity",
        ):
            expected = self._transaction_identity(transaction, key)
            names = self._recovery_names_for_identity(directory_fd, expected)
            recovery_names_by_key[key] = names
            for name in names:
                if name not in recovery_cleanup_names:
                    recovery_cleanup_names.add(name)
                    recovery_cleanup.append((name, key))
        for public_name, backup_name, key in (
            (
                transaction["target"],
                (transaction["data_temp"], transaction.get("data_guard")),
                "new_data_identity",
            ),
            (
                transaction["target"] + ".json",
                (transaction["meta_temp"], transaction.get("meta_guard")),
                "new_meta_identity",
            ),
        ):
            if self._entry_identity(directory_fd, public_name) is not None:
                continue
            expected = self._transaction_identity(transaction, key)
            candidates = list(
                backup_name if isinstance(backup_name, tuple) else (backup_name,)
            )
            candidates.extend(recovery_names_by_key[key])
            for candidate in candidates:
                if candidate is None:
                    continue
                if self._entry_identity_any(directory_fd, candidate) != expected:
                    continue
                try:
                    self._link_expected(directory_fd, candidate, public_name, expected)
                except (DestinationError, OSError):
                    return False
                break
        if not self._transaction_public_pair_is_intact(directory_fd, transaction):
            # Never discard the old pair while the public names no longer
            # point at the committed replacement.
            return False
        cleanup_items = [
            (transaction["data_backup"], "target_identity"),
            (transaction["meta_backup"], "meta_identity"),
            (transaction["meta_temp"], "new_meta_identity"),
        ]
        cleanup_items.extend(recovery_cleanup)
        for name, key in cleanup_items:
            if not self._transaction_public_pair_is_intact(directory_fd, transaction):
                return False
            expected = self._transaction_identity(transaction, key)
            if expected is None:
                continue
            if not self._remove_expected(directory_fd, name, expected):
                return False
            if not self._transaction_public_pair_is_intact(directory_fd, transaction):
                return False
        for name, key in (
            (transaction.get("meta_guard"), "new_meta_identity"),
            (transaction["data_temp"], "new_data_identity"),
            (transaction.get("data_guard"), "new_data_identity"),
        ):
            if not self._transaction_public_pair_is_intact(directory_fd, transaction):
                return False
            if name is None:
                continue
            expected = self._transaction_identity(transaction, key)
            if expected is None:
                continue
            if not self._remove_expected(directory_fd, name, expected):
                return False
            if not self._transaction_public_pair_is_intact(directory_fd, transaction):
                return False
        if not self._transaction_public_pair_is_intact(directory_fd, transaction):
            return False
        if not self._remove_expected(
            directory_fd,
            marker_name,
            self._journal_identity(directory_fd, marker_name),
        ):
            return False
        if not self._transaction_public_pair_is_intact(directory_fd, transaction):
            return False
        if not self._remove_expected(
            directory_fd,
            commit_name,
            self._journal_identity(directory_fd, commit_name),
        ):
            return False
        if not self._transaction_public_pair_is_intact(directory_fd, transaction):
            return False
        self._fsync_directory(directory_fd)
        return True

    def _transaction_public_pair_is_intact(
        self,
        directory_fd: int,
        transaction: dict,
    ) -> bool:
        try:
            return (
                self._entry_identity(directory_fd, transaction["target"])
                == self._transaction_identity(transaction, "new_data_identity")
                and self._entry_identity(directory_fd, transaction["target"] + ".json")
                == self._transaction_identity(transaction, "new_meta_identity")
            )
        except (DestinationError, OSError):
            return False

    def _recover_transactions(self, directory_fd: DirectoryHandle, entries) -> set[str]:
        markers: dict[str, tuple[str, dict]] = {}
        commits: dict[str, tuple[str, dict]] = {}
        protected: set[str] = set()
        for entry in entries:
            token = _txn_token(entry.name)
            if token is None:
                continue
            protected.add(entry.name)
            self._remember_journal_entry(directory_fd, entry)
            try:
                raw = self._read_meta(directory_fd, entry.name)
                transaction = self._parse_transaction(entry.name, raw)
            except (DestinationError, ValueError, TypeError, KeyError):
                log.warning("invalid transaction marker, preserved: %s", entry.name)
                continue
            for key in (
                "data_temp",
                "data_guard",
                "meta_temp",
                "meta_guard",
                "data_backup",
                "meta_backup",
                "target",
            ):
                if transaction.get(key) is None:
                    continue
                protected.add(transaction[key])
            if _TXN_MARKER_RE.fullmatch(entry.name):
                markers[token] = (entry.name, transaction)
            else:
                commits[token] = (entry.name, transaction)

        for token, (commit_name, transaction) in commits.items():
            marker_name = f".pbtxn-{token}.json"
            try:
                self._cleanup_committed_transaction(
                    directory_fd,
                    transaction,
                    marker_name,
                    commit_name,
                )
            except (DestinationError, OSError):
                log.warning("committed transaction recovery failed: %s", commit_name)

        for token, (marker_name, transaction) in markers.items():
            if token in commits:
                continue
            try:
                self._rollback_transaction(directory_fd, transaction, marker_name)
            except (DestinationError, OSError):
                log.warning("transaction rollback failed: %s", marker_name)
        return protected

    def _fsync_directory(self, directory_fd: DirectoryHandle) -> None:
        try:
            self._fs.flush_directory(directory_fd)
        except (OSError, UnsupportedFilesystemError) as exc:
            raise DestinationError(f"cannot synchronize directory: {exc}") from exc

    def _meta_name(self, filename: str) -> str:
        return filename + ".json"

    def _read_meta(self, directory_fd: DirectoryHandle, name: str) -> dict:
        """Read a sidecar from a descriptor within the configured budget."""
        try:
            with self._open_file(directory_fd, name, "rb") as fh:
                max_bytes = self.limits.max_metadata_bytes
                encoded = fh.read(max_bytes + 1) if max_bytes is not None else fh.read()
        except FileNotFoundError:
            raise
        except DestinationError:
            raise
        except OSError as exc:
            raise DestinationError(f"cannot read sidecar {name!r}") from exc
        if (
            self.limits.max_metadata_bytes is not None
            and len(encoded) > self.limits.max_metadata_bytes
        ):
            raise DestinationError(f"sidecar is too large: {name!r}")
        try:
            raw = json.loads(encoded.decode("utf-8"))
        except (UnicodeError, ValueError, RecursionError) as exc:
            raise DestinationError(f"sidecar is unreadable: {name!r}") from exc
        if not isinstance(raw, dict):
            raise DestinationError(f"invalid sidecar: {name!r}")
        return raw

    def _validated_item(
        self,
        raw: dict,
        filename: str,
        actual_size: int | None = None,
    ) -> StoredImage:
        """Validate a sidecar before any read, deletion, or replacement."""
        if not _meta_keys_ok(raw) or raw.get("filename") != filename:
            raise ValueError("inconsistent sidecar")
        created_at = datetime.fromisoformat(raw["created_at"])
        if created_at.tzinfo is None or created_at.utcoffset() is None:
            raise ValueError("date has no timezone")
        created_at = created_at.astimezone(timezone.utc)
        width = raw["width"]
        height = raw["height"]
        size = raw["size"]
        fmt = raw["format"]
        kind = raw.get("kind", "image")
        if kind not in ("image", "text", "binary"):
            raise ValueError("invalid kind")
        mime = raw.get("mime")
        if mime is None:
            mime = mime_for(fmt) if kind == "image" else "application/octet-stream"
        if (
            not isinstance(mime, str)
            or not _MIME_RE.fullmatch(mime)
            or (
                self.limits.max_mime_length is not None
                and len(mime) > self.limits.max_mime_length
            )
        ):
            raise ValueError("invalid MIME type")
        if isinstance(size, bool) or not isinstance(size, int):
            raise ValueError("invalid numeric types")
        if kind == "image":
            if any(isinstance(v, bool) or not isinstance(v, int) for v in (width, height)):
                raise ValueError("invalid numeric types")
            if width < 1 or height < 1 or (
                self.limits.max_image_dimension is not None
                and (
                    width > self.limits.max_image_dimension
                    or height > self.limits.max_image_dimension
                )
            ):
                raise ValueError("invalid dimensions")
            if (
                self.max_image_pixels is not None
                and width * height > self.max_image_pixels
            ):
                raise ValueError("inconsistent metadata")
            if fmt not in FORMATS:
                raise ValueError("invalid format")
            if mime != mime_for(fmt):
                raise ValueError("inconsistent image MIME type")
        elif kind == "text":
            if width is not None or height is not None or fmt is not None:
                raise ValueError("unexpected dimensions or format")
            if not (mime.startswith("text/") or mime in _TEXT_MIMES):
                raise ValueError("invalid text MIME type")
        else:
            if width is not None or height is not None:
                raise ValueError("unexpected dimensions")
            if fmt is not None:
                raise ValueError("unexpected format")
            if mime != "application/octet-stream":
                raise ValueError("invalid binary MIME type")
        if size < 0 or (actual_size is not None and size != actual_size):
            raise ValueError("inconsistent metadata")
        comment = validate_comment(
            raw.get("comment", ""),
            max_length=self.limits.max_comment_length,
            max_bytes=self.limits.max_comment_bytes,
        )
        return StoredImage(filename, created_at, width, height, size, fmt, kind, mime, comment)

    def _require_owned(
        self,
        directory_fd: DirectoryHandle,
        filename: str,
        *,
        allow_stale_sidecar: bool = False,
    ) -> tuple[FileHandle, tuple[int, int]]:
        """Operate only on a file with a present regular sidecar."""
        if not self._valid_filename(filename):
            raise DestinationError(f"invalid filename: {filename!r}")
        meta_name = self._meta_name(filename)
        file_handle = None
        try:
            file_handle = self._open_file(directory_fd, filename, "rb")
            # Keep the opened inode while checking the sidecar. A replacement
            # of the public name cannot redirect read() to a foreign inode.
            meta_identity = self._entry_identity(directory_fd, meta_name)
            raw = self._read_meta(directory_fd, meta_name)
            if self._entry_identity(directory_fd, meta_name) != meta_identity:
                raise StorageConflictError(f"sidecar changed during operation: {filename!r}")
            item = self._validated_item(raw, filename)
            if not allow_stale_sidecar and file_handle.size != item.size:
                raise ValueError("inconsistent size")
            file_handle.seek(0)
            if meta_identity is None:
                raise UnknownImageError(f"unknown Pasteberth file: {filename!r}")
            return file_handle, meta_identity
        except FileNotFoundError as exc:
            if file_handle is not None and not file_handle.closed:
                file_handle.close()
            raise UnknownImageError(f"unknown Pasteberth file: {filename!r}") from exc
        except (DestinationError, OSError, TypeError, ValueError, KeyError) as exc:
            if file_handle is not None and not file_handle.closed:
                file_handle.close()
            raise DestinationError(f"sidecar is unreadable for {filename!r}") from exc

    def _generate_name(self, ext: str) -> str:
        stamp = datetime.now().astimezone().strftime("%Y-%m-%d_%H-%M-%S")
        return f"{stamp}_{secrets.token_hex(3)}{ext}"

    def _write_meta_atomic(self, directory_fd: DirectoryHandle, meta: dict) -> None:
        target = meta["filename"] + ".json"
        temp_name = self._write_meta_temp(directory_fd, meta)
        temp_identity = self._entry_identity(directory_fd, temp_name)
        if temp_identity is None:
            raise DestinationError(f"temporary sidecar disappeared: {target!r}")
        try:
            self._move_expected(directory_fd, temp_name, target, temp_identity)
            self._fsync_directory(directory_fd)
        except BaseException:
            try:
                self._remove_expected(directory_fd, target, temp_identity)
                self._remove_expected(directory_fd, temp_name, temp_identity)
            except (DestinationError, OSError):
                pass
            raise

    def _write_meta_temp(self, directory_fd: DirectoryHandle, meta: dict) -> str:
        temp_name = f".pbmeta-{secrets.token_hex(12)}.tmp"
        file_handle = None
        temp_identity = None
        try:
            file_handle = self._create_file(
                directory_fd,
                temp_name,
                mode="w",
            )
            temp_identity = file_handle.identity
            with file_handle as fh:
                json.dump(meta, fh, ensure_ascii=False, separators=(",", ":"))
                fh.sync()
            return temp_name
        except BaseException:
            if file_handle is not None and not file_handle.closed:
                try:
                    file_handle.close()
                except OSError:
                    pass
            if temp_identity is not None:
                try:
                    self._remove_expected(directory_fd, temp_name, temp_identity)
                except (DestinationError, OSError):
                    pass
            raise

    def _write_data_temp(self, directory_fd: DirectoryHandle, data: bytes) -> str:
        temp_name = f".pbdata-{secrets.token_hex(12)}.tmp"
        file_handle = None
        temp_identity = None
        try:
            file_handle = self._create_file(
                directory_fd,
                temp_name,
                mode="wb",
            )
            temp_identity = file_handle.identity
            with file_handle as fh:
                fh.write(data)
                fh.sync()
            return temp_name
        except BaseException:
            if file_handle is not None and not file_handle.closed:
                try:
                    file_handle.close()
                except OSError:
                    pass
            if temp_identity is not None:
                try:
                    self._remove_expected(directory_fd, temp_name, temp_identity)
                except (DestinationError, OSError):
                    pass
            raise

    def _install_new(
        self,
        directory_fd: DirectoryHandle,
        temp_name: str,
        target_name: str,
    ) -> None:
        """Install a temporary file without replacing a concurrent creation."""
        expected = self._entry_identity(directory_fd, temp_name)
        if expected is None:
            raise DestinationError(f"temporary file disappeared during write: {target_name!r}")
        self._move_expected(directory_fd, temp_name, target_name, expected)

    @staticmethod
    def _entry_exists(directory_fd: DirectoryHandle, name: str) -> bool:
        try:
            info = platform_fs().entry_info(directory_fd, name)
        except FileNotFoundError:
            return False
        except (OSError, UnsupportedFilesystemError) as exc:
            raise DestinationError(f"cannot inspect {name!r}: {exc}") from exc
        # Occupied symlinks and non-regular entries are foreign conflicts too;
        # callers must not try to open or replace them.
        return info is not None

    @staticmethod
    def _entry_identity(
        directory_fd: DirectoryHandle,
        name: str,
    ) -> tuple[int, int] | None:
        try:
            return platform_fs().identity(directory_fd, name)
        except FileNotFoundError:
            return None
        except (OSError, UnsupportedFilesystemError) as exc:
            raise DestinationError(f"cannot inspect {name!r}: {exc}") from exc

    @staticmethod
    def _stored_item_and_meta(
        data: bytes,
        info: ContentInfo,
        filename: str,
    ) -> tuple[StoredImage, dict]:
        created_at = datetime.now(timezone.utc)
        stored = StoredImage(
            filename=filename,
            created_at=created_at,
            width=info.width,
            height=info.height,
            size=len(data),
            fmt=info.fmt,
            kind=info.kind,
            mime=info.mime,
        )
        return stored, {
            "filename": filename,
            "created_at": created_at.isoformat(timespec="microseconds"),
            "width": stored.width,
            "height": stored.height,
            "size": stored.size,
            "format": stored.fmt,
            "kind": stored.kind,
            "mime": stored.mime,
            "comment": stored.comment,
        }

    def _adopt_named(
        self,
        directory_fd: DirectoryHandle,
        data: bytes,
        info: ContentInfo,
        filename: str,
    ) -> StoredImage:
        """Add a sidecar for an existing file without rewriting its data."""
        meta_name = self._meta_name(filename)
        file_handle = None
        try:
            file_handle = self._open_file(directory_fd, filename, "rb")
            target_identity = file_handle.identity
            if file_handle.size != len(data):
                raise StorageConflictError(
                    f"file changed during adoption: {filename!r}"
                )
            file_handle.seek(0)
            offset = 0
            while chunk := file_handle.read(1024 * 1024):
                if data[offset:offset + len(chunk)] != chunk:
                    raise StorageConflictError(
                        f"file changed during adoption: {filename!r}"
                    )
                offset += len(chunk)
            if offset != len(data):
                raise StorageConflictError(
                    f"file changed during adoption: {filename!r}"
                )
            if self._entry_identity(directory_fd, filename) != target_identity:
                raise StorageConflictError(
                    f"file changed during adoption: {filename!r}"
                )
            if self._entry_exists(directory_fd, meta_name):
                raise StorageConflictError(
                    f"sidecar appeared during adoption: {filename!r}"
                )

            stored, meta = self._stored_item_and_meta(data, info, filename)
            self._write_meta_atomic(directory_fd, meta)
            meta_identity = self._entry_identity(directory_fd, meta_name)
            if meta_identity is None:
                raise StorageConflictError(
                    f"sidecar disappeared during adoption: {filename!r}"
                )
            try:
                target_still_matches = (
                    self._entry_identity(directory_fd, filename) == target_identity
                )
            except (DestinationError, OSError):
                target_still_matches = False
            if not target_still_matches:
                self._remove_expected(directory_fd, meta_name, meta_identity)
                raise StorageConflictError(
                    f"file changed during adoption: {filename!r}"
                )
            return stored
        except FileNotFoundError as exc:
            raise StorageConflictError(
                f"file disappeared during adoption: {filename!r}"
            ) from exc
        except UnsafeLinkError as exc:
            raise StorageConflictError(
                f"foreign file is not regular: {filename!r}"
            ) from exc
        finally:
            if file_handle is not None and not file_handle.closed:
                file_handle.close()

    def _save_named(
        self,
        directory_fd: DirectoryHandle,
        data: bytes,
        info: ContentInfo,
        filename: str,
        *,
        allow_replace: bool = False,
        adopt_existing: bool = False,
    ) -> StoredImage:
        meta_name = self._meta_name(filename)
        if filename in self._active_transaction_names(directory_fd):
            raise StorageConflictError(
                f"transaction in progress for filename: {filename!r}"
            )
        target_exists = self._entry_exists(directory_fd, filename)
        meta_exists = self._entry_exists(directory_fd, meta_name)
        target_identity: tuple[int, int] | None = None
        meta_identity: tuple[int, int] | None = None
        if target_exists and not meta_exists:
            if adopt_existing:
                return self._adopt_named(directory_fd, data, info, filename)
            # Foreign file: never overwrite it; expose a client conflict (409).
            raise StorageConflictError(
                f"foreign file present without sidecar: {filename!r}"
            )
        if meta_exists and not target_exists:
            # Orphan sidecar: inconsistent internal state, not a client conflict.
            raise DestinationError(f"orphan sidecar without file: {filename!r}")
        if target_exists:
            try:
                owned_file, meta_identity = self._require_owned(directory_fd, filename)
            except DestinationError as exc:
                # A target with a malformed or stale sidecar is not a managed
                # replacement candidate. Keep it intact and expose a conflict.
                raise StorageConflictError(
                    f"inconsistent file and sidecar: {filename!r}"
                ) from exc
            try:
                target_identity = owned_file.identity
            finally:
                owned_file.close()
            if not allow_replace:
                raise ReplacementRequiredError(
                    f"explicit replacement required for {filename!r}"
                )

        stored, meta = self._stored_item_and_meta(data, info, filename)
        data_temp = self._write_data_temp(directory_fd, data)
        data_temp_identity = self._entry_identity(directory_fd, data_temp)
        try:
            meta_temp = self._write_meta_temp(directory_fd, meta)
        except BaseException:
            try:
                if data_temp_identity is not None:
                    self._remove_expected(directory_fd, data_temp, data_temp_identity)
            except (DestinationError, OSError):
                pass
            raise
        meta_temp_identity = self._entry_identity(directory_fd, meta_temp)

        transaction = None
        marker_name = None
        commit_name = None
        commit_published = False
        if target_exists and (target_identity is None or meta_identity is None):
            for temp_name in (data_temp, meta_temp):
                try:
                    expected = data_temp_identity if temp_name == data_temp else meta_temp_identity
                    if expected is not None:
                        self._remove_expected(directory_fd, temp_name, expected)
                except (DestinationError, OSError):
                    pass
            raise StorageConflictError(f"target file or sidecar disappeared: {filename!r}")
        new_data_identity = data_temp_identity
        new_meta_identity = meta_temp_identity
        if new_data_identity is None or new_meta_identity is None:
            for temp_name in (data_temp, meta_temp):
                try:
                    expected = data_temp_identity if temp_name == data_temp else meta_temp_identity
                    if expected is not None:
                        self._remove_expected(directory_fd, temp_name, expected)
                except (DestinationError, OSError):
                    pass
            raise DestinationError(f"replacement temporary files disappeared: {filename!r}")
        token = secrets.token_hex(12)
        marker_name = f".pbtxn-{token}.json"
        transaction = {
            "version": 1,
            "state": "prepared",
            "target": filename,
            "data_temp": data_temp,
            "meta_temp": meta_temp,
            "data_backup": f".pbbackup-{token}.data",
            "meta_backup": f".pbbackup-{token}.json",
            "data_guard": f".pbtxn-guard-{token}.data",
            "meta_guard": f".pbtxn-guard-{token}.json",
            "target_identity": list(target_identity) if target_identity is not None else None,
            "meta_identity": list(meta_identity) if meta_identity is not None else None,
            "new_data_identity": list(new_data_identity),
            "new_meta_identity": list(new_meta_identity),
        }
        try:
            self._write_transaction_file(directory_fd, marker_name, transaction)
        except BaseException:
            for temp_name, expected in (
                (data_temp, new_data_identity),
                (meta_temp, new_meta_identity),
            ):
                if self._entry_identity(directory_fd, temp_name) == expected:
                    self._remove_expected(directory_fd, temp_name, expected)
            raise
        try:
            if transaction is not None:
                if target_identity is not None:
                    self._move_expected(
                        directory_fd,
                        filename,
                        transaction["data_backup"],
                        target_identity,
                    )
                if meta_identity is not None:
                    self._move_expected(
                        directory_fd,
                        meta_name,
                        transaction["meta_backup"],
                        meta_identity,
                    )
                if target_identity is None:
                    self._link_expected(
                        directory_fd,
                        data_temp,
                        transaction["data_guard"],
                        self._transaction_identity(transaction, "new_data_identity"),
                    )
                    self._install_new(directory_fd, data_temp, filename)
                else:
                    self._link_expected(
                        directory_fd,
                        data_temp,
                        filename,
                        self._transaction_identity(transaction, "new_data_identity"),
                    )
                    if transaction["data_guard"] is not None:
                        self._link_expected(
                            directory_fd,
                            data_temp,
                            transaction["data_guard"],
                            self._transaction_identity(transaction, "new_data_identity"),
                        )
                if meta_identity is None:
                    self._link_expected(
                        directory_fd,
                        meta_temp,
                        transaction["meta_guard"],
                        self._transaction_identity(transaction, "new_meta_identity"),
                    )
                    self._install_new(directory_fd, meta_temp, meta_name)
                else:
                    self._link_expected(
                        directory_fd,
                        meta_temp,
                        meta_name,
                        self._transaction_identity(transaction, "new_meta_identity"),
                    )
                    if transaction["meta_guard"] is not None:
                        self._link_expected(
                            directory_fd,
                            meta_temp,
                            transaction["meta_guard"],
                            self._transaction_identity(transaction, "new_meta_identity"),
                        )
            else:
                self._install_new(directory_fd, data_temp, filename)
                self._install_new(directory_fd, meta_temp, meta_name)
            self._fsync_directory(directory_fd)
            if transaction is not None:
                commit_name = marker_name[:-5] + ".commit"
                committed = dict(transaction)
                committed["state"] = "committed"
                try:
                    self._write_transaction_file(directory_fd, commit_name, committed)
                except BaseException:
                    commit_published = self._commit_file_matches(
                        directory_fd,
                        commit_name,
                        transaction,
                    )
                    raise
                commit_published = True
                try:
                    cleanup_complete = self._cleanup_committed_transaction(
                        directory_fd,
                        transaction,
                        marker_name,
                        commit_name,
                    )
                    if not cleanup_complete and not self._transaction_public_pair_is_intact(
                        directory_fd,
                        transaction,
                    ):
                        raise DestinationError(
                            "replacement published but target was not verified; "
                            "cleanup deferred"
                        )
                except (DestinationError, OSError) as exc:
                    if not self._transaction_public_pair_is_intact(directory_fd, transaction):
                        raise DestinationError(
                            "replacement published but cleanup deferred"
                        ) from exc
                    # The replacement is published; only private cleanup is
                    # deferred when the public pair remains intact.
                    log.warning("deferred transaction cleanup: %s", marker_name)
            return stored
        except BaseException:
            if transaction is not None and marker_name is not None and not commit_published:
                try:
                    if not self._rollback_transaction(directory_fd, transaction, marker_name):
                        log.warning("deferred transaction rollback: %s", marker_name)
                except (DestinationError, OSError):
                    log.exception("transaction rollback failed: %s", marker_name)
            if not commit_published:
                for temp_name in (data_temp, meta_temp):
                    try:
                        expected = (
                            self._transaction_identity(transaction, "new_data_identity")
                            if transaction is not None and temp_name == data_temp
                            else self._transaction_identity(transaction, "new_meta_identity")
                            if transaction is not None
                            else data_temp_identity
                            if temp_name == data_temp
                            else meta_temp_identity
                        )
                        if expected is not None and self._entry_identity(directory_fd, temp_name) == expected:
                            self._remove_expected(directory_fd, temp_name, expected)
                    except (DestinationError, OSError):
                        pass
            raise

    # -- espace disque -----------------------------------------------------

    def space_info(self) -> SpaceInfo:
        try:
            with self._directory_fd() as directory_fd:
                native = self._fs.volume_space(directory_fd)
                total = native.total_bytes
                available = native.available_bytes
        except (OSError, UnsupportedFilesystemError) as exc:
            raise DestinationError(f"cannot measure free space: {exc}") from exc
        if total <= 0:
            raise DestinationError("filesystem has no measurable capacity")
        return SpaceInfo(total, available)

    @property
    def device_id(self) -> int:
        try:
            with self._directory_fd() as directory_fd:
                return self._fs.volume_identity(directory_fd)
        except (OSError, UnsupportedFilesystemError) as exc:
            raise DestinationError(f"cannot measure filesystem: {exc}") from exc

    def ensure_space(self, incoming_bytes: int, minimum_percent: float) -> None:
        info = self.space_info()
        required = max(0, incoming_bytes) + _SPACE_MARGIN_BYTES
        remaining = info.available_bytes - required
        minimum_bytes = info.total_bytes * minimum_percent / 100.0
        if info.available_bytes < required or remaining < minimum_bytes:
            raise StorageLowError(info, minimum_percent)

    # -- reconciliation ----------------------------------------------------

    def reconcile(self) -> None:
        """Reconcile old work files left by a crash."""
        with self._directory_fd() as directory_fd:
            try:
                entries = self._fs.entries(directory_fd)
            except (OSError, UnsupportedFilesystemError) as exc:
                raise DestinationError(
                    f"cannot read {self.directory}: {exc}"
                ) from exc
            protected = self._recover_transactions(directory_fd, entries)
            protected.update(self._recover_deletions(directory_fd, entries))
            protected.update(self._recover_renames(directory_fd, entries))
            for entry in entries:
                if entry.name in protected or _internal_marker_name(entry.name):
                    continue
                if not (
                    _META_TEMP_RE.fullmatch(entry.name)
                    or _DATA_TEMP_RE.fullmatch(entry.name)
                    or _TXN_TEMP_RE.fullmatch(entry.name)
                    or _TXN_DATA_GUARD_RE.fullmatch(entry.name)
                    or _TXN_META_GUARD_RE.fullmatch(entry.name)
                    or _DELETE_TEMP_RE.fullmatch(entry.name)
                    or _RENAME_TEMP_RE.fullmatch(entry.name)
                    or _TRASH_RE.fullmatch(entry.name)
                    or _RENAME_BACKUP_RE.fullmatch(entry.name)
                    or _RENAME_DATA_BACKUP_RE.fullmatch(entry.name)
                    or _RENAME_DATA_GUARD_RE.fullmatch(entry.name)
                    or _RENAME_META_BACKUP_GUARD_RE.fullmatch(entry.name)
                    or _RENAME_META_GUARD_RE.fullmatch(entry.name)
                ):
                    continue
                log.warning(
                    "internal work file without transaction preserved: %s",
                    entry.name,
                )

    # -- API Destination ---------------------------------------------------

    def save(
        self,
        data: bytes,
        info: ContentInfo,
        filename: str | None = None,
        *,
        allow_replace: bool = False,
        adopt_existing: bool = False,
    ) -> StoredImage:
        self._ensure_dir()
        if filename is not None:
            if not self._valid_filename(filename):
                raise DestinationError(f"invalid filename: {filename!r}")
            with self._directory_fd() as directory_fd:
                return self._save_named(
                    directory_fd,
                    data,
                    info,
                    filename,
                    allow_replace=allow_replace,
                    adopt_existing=adopt_existing,
                )
        ext = info.ext
        last_exc: Exception | None = None
        with self._directory_fd() as directory_fd:
            for _attempt in range(8):
                filename = self._generate_name(ext)
                try:
                    return self._save_named(
                        directory_fd,
                        data,
                        info,
                        filename,
                        allow_replace=False,
                    )
                except (StorageConflictError, ReplacementRequiredError) as exc:
                    # Generated names are retried on any occupied entry, just
                    # like the former O_EXCL path, while named saves still
                    # report conflicts to their caller.
                    last_exc = exc
                    continue
        raise DestinationError(f"repeated filename-generation collision ({last_exc})")

    def list(self) -> list[StoredImage]:
        self._ensure_dir()
        items: list[StoredImage] = []
        with self._directory_fd() as directory_fd:
            try:
                entries = sorted(self._fs.entries(directory_fd), key=lambda e: e.name)
            except (OSError, UnsupportedFilesystemError) as exc:
                raise DestinationError(
                    f"cannot read {self.directory}: {exc}"
                ) from exc
            blocked_targets: set[str] = set()
            committed_targets: set[str] = set()
            for entry in entries:
                if _delete_token(entry.name) is not None:
                    if self._historical_pbdel_pair(directory_fd, entry.name):
                        continue
                    try:
                        transaction = self._parse_delete_transaction(
                            entry.name,
                            self._read_meta(directory_fd, entry.name),
                        )
                        blocked_targets.add(transaction["target"])
                    except (DestinationError, ValueError, TypeError, KeyError):
                        pass
                    continue
                if _rename_token(entry.name) is not None:
                    try:
                        transaction = self._parse_rename_transaction(
                            entry.name,
                            self._read_meta(directory_fd, entry.name),
                        )
                        source = transaction["source"]
                        target = transaction["target"]
                        if transaction["state"] == "prepared":
                            blocked_targets.update({source, target})
                        elif (
                            self._entry_identity(directory_fd, target)
                            != self._transaction_identity(transaction, "source_identity")
                            or self._entry_identity(directory_fd, target + ".json")
                            != self._transaction_identity(transaction, "new_meta_identity")
                            or self._entry_identity(directory_fd, source) is not None
                            or self._entry_identity(directory_fd, source + ".json") is not None
                        ):
                            blocked_targets.update({source, target})
                        else:
                            committed_targets.add(target)
                    except (DestinationError, ValueError, TypeError, KeyError):
                        continue
                    continue
                if not _internal_transaction_name(entry.name):
                    continue
                try:
                    transaction = self._parse_transaction(
                        entry.name,
                        self._read_meta(directory_fd, entry.name),
                    )
                    target = transaction["target"]
                    if transaction["state"] == "prepared":
                        blocked_targets.add(target)
                    elif (
                        self._entry_identity(directory_fd, target)
                        != self._transaction_identity(transaction, "new_data_identity")
                        or self._entry_identity(directory_fd, target + ".json")
                        != self._transaction_identity(transaction, "new_meta_identity")
                    ):
                        blocked_targets.add(target)
                    else:
                        committed_targets.add(target)
                except (DestinationError, ValueError, TypeError, KeyError):
                    continue
            blocked_targets.difference_update(committed_targets)
            for entry in entries:
                # Dotted names for dropped files are legitimate; internal work
                # files are not sidecars.
                if (
                    not entry.name.endswith(".json")
                    or entry.name == ".pasteberth.lock"
                        or entry.name.startswith(
                        _INTERNAL_RESERVED_PREFIXES
                    )
                    or _internal_marker_name(entry.name)
                ):
                    continue
                try:
                    raw = self._read_meta(directory_fd, entry.name)
                except (OSError, DestinationError):
                    log.warning("sidecar unreadable, ignored: %s", entry.name)
                    continue
                if not _meta_keys_ok(raw):
                    log.warning("invalid sidecar, ignored: %s", entry.name)
                    continue
                filename = raw.get("filename")
                if not self._valid_filename(filename) or entry.name != filename + ".json":
                    log.warning("inconsistent sidecar, ignored: %s", entry.name)
                    continue
                if filename in blocked_targets:
                    log.warning("transaction active, item ignored: %s", filename)
                    continue
                try:
                    image_file = self._open_file(directory_fd, filename, "rb")
                except FileNotFoundError:
                    log.warning("orphan sidecar preserved: %s", entry.name)
                    continue
                except (OSError, DestinationError):
                    log.warning("file linked to sidecar is unreadable, ignored: %s", entry.name)
                    continue
                try:
                    actual_size = image_file.size
                finally:
                    image_file.close()
                try:
                    item = self._validated_item(raw, filename, actual_size)
                except (TypeError, ValueError, KeyError):
                    log.warning("invalid metadata, ignored: %s", entry.name)
                    continue
                items.append(item)

        items.sort(key=lambda i: (i.created_at, i.filename), reverse=True)
        return items

    def rename(self, source: str, target: str) -> StoredImage:
        """Rename a managed pair without ever replacing a target."""
        if not self._valid_filename(source) or not self._valid_filename(target):
            raise DestinationError("invalid filename")
        if source == target:
            raise DestinationError("source and target names are identical")

        with self._directory_fd() as directory_fd:
            active_names = self._active_transaction_names(directory_fd)
            if source in active_names or target in active_names:
                raise StorageConflictError("rename transaction in progress")
            owned_file, source_meta_identity = self._require_owned(directory_fd, source)
            try:
                source_identity = owned_file.identity
                raw = self._read_meta(directory_fd, self._meta_name(source))
                item = self._validated_item(raw, source, owned_file.size)
            finally:
                owned_file.close()
            if source_meta_identity is None:
                raise StorageConflictError(f"sidecar disappeared: {source!r}")

            target_meta = self._meta_name(target)
            if self._entry_exists(directory_fd, target) or self._entry_exists(
                directory_fd, target_meta
            ):
                raise StorageConflictError(f"target already exists: {target!r}")

            new_meta = dict(raw)
            new_meta["filename"] = target
            meta_temp = self._write_meta_temp(directory_fd, new_meta)
            new_meta_identity = self._entry_identity(directory_fd, meta_temp)
            if new_meta_identity is None:
                raise DestinationError(f"temporary sidecar disappeared: {target_meta!r}")

            token = secrets.token_hex(12)
            marker_name = f".pbrename-{token}.json"
            commit_name = f".pbrename-{token}.commit"
            transaction = {
                "version": 1,
                "state": "prepared",
                "source": source,
                "target": target,
                "meta_temp": meta_temp,
                "meta_backup": f".pbrename-backup-{token}.json",
                "data_backup": f".pbrename-backup-{token}.data",
                "data_guard": f".pbrename-guard-{token}.data",
                "meta_guard": f".pbrename-guard-{token}.json",
                "meta_backup_guard": f".pbrename-backup-{token}.guard.json",
                "source_identity": list(source_identity),
                "source_meta_identity": list(source_meta_identity),
                "new_meta_identity": list(new_meta_identity),
            }
            try:
                self._write_transaction_file(directory_fd, marker_name, transaction)
            except BaseException:
                if self._entry_identity(directory_fd, meta_temp) == new_meta_identity:
                    self._remove_expected(directory_fd, meta_temp, new_meta_identity)
                raise

            commit_published = False
            try:
                self._link_expected(
                    directory_fd,
                    source,
                    transaction["data_backup"],
                    source_identity,
                )
                self._link_expected(
                    directory_fd,
                    transaction["data_backup"],
                    transaction["data_guard"],
                    source_identity,
                )
                self._link_expected(
                    directory_fd,
                    self._meta_name(source),
                    transaction["meta_backup_guard"],
                    source_meta_identity,
                )
                self._move_expected(directory_fd, source, target, source_identity)
                self._move_expected(
                    directory_fd,
                    self._meta_name(source),
                    transaction["meta_backup"],
                    source_meta_identity,
                )
                self._link_expected(
                    directory_fd,
                    meta_temp,
                    target_meta,
                    new_meta_identity,
                )
                self._link_expected(
                    directory_fd,
                    meta_temp,
                    transaction["meta_guard"],
                    new_meta_identity,
                )
                self._fsync_directory(directory_fd)
                committed = dict(transaction)
                committed["state"] = "committed"
                try:
                    self._write_transaction_file(directory_fd, commit_name, committed)
                except BaseException:
                    commit_published = self._commit_file_matches(
                        directory_fd,
                        commit_name,
                        transaction,
                    )
                    raise
                commit_published = True
                try:
                    cleanup_complete = self._cleanup_committed_rename(
                        directory_fd,
                        transaction,
                        marker_name,
                        commit_name,
                    )
                    if not cleanup_complete and not self._rename_public_pair_is_intact(
                        directory_fd,
                        transaction,
                    ):
                        raise DestinationError(
                            "rename published but target was not verified; "
                            "cleanup deferred"
                        )
                except (DestinationError, OSError) as exc:
                    log.warning("deferred rename cleanup: %s", marker_name)
                    if not self._rename_public_pair_is_intact(directory_fd, transaction):
                        raise DestinationError(
                            "rename published but cleanup deferred"
                        ) from exc
                return StoredImage(
                    target,
                    item.created_at,
                    item.width,
                    item.height,
                    item.size,
                    item.fmt,
                    item.kind,
                    item.mime,
                    item.comment,
                )
            except BaseException:
                if not commit_published:
                    try:
                        if not self._rollback_rename_transaction(
                            directory_fd, transaction, marker_name
                        ):
                            log.warning("deferred rename rollback: %s", marker_name)
                    except (DestinationError, OSError):
                        log.exception("rename rollback failed: %s", marker_name)
                try:
                    if self._entry_identity(directory_fd, meta_temp) == new_meta_identity:
                        self._remove_expected(directory_fd, meta_temp, new_meta_identity)
                except (DestinationError, OSError):
                    pass
                raise

    def update_comment(self, filename: str, comment: str) -> StoredImage:
        """Replace only a managed item's sidecar comment atomically."""
        if not self._valid_filename(filename):
            raise DestinationError(f"invalid filename: {filename!r}")
        comment = validate_comment(
            comment,
            max_length=self.limits.max_comment_length,
            max_bytes=self.limits.max_comment_bytes,
        )
        with self._directory_fd() as directory_fd:
            if filename in self._active_transaction_names(directory_fd):
                raise StorageConflictError(
                    f"transaction in progress for filename: {filename!r}"
                )
            owned_file, meta_identity = self._require_owned(directory_fd, filename)
            try:
                target_identity = owned_file.identity
                meta_name = self._meta_name(filename)
                raw = self._read_meta(directory_fd, meta_name)
                if self._entry_identity(directory_fd, meta_name) != meta_identity:
                    raise StorageConflictError(
                        f"sidecar changed during operation: {filename!r}"
                    )
                item = self._validated_item(raw, filename, owned_file.size)
            finally:
                owned_file.close()
            if target_identity is None or meta_identity is None:
                raise StorageConflictError(f"file or sidecar disappeared: {filename!r}")
            if self._entry_identity(directory_fd, filename) != target_identity:
                raise StorageConflictError(f"file changed during operation: {filename!r}")

            updated_meta = dict(raw)
            updated_meta["comment"] = comment
            temp_name = self._write_meta_temp(directory_fd, updated_meta)
            temp_identity = self._entry_identity(directory_fd, temp_name)
            if temp_identity is None:
                raise DestinationError(f"temporary sidecar disappeared: {meta_name!r}")
            published = False
            try:
                self._fs.replace(
                    directory_fd,
                    temp_name,
                    meta_name,
                    expected_source=temp_identity,
                    expected_target=meta_identity,
                )
                published = True
                self._fsync_directory(directory_fd)
            except (EntryChangedError, UnsafeLinkError) as exc:
                raise StorageConflictError(
                    f"sidecar changed during operation: {filename!r}"
                ) from exc
            finally:
                if not published:
                    try:
                        self._remove_expected(directory_fd, temp_name, temp_identity)
                    except (DestinationError, OSError):
                        pass
            return StoredImage(
                filename,
                item.created_at,
                item.width,
                item.height,
                item.size,
                item.fmt,
                item.kind,
                item.mime,
                comment,
            )

    def delete(self, filename: str, *, allow_stale_sidecar: bool = False) -> None:
        with self._directory_fd() as directory_fd:
            if filename in self._active_transaction_names(directory_fd):
                raise StorageConflictError(
                    f"transaction in progress for filename: {filename!r}"
                )
            owned_file, meta_identity = self._require_owned(
                directory_fd,
                filename,
                allow_stale_sidecar=allow_stale_sidecar,
            )
            try:
                target_identity = owned_file.identity
                meta_name = self._meta_name(filename)
            finally:
                owned_file.close()
            if target_identity is None or meta_identity is None:
                raise StorageConflictError(f"file or sidecar disappeared: {filename!r}")
            token = secrets.token_hex(12)
            marker_name = f".pbdel-{token}.json"
            data_trash = f".pbtrash-{token}.data"
            meta_trash = f".pbtrash-{token}.json"
            transaction = {
                "version": 1,
                "target": filename,
                "data_trash": data_trash,
                "meta_trash": meta_trash,
                "target_identity": list(target_identity),
                "meta_identity": list(meta_identity),
            }
            self._write_transaction_file(directory_fd, marker_name, transaction)
            try:
                # Move verified entries to private names first. A durable
                # tombstone lets startup finish the operation if the process
                # stops between the two moves.
                self._move_expected(directory_fd, filename, data_trash, target_identity)
                self._move_expected(directory_fd, meta_name, meta_trash, meta_identity)
                self._fsync_directory(directory_fd)
            except BaseException:
                try:
                    if not self._rollback_delete_transaction(directory_fd, transaction, marker_name):
                        log.warning("deferred deletion rollback: %s", marker_name)
                except (DestinationError, OSError):
                    log.exception("deletion rollback failed: %s", marker_name)
                raise
            try:
                if not self._finish_delete_transaction(directory_fd, transaction, marker_name):
                    log.warning("deferred deletion cleanup: %s", marker_name)
            except (DestinationError, OSError):
                log.warning("deferred deletion cleanup: %s", marker_name)

    def read(self, filename: str) -> bytes:
        with self._directory_fd() as directory_fd:
            file_handle, _meta_identity = self._require_owned(directory_fd, filename)
            try:
                with file_handle as fh:
                    return fh.read()
            except FileNotFoundError as exc:
                raise UnknownImageError(f"unknown Pasteberth file: {filename!r}") from exc
            except (OSError, DestinationError) as exc:
                if isinstance(exc, DestinationError):
                    raise
                raise DestinationError(f"cannot read ({exc})") from exc

    @contextmanager
    def open_read(self, filename: str):
        """Open a managed file without releasing the destination lock."""
        with self._directory_fd() as directory_fd:
            file_handle = None
            try:
                file_handle, _meta_identity = self._require_owned(directory_fd, filename)
                with file_handle as fh:
                    yield fh
            except FileNotFoundError as exc:
                raise UnknownImageError(
                    f"unknown Pasteberth file: {filename!r}"
                ) from exc
            except (OSError, DestinationError) as exc:
                if isinstance(exc, DestinationError):
                    raise
                raise DestinationError(f"cannot read ({exc})") from exc
            finally:
                if file_handle is not None and not file_handle.closed:
                    file_handle.close()

    def reference_path(self, filename: str) -> str:
        return str(self.directory / filename)

    def apply_retention(self, retain: int, protected_filename: str | None = None) -> list[str]:
        """Delete the oldest images beyond ``retain``.

        The newly created image can be protected against a clock moving
        backwards or future historical sidecars.
        """
        items = self.list()
        if protected_filename and any(i.filename == protected_filename for i in items):
            others = [i for i in items if i.filename != protected_filename]
            victims = others[max(0, retain - 1):]
        else:
            victims = items[retain:]
        deleted: list[str] = []
        failures: list[str] = []
        for item in victims:
            try:
                self.delete(item.filename)
                deleted.append(item.filename)
                log.info("retention: deleted %s", item.filename)
            except DestinationError as exc:
                failures.append(item.filename)
                log.error("retention: deletion failed for %s: %s", item.filename, exc)
        if failures:
            raise RetentionError(failures)
        return deleted
