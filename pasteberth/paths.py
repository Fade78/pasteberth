"""Compatibility facade for safe filesystem path operations."""
from __future__ import annotations

from pathlib import Path

from pasteberth.platformfs import DirectoryHandle, platform_fs


def first_symlink_component(path: Path) -> Path | None:
    return platform_fs().first_symlink_component(Path(path))


def open_directory(
    path: Path,
    *,
    create: bool = False,
    mode: int = 0o700,
) -> DirectoryHandle:
    return platform_fs().open_directory(
        Path(path),
        create=create,
        mode=mode,
    )
