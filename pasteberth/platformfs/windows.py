"""Windows backend boundary.

Win32 handles are deliberately kept out of the POSIX modules.  The complete
implementation will land behind this boundary; until then, selecting Windows
fails explicitly instead of silently weakening a transaction guarantee.
"""
from __future__ import annotations

from pathlib import Path

from pasteberth.platformfs.base import (
    DirectoryHandle,
    FileHandle,
    FileIdentity,
    PermissionAudit,
    PlatformCapabilities,
    PlatformFS,
    UnsupportedFilesystemError,
    VolumeSpace,
)


class WindowsPlatformFS(PlatformFS):
    backend_name = "windows"

    @property
    def capabilities(self) -> PlatformCapabilities:
        return PlatformCapabilities(backend=self.backend_name)

    def _unsupported(self) -> None:
        raise UnsupportedFilesystemError(
            "le backend Windows natif n'est pas encore disponible"
        )

    def open_directory(self, path: Path, *, create: bool = False, mode: int = 0o700):
        self._unsupported()

    def open_existing(self, directory: DirectoryHandle, name: str, *, mode: str = "rb"):
        self._unsupported()

    def create_exclusive(
        self,
        directory: DirectoryHandle,
        name: str,
        *,
        mode: str = "wb",
        permissions: int = 0o600,
    ):
        self._unsupported()

    def entries(self, directory: DirectoryHandle):
        self._unsupported()

    def entry_info(self, directory: DirectoryHandle, name: str):
        self._unsupported()

    def identity(
        self,
        directory: DirectoryHandle,
        name: str,
        *,
        require_regular: bool = True,
    ):
        self._unsupported()

    def link_expected(
        self,
        directory: DirectoryHandle,
        source: str,
        target: str,
        expected: FileIdentity,
    ):
        self._unsupported()

    def rename_noreplace(
        self,
        directory: DirectoryHandle,
        source: str,
        target: str,
        *,
        expected: FileIdentity | None = None,
    ):
        self._unsupported()

    def remove_expected(
        self,
        directory: DirectoryHandle,
        name: str,
        expected: FileIdentity | None,
    ):
        self._unsupported()

    def acquire_lock(
        self,
        directory: DirectoryHandle,
        *,
        name: str = ".pasteberth.lock",
        exclusive: bool,
        blocking: bool = True,
    ):
        self._unsupported()

    def flush_directory(self, directory: DirectoryHandle):
        self._unsupported()

    def volume_space(self, directory: DirectoryHandle) -> VolumeSpace:
        self._unsupported()

    def volume_identity(self, directory: DirectoryHandle) -> int:
        self._unsupported()

    def first_symlink_component(self, path: Path):
        self._unsupported()

    def audit_permissions(self, path: Path, *, directory: bool) -> PermissionAudit:
        self._unsupported()

    def is_owned(self, entry) -> bool:
        self._unsupported()
