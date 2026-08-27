"""Destinations de stockage et rétention circulaire par zone.

La destination locale suppose un répertoire privé au processus Pasteberth.
Les accès aux fichiers passent par un descripteur de répertoire et refusent
les liens symboliques afin que la preuve de propriété du sidecar ne devienne
pas une primitive de lecture ou de suppression arbitraire.
"""
from __future__ import annotations

import ctypes
import fcntl
import json
import logging
import os
import re
import secrets
import stat
import time
import unicodedata
from abc import ABC, abstractmethod
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
_ORPHAN_GRACE_SECONDS = 3600.0
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
_DELETE_MARKER_RE = re.compile(r"^\.pbdel-([0-9a-f]{24})\.json$")
_DELETE_TEMP_RE = re.compile(r"^\.pbdel-[0-9a-f]{24}\.tmp$")
_TXN_KEYS = {
    "version",
    "state",
    "target",
    "data_temp",
    "meta_temp",
    "data_backup",
    "meta_backup",
    "target_identity",
    "meta_identity",
    "new_data_identity",
    "new_meta_identity",
}
_DELETE_KEYS = {
    "version",
    "target",
    "data_trash",
    "meta_trash",
    "target_identity",
    "meta_identity",
}


def _txn_token(name: str) -> str | None:
    match = _TXN_MARKER_RE.fullmatch(name) or _TXN_COMMIT_RE.fullmatch(name)
    return match.group(1) if match else None


def _delete_token(name: str) -> str | None:
    match = _DELETE_MARKER_RE.fullmatch(name)
    return match.group(1) if match else None


def _internal_transaction_name(name: object) -> bool:
    return isinstance(name, str) and (
        bool(_TXN_MARKER_RE.fullmatch(name))
        or bool(_TXN_COMMIT_RE.fullmatch(name))
    )


def _internal_marker_name(name: object) -> bool:
    return _internal_transaction_name(name) or (
        isinstance(name, str) and bool(_DELETE_MARKER_RE.fullmatch(name))
    )


def valid_filename(name: object) -> bool:
    if not isinstance(name, str) or not name.strip():
        return False
    if name in {".", "..", ".pasteberth.lock"}:
        return False
    if name.startswith((".pbmeta-", ".pbdata-", ".pbbackup-", ".pbtxn-", ".pbtrash-")):
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
    os.link(
        source,
        target,
        src_dir_fd=directory_fd,
        dst_dir_fd=directory_fd,
        follow_symlinks=False,
    )
    try:
        os.unlink(source, dir_fd=directory_fd)
    except BaseException:
        try:
            os.unlink(target, dir_fd=directory_fd)
        except OSError:
            pass
        raise


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


class RetentionError(DestinationError):
    """Au moins une suppression de rétention a échoué."""

    def __init__(self, failures: list[str]):
        self.failures = failures
        super().__init__(f"{len(failures)} suppression(s) de rétention impossible(s)")


class Destination(ABC):
    """Interface pragmatique : future SshDestination => mêmes méthodes."""

    @abstractmethod
    def save(self, data: bytes, info: ImageInfo, filename: str | None = None) -> StoredImage: ...

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
    def operation_lock(self, *, exclusive: bool):
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
                fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
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
        if not isinstance(raw, dict) or set(raw) != _TXN_KEYS:
            raise ValueError("marqueur de transaction invalide")
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
        try:
            os.unlink(name, dir_fd=directory_fd)
        except FileNotFoundError:
            return True
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
        trash_name = f".pbtrash-{secrets.token_hex(12)}.data"
        try:
            self._move_expected(directory_fd, name, trash_name, expected)
            return self._unlink_expected(directory_fd, trash_name, expected)
        except (DestinationError, OSError, StorageConflictError):
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
                continue
            if trash_identity != expected:
                complete = False
                continue
            current_identity = self._entry_identity(directory_fd, target)
            if current_identity is None:
                if not self._restore_noreplace(directory_fd, trash, target, expected):
                    complete = False
            elif current_identity == expected:
                if not self._remove_expected(directory_fd, trash, expected):
                    complete = False
            else:
                # A foreign public entry appeared. Preserve it and leave the
                # old object hidden for reconciliation rather than overwrite it.
                complete = False
        if complete:
            marker_identity = self._entry_identity(directory_fd, marker_name)
            if not self._remove_expected(directory_fd, marker_name, marker_identity):
                complete = False
            else:
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
            if trash_identity is None and public_identity == expected:
                try:
                    self._move_expected(directory_fd, target, trash, expected)
                except (DestinationError, OSError):
                    complete = False
            elif trash_identity is not None and trash_identity != expected:
                complete = False

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
            marker_identity = self._entry_identity(directory_fd, marker_name)
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
            backup_identity = self._entry_identity(directory_fd, backup)
            current_identity = self._entry_identity(directory_fd, name)
            if backup_identity is None:
                continue
            if expected is None:
                complete = False
                continue
            if backup_identity != expected:
                complete = False
                continue
            if current_identity is None:
                if not self._restore_noreplace(directory_fd, backup, name, expected):
                    complete = False
            elif current_identity == expected:
                if not self._remove_expected(directory_fd, backup, expected):
                    complete = False
            else:
                complete = False

        for name, expected in (
            (transaction["data_temp"], new_data_identity),
            (transaction["meta_temp"], new_meta_identity),
        ):
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
            # If neither old backup remains, this transaction may have failed
            # before moving either managed entry (for example because a
            # foreign file appeared and was restored). Do not retain a marker
            # forever merely because the public name is now foreign.
            backups_left = any(
                self._entry_identity(directory_fd, name) is not None
                for name in (transaction["data_backup"], transaction["meta_backup"])
            )
            if (
                backups_left
                or current_target is None
                or current_meta is None
                or current_target == new_data_identity
                or current_meta == new_meta_identity
            ):
                complete = False
        if complete:
            if not self._remove_expected(
                directory_fd,
                marker_name,
                self._entry_identity(directory_fd, marker_name),
            ):
                complete = False
            else:
                self._fsync_directory(directory_fd)
        return complete

    def _cleanup_committed_transaction(
        self,
        directory_fd: int,
        transaction: dict,
        marker_name: str,
        commit_name: str,
    ) -> bool:
        if (
            self._entry_identity(directory_fd, transaction["target"])
            != self._transaction_identity(transaction, "new_data_identity")
            or self._entry_identity(directory_fd, transaction["target"] + ".json")
            != self._transaction_identity(transaction, "new_meta_identity")
        ):
            # Never discard the old pair while the public names no longer
            # point at the committed replacement.
            return False
        complete = True
        for name, key in (
            (transaction["data_backup"], "target_identity"),
            (transaction["meta_backup"], "meta_identity"),
            (transaction["data_temp"], "new_data_identity"),
            (transaction["meta_temp"], "new_meta_identity"),
        ):
            expected = self._transaction_identity(transaction, key)
            if expected is None:
                continue
            if not self._remove_expected(directory_fd, name, expected):
                complete = False
        if complete:
            if not self._remove_expected(
                directory_fd,
                marker_name,
                self._entry_identity(directory_fd, marker_name),
            ):
                complete = False
            if not self._remove_expected(
                directory_fd,
                commit_name,
                self._entry_identity(directory_fd, commit_name),
            ):
                complete = False
            if complete:
                self._fsync_directory(directory_fd)
        return complete

    def _recover_transactions(self, directory_fd: int, entries: list[os.DirEntry]) -> set[str]:
        markers: dict[str, tuple[str, dict]] = {}
        commits: dict[str, tuple[str, dict]] = {}
        protected: set[str] = set()
        for entry in entries:
            token = _txn_token(entry.name)
            if token is None:
                continue
            protected.add(entry.name)
            try:
                raw = self._read_meta(directory_fd, entry.name)
                transaction = self._parse_transaction(entry.name, raw)
            except (DestinationError, ValueError, TypeError, KeyError):
                log.warning("marqueur de transaction invalide, conservé : %s", entry.name)
                continue
            for key in ("data_temp", "meta_temp", "data_backup", "meta_backup", "target"):
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

    def _require_owned(self, directory_fd: int, filename: str) -> int:
        """N'opère que sur un fichier avec sidecar régulier présent."""
        if not valid_filename(filename):
            raise DestinationError(f"nom de fichier invalide : {filename!r}")
        meta_name = self._meta_name(filename)
        fd = -1
        try:
            fd = self._open_file(directory_fd, filename, os.O_RDONLY)
            # Keep the opened inode while checking the sidecar. A replacement
            # of the public name cannot redirect read() to a foreign inode.
            raw = self._read_meta(directory_fd, meta_name)
            item = self._validated_item(raw, filename)
            if os.fstat(fd).st_size != item.size:
                raise ValueError("taille incohérente")
            os.lseek(fd, 0, os.SEEK_SET)
            return fd
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
        return LocalDestination._entry_identity(directory_fd, name) is not None

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

    def _save_named(self, directory_fd: int, data: bytes, info: ImageInfo, filename: str) -> StoredImage:
        meta_name = self._meta_name(filename)
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
            owned_fd = self._require_owned(directory_fd, filename)
            try:
                file_stat = os.fstat(owned_fd)
                target_identity = (file_stat.st_dev, file_stat.st_ino)
            finally:
                os.close(owned_fd)
            meta_identity = self._entry_identity(directory_fd, meta_name)

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
                    self._install_new(directory_fd, data_temp, filename)
                else:
                    self._move_expected(
                        directory_fd,
                        data_temp,
                        filename,
                        self._transaction_identity(transaction, "new_data_identity"),
                    )
                if meta_identity is None:
                    self._install_new(directory_fd, meta_temp, meta_name)
                else:
                    self._move_expected(
                        directory_fd,
                        meta_temp,
                        meta_name,
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
                self._write_transaction_file(directory_fd, commit_name, committed)
                commit_published = True
                try:
                    if not self._cleanup_committed_transaction(
                        directory_fd,
                        transaction,
                        marker_name,
                        commit_name,
                    ):
                        log.warning("nettoyage de transaction différé : %s", marker_name)
                except (DestinationError, OSError):
                    # The new pair is durable once the commit marker exists;
                    # recovery will retry cleanup on the next startup.
                    log.warning("nettoyage de transaction différé : %s", marker_name)
            return stored
        except BaseException:
            if transaction is not None and marker_name is not None and not commit_published:
                try:
                    if not self._rollback_transaction(directory_fd, transaction, marker_name):
                        log.warning("annulation de transaction différée : %s", marker_name)
                except (DestinationError, OSError):
                    log.exception("annulation de transaction impossible : %s", marker_name)
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
        now = time.time()
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
            for entry in entries:
                if entry.name in protected or _internal_marker_name(entry.name):
                    continue
                if not (
                    entry.name.startswith((".pbmeta-", ".pbdata-", ".pbbackup-", ".pbtrash-"))
                    or _TXN_TEMP_RE.fullmatch(entry.name)
                    or _DELETE_TEMP_RE.fullmatch(entry.name)
                ):
                    continue
                if entry.name.startswith(".pbbackup-"):
                    # Backups from versions without transaction markers cannot
                    # be associated safely with a target; preserve their data.
                    log.warning("sauvegarde orpheline conservée : %s", entry.name)
                    continue
                try:
                    info = os.stat(entry.name, dir_fd=directory_fd, follow_symlinks=False)
                    if not stat.S_ISREG(info.st_mode):
                        continue
                    uid_getter = getattr(os, "getuid", None)
                    if uid_getter is not None and info.st_uid != uid_getter():
                        log.warning("fichier étranger laissé intact : %s", entry.name)
                        continue
                    if now - info.st_mtime < _ORPHAN_GRACE_SECONDS:
                        continue
                    orphan_identity = (info.st_dev, info.st_ino)
                    trash_name = f".pbtrash-{secrets.token_hex(12)}.data"
                    self._move_expected(
                        directory_fd,
                        entry.name,
                        trash_name,
                        orphan_identity,
                    )
                    if self._unlink_expected(directory_fd, trash_name, orphan_identity):
                        log.warning("fichier temporaire/orphelin supprimé : %s", entry.name)
                    else:
                        log.warning("fichier temporaire modifié, conservé : %s", entry.name)
                except StorageConflictError:
                    log.warning("fichier temporaire modifié, conservé : %s", entry.name)
                except (DestinationError, OSError):
                    log.warning("impossible de réconcilier %s", entry.name)

    # -- API Destination ---------------------------------------------------

    def save(
        self,
        data: bytes,
        info: ImageInfo,
        filename: str | None = None,
    ) -> StoredImage:
        self._ensure_dir()
        if filename is not None:
            if not valid_filename(filename):
                raise DestinationError(f"nom de fichier invalide : {filename!r}")
            with self._directory_fd() as directory_fd:
                return self._save_named(directory_fd, data, info, filename)
        ext = info.ext
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
                created_identity = None
                try:
                    file_stat = os.fstat(fd)
                    created_identity = (file_stat.st_dev, file_stat.st_ino)
                    with os.fdopen(fd, "wb") as fh:
                        fd = -1
                        fh.write(data)
                        fh.flush()
                        os.fsync(fh.fileno())
                except OSError as exc:
                    if fd >= 0:
                        try:
                            os.close(fd)
                        except OSError:
                            pass
                    if created_identity is not None:
                        try:
                            self._remove_expected(directory_fd, filename, created_identity)
                        except (DestinationError, OSError):
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
                    kind=info.kind,
                    mime=info.mime,
                )
                target_identity = None
                try:
                    target_identity = self._entry_identity(directory_fd, filename)
                    self._write_meta_atomic(
                        directory_fd,
                        {
                            "filename": filename,
                            "created_at": created_at.isoformat(timespec="microseconds"),
                            "width": stored.width,
                            "height": stored.height,
                            "size": stored.size,
                            "format": stored.fmt,
                            "kind": stored.kind,
                            "mime": stored.mime,
                        },
                    )
                except BaseException as exc:
                    try:
                        if target_identity is not None:
                            self._remove_expected(directory_fd, filename, target_identity)
                        self._fsync_directory(directory_fd)
                    except (DestinationError, OSError):
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
                    or entry.name.startswith((".pbmeta-", ".pbdata-", ".pbbackup-", ".pbtrash-"))
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

    def delete(self, filename: str) -> None:
        with self._directory_fd() as directory_fd:
            owned_fd = self._require_owned(directory_fd, filename)
            try:
                info = os.fstat(owned_fd)
                target_identity = (info.st_dev, info.st_ino)
                meta_name = self._meta_name(filename)
                meta_identity = self._entry_identity(directory_fd, meta_name)
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
            fd = self._require_owned(directory_fd, filename)
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
