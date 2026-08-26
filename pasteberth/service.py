"""Logique métier : upload -> validation -> stockage -> rétention.

Le service est le seul point de passage du web vers les destinations ;
il sérialise les opérations par zone (verrou par zone) pour garantir une
rétention cohérente sous concurrence, tout en laissant les zones
indépendantes entre elles.
"""
from __future__ import annotations

import fcntl
import logging
import os
import threading
from contextlib import contextmanager
from pathlib import Path

from pasteberth.config import Config, ZoneConfig
from pasteberth.images import (
    InvalidImageError,
    inspect_image,
    mime_allowed,
    mime_for,
)
from pasteberth.storage import (
    DestinationError,
    LocalDestination,
    RetentionError,
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
        os.chmod(lock_root, 0o700)
        self.path = lock_root / f"space-{uid}-{device_id}.lock"
        self._thread_lock = threading.Lock()

    @contextmanager
    def locked(self):
        with self._thread_lock:
            fd = os.open(
                self.path,
                os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            try:
                uid = getattr(os, "getuid", lambda: 0)()
                if os.fstat(fd).st_uid != uid:
                    raise PermissionError(f"verrou filesystem non détenu par l'utilisateur : {self.path}")
                os.fchmod(fd, 0o600)
                fcntl.flock(fd, fcntl.LOCK_EX)
                yield
            finally:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                finally:
                    os.close(fd)


class ServiceError(Exception):
    """Erreur métier avec code exploitable par la couche HTTP."""

    STATUS = {
        "unknown_zone": 404,
        "unknown_image": 404,
        "empty_upload": 400,
        "invalid_image": 400,
        "unsupported_format": 415,
        "unsupported_media_type": 415,
        "too_large": 413,
        "storage_low": 507,
        "retention_error": 503,
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
            zones.append(
                {
                    "id": zid,
                    "label": zone.label,
                    "color": zone.color,
                    "retain": zone.retain,
                    "count": count,
                }
            )
        return {
            "auth_enabled": self.auth_enabled,
            "max_upload_bytes": self.cfg.max_upload_bytes,
            "max_image_pixels": self.cfg.max_image_pixels,
            "zones": zones,
        }

    # --------------------------------------------------------------- upload

    def upload(self, zid: str, data: bytes, declared_mime: str | None) -> dict:
        zone = self._zone_cfg.get(zid)
        if zone is None:
            raise ServiceError("unknown_zone", f"zone inconnue : {zid}")
        if not data:
            raise ServiceError("empty_upload", "aucune donnée reçue")
        if len(data) > self.cfg.max_upload_bytes:
            raise ServiceError(
                "too_large",
                f"image trop grande ({len(data)} > {self.cfg.max_upload_bytes} octets)",
            )
        if not mime_allowed(declared_mime):
            raise ServiceError(
                "unsupported_media_type",
                f"Content-Type déclaré refusé : {declared_mime!r}",
            )
        try:
            info = inspect_image(data, max_pixels=self.cfg.max_image_pixels)
        except InvalidImageError as exc:
            raise ServiceError(exc.code, str(exc)) from exc

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
                stored = dest.save(data, info)
                deleted = dest.apply_retention(zone.retain, stored.filename)
        except StorageLowError as exc:
            raise ServiceError("storage_low", str(exc)) from exc
        except RetentionError as exc:
            raise ServiceError("retention_error", str(exc)) from exc
        except (DestinationError, OSError) as exc:
            raise ServiceError("destination_error", str(exc)) from exc
        log.info(
            "upload zone=%s fichier=%s %dx%d %d octets (rétention : %d supprimé(s))",
            zid,
            stored.filename,
            stored.width,
            stored.height,
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

    def delete(self, zid: str, filename: str) -> None:
        """Supprime une image connue (fichier + sidecar) de la zone."""
        if zid not in self._zone_cfg:
            raise ServiceError("unknown_zone", f"zone inconnue : {zid}")
        if not valid_filename(filename):
            raise ServiceError("unknown_image", "nom de fichier invalide")
        try:
            with self._locks[zid], self._destinations[zid].operation_lock(exclusive=True):
                self._destinations[zid].delete(filename)
        except UnknownImageError as exc:
            raise ServiceError("unknown_image", str(exc)) from exc
        except (DestinationError, OSError) as exc:
            raise ServiceError("destination_error", str(exc)) from exc
        log.info("suppression zone=%s fichier=%s", zid, filename)

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
        # Format déduit de l'extension, elle-même générée côté serveur.
        ext = filename.rsplit(".", 1)[-1]
        fmt = {"png": "png", "jpg": "jpeg", "webp": "webp"}[ext]
        return data, mime_for(fmt)

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
            "preview_url": f"/previews/{zid}/{item.filename}",
            "reference": f"{zone.reference_prefix}{reference_path}{zone.reference_suffix}",
        }
