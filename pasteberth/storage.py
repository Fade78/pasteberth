"""Destinations de stockage et rétention circulaire par zone.

La destination locale suppose un répertoire privé au processus Pasteberth.
Les accès aux fichiers passent par un descripteur de répertoire et refusent
les liens symboliques afin que la preuve de propriété du sidecar ne devienne
pas une primitive de lecture ou de suppression arbitraire.
"""
from __future__ import annotations

import ctypes
import errno
import fcntl
import json
import logging
import os
import re
import secrets
import stat
import unicodedata
from abc import ABC, abstractmethod
from contextvars import ContextVar
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from pasteberth.images import (
    FORMATS,
    HARD_MAX_PIXELS,
    MAX_MIME_LENGTH,
    MAX_DIMENSION,
    ImageInfo,
    mime_for,
)
from pasteberth.paths import first_symlink_component, open_directory

log = logging.getLogger("pasteberth.storage")

_GENERATED_FILENAME_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}_[0-9a-f]{6}\.[a-z0-9]{1,10}$"
)
_CLIENT_FILENAME_RE = re.compile(r"^[^/\\\x00\r\n]{1,200}$")
_META_KEYS = {"filename", "created_at", "width", "height", "size", "format"}
# kind/mime ajoutés en v1.0.3 ; les sidecars v1.0.1/v1.0.2 (6 clés) restent valides.
_META_KEYS_NEW = _META_KEYS | {"kind", "mime"}
_MIME_RE = re.compile(r"^[A-Za-z0-9!#$%&'*+.^_`|~-]+/[A-Za-z0-9!#$%&'*+.^_`|~-]+$")
_TEXT_MIMES = {"application/json", "application/xml", "application/x-yaml"}
_MAX_META_BYTES = 64 * 1024


def _meta_keys_ok(raw: dict) -> bool:
    return set(raw) in (_META_KEYS, _META_KEYS_NEW)
_SPACE_MARGIN_BYTES = 64 * 1024
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
_RENAME_NOREPLACE = 1

try:
    _libc = ctypes.CDLL(None, use_errno=True)
    _renameat2 = _libc.renameat2
    _renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    _renameat2.restype = ctypes.c_int
except (AttributeError, OSError):
    _renameat2 = None

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


def valid_filename(name: object) -> bool:
    if not isinstance(name, str) or not name.strip():
        return False
    if name in {".", "..", ".pasteberth.lock"}:
        return False
    if name.startswith(
        (".pbmeta-", ".pbdata-", ".pbbackup-", ".pbtxn-", ".pbtrash-", ".pbrename-")
    ):
        return False
    try:
        if len(name.encode("utf-8")) > 240:
            return False
    except UnicodeEncodeError:
        return False
    if any(unicodedata.category(char).startswith("C") for char in name):
        return False
    return bool(_CLIENT_FILENAME_RE.fullmatch(name))


def generated_filename(name: object) -> bool:
    """Indique si un nom provient du générateur interne historique."""
    return isinstance(name, str) and bool(_GENERATED_FILENAME_RE.fullmatch(name))


def _rename_noreplace(directory_fd: int, source: str, target: str) -> None:
    """Déplace ``source`` vers ``target`` sans jamais remplacer la cible."""
    if _renameat2 is not None:
        result = _renameat2(
            directory_fd,
            os.fsencode(source),
            directory_fd,
            os.fsencode(target),
            _RENAME_NOREPLACE,
        )
        if result == 0:
            return
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number), source, target)

    # Linux provides renameat2 in all supported deployments. The fallback is
    # only for older Unix libc implementations; link() still refuses an
    # occupied destination, but source removal is necessarily less atomic.
    try:
        source_info = os.stat(
            source,
            dir_fd=directory_fd,
            follow_symlinks=False,
        )
        source_identity = (source_info.st_dev, source_info.st_ino)
    except OSError:
        raise
    os.link(
        source,
        target,
        src_dir_fd=directory_fd,
        dst_dir_fd=directory_fd,
        follow_symlinks=False,
    )
    # If unlinking the source fails, or the source was replaced meanwhile,
    # leave the extra hardlink in place. Removing it would require a second
    # race-prone identity check and could delete a foreign target that
    # appeared after link().
    try:
        current_info = os.stat(
            source,
            dir_fd=directory_fd,
            follow_symlinks=False,
        )
    except OSError:
        return
    if (current_info.st_dev, current_info.st_ino) != source_identity:
        return
    os.unlink(source, dir_fd=directory_fd)


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


class DestinationBusyError(DestinationError):
    """La destination est déjà verrouillée par une autre opération."""


class StorageLowError(DestinationError):
    """L'écriture ferait franchir le seuil d'espace libre configuré."""

    def __init__(self, info: SpaceInfo, minimum_percent: float):
        self.info = info
        self.minimum_percent = minimum_percent
        super().__init__(
            f"espace libre insuffisant ({info.available_percent:.2f}% disponible, "
            f"minimum {minimum_percent:.2f}%)"
        )


class StorageConflictError(DestinationError):
    """Le nom cible existe sans sidecar cohérent (fichier étranger)."""


class ReplacementRequiredError(DestinationError):
    """Une paire Pasteberth existante exige un remplacement explicite."""


class RetentionError(DestinationError):
    """Au moins une suppression de rétention a échoué."""

    def __init__(self, failures: list[str]):
        self.failures = failures
        super().__init__(f"{len(failures)} suppression(s) de rétention impossible(s)")


class Destination(ABC):
    """Interface pragmatique : future SshDestination => mêmes méthodes."""

    @abstractmethod
    def save(
        self,
        data: bytes,
        info: ImageInfo,
        filename: str | None = None,
        *,
        allow_replace: bool = False,
    ) -> StoredImage: ...

    @abstractmethod
    def list(self) -> list[StoredImage]:
        """Historique, de la plus récente à la plus ancienne."""

    @abstractmethod
    def delete(self, filename: str, *, allow_stale_sidecar: bool = False) -> None: ...

    @abstractmethod
    def rename(
        self,
        source: str,
        target: str,
    ) -> StoredImage: ...

    @abstractmethod
    def read(self, filename: str) -> bytes: ...

    @abstractmethod
    def open_read(self, filename: str):
        """Ouvre un fichier géré pour une lecture maintenue sous verrou."""
        ...

    @abstractmethod
    def reference_path(self, filename: str) -> str:
        """Chemin tel que le voit le harness (base de la référence)."""


class LocalDestination(Destination):
    def __init__(self, directory: Path, *, create_directory: bool = True):
        self.directory = Path(directory)
        self.create_directory = create_directory
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

    def _ensure_dir(self) -> None:
        # Feature: on n'inspecte plus les permissions ici — les répertoires
        # partagés sont acceptés (avertissement au démarrage via config.py).
        # Refuser au runtime casserait les zones partagées légitimes et
        # pousserait à contourner la protection.
        try:
            symlink = first_symlink_component(self.directory)
        except (OSError, ValueError) as exc:
            raise DestinationError(
                f"inspection impossible de {self.directory} : {exc}"
            ) from exc
        if symlink is not None:
            raise DestinationError(f"chemin zone symbolique refusé : {symlink}")
        try:
            fd = open_directory(
                self.directory,
                create=self.create_directory,
                mode=0o700,
            )
            os.close(fd)
        except FileNotFoundError as exc:
            if not self.create_directory:
                raise DestinationError(f"répertoire inexistant : {self.directory}") from exc
            raise DestinationError(
                f"impossible de créer {self.directory} : {exc}"
            ) from exc
        except (OSError, ValueError) as exc:
            raise DestinationError(
                f"impossible d'ouvrir {self.directory} : {exc}"
            ) from exc

    @contextmanager
    def _directory_fd(self):
        self._ensure_dir()
        try:
            fd = open_directory(self.directory)
        except OSError as exc:
            raise DestinationError(f"ouverture impossible de {self.directory} : {exc}") from exc
        try:
            yield fd
        finally:
            os.close(fd)

    @contextmanager
    def operation_lock(self, *, exclusive: bool, blocking: bool = True):
        """Verrouille les opérations même entre processus du même utilisateur."""
        lock_name = ".pasteberth.lock"
        with self._directory_fd() as directory_fd:
            fd = -1
            locked = False
            try:
                fd = os.open(
                    lock_name,
                    os.O_RDWR | os.O_CREAT | _O_NOFOLLOW,
                    0o600,
                    dir_fd=directory_fd,
                )
                try:
                    fd = self._regular_fd(fd, lock_name)
                except BaseException:
                    fd = -1
                    raise
                os.fchmod(fd, 0o600)
                lock_flags = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
                if not blocking:
                    lock_flags |= fcntl.LOCK_NB
                try:
                    fcntl.flock(fd, lock_flags)
                except OSError as exc:
                    if not blocking and exc.errno in (errno.EACCES, errno.EAGAIN):
                        raise DestinationBusyError(
                            f"destination occupée : {self.directory}"
                        ) from exc
                    raise
                locked = True
                yield
            except DestinationError:
                raise
            except OSError as exc:
                raise DestinationError(
                    f"verrouillage impossible de {self.directory} : {exc}"
                ) from exc
            finally:
                if fd >= 0:
                    try:
                        if locked:
                            fcntl.flock(fd, fcntl.LOCK_UN)
                    finally:
                        os.close(fd)

    @staticmethod
    def _regular_fd(fd: int, name: str) -> int:
        try:
            info = os.fstat(fd)
        except OSError:
            os.close(fd)
            raise
        if not stat.S_ISREG(info.st_mode):
            os.close(fd)
            raise DestinationError(f"fichier non régulier : {name!r}")
        uid_getter = getattr(os, "getuid", None)
        uid = uid_getter() if uid_getter is not None else None
        if uid is not None and getattr(info, "st_uid", uid) != uid:
            os.close(fd)
            raise DestinationError(f"fichier non détenu par le processus : {name!r}")
        return fd

    def _open_file(self, directory_fd: int, name: str, flags: int) -> int:
        is_sidecar_name = (
            isinstance(name, str)
            and name.endswith(".json")
            and valid_filename(name[:-5])
        )
        if not valid_filename(name) and not is_sidecar_name and not _internal_marker_name(name):
            raise DestinationError(f"nom de fichier invalide : {name!r}")
        try:
            fd = os.open(name, flags | _O_NOFOLLOW | _O_NONBLOCK, dir_fd=directory_fd)
            return self._regular_fd(fd, name)
        except FileNotFoundError:
            raise
        except DestinationError:
            raise
        except OSError as exc:
            raise DestinationError(f"ouverture impossible de {name!r} : {exc}") from exc

    def _write_transaction_file(self, directory_fd: int, name: str, transaction: dict) -> None:
        """Publie un marqueur de transaction sans remplacer un nom existant."""
        if not _internal_marker_name(name):
            raise ValueError(f"nom de transaction invalide : {name!r}")
        temp_name = name.rsplit(".", 1)[0] + ".tmp"
        fd = -1
        temp_identity = None
        try:
            fd = os.open(
                temp_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW,
                0o600,
                dir_fd=directory_fd,
            )
            info = os.fstat(fd)
            temp_identity = (info.st_dev, info.st_ino)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fd = -1
                json.dump(transaction, fh, ensure_ascii=False, separators=(",", ":"))
                fh.flush()
                os.fsync(fh.fileno())
            self._move_expected(directory_fd, temp_name, name, temp_identity)
            self._journal_identities[name] = temp_identity
            self._fsync_directory(directory_fd)
        except BaseException:
            if fd >= 0:
                try:
                    os.close(fd)
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
            raise ValueError(f"identité de transaction invalide : {key}")
        if any(isinstance(part, bool) or not isinstance(part, int) for part in value):
            raise ValueError(f"identité de transaction invalide : {key}")
        return (value[0], value[1])

    @staticmethod
    def _parse_transaction(marker_name: str, raw: object) -> dict:
        if not isinstance(raw, dict) or set(raw) not in (
            _TXN_KEYS,
            _TXN_KEYS_WITHOUT_GUARDS,
            _TXN_KEYS_WITHOUT_META_GUARD,
        ):
            raise ValueError("marqueur de transaction invalide")
        raw = dict(raw)
        token = _txn_token(marker_name)
        if token is None or raw["version"] != 1 or raw["state"] not in ("prepared", "committed"):
            raise ValueError("marqueur de transaction invalide")
        if (
            (_TXN_MARKER_RE.fullmatch(marker_name) and raw["state"] != "prepared")
            or (_TXN_COMMIT_RE.fullmatch(marker_name) and raw["state"] != "committed")
        ):
            raise ValueError("état de transaction incohérent")
        target = raw["target"]
        if not valid_filename(target):
            raise ValueError("cible de transaction invalide")
        expected_names = {
            "data_backup": f".pbbackup-{token}.data",
            "meta_backup": f".pbbackup-{token}.json",
        }
        if any(raw[key] != value for key, value in expected_names.items()):
            raise ValueError("fichiers de transaction incohérents")
        if not _DATA_TEMP_RE.fullmatch(raw["data_temp"]):
            raise ValueError("temporaire de données invalide")
        if not _META_TEMP_RE.fullmatch(raw["meta_temp"]):
            raise ValueError("temporaire de sidecar invalide")
        if raw.get("data_guard") is not None:
            if raw["data_guard"] != f".pbtxn-guard-{token}.data":
                raise ValueError("garde de données incohérente")
        if raw.get("meta_guard") is not None:
            if raw["meta_guard"] != f".pbtxn-guard-{token}.json":
                raise ValueError("garde de sidecar incohérente")
        raw.setdefault("data_guard", None)
        raw.setdefault("meta_guard", None)
        for key in ("target_identity", "meta_identity"):
            LocalDestination._transaction_identity(raw, key)
        for key in ("new_data_identity", "new_meta_identity"):
            if LocalDestination._transaction_identity(raw, key) is None:
                raise ValueError(f"identité de transaction absente : {key}")
        return raw

    @staticmethod
    def _parse_delete_transaction(marker_name: str, raw: object) -> dict:
        if not isinstance(raw, dict) or set(raw) != _DELETE_KEYS:
            raise ValueError("marqueur de suppression invalide")
        token = _delete_token(marker_name)
        if token is None or raw["version"] != 1:
            raise ValueError("marqueur de suppression invalide")
        target = raw["target"]
        if not valid_filename(target):
            raise ValueError("cible de suppression invalide")
        if (
            raw["data_trash"] != f".pbtrash-{token}.data"
            or raw["meta_trash"] != f".pbtrash-{token}.json"
        ):
            raise ValueError("fichiers de suppression incohérents")
        for key in ("target_identity", "meta_identity"):
            if LocalDestination._transaction_identity(raw, key) is None:
                raise ValueError(f"identité de suppression absente : {key}")
        return raw

    @staticmethod
    def _parse_rename_transaction(marker_name: str, raw: object) -> dict:
        if not isinstance(raw, dict) or set(raw) not in (
            _RENAME_KEYS,
            _RENAME_KEYS_WITHOUT_GUARDS,
            _RENAME_KEYS_WITHOUT_META_GUARDS,
            _LEGACY_RENAME_KEYS,
        ):
            raise ValueError("marqueur de renommage invalide")
        raw = dict(raw)
        token = _rename_token(marker_name)
        if token is None or raw["version"] != 1 or raw["state"] not in (
            "prepared",
            "committed",
        ):
            raise ValueError("marqueur de renommage invalide")
        if (
            (_RENAME_MARKER_RE.fullmatch(marker_name) and raw["state"] != "prepared")
            or (_RENAME_COMMIT_RE.fullmatch(marker_name) and raw["state"] != "committed")
        ):
            raise ValueError("état de renommage incohérent")
        source = raw["source"]
        target = raw["target"]
        if not valid_filename(source) or not valid_filename(target) or source == target:
            raise ValueError("noms de renommage invalides")
        if raw["meta_backup"] != f".pbrename-backup-{token}.json":
            raise ValueError("sauvegarde de renommage incohérente")
        if "data_backup" in raw:
            if raw["data_backup"] != f".pbrename-backup-{token}.data":
                raise ValueError("sauvegarde de données incohérente")
        else:
            # Markers created before the data backup was added remain
            # recoverable; they simply retain the old best-effort semantics.
            raw["data_backup"] = None
        if raw.get("data_guard") is not None:
            if raw["data_guard"] != f".pbrename-guard-{token}.data":
                raise ValueError("garde de données incohérente")
        if raw.get("meta_guard") is not None:
            if raw["meta_guard"] != f".pbrename-guard-{token}.json":
                raise ValueError("garde de sidecar incohérente")
        if raw.get("meta_backup_guard") is not None:
            if raw["meta_backup_guard"] != f".pbrename-backup-{token}.guard.json":
                raise ValueError("garde de sauvegarde de sidecar incohérente")
        raw.setdefault("data_guard", None)
        raw.setdefault("meta_guard", None)
        raw.setdefault("meta_backup_guard", None)
        if not _META_TEMP_RE.fullmatch(raw["meta_temp"]):
            raise ValueError("temporaire de renommage invalide")
        for key in ("source_identity", "source_meta_identity", "new_meta_identity"):
            if LocalDestination._transaction_identity(raw, key) is None:
                raise ValueError(f"identité de renommage absente : {key}")
        return raw

    def _active_transaction_names(self, directory_fd: int) -> set[str]:
        names: set[str] = set()
        try:
            with os.scandir(directory_fd) as scan:
                entries = list(scan)
        except OSError as exc:
            raise DestinationError(
                f"lecture impossible de {self.directory} : {exc}"
            ) from exc
        for entry in entries:
            try:
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

    def _remember_journal_entry(self, directory_fd: int, entry: os.DirEntry) -> None:
        try:
            self._journal_identities[entry.name] = (
                os.fstat(directory_fd).st_dev,
                entry.inode(),
            )
        except OSError:
            pass

    def _journal_identity(self, directory_fd: int, name: str) -> tuple[int, int] | None:
        expected = self._journal_identities.get(name)
        if expected is not None:
            return expected
        return self._entry_identity(directory_fd, name)

    def _unlink_expected(
        self,
        directory_fd: int,
        name: str,
        expected: tuple[int, int] | None,
    ) -> bool:
        try:
            info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            return True
        if not stat.S_ISREG(info.st_mode):
            raise DestinationError(f"fichier temporaire non régulier : {name!r}")
        uid_getter = getattr(os, "getuid", None)
        if uid_getter is not None and info.st_uid != uid_getter():
            return False
        actual = (info.st_dev, info.st_ino)
        if expected is not None and actual != expected:
            return False
        quarantine_name = f".pbtrash-{secrets.token_hex(12)}.data"
        try:
            self._move_expected(directory_fd, name, quarantine_name, actual)
        except (DestinationError, OSError):
            return False
        try:
            quarantine_info = os.stat(
                quarantine_name,
                dir_fd=directory_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return True
        if not stat.S_ISREG(quarantine_info.st_mode):
            return False
        if (quarantine_info.st_dev, quarantine_info.st_ino) != actual:
            return False
        try:
            os.unlink(quarantine_name, dir_fd=directory_fd)
        except FileNotFoundError:
            return True
        except OSError:
            return False
        return True

    def _restore_noreplace(
        self,
        directory_fd: int,
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

    @staticmethod
    def _entry_identity_any(directory_fd: int, name: str) -> tuple[int, int] | None:
        try:
            info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        return (info.st_dev, info.st_ino)

    def _move_expected(
        self,
        directory_fd: int,
        source: str,
        target: str,
        expected: tuple[int, int],
    ) -> None:
        if self._entry_identity(directory_fd, source) != expected:
            raise StorageConflictError(f"fichier modifié pendant l'opération : {source!r}")
        try:
            _rename_noreplace(directory_fd, source, target)
        except FileExistsError as exc:
            raise StorageConflictError(f"cible apparue pendant l'opération : {target!r}") from exc
        actual = self._entry_identity_any(directory_fd, target)
        if actual == expected:
            return
        if actual is not None:
            self._restore_any(directory_fd, target, source, actual)
        raise StorageConflictError(f"fichier étranger apparu pendant l'opération : {source!r}")

    def _link_expected(
        self,
        directory_fd: int,
        source: str,
        target: str,
        expected: tuple[int, int],
    ) -> None:
        """Crée un lien de sauvegarde sans remplacer une entrée concurrente."""
        if self._entry_identity(directory_fd, source) != expected:
            raise StorageConflictError(f"fichier modifié pendant l'opération : {source!r}")
        try:
            os.link(
                source,
                target,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
                follow_symlinks=False,
            )
        except FileExistsError as exc:
            raise StorageConflictError(f"cible apparue pendant l'opération : {target!r}") from exc
        if self._entry_identity_any(directory_fd, target) != expected:
            raise StorageConflictError(f"fichier étranger apparu pendant l'opération : {target!r}")

    def _restore_any(
        self,
        directory_fd: int,
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
        directory_fd: int,
        name: str,
        expected: tuple[int, int] | None,
    ) -> bool:
        """Retire une entrée en la déplaçant d'abord hors de son nom public."""
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
        """Rattache une copie privée au journal si une cible étrangère bloque la restauration."""
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

    @staticmethod
    def _recovery_names_for_identity(
        directory_fd: int,
        expected: tuple[int, int] | None,
    ) -> tuple[str, ...]:
        if expected is None:
            return ()
        try:
            with os.scandir(directory_fd) as scan:
                return tuple(
                    entry.name
                    for entry in scan
                    if _TRASH_RE.fullmatch(entry.name)
                    and LocalDestination._entry_identity_any(directory_fd, entry.name)
                    == expected
                )
        except OSError:
            return ()

    def _remove_anonymous_recovery(
        self,
        directory_fd: int,
        *identities: tuple[int, int] | None,
    ) -> None:
        """Retire les copies de récupération anonymes devenues inutiles."""
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
        """Conserve un journal si la paire publique disparaît pendant cleanup."""
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
        """Conserve un journal préparé tant qu'une ancienne paire reste cachée."""
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
        """Conserve un tombstone si une paire publique est devenue étrangère."""
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
        """Conserve le journal d'annulation avec une copie de la source."""
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
                log.warning("tombstone de suppression impossible à conserver : %s", marker_name)
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
                log.warning("tombstone de suppression impossible à conserver : %s", marker_name)
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

    def _recover_deletions(self, directory_fd: int, entries: list[os.DirEntry]) -> set[str]:
        protected: set[str] = set()
        for entry in entries:
            if _delete_token(entry.name) is None:
                continue
            protected.add(entry.name)
            self._remember_journal_entry(directory_fd, entry)
            try:
                transaction = self._parse_delete_transaction(
                    entry.name,
                    self._read_meta(directory_fd, entry.name),
                )
            except (DestinationError, ValueError, TypeError, KeyError):
                log.warning("marqueur de suppression invalide, conservé : %s", entry.name)
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
                    log.warning("récupération de suppression différée : %s", entry.name)
            except (DestinationError, OSError):
                log.warning("récupération de suppression impossible : %s", entry.name)
        return protected

    def _rollback_rename_transaction(
        self,
        directory_fd: int,
        transaction: dict,
        marker_name: str,
    ) -> bool:
        """Annule un renommage préparé sans écraser une entrée étrangère."""
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
                log.warning("journal de renommage impossible à conserver : %s", marker_name)
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
                log.warning("journal de renommage impossible à conserver : %s", marker_name)
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
                    log.warning("journal de renommage impossible à conserver : %s", commit_name)
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
                    log.warning("journal de renommage impossible à conserver : %s", commit_name)
        return result

    def _cleanup_committed_rename_body(
        self,
        directory_fd: int,
        transaction: dict,
        marker_name: str,
        commit_name: str,
    ) -> bool:
        """Supprime les artefacts d'un renommage durablement publié."""
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

    def _recover_renames(self, directory_fd: int, entries: list[os.DirEntry]) -> set[str]:
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
                log.warning("marqueur de renommage invalide, conservé : %s", entry.name)
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
                    log.warning("nettoyage de renommage différé : %s", commit_name)
            except (DestinationError, OSError):
                log.warning("récupération de renommage validée impossible : %s", commit_name)

        for token, (marker_name, transaction) in markers.items():
            if token in commits:
                continue
            try:
                if not self._rollback_rename_transaction(
                    directory_fd, transaction, marker_name
                ):
                    log.warning("annulation de renommage différée : %s", marker_name)
            except (DestinationError, OSError):
                log.warning("annulation de renommage impossible : %s", marker_name)
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
                log.warning("journal de transaction impossible à conserver : %s", marker_name)
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
                log.warning("journal de transaction impossible à conserver : %s", marker_name)
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
        directory_fd: int,
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
                    log.warning("journal de transaction impossible à conserver : %s", commit_name)
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
                    log.warning("journal de transaction impossible à conserver : %s", commit_name)
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

    def _recover_transactions(self, directory_fd: int, entries: list[os.DirEntry]) -> set[str]:
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
                log.warning("marqueur de transaction invalide, conservé : %s", entry.name)
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
                log.warning("récupération de transaction validée impossible : %s", commit_name)

        for token, (marker_name, transaction) in markers.items():
            if token in commits:
                continue
            try:
                self._rollback_transaction(directory_fd, transaction, marker_name)
            except (DestinationError, OSError):
                log.warning("annulation de transaction impossible : %s", marker_name)
        return protected

    @staticmethod
    def _fsync_directory(directory_fd: int) -> None:
        try:
            os.fsync(directory_fd)
        except OSError as exc:
            raise DestinationError(f"synchronisation du répertoire impossible : {exc}") from exc

    def _meta_name(self, filename: str) -> str:
        return filename + ".json"

    def _read_meta(self, directory_fd: int, name: str) -> dict:
        """Lit un sidecar depuis un descripteur, avec une taille bornée."""
        fd = -1
        try:
            fd = self._open_file(directory_fd, name, os.O_RDONLY)
            with os.fdopen(fd, "rb") as fh:
                fd = -1
                encoded = fh.read(_MAX_META_BYTES + 1)
        except FileNotFoundError:
            raise
        except DestinationError:
            raise
        except OSError as exc:
            raise DestinationError(f"lecture impossible du sidecar {name!r}") from exc
        finally:
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
        if len(encoded) > _MAX_META_BYTES:
            raise DestinationError(f"sidecar trop volumineux : {name!r}")
        try:
            raw = json.loads(encoded.decode("utf-8"))
        except (UnicodeError, ValueError, RecursionError) as exc:
            raise DestinationError(f"sidecar illisible : {name!r}") from exc
        if not isinstance(raw, dict):
            raise DestinationError(f"sidecar invalide : {name!r}")
        return raw

    @staticmethod
    def _validated_item(
        raw: dict,
        filename: str,
        actual_size: int | None = None,
    ) -> StoredImage:
        """Valide un sidecar avant toute lecture, suppression ou remplacement."""
        if not _meta_keys_ok(raw) or raw.get("filename") != filename:
            raise ValueError("sidecar incohérent")
        created_at = datetime.fromisoformat(raw["created_at"])
        if created_at.tzinfo is None or created_at.utcoffset() is None:
            raise ValueError("date sans fuseau")
        created_at = created_at.astimezone(timezone.utc)
        width = raw["width"]
        height = raw["height"]
        size = raw["size"]
        fmt = raw["format"]
        kind = raw.get("kind", "image")
        if kind not in ("image", "text", "binary"):
            raise ValueError("kind invalide")
        mime = raw.get("mime")
        if mime is None:
            mime = mime_for(fmt) if kind == "image" else "application/octet-stream"
        if (
            not isinstance(mime, str)
            or not _MIME_RE.fullmatch(mime)
            or len(mime) > MAX_MIME_LENGTH
        ):
            raise ValueError("mime invalide")
        if isinstance(size, bool) or not isinstance(size, int):
            raise ValueError("types numériques invalides")
        if kind == "image":
            if any(isinstance(v, bool) or not isinstance(v, int) for v in (width, height)):
                raise ValueError("types numériques invalides")
            if not (1 <= width <= MAX_DIMENSION and 1 <= height <= MAX_DIMENSION):
                raise ValueError("dimensions invalides")
            if width * height > HARD_MAX_PIXELS:
                raise ValueError("métadonnées incohérentes")
            if fmt not in FORMATS:
                raise ValueError("format invalide")
            if mime != mime_for(fmt):
                raise ValueError("mime image incohérent")
        elif kind == "text":
            if width is not None or height is not None or fmt is not None:
                raise ValueError("dimensions ou format inattendus")
            if not (mime.startswith("text/") or mime in _TEXT_MIMES):
                raise ValueError("mime texte invalide")
        else:
            if width is not None or height is not None:
                raise ValueError("dimensions inattendues")
            if fmt is not None:
                raise ValueError("format inattendu")
            if mime != "application/octet-stream":
                raise ValueError("mime binaire invalide")
        if size < 0 or (actual_size is not None and size != actual_size):
            raise ValueError("métadonnées incohérentes")
        return StoredImage(filename, created_at, width, height, size, fmt, kind, mime)

    def _require_owned(
        self,
        directory_fd: int,
        filename: str,
        *,
        allow_stale_sidecar: bool = False,
    ) -> tuple[int, tuple[int, int]]:
        """N'opère que sur un fichier avec sidecar régulier présent."""
        if not valid_filename(filename):
            raise DestinationError(f"nom de fichier invalide : {filename!r}")
        meta_name = self._meta_name(filename)
        fd = -1
        try:
            fd = self._open_file(directory_fd, filename, os.O_RDONLY)
            # Keep the opened inode while checking the sidecar. A replacement
            # of the public name cannot redirect read() to a foreign inode.
            meta_identity = self._entry_identity(directory_fd, meta_name)
            raw = self._read_meta(directory_fd, meta_name)
            if self._entry_identity(directory_fd, meta_name) != meta_identity:
                raise StorageConflictError(f"sidecar modifié pendant l'opération : {filename!r}")
            item = self._validated_item(raw, filename)
            if not allow_stale_sidecar and os.fstat(fd).st_size != item.size:
                raise ValueError("taille incohérente")
            os.lseek(fd, 0, os.SEEK_SET)
            if meta_identity is None:
                raise UnknownImageError(f"fichier inconnu de Pasteberth : {filename!r}")
            return fd, meta_identity
        except FileNotFoundError as exc:
            if fd >= 0:
                os.close(fd)
            raise UnknownImageError(f"fichier inconnu de Pasteberth : {filename!r}") from exc
        except (DestinationError, TypeError, ValueError, KeyError) as exc:
            if fd >= 0:
                os.close(fd)
            raise DestinationError(f"sidecar illisible pour {filename!r}") from exc

    def _generate_name(self, ext: str) -> str:
        stamp = datetime.now().astimezone().strftime("%Y-%m-%d_%H-%M-%S")
        return f"{stamp}_{secrets.token_hex(3)}{ext}"

    def _write_meta_atomic(self, directory_fd: int, meta: dict) -> None:
        target = meta["filename"] + ".json"
        temp_name = self._write_meta_temp(directory_fd, meta)
        temp_identity = self._entry_identity(directory_fd, temp_name)
        if temp_identity is None:
            raise DestinationError(f"sidecar temporaire disparu : {target!r}")
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

    def _write_meta_temp(self, directory_fd: int, meta: dict) -> str:
        temp_name = f".pbmeta-{secrets.token_hex(12)}.tmp"
        fd = -1
        temp_identity = None
        try:
            fd = os.open(
                temp_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW,
                0o600,
                dir_fd=directory_fd,
            )
            info = os.fstat(fd)
            temp_identity = (info.st_dev, info.st_ino)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fd = -1
                json.dump(meta, fh, ensure_ascii=False, separators=(",", ":"))
                fh.flush()
                os.fsync(fh.fileno())
            return temp_name
        except BaseException:
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
            if temp_identity is not None:
                try:
                    self._remove_expected(directory_fd, temp_name, temp_identity)
                except (DestinationError, OSError):
                    pass
            raise

    def _write_data_temp(self, directory_fd: int, data: bytes) -> str:
        temp_name = f".pbdata-{secrets.token_hex(12)}.tmp"
        fd = -1
        temp_identity = None
        try:
            fd = os.open(
                temp_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW,
                0o600,
                dir_fd=directory_fd,
            )
            info = os.fstat(fd)
            temp_identity = (info.st_dev, info.st_ino)
            with os.fdopen(fd, "wb") as fh:
                fd = -1
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            return temp_name
        except BaseException:
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
            if temp_identity is not None:
                try:
                    self._remove_expected(directory_fd, temp_name, temp_identity)
                except (DestinationError, OSError):
                    pass
            raise

    def _install_new(self, directory_fd: int, temp_name: str, target_name: str) -> None:
        """Installe un fichier temporaire sans remplacer une création concurrente."""
        expected = self._entry_identity(directory_fd, temp_name)
        if expected is None:
            raise DestinationError(f"temporaire disparu pendant l'écriture : {target_name!r}")
        self._move_expected(directory_fd, temp_name, target_name, expected)

    @staticmethod
    def _entry_exists(directory_fd: int, name: str) -> bool:
        try:
            os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise DestinationError(f"inspection impossible de {name!r} : {exc}") from exc
        # Occupied symlinks and non-regular entries are foreign conflicts too;
        # callers must not try to open or replace them.
        return True

    @staticmethod
    def _entry_identity(directory_fd: int, name: str) -> tuple[int, int] | None:
        try:
            info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise DestinationError(f"inspection impossible de {name!r} : {exc}") from exc
        if not stat.S_ISREG(info.st_mode):
            raise DestinationError(f"fichier non régulier : {name!r}")
        return (info.st_dev, info.st_ino)

    def _save_named(
        self,
        directory_fd: int,
        data: bytes,
        info: ImageInfo,
        filename: str,
        *,
        allow_replace: bool = False,
    ) -> StoredImage:
        meta_name = self._meta_name(filename)
        if filename in self._active_transaction_names(directory_fd):
            raise StorageConflictError(
                f"transaction en cours pour le nom : {filename!r}"
            )
        target_exists = self._entry_exists(directory_fd, filename)
        meta_exists = self._entry_exists(directory_fd, meta_name)
        target_identity: tuple[int, int] | None = None
        meta_identity: tuple[int, int] | None = None
        if target_exists and not meta_exists:
            # Fichier étranger : jamais écrasé, conflit côté client (409).
            raise StorageConflictError(
                f"fichier etranger present sans sidecar : {filename!r}"
            )
        if meta_exists and not target_exists:
            # Sidecar orphelin : état interne incohérent, pas un conflit client.
            raise DestinationError(f"sidecar orphelin sans fichier : {filename!r}")
        if target_exists:
            try:
                owned_fd, meta_identity = self._require_owned(directory_fd, filename)
            except DestinationError as exc:
                # A target with a malformed or stale sidecar is not a managed
                # replacement candidate. Keep it intact and expose a conflict.
                raise StorageConflictError(
                    f"fichier et sidecar incohérents : {filename!r}"
                ) from exc
            try:
                file_stat = os.fstat(owned_fd)
                target_identity = (file_stat.st_dev, file_stat.st_ino)
            finally:
                os.close(owned_fd)
            if not allow_replace:
                raise ReplacementRequiredError(
                    f"remplacement explicite requis pour {filename!r}"
                )

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
        meta = {
            "filename": filename,
            "created_at": created_at.isoformat(timespec="microseconds"),
            "width": stored.width,
            "height": stored.height,
            "size": stored.size,
            "format": stored.fmt,
            "kind": stored.kind,
            "mime": stored.mime,
        }
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
            raise StorageConflictError(f"fichier cible ou sidecar disparu : {filename!r}")
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
            raise DestinationError(f"temporaires de remplacement disparus : {filename!r}")
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
                            "remplacement publié mais cible non vérifiée; "
                            "nettoyage différé"
                        )
                except (DestinationError, OSError) as exc:
                    if not self._transaction_public_pair_is_intact(directory_fd, transaction):
                        raise DestinationError(
                            "remplacement publié mais nettoyage différé"
                        ) from exc
                    # The replacement is published; only private cleanup is
                    # deferred when the public pair remains intact.
                    log.warning("nettoyage de transaction différé : %s", marker_name)
            return stored
        except BaseException:
            if transaction is not None and marker_name is not None and not commit_published:
                try:
                    if not self._rollback_transaction(directory_fd, transaction, marker_name):
                        log.warning("annulation de transaction différée : %s", marker_name)
                except (DestinationError, OSError):
                    log.exception("annulation de transaction impossible : %s", marker_name)
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
                statvfs = os.fstatvfs(directory_fd)
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
            with self._directory_fd() as directory_fd:
                return os.fstat(directory_fd).st_dev
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
        """Réconcilie les fichiers de travail anciens issus d'un crash."""
        with self._directory_fd() as directory_fd:
            try:
                with os.scandir(directory_fd) as scan:
                    entries = list(scan)
            except OSError as exc:
                raise DestinationError(
                    f"lecture impossible de {self.directory} : {exc}"
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
                    "fichier de travail interne sans transaction conservé : %s",
                    entry.name,
                )

    # -- API Destination ---------------------------------------------------

    def save(
        self,
        data: bytes,
        info: ImageInfo,
        filename: str | None = None,
        *,
        allow_replace: bool = False,
    ) -> StoredImage:
        self._ensure_dir()
        if filename is not None:
            if not valid_filename(filename):
                raise DestinationError(f"nom de fichier invalide : {filename!r}")
            with self._directory_fd() as directory_fd:
                return self._save_named(
                    directory_fd,
                    data,
                    info,
                    filename,
                    allow_replace=allow_replace,
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
        raise DestinationError(f"génération de nom en collision répétée ({last_exc})")

    def list(self) -> list[StoredImage]:
        self._ensure_dir()
        items: list[StoredImage] = []
        with self._directory_fd() as directory_fd:
            try:
                with os.scandir(directory_fd) as scan:
                    entries = sorted(scan, key=lambda e: e.name)
            except OSError as exc:
                raise DestinationError(
                    f"lecture impossible de {self.directory} : {exc}"
                ) from exc
            blocked_targets: set[str] = set()
            committed_targets: set[str] = set()
            for entry in entries:
                if _delete_token(entry.name) is not None:
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
                # Les noms à point des fichiers déposés sont légitimes ; les
                # fichiers de travail internes ne sont pas des sidecars.
                if (
                    not entry.name.endswith(".json")
                    or entry.name == ".pasteberth.lock"
                    or entry.name.startswith(
                        (
                            ".pbmeta-",
                            ".pbdata-",
                            ".pbbackup-",
                            ".pbtxn-",
                            ".pbtrash-",
                            ".pbrename-",
                        )
                    )
                    or _internal_marker_name(entry.name)
                ):
                    continue
                try:
                    raw = self._read_meta(directory_fd, entry.name)
                except (OSError, DestinationError):
                    log.warning("sidecar illisible, ignoré : %s", entry.name)
                    continue
                if not _meta_keys_ok(raw):
                    log.warning("sidecar invalide, ignoré : %s", entry.name)
                    continue
                filename = raw.get("filename")
                if not valid_filename(filename) or entry.name != filename + ".json":
                    log.warning("sidecar incohérent, ignoré : %s", entry.name)
                    continue
                if filename in blocked_targets:
                    log.warning("transaction active, élément ignoré : %s", filename)
                    continue
                try:
                    image_fd = self._open_file(directory_fd, filename, os.O_RDONLY)
                except FileNotFoundError:
                    log.warning("sidecar orphelin conservé : %s", entry.name)
                    continue
                except (OSError, DestinationError):
                    log.warning("image liée au sidecar illisible, ignorée : %s", entry.name)
                    continue
                try:
                    actual_size = os.fstat(image_fd).st_size
                finally:
                    os.close(image_fd)
                try:
                    item = self._validated_item(raw, filename, actual_size)
                except (TypeError, ValueError, KeyError):
                    log.warning("métadonnées invalides, ignorées : %s", entry.name)
                    continue
                items.append(item)

        items.sort(key=lambda i: (i.created_at, i.filename), reverse=True)
        return items

    def rename(self, source: str, target: str) -> StoredImage:
        """Renomme une paire gérée sans jamais remplacer une cible."""
        if not valid_filename(source) or not valid_filename(target):
            raise DestinationError("nom de fichier invalide")
        if source == target:
            raise DestinationError("les noms source et cible sont identiques")

        with self._directory_fd() as directory_fd:
            active_names = self._active_transaction_names(directory_fd)
            if source in active_names or target in active_names:
                raise StorageConflictError("transaction en cours sur le renommage")
            owned_fd, source_meta_identity = self._require_owned(directory_fd, source)
            try:
                info = os.fstat(owned_fd)
                source_identity = (info.st_dev, info.st_ino)
                raw = self._read_meta(directory_fd, self._meta_name(source))
                item = self._validated_item(raw, source, info.st_size)
            finally:
                os.close(owned_fd)
            if source_meta_identity is None:
                raise StorageConflictError(f"sidecar disparu : {source!r}")

            target_meta = self._meta_name(target)
            if self._entry_exists(directory_fd, target) or self._entry_exists(
                directory_fd, target_meta
            ):
                raise StorageConflictError(f"cible déjà existante : {target!r}")

            new_meta = dict(raw)
            new_meta["filename"] = target
            meta_temp = self._write_meta_temp(directory_fd, new_meta)
            new_meta_identity = self._entry_identity(directory_fd, meta_temp)
            if new_meta_identity is None:
                raise DestinationError(f"sidecar temporaire disparu : {target_meta!r}")

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
                            "renommage publié mais cible non vérifiée; "
                            "nettoyage différé"
                        )
                except (DestinationError, OSError) as exc:
                    log.warning("nettoyage de renommage différé : %s", marker_name)
                    if not self._rename_public_pair_is_intact(directory_fd, transaction):
                        raise DestinationError(
                            "renommage publié mais nettoyage différé"
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
                )
            except BaseException:
                if not commit_published:
                    try:
                        if not self._rollback_rename_transaction(
                            directory_fd, transaction, marker_name
                        ):
                            log.warning("annulation de renommage différée : %s", marker_name)
                    except (DestinationError, OSError):
                        log.exception("annulation de renommage impossible : %s", marker_name)
                try:
                    if self._entry_identity(directory_fd, meta_temp) == new_meta_identity:
                        self._remove_expected(directory_fd, meta_temp, new_meta_identity)
                except (DestinationError, OSError):
                    pass
                raise

    def delete(self, filename: str, *, allow_stale_sidecar: bool = False) -> None:
        with self._directory_fd() as directory_fd:
            if filename in self._active_transaction_names(directory_fd):
                raise StorageConflictError(
                    f"transaction en cours pour le nom : {filename!r}"
                )
            owned_fd, meta_identity = self._require_owned(
                directory_fd,
                filename,
                allow_stale_sidecar=allow_stale_sidecar,
            )
            try:
                info = os.fstat(owned_fd)
                target_identity = (info.st_dev, info.st_ino)
                meta_name = self._meta_name(filename)
            finally:
                os.close(owned_fd)
            if target_identity is None or meta_identity is None:
                raise StorageConflictError(f"fichier ou sidecar disparu : {filename!r}")
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
                        log.warning("annulation de suppression différée : %s", marker_name)
                except (DestinationError, OSError):
                    log.exception("annulation de suppression impossible : %s", marker_name)
                raise
            try:
                if not self._finish_delete_transaction(directory_fd, transaction, marker_name):
                    log.warning("nettoyage de suppression différé : %s", marker_name)
            except (DestinationError, OSError):
                log.warning("nettoyage de suppression différé : %s", marker_name)

    def read(self, filename: str) -> bytes:
        with self._directory_fd() as directory_fd:
            fd, _meta_identity = self._require_owned(directory_fd, filename)
            try:
                with os.fdopen(fd, "rb") as fh:
                    fd = -1
                    return fh.read()
            except FileNotFoundError as exc:
                raise UnknownImageError(f"fichier inconnu de Pasteberth : {filename!r}") from exc
            except (OSError, DestinationError) as exc:
                if isinstance(exc, DestinationError):
                    raise
                raise DestinationError(f"lecture impossible ({exc})") from exc
            finally:
                if fd >= 0:
                    try:
                        os.close(fd)
                    except OSError:
                        pass

    @contextmanager
    def open_read(self, filename: str):
        """Ouvre un fichier géré sans relâcher le verrou de la destination."""
        with self._directory_fd() as directory_fd:
            fd = -1
            try:
                fd, _meta_identity = self._require_owned(directory_fd, filename)
                with os.fdopen(fd, "rb") as fh:
                    fd = -1
                    yield fh
            except FileNotFoundError as exc:
                raise UnknownImageError(
                    f"fichier inconnu de Pasteberth : {filename!r}"
                ) from exc
            except (OSError, DestinationError) as exc:
                if isinstance(exc, DestinationError):
                    raise
                raise DestinationError(f"lecture impossible ({exc})") from exc
            finally:
                if fd >= 0:
                    try:
                        os.close(fd)
                    except OSError:
                        pass

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
