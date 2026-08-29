"""Logique métier : upload -> validation -> stockage -> rétention.

Le service est le seul point de passage du web vers les destinations ;
il sérialise les opérations par zone (verrou par zone) pour garantir une
rétention cohérente sous concurrence, tout en laissant les zones
indépendantes entre elles.
"""
from __future__ import annotations

import fnmatch
import fcntl
import logging
import os
import threading
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote

from pasteberth.config import Config, ZoneConfig
from pasteberth.content import classify
from pasteberth.images import (
    InvalidImageError,
    mime_allowed,
    mime_syntax_allowed,
)
from pasteberth.paths import open_directory
from pasteberth.storage import (
    DestinationError,
    LocalDestination,
    ReplacementRequiredError,
    RetentionError,
    StorageConflictError,
    StorageLowError,
    StoredImage,
    UnknownImageError,
    valid_filename,
)

log = logging.getLogger("pasteberth.service")


class _DeviceSpaceLock:
    """Verrou par filesystem partagé entre threads et processus locaux."""

    def __init__(self, device_id: int):
        uid = getattr(os, "getuid", lambda: 0)()
        runtime_root = os.environ.get("XDG_RUNTIME_DIR")
        base = Path(runtime_root) if runtime_root else Path.home() / ".cache"
        lock_root = base / "pasteberth"
        lock_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(
            lock_root,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            os.fchmod(fd, 0o700)
        finally:
            os.close(fd)
        self.lock_root = lock_root
        self.lock_name = f"space-{uid}-{device_id}.lock"
        self._thread_lock = threading.Lock()

    @contextmanager
    def locked(self):
        with self._thread_lock:
            root_fd = -1
            fd = -1
            try:
                root_fd = open_directory(self.lock_root)
                fd = os.open(
                    self.lock_name,
                    os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                    dir_fd=root_fd,
                )
                uid = getattr(os, "getuid", lambda: 0)()
                if os.fstat(fd).st_uid != uid:
                    raise PermissionError(
                        "verrou filesystem non détenu par l'utilisateur : "
                        f"{self.lock_root / self.lock_name}"
                    )
                os.fchmod(fd, 0o600)
                fcntl.flock(fd, fcntl.LOCK_EX)
                yield
            finally:
                if fd >= 0:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_UN)
                    finally:
                        os.close(fd)
                if root_fd >= 0:
                    os.close(root_fd)


class ServiceError(Exception):
    """Erreur métier avec code exploitable par la couche HTTP."""

    STATUS = {
        "unknown_zone": 404,
        "unknown_image": 404,
        "empty_upload": 400,
        "invalid_filename": 400,
        "invalid_image": 400,
        "unsupported_format": 415,
        "unsupported_media_type": 415,
        "too_large": 413,
        "storage_low": 507,
        "retention_error": 503,
        "storage_conflict": 409,
        "replacement_required": 428,
        "destination_error": 500,
    }

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.status = self.STATUS.get(code, 400)


class PasteService:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._zone_cfg: dict[str, ZoneConfig] = {}
        self._destinations: dict[str, LocalDestination] = {}
        self._locks: dict[str, threading.RLock] = {}
        self._space_locks: dict[int, _DeviceSpaceLock] = {}
        for zid, zone in cfg.zones.items():
            self._zone_cfg[zid] = zone
            self._destinations[zid] = LocalDestination(
                zone.directory, create_directory=zone.create_directory
            )
            self._locks[zid] = threading.RLock()
            device = self._destinations[zid].device_id
            self._space_locks.setdefault(device, _DeviceSpaceLock(device))

    # ---------------------------------------------------------------- zones

    @property
    def auth_enabled(self) -> bool:
        return self.cfg.auth.enabled

    def has_zone(self, zid: str) -> bool:
        return zid in self._zone_cfg

    def overview(self) -> dict:
        zones = []
        for zid, zone in self._zone_cfg.items():
            count = len(self.history(zid))
            # Compute which groups this zone belongs to
            zone_groups = []
            for group in self.cfg.groups:
                if any(fnmatch.fnmatch(zid, pattern) for pattern in group.pattern):
                    zone_groups.append(group.name)
            zones.append(
                {
                    "id": zid,
                    "label": zone.label,
                    "color": zone.color,
                    "retain": zone.retain,
                    "count": len(self.history(zid)),
                    "groups": zone_groups,
                }
            )
        # Build groups response
        groups = []
        for group in self.cfg.groups:
            zone_ids = [
                zid for zid in self._zone_cfg
                if any(fnmatch.fnmatch(zid, pattern) for pattern in group.pattern)
            ]
            groups.append(
                {
                    "name": group.name,
                    "pattern": list(group.pattern),
                    "zone_ids": zone_ids,
                    "hide_empty": group.hide_empty,
                    "show_count": group.show_count,
                    "zone_count": len(zone_ids),
                }
            )
        return {
            "auth_enabled": self.auth_enabled,
            "max_upload_bytes": self.cfg.max_upload_bytes,
            "max_image_pixels": self.cfg.max_image_pixels,
            "zones": zones,
            "groups": groups,
        }

    # --------------------------------------------------------------- upload

    def upload(
        self,
        zid: str,
        data: bytes,
        declared_mime: str | None,
        filename_hint: str | None = None,
        preserve_filename: bool = False,
        *,
        allow_replace: bool = False,
    ) -> dict:
        zone = self._zone_cfg.get(zid)
        if zone is None:
            raise ServiceError("unknown_zone", f"zone inconnue : {zid}")
        if not data:
            raise ServiceError("empty_upload", "aucune donnée reçue")
        if len(data) > self.cfg.max_upload_bytes:
            raise ServiceError(
                "too_large",
                f"contenu trop grand ({len(data)} > {self.cfg.max_upload_bytes} octets)",
            )
        target_filename = None
        if preserve_filename:
            if not filename_hint or not valid_filename(filename_hint):
                raise ServiceError(
                    "invalid_filename",
                    "le nom du fichier glissé est invalide",
                )
            target_filename = filename_hint
        if not mime_syntax_allowed(declared_mime):
            raise ServiceError(
                "unsupported_media_type",
                f"Content-Type déclaré invalide : {declared_mime!r}",
            )
        # A drag-and-drop carries a real file name, so its declared MIME may be
        # an arbitrary vendor type. Content classification remains authoritative.
        if not mime_allowed(declared_mime) and target_filename is None:
            raise ServiceError(
                "unsupported_media_type",
                f"Content-Type déclaré refusé : {declared_mime!r}",
            )
        try:
            info = classify(
                data,
                declared_mime,
                filename_hint,
                max_pixels=self.cfg.max_image_pixels,
            )
        except InvalidImageError as exc:
            raise ServiceError(exc.code, str(exc)) from exc
        accepted = {
            "image": self.cfg.accept_img,
            "text": self.cfg.accept_doc,
            "binary": self.cfg.accept_bin,
        }
        if not accepted[info.kind]:
            raise ServiceError(
                "unsupported_media_type",
                f"contenu {info.kind} refusé par la configuration (accept_*)",
            )

        dest = self._destinations[zid]
        try:
            device = dest.device_id
        except (DestinationError, OSError) as exc:
            raise ServiceError("destination_error", str(exc)) from exc
        try:
            with (
                self._locks[zid],
                dest.operation_lock(exclusive=True),
                self._space_locks[device].locked(),
            ):
                dest.ensure_space(len(data), zone.min_free_percent)
                stored = dest.save(
                    data,
                    info,
                    filename=target_filename,
                    allow_replace=allow_replace,
                )
                deleted = dest.apply_retention(zone.retain, stored.filename)
        except StorageLowError as exc:
            raise ServiceError("storage_low", str(exc)) from exc
        except RetentionError as exc:
            raise ServiceError("retention_error", str(exc)) from exc
        except ReplacementRequiredError as exc:
            raise ServiceError("replacement_required", str(exc)) from exc
        except StorageConflictError as exc:
            raise ServiceError("storage_conflict", str(exc)) from exc
        except (DestinationError, OSError) as exc:
            raise ServiceError("destination_error", str(exc)) from exc
        log.info(
            "upload zone=%s fichier=%s kind=%s %d octets (rétention : %d supprimé(s))",
            zid,
            stored.filename,
            stored.kind,
            stored.size,
            len(deleted),
        )
        return self.item_payload(zid, stored)

    # ------------------------------------------------------------ historique

    def history(self, zid: str) -> list[dict]:
        if zid not in self._zone_cfg:
            raise ServiceError("unknown_zone", f"zone inconnue : {zid}")
        try:
            with self._locks[zid], self._destinations[zid].operation_lock(exclusive=False):
                items = self._destinations[zid].list()
        except (DestinationError, OSError) as exc:
            raise ServiceError("destination_error", str(exc)) from exc
        return [self.item_payload(zid, item) for item in items]

    # --------------------------------------------------------------- preview

    def delete(
        self,
        zid: str,
        filename: str,
        *,
        allow_stale_sidecar: bool = False,
    ) -> None:
        """Supprime une image connue (fichier + sidecar) de la zone."""
        if zid not in self._zone_cfg:
            raise ServiceError("unknown_zone", f"zone inconnue : {zid}")
        if not valid_filename(filename):
            raise ServiceError("unknown_image", "nom de fichier invalide")
        try:
            with self._locks[zid], self._destinations[zid].operation_lock(exclusive=True):
                self._destinations[zid].delete(
                    filename,
                    allow_stale_sidecar=allow_stale_sidecar,
                )
        except UnknownImageError as exc:
            raise ServiceError("unknown_image", str(exc)) from exc
        except StorageConflictError as exc:
            raise ServiceError("storage_conflict", str(exc)) from exc
        except (DestinationError, OSError) as exc:
            raise ServiceError("destination_error", str(exc)) from exc
        log.info("suppression zone=%s fichier=%s", zid, filename)

    def rename(self, zid: str, source: str, target: str) -> dict:
        """Renomme une paire gérée (fichier + sidecar) dans une zone."""
        if zid not in self._zone_cfg:
            raise ServiceError("unknown_zone", f"zone inconnue : {zid}")
        if not valid_filename(source) or not valid_filename(target) or source == target:
            raise ServiceError("invalid_filename", "nom source ou cible invalide")
        try:
            with self._locks[zid], self._destinations[zid].operation_lock(exclusive=True):
                stored = self._destinations[zid].rename(source, target)
        except UnknownImageError as exc:
            raise ServiceError("unknown_image", str(exc)) from exc
        except StorageConflictError as exc:
            raise ServiceError("storage_conflict", str(exc)) from exc
        except (DestinationError, OSError) as exc:
            raise ServiceError("destination_error", str(exc)) from exc
        log.info("renommage zone=%s source=%s cible=%s", zid, source, target)
        return self.item_payload(zid, stored)

    def preview(self, zid: str, filename: str) -> tuple[bytes, str]:
        """Contenu binaire + MIME ; n'accepte que les fichiers connus."""
        if zid not in self._zone_cfg:
            raise ServiceError("unknown_zone", f"zone inconnue : {zid}")
        if not valid_filename(filename):
            raise ServiceError("unknown_image", "nom de fichier invalide")
        try:
            with self._locks[zid], self._destinations[zid].operation_lock(exclusive=False):
                known = {
                    item.filename: item
                    for item in self._destinations[zid].list()
                }
                item = known.get(filename)
                if item is None:
                    raise ServiceError("unknown_image", "fichier inconnu dans cette zone")
                if item.size > self.cfg.max_upload_bytes:
                    raise ServiceError("too_large", "preview trop grande à servir")
                data = self._destinations[zid].read(filename)
        except ServiceError:
            raise
        except UnknownImageError as exc:
            raise ServiceError("unknown_image", str(exc)) from exc
        except (DestinationError, OSError) as exc:
            raise ServiceError("destination_error", str(exc)) from exc
        return data, item.mime

    # ---------------------------------------------------------------- divers

    def item_payload(self, zid: str, item: StoredImage) -> dict:
        zone = self._zone_cfg[zid]
        reference_path = self._destinations[zid].reference_path(item.filename)
        return {
            "id": item.filename,
            "filename": item.filename,
            "created_at": item.created_at.isoformat(timespec="microseconds"),
            "width": item.width,
            "height": item.height,
            "size": item.size,
            "format": item.fmt,
            "kind": item.kind,
            "mime": item.mime,
            "preview_url": (
                f"/previews/{quote(zid, safe='')}/{quote(item.filename, safe='')}"
            ),
            "reference": f"{zone.reference_prefix}{reference_path}{zone.reference_suffix}",
        }
