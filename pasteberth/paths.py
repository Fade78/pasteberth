"""Contrôles communs des chemins filesystem."""
from __future__ import annotations

import stat
import os
from pathlib import Path


_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


def first_symlink_component(path: Path) -> Path | None:
    """Retourne le premier composant symbolique existant d'un chemin."""
    path = Path(path)
    current = Path(path.anchor) if path.is_absolute() else Path(".")
    parts = path.parts[1:] if path.is_absolute() else path.parts
    for part in parts:
        current /= part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            break
        if stat.S_ISLNK(mode):
            return current
    return None


def open_directory(path: Path, *, create: bool = False, mode: int = 0o700) -> int:
    """Ouvre un chemin absolu composant par composant sans suivre de lien."""
    path = Path(path)
    if not path.is_absolute():
        raise ValueError(f"chemin de répertoire non absolu : {path}")
    fd = os.open(path.anchor, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW)
    try:
        for part in path.parts[1:]:
            next_fd = -1
            try:
                try:
                    next_fd = os.open(
                        part,
                        os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW,
                        dir_fd=fd,
                    )
                except FileNotFoundError:
                    if not create:
                        raise
                    try:
                        os.mkdir(part, mode=mode, dir_fd=fd)
                    except FileExistsError:
                        pass
                    next_fd = os.open(
                        part,
                        os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW,
                        dir_fd=fd,
                    )
                    os.fchmod(next_fd, mode)
                os.close(fd)
                fd = next_fd
                next_fd = -1
            finally:
                if next_fd >= 0:
                    os.close(next_fd)
        return fd
    except BaseException:
        os.close(fd)
        raise
