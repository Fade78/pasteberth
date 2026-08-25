"""Contrôles communs des chemins filesystem."""
from __future__ import annotations

import stat
from pathlib import Path


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
