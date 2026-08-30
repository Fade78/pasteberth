"""Semantic filesystem primitives used by Pasteberth.

The transaction engine must reason about names, identities and capabilities,
not about POSIX or Win32 flags.  Concrete backends keep their native handles
private and expose only the small contract defined here.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, NamedTuple


class UnsupportedFilesystemError(RuntimeError):
    """The selected filesystem cannot provide a required guarantee."""


class UnsafeLinkError(OSError):
    """An entry is a link or another object unsafe for a regular-file action."""


class EntryExistsError(FileExistsError):
    """A no-replace operation found an occupied destination."""


class EntryChangedError(OSError):
    """An entry no longer has the identity observed by the caller."""


class BusyError(BlockingIOError):
    """A non-blocking lock could not be acquired."""


class PermissionSecurityError(PermissionError):
    """An ownership, mode or ACL security check failed."""


class InvalidNameError(ValueError):
    """A name is not a single safe filesystem component."""


class FileIdentity(NamedTuple):
    """Opaque object identity serialized as the historical pair of integers."""

    volume: int
    file_id: int


@dataclass(frozen=True)
class EntryInfo:
    name: str
    identity: FileIdentity
    size: int
    is_regular: bool
    is_symlink: bool
    owner: int | str | None = None


@dataclass(frozen=True)
class VolumeSpace:
    total_bytes: int
    available_bytes: int


@dataclass(frozen=True)
class PermissionAudit:
    """Portable result for a private-file or private-directory audit."""

    path: Path
    private: bool
    owner: int | str | None
    mode: int | None
    detail: str | None = None


@dataclass(frozen=True)
class PlatformCapabilities:
    """Capabilities are explicit so callers can fail closed."""

    backend: str
    safe_directory_open: bool = False
    safe_file_open: bool = False
    exclusive_create: bool = False
    identity: bool = False
    hard_link_guard: bool = False
    atomic_no_replace_rename: bool = False
    expected_remove: bool = False
    interprocess_locks: bool = False
    file_flush: bool = False
    directory_flush: bool = False
    volume_space: bool = False
    volume_identity: bool = False
    private_permissions: bool = False

    def as_dict(self) -> dict[str, bool | str]:
        return {
            name: value
            for name, value in self.__dict__.items()
        }

    def require(self, *names: str) -> None:
        missing = [name for name in names if not getattr(self, name, False)]
        if missing:
            joined = ", ".join(missing)
            raise UnsupportedFilesystemError(
                f"backend {self.backend!r} sans capabilities requises : {joined}"
            )


class DirectoryHandle:
    """Context-managed directory handle owned by a platform backend."""

    def __init__(self, path: Path, identity: FileIdentity):
        self.path = path
        self.identity = identity
        self._closed = False

    def _close_native(self) -> None:
        raise NotImplementedError

    @property
    def closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        if not self._closed:
            self._close_native()
            self._closed = True

    def __enter__(self) -> "DirectoryHandle":
        if self._closed:
            raise ValueError("handle de répertoire déjà fermé")
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()


class FileHandle:
    """Context-managed file stream with a stable semantic identity."""

    def __init__(self, stream: BinaryIO, identity: FileIdentity, size: int):
        self._stream = stream
        self.identity = identity
        self.size = size
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed or self._stream.closed

    def _sync_native(self) -> None:
        raise NotImplementedError

    def sync(self) -> None:
        self._stream.flush()
        self._sync_native()

    def close(self) -> None:
        if not self._closed:
            self._stream.close()
            self._closed = True

    def __getattr__(self, name: str):
        # Keep the wrapper transparent for read(), seek(), write(), etc. while
        # preventing callers from needing the native descriptor or handle.
        return getattr(self._stream, name)

    def __enter__(self) -> "FileHandle":
        if self.closed:
            raise ValueError("handle de fichier déjà fermé")
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()


class PlatformFS:
    """Abstract semantic filesystem contract."""

    backend_name = "unknown"

    @property
    def capabilities(self) -> PlatformCapabilities:
        raise NotImplementedError

    def open_directory(
        self,
        path: Path,
        *,
        create: bool = False,
        mode: int = 0o700,
    ) -> DirectoryHandle:
        raise NotImplementedError

    def open_existing(
        self,
        directory: DirectoryHandle,
        name: str,
        *,
        mode: str = "rb",
    ) -> FileHandle:
        raise NotImplementedError

    def create_exclusive(
        self,
        directory: DirectoryHandle,
        name: str,
        *,
        mode: str = "wb",
        permissions: int = 0o600,
    ) -> FileHandle:
        raise NotImplementedError

    def entries(self, directory: DirectoryHandle) -> tuple[EntryInfo, ...]:
        raise NotImplementedError

    def entry_info(self, directory: DirectoryHandle, name: str) -> EntryInfo | None:
        raise NotImplementedError

    def identity(
        self,
        directory: DirectoryHandle,
        name: str,
        *,
        require_regular: bool = True,
    ) -> FileIdentity | None:
        raise NotImplementedError

    def link_expected(
        self,
        directory: DirectoryHandle,
        source: str,
        target: str,
        expected: FileIdentity,
    ) -> None:
        raise NotImplementedError

    def rename_noreplace(
        self,
        directory: DirectoryHandle,
        source: str,
        target: str,
        *,
        expected: FileIdentity | None = None,
    ) -> None:
        raise NotImplementedError

    def remove_expected(
        self,
        directory: DirectoryHandle,
        name: str,
        expected: FileIdentity | None,
    ) -> bool:
        raise NotImplementedError

    def acquire_lock(
        self,
        directory: DirectoryHandle,
        *,
        name: str = ".pasteberth.lock",
        exclusive: bool,
        blocking: bool = True,
    ):
        raise NotImplementedError

    def flush_directory(self, directory: DirectoryHandle) -> None:
        raise NotImplementedError

    def volume_space(self, directory: DirectoryHandle) -> VolumeSpace:
        raise NotImplementedError

    def volume_identity(self, directory: DirectoryHandle) -> int:
        raise NotImplementedError

    def first_symlink_component(self, path: Path) -> Path | None:
        raise NotImplementedError

    def audit_permissions(
        self,
        path: Path,
        *,
        directory: bool,
    ) -> PermissionAudit:
        raise NotImplementedError

    def is_owned(self, entry: EntryInfo) -> bool:
        raise NotImplementedError

    @staticmethod
    def validate_component(name: str) -> None:
        if not isinstance(name, str) or not name:
            raise InvalidNameError(f"nom de fichier invalide : {name!r}")
        if name in {".", ".."} or "/" in name or "\\" in name or "\x00" in name:
            raise InvalidNameError(f"nom de fichier invalide : {name!r}")
