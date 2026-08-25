"""Destinations de stockage et rétention circulaire par zone.

La destination locale suppose un répertoire privé au processus Pasteberth.
Les accès aux fichiers passent par un descripteur de répertoire et refusent
les liens symboliques afin que la preuve de propriété du sidecar ne devienne
pas une primitive de lecture ou de suppression arbitraire.
"""
from __future__ import annotations

import json
import fcntl
import logging
import os
import re
import secrets
import stat
import time
from abc import ABC, abstractmethod
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from pasteberth.images import (
    FORMATS,
    HARD_MAX_PIXELS,
    MAX_DIMENSION,
    ImageInfo,
    extension_for,
)
from pasteberth.paths import first_symlink_component

log = logging.getLogger("pasteberth.storage")

_FILENAME_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}_[0-9a-f]{6}\.(?:png|jpg|webp)$"
)
_META_KEYS = {"filename", "created_at", "width", "height", "size", "format"}
_SPACE_MARGIN_BYTES = 64 * 1024
_ORPHAN_GRACE_SECONDS = 3600.0
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


def valid_filename(name: object) -> bool:
    return isinstance(name, str) and bool(_FILENAME_RE.fullmatch(name))


@dataclass(frozen=True)
class StoredImage:
    filename: str
    created_at: datetime  # timezone-aware UTC
    width: int
    height: int
    size: int
    fmt: str


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
    """Erreur d'E/S d'une destination (répertoire disparu, permissions…)."""


class UnknownImageError(DestinationError):
    """Le fichier n'est plus un objet Pasteberth connu."""


class StorageLowError(DestinationError):
    """L'écriture ferait franchir le seuil d'espace libre configuré."""

    def __init__(self, info: SpaceInfo, minimum_percent: float):
        self.info = info
        self.minimum_percent = minimum_percent
        super().__init__(
            f"espace libre insuffisant ({info.available_percent:.2f}% disponible, "
            f"minimum {minimum_percent:.2f}%)"
        )


class RetentionError(DestinationError):
    """Au moins une suppression de rétention a échoué."""

    def __init__(self, failures: list[str]):
        self.failures = failures
        super().__init__(f"{len(failures)} suppression(s) de rétention impossible(s)")


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
        with self.operation_lock(exclusive=True):
            self.reconcile()

    def _ensure_dir(self) -> None:
        symlink = first_symlink_component(self.directory)
        if symlink is not None:
            raise DestinationError(f"chemin zone symbolique refusé : {symlink}")
        if self.directory.is_dir():
            mode = stat.S_IMODE(self.directory.stat().st_mode)
            if mode & 0o077:
                raise DestinationError(
                    f"permissions trop ouvertes sur {self.directory} ({oct(mode)}), "
                    "utilisez chmod 700"
                )
            return
        if self.create_directory:
            try:
                self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                os.chmod(self.directory, 0o700)
            except OSError as exc:
                raise DestinationError(
                    f"impossible de créer {self.directory} : {exc}"
                ) from exc
        else:
            raise DestinationError(f"répertoire inexistant : {self.directory}")

    @contextmanager
    def _directory_fd(self):
        self._ensure_dir()
        try:
            fd = os.open(self.directory, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW)
        except OSError as exc:
            raise DestinationError(f"ouverture impossible de {self.directory} : {exc}") from exc
        try:
            yield fd
        finally:
            os.close(fd)

    @contextmanager
    def operation_lock(self, *, exclusive: bool):
        """Verrouille les opérations même entre processus du même utilisateur."""
        lock_path = self.directory / ".pasteberth.lock"
        try:
            fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | _O_NOFOLLOW, 0o600)
            os.chmod(lock_path, 0o600)
        except OSError as exc:
            raise DestinationError(f"verrouillage impossible de {self.directory} : {exc}") from exc
        try:
            fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    @staticmethod
    def _regular_fd(fd: int, name: str) -> int:
        try:
            mode = os.fstat(fd).st_mode
        except OSError:
            os.close(fd)
            raise
        if not stat.S_ISREG(mode):
            os.close(fd)
            raise DestinationError(f"fichier non régulier : {name!r}")
        return fd

    def _open_file(self, directory_fd: int, name: str, flags: int) -> int:
        if not valid_filename(name) and not name.endswith(".json"):
            raise DestinationError(f"nom de fichier invalide : {name!r}")
        try:
            fd = os.open(name, flags | _O_NOFOLLOW, dir_fd=directory_fd)
            return self._regular_fd(fd, name)
        except FileNotFoundError:
            raise
        except DestinationError:
            raise
        except OSError as exc:
            raise DestinationError(f"ouverture impossible de {name!r} : {exc}") from exc

    @staticmethod
    def _fsync_directory(directory_fd: int) -> None:
        try:
            os.fsync(directory_fd)
        except OSError as exc:
            raise DestinationError(f"synchronisation du répertoire impossible : {exc}") from exc

    def _meta_name(self, filename: str) -> str:
        return filename + ".json"

    def _require_owned(self, directory_fd: int, filename: str) -> None:
        """N'opère que sur un fichier avec sidecar régulier présent."""
        if not valid_filename(filename):
            raise DestinationError(f"nom de fichier invalide : {filename!r}")
        meta_name = self._meta_name(filename)
        fd = -1
        try:
            fd = self._open_file(directory_fd, meta_name, os.O_RDONLY)
            with os.fdopen(fd, "r", encoding="utf-8") as fh:
                fd = -1
                raw = json.load(fh)
            if not isinstance(raw, dict) or set(raw) != _META_KEYS or raw.get("filename") != filename:
                raise DestinationError(f"sidecar invalide pour {filename!r}")
        except FileNotFoundError as exc:
            raise UnknownImageError(f"fichier inconnu de Pasteberth : {filename!r}") from exc
        except (OSError, ValueError, UnicodeError) as exc:
            raise DestinationError(f"sidecar illisible pour {filename!r}") from exc
        finally:
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass

    def _generate_name(self, ext: str) -> str:
        stamp = datetime.now().astimezone().strftime("%Y-%m-%d_%H-%M-%S")
        return f"{stamp}_{secrets.token_hex(3)}{ext}"

    def _write_meta_atomic(self, directory_fd: int, meta: dict) -> None:
        target = meta["filename"] + ".json"
        temp_name = f".pbmeta-{secrets.token_hex(12)}.tmp"
        fd = -1
        try:
            fd = os.open(
                temp_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW,
                0o600,
                dir_fd=directory_fd,
            )
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fd = -1
                json.dump(meta, fh, ensure_ascii=False, separators=(",", ":"))
                fh.flush()
                os.fsync(fh.fileno())
            os.link(
                temp_name,
                target,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
                follow_symlinks=False,
            )
            os.unlink(temp_name, dir_fd=directory_fd)
            self._fsync_directory(directory_fd)
        except BaseException:
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
            try:
                os.unlink(temp_name, dir_fd=directory_fd)
            except OSError:
                pass
            raise

    # -- espace disque -----------------------------------------------------

    def space_info(self) -> SpaceInfo:
        try:
            statvfs = os.statvfs(self.directory)
            total = statvfs.f_blocks * statvfs.f_frsize
            available = statvfs.f_bavail * statvfs.f_frsize
        except OSError as exc:
            raise DestinationError(f"mesure de l'espace libre impossible : {exc}") from exc
        if total <= 0:
            raise DestinationError("filesystem sans capacité mesurable")
        return SpaceInfo(total, available)

    @property
    def device_id(self) -> int:
        try:
            return os.stat(self.directory).st_dev
        except OSError as exc:
            raise DestinationError(f"mesure du filesystem impossible : {exc}") from exc

    def ensure_space(self, incoming_bytes: int, minimum_percent: float) -> None:
        info = self.space_info()
        required = max(0, incoming_bytes) + _SPACE_MARGIN_BYTES
        remaining = info.available_bytes - required
        minimum_bytes = info.total_bytes * minimum_percent / 100.0
        if info.available_bytes < required or remaining < minimum_bytes:
            raise StorageLowError(info, minimum_percent)

    # -- réconciliation ----------------------------------------------------

    def reconcile(self) -> None:
        """Supprime les temporaires et orphelins anciens issus d'un crash."""
        now = time.time()
        try:
            entries = list(os.scandir(self.directory))
        except OSError as exc:
            raise DestinationError(f"lecture impossible de {self.directory} : {exc}") from exc
        for entry in entries:
            if not (entry.name.startswith(".pbmeta-") or valid_filename(entry.name)):
                continue
            try:
                age = now - entry.stat(follow_symlinks=False).st_mtime
                if age < _ORPHAN_GRACE_SECONDS:
                    continue
                if valid_filename(entry.name) and (self.directory / (entry.name + ".json")).exists():
                    continue
                entry_path = self.directory / entry.name
                if entry.is_symlink():
                    continue
                entry_path.unlink()
                log.warning("fichier temporaire/orphelin supprimé : %s", entry.name)
            except OSError:
                log.warning("impossible de réconcilier %s", entry.name)

    # -- API Destination ---------------------------------------------------

    def save(self, data: bytes, info: ImageInfo) -> StoredImage:
        self._ensure_dir()
        ext = extension_for(info.fmt)
        last_exc: Exception | None = None
        with self._directory_fd() as directory_fd:
            for _attempt in range(8):
                filename = self._generate_name(ext)
                try:
                    fd = os.open(
                        filename,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW,
                        0o600,
                        dir_fd=directory_fd,
                    )
                except FileExistsError as exc:
                    last_exc = exc
                    continue
                try:
                    with os.fdopen(fd, "wb") as fh:
                        fh.write(data)
                        fh.flush()
                        os.fsync(fh.fileno())
                except OSError as exc:
                    try:
                        os.unlink(filename, dir_fd=directory_fd)
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
                try:
                    self._write_meta_atomic(
                        directory_fd,
                        {
                            "filename": filename,
                            "created_at": created_at.isoformat(timespec="microseconds"),
                            "width": stored.width,
                            "height": stored.height,
                            "size": stored.size,
                            "format": stored.fmt,
                        },
                    )
                except BaseException as exc:
                    try:
                        os.unlink(filename, dir_fd=directory_fd)
                        self._fsync_directory(directory_fd)
                    except OSError:
                        log.exception("nettoyage impossible après échec de sidecar : %s", filename)
                    if isinstance(exc, DestinationError):
                        raise
                    if not isinstance(exc, Exception):
                        raise
                    raise DestinationError(f"écriture du sidecar impossible ({exc})") from exc
                return stored
        raise DestinationError(f"génération de nom en collision répétée ({last_exc})")

    def list(self) -> list[StoredImage]:
        self._ensure_dir()
        items: list[StoredImage] = []
        orphan_sidecars: list[str] = []
        try:
            entries = sorted(os.scandir(self.directory), key=lambda e: e.name)
        except OSError as exc:
            raise DestinationError(f"lecture impossible de {self.directory} : {exc}") from exc

        with self._directory_fd() as directory_fd:
            for entry in entries:
                if not entry.name.endswith(".json") or entry.name.startswith("."):
                    continue
                try:
                    fd = self._open_file(directory_fd, entry.name, os.O_RDONLY)
                    with os.fdopen(fd, "r", encoding="utf-8") as fh:
                        raw = json.load(fh)
                except (OSError, ValueError, UnicodeError, DestinationError):
                    log.warning("sidecar illisible, ignoré : %s", entry.name)
                    continue
                if not isinstance(raw, dict) or set(raw) != _META_KEYS:
                    log.warning("sidecar invalide, ignoré : %s", entry.name)
                    continue
                filename = raw.get("filename")
                if not valid_filename(filename) or entry.name != filename + ".json":
                    log.warning("sidecar incohérent, ignoré : %s", entry.name)
                    continue
                try:
                    image_fd = self._open_file(directory_fd, filename, os.O_RDONLY)
                except FileNotFoundError:
                    orphan_sidecars.append(entry.name)
                    continue
                except (OSError, DestinationError):
                    log.warning("image liée au sidecar illisible, ignorée : %s", entry.name)
                    continue
                try:
                    actual_size = os.fstat(image_fd).st_size
                finally:
                    os.close(image_fd)
                try:
                    created_at = datetime.fromisoformat(raw["created_at"])
                    if created_at.tzinfo is None or created_at.utcoffset() is None:
                        raise ValueError("date sans fuseau")
                    created_at = created_at.astimezone(timezone.utc)
                    width = raw["width"]
                    height = raw["height"]
                    size = raw["size"]
                    fmt = raw["format"]
                    if any(isinstance(v, bool) or not isinstance(v, int) for v in (width, height, size)):
                        raise ValueError("types numériques invalides")
                    if not (1 <= width <= MAX_DIMENSION and 1 <= height <= MAX_DIMENSION):
                        raise ValueError("dimensions invalides")
                    if width * height > HARD_MAX_PIXELS or size < 0 or size != actual_size:
                        raise ValueError("métadonnées incohérentes")
                    if fmt not in FORMATS:
                        raise ValueError("format invalide")
                    item = StoredImage(filename, created_at, width, height, size, fmt)
                except (TypeError, ValueError, KeyError):
                    log.warning("métadonnées invalides, ignorées : %s", entry.name)
                    continue
                items.append(item)

            for meta_name in orphan_sidecars:
                try:
                    os.unlink(meta_name, dir_fd=directory_fd)
                    log.info("sidecar orphelin supprimé : %s", meta_name)
                except OSError:
                    log.warning("sidecar orphelin impossible à supprimer : %s", meta_name)
        items.sort(key=lambda i: (i.created_at, i.filename), reverse=True)
        return items

    def delete(self, filename: str) -> None:
        with self._directory_fd() as directory_fd:
            self._require_owned(directory_fd, filename)
            try:
                os.unlink(filename, dir_fd=directory_fd)
            except FileNotFoundError:
                pass
            except OSError as exc:
                raise DestinationError(f"suppression impossible ({exc})") from exc
            try:
                os.unlink(self._meta_name(filename), dir_fd=directory_fd)
            except FileNotFoundError:
                pass
            except OSError as exc:
                raise DestinationError(f"suppression du sidecar impossible ({exc})") from exc
            self._fsync_directory(directory_fd)

    def read(self, filename: str) -> bytes:
        with self._directory_fd() as directory_fd:
            self._require_owned(directory_fd, filename)
            try:
                fd = self._open_file(directory_fd, filename, os.O_RDONLY)
                with os.fdopen(fd, "rb") as fh:
                    return fh.read()
            except FileNotFoundError as exc:
                raise UnknownImageError(f"fichier inconnu de Pasteberth : {filename!r}") from exc
            except (OSError, DestinationError) as exc:
                if isinstance(exc, DestinationError):
                    raise
                raise DestinationError(f"lecture impossible ({exc})") from exc

    def reference_path(self, filename: str) -> str:
        return str(self.directory / filename)

    def apply_retention(self, retain: int, protected_filename: str | None = None) -> list[str]:
        """Supprime les plus anciennes images au-delà de ``retain``.

        L'image nouvellement créée peut être protégée contre une horloge
        murale reculée ou des sidecars historiques dans le futur.
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
                log.info("rétention : suppression de %s", item.filename)
            except DestinationError as exc:
                failures.append(item.filename)
                log.error("rétention : échec sur %s : %s", item.filename, exc)
        if failures:
            raise RetentionError(failures)
        return deleted
