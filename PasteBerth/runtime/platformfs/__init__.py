"""Select exactly one semantic filesystem backend for the host OS."""
from __future__ import annotations

import platform
from functools import lru_cache

from .base import (
    BusyError,
    DirectoryHandle,
    EntryChangedError,
    EntryExistsError,
    EntryInfo,
    FileHandle,
    FileIdentity,
    InvalidNameError,
    PermissionAudit,
    PermissionSecurityError,
    PlatformCapabilities,
    PlatformFS,
    UnsafeLinkError,
    UnsupportedFilesystemError,
    VolumeSpace,
)


@lru_cache(maxsize=1)
def platform_fs() -> PlatformFS:
    """Return the host backend without importing unrelated native modules."""
    system = platform.system()
    if system == "Linux":
        from .linux import LinuxPlatformFS

        return LinuxPlatformFS()
    if system == "Darwin":
        from .darwin import DarwinPlatformFS

        return DarwinPlatformFS()
    if system == "Windows":
        from .windows import WindowsPlatformFS

        return WindowsPlatformFS()
    raise UnsupportedFilesystemError(f"unsupported operating system: {system}")


__all__ = [
    "BusyError",
    "DirectoryHandle",
    "EntryChangedError",
    "EntryExistsError",
    "EntryInfo",
    "FileHandle",
    "FileIdentity",
    "InvalidNameError",
    "PermissionAudit",
    "PermissionSecurityError",
    "PlatformCapabilities",
    "PlatformFS",
    "UnsafeLinkError",
    "UnsupportedFilesystemError",
    "VolumeSpace",
    "platform_fs",
]
