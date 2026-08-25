"""Destinations de stockage et rétention circulaire par zone.

Abstraction minimale : une ``Destination`` sait sauvegarder, lister,
lire et supprimer des images. La V1 n'implémente que ``LocalDestination``
(locale au serveur Pasteberth), mais une future ``SshDestination`` pourrait
offrir la même interface sans toucher à la logique métier.

Garanties :
- noms de fichiers générés côté serveur uniquement, uniques (O_EXCL) ;
- métadonnées dans un sidecar JSON atomique (<fichier>.json) ;
- seuls les fichiers dont Pasteberth connaît le sidecar sont supprimés ;
- un fichier étranger dans le répertoire n'est jamais touché.
"""
from __future__ import annotations

import json
import logging
import os
import re
import secrets
import tempfile
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from pasteberth.images import ImageInfo, extension_for

log = logging.getLogger("pasteberth.storage")

_FILENAME_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}_[0-9a-f]{6}\.(?:png|jpg|webp)$"
)

_META_KEYS = {"filename", "created_at", "width", "height", "size", "format"}


def valid_filename(name: str) -> bool:
    return bool(_FILENAME_RE.fullmatch(name))


@dataclass(frozen=True)
class StoredImage:
    filename: str
    created_at: datetime  # timezone-aware UTC
    width: int
    height: int
    size: int
    fmt: str


class DestinationError(Exception):
    """Erreur d'E/S d'une destination (répertoire disparu, permissions…)."""


class Destination(ABC):
    """Interface pragmatique : future SshDestination => mêmes méthodes."""

    @abstractmethod
    def save(self, data: bytes, info: ImageInfo) -> StoredImage: ...

    @abstractmethod
    def list(self) -> list[StoredImage]:
        """Historique, de la plus récente à la plus ancienne."""

    @abstractmethod
    def delete(self, filename: str) -> None: ...

    @abstractmethod
    def read(self, filename: str) -> bytes: ...

    @abstractmethod
    def reference_path(self, filename: str) -> str:
        """Chemin tel que le voit le harness (base de la référence)."""


class LocalDestination(Destination):
    def __init__(self, directory: Path, *, create_directory: bool = True):
        self.directory = Path(directory)
        self.create_directory = create_directory
        self._ensure_dir()

    def _ensure_dir(self) -> None:
        if self.directory.is_dir():
            return
        if self.create_directory:
            try:
                self.directory.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise DestinationError(
                    f"impossible de créer {self.directory} : {exc}"
                ) from exc
        else:
            raise DestinationError(f"répertoire inexistant : {self.directory}")

    # -- helpers ---------------------------------------------------------

    @property
    def _meta_suffix(self) -> str:
        return ".json"

    def _meta_path(self, filename: str) -> Path:
        return self.directory / (filename + self._meta_suffix)

    def _image_path(self, filename: str) -> Path:
        return self.directory / filename

    def _require_owned(self, filename: str) -> None:
        """N'opère que sur les fichiers dont le sidecar prouve la paternité."""
        if not valid_filename(filename):
            raise DestinationError(f"nom de fichier invalide : {filename!r}")
        if not self._meta_path(filename).is_file():
            raise DestinationError(f"fichier inconnu de Pasteberth : {filename!r}")

    def _generate_name(self, ext: str) -> str:
        stamp = datetime.now().astimezone().strftime("%Y-%m-%d_%H-%M-%S")
        return f"{stamp}_{secrets.token_hex(3)}{ext}"

    def _write_meta_atomic(self, meta: dict) -> None:
        target = self._meta_path(meta["filename"])
        fd, tmp_name = tempfile.mkstemp(
            dir=self.directory, prefix=".pbmeta-", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(meta, fh, ensure_ascii=False, separators=(",", ":"))
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, target)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    # -- API Destination --------------------------------------------------

    def save(self, data: bytes, info: ImageInfo) -> StoredImage:
        self._ensure_dir()
        ext = extension_for(info.fmt)
        last_exc: Exception | None = None
        for _attempt in range(8):
            filename = self._generate_name(ext)
            path = self._image_path(filename)
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            except FileExistsError as exc:  # collision improbable : nouveau suffixe
                last_exc = exc
                continue
            try:
                with os.fdopen(fd, "wb") as fh:
                    fh.write(data)
                    fh.flush()
                    os.fsync(fh.fileno())
            except OSError as exc:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
                raise DestinationError(f"écriture impossible ({exc})") from exc
            created_at = datetime.now(timezone.utc)
            stored = StoredImage(
                filename=filename,
                created_at=created_at,
                width=info.width,
                height=info.height,
                size=len(data),
                fmt=info.fmt,
            )
            self._write_meta_atomic(
                {
                    "filename": filename,
                    "created_at": created_at.isoformat(timespec="microseconds"),
                    "width": stored.width,
                    "height": stored.height,
                    "size": stored.size,
                    "format": stored.fmt,
                }
            )
            return stored
        raise DestinationError(f"génération de nom en collision répétée ({last_exc})")

    def list(self) -> list[StoredImage]:
        self._ensure_dir()
        items: list[StoredImage] = []
        orphan_sidecars: list[Path] = []
        try:
            entries = sorted(os.scandir(self.directory), key=lambda e: e.name)
        except OSError as exc:
            raise DestinationError(f"lecture impossible de {self.directory} : {exc}") from exc
        for entry in entries:
            if not entry.name.endswith(self._meta_suffix) or entry.name.startswith("."):
                continue
            path = Path(entry.path)
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                log.warning("sidecar illisible, ignoré : %s", entry.name)
                continue
            if not isinstance(raw, dict) or not _META_KEYS.issubset(raw):
                log.warning("sidecar invalide, ignoré : %s", entry.name)
                continue
            filename = raw["filename"]
            if not valid_filename(filename) or path.name != filename + self._meta_suffix:
                log.warning("sidecar incohérent, ignoré : %s", entry.name)
                continue
            if not self._image_path(filename).is_file():
                orphan_sidecars.append(path)  # image déjà partie : sidecar orphelin
                continue
            try:
                created_at = datetime.fromisoformat(raw["created_at"])
                item = StoredImage(
                    filename=filename,
                    created_at=created_at,
                    width=int(raw["width"]),
                    height=int(raw["height"]),
                    size=int(raw["size"]),
                    fmt=str(raw["format"]),
                )
            except (TypeError, ValueError, KeyError):
                log.warning("métadonnées invalides, ignorées : %s", entry.name)
                continue
            items.append(item)
        for path in orphan_sidecars:
            try:
                path.unlink()
                log.info("sidecar orphelin supprimé : %s", path.name)
            except OSError:
                pass
        items.sort(key=lambda i: (i.created_at, i.filename), reverse=True)
        return items

    def delete(self, filename: str) -> None:
        self._require_owned(filename)
        try:
            self._image_path(filename).unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise DestinationError(f"suppression impossible ({exc})") from exc
        try:
            self._meta_path(filename).unlink()
        except FileNotFoundError:
            pass

    def read(self, filename: str) -> bytes:
        self._require_owned(filename)
        try:
            return self._image_path(filename).read_bytes()
        except OSError as exc:
            raise DestinationError(f"lecture impossible ({exc})") from exc

    def reference_path(self, filename: str) -> str:
        return str(self._image_path(filename))

    def apply_retention(self, retain: int) -> list[str]:
        """Supprime les plus anciennes images au-delà de ``retain``."""
        items = self.list()
        victims = items[retain:]
        deleted: list[str] = []
        for item in victims:
            try:
                self.delete(item.filename)
                deleted.append(item.filename)
                log.info("rétention : suppression de %s", item.filename)
            except DestinationError as exc:
                log.error("rétention : échec sur %s : %s", item.filename, exc)
        return deleted
