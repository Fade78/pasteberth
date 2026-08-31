"""Logique métier : upload -> validation -> stockage -> rétention.

Le service est le seul point de passage du web vers les destinations ;
il sérialise les opérations par zone (verrou par zone) pour garantir une
rétention cohérente sous concurrence, tout en laissant les zones
indépendantes entre elles.
"""
from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from urllib.parse import quote

from pasteberth.config import Config, ZoneConfig, public_path, resolve_group_zone_ids
from pasteberth.content import classify
from pasteberth.images import (
    InvalidImageError,
    mime_allowed,
    mime_syntax_allowed,
)
from pasteberth.platformfs import platform_fs
from pasteberth.storage import (
    DestinationError,
    DestinationBusyError,
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
        self._fs = platform_fs()
        owner = self._fs.owner_token()
        base = self._fs.runtime_directory()
        lock_root = base / "pasteberth"
        with self._fs.open_directory(lock_root, create=True, mode=0o700):
            pass
        self.lock_root = lock_root
        self.lock_name = f"space-{owner}-{device_id}.lock"
        self._thread_lock = threading.Lock()

    @contextmanager
    def locked(self):
        with self._thread_lock:
            with self._fs.open_directory(self.lock_root) as root:
                with self._fs.acquire_lock(
                    root,
                    name=self.lock_name,
                    exclusive=True,
                ):
                    yield


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
        "zone_busy": 423,
        "zip_disabled": 403,
        "invalid_request": 400,
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

        # La configuration est immuable pendant la durée du processus. Garder
        # les memberships précalculés évite de rescanner les patterns à chaque
        # requête et permet à /api/groups de rester indépendant des fichiers.
        self._group_zone_ids = resolve_group_zone_ids(cfg.groups, self._zone_cfg)
        zone_groups: dict[str, list[str]] = {zid: [] for zid in self._zone_cfg}
        for group in cfg.groups:
            zone_ids = self._group_zone_ids[group.name]
            for zid in zone_ids:
                zone_groups[zid].append(group.name)
        self._zone_groups = {
            zid: tuple(groups) for zid, groups in zone_groups.items()
        }
        self._operation_state: dict[str, str] = {}
        self._operation_state_lock = threading.Lock()

    @contextmanager
    def zone_operation(
        self,
        zid: str,
        *,
        kind: str,
        exclusive: bool,
        blocking: bool = True,
    ):
        """Coordonne une opération de zone dans ce processus et sur disque."""
        if zid not in self._zone_cfg:
            raise ServiceError("unknown_zone", f"zone inconnue : {zid}")
        with self._operation_state_lock:
            active = self._operation_state.get(zid)
        if active in {"delete_batch", "archive"}:
            raise ServiceError(
                "zone_busy",
                f"zone {zid!r} occupée par une opération {active}",
            )

        zone_lock = self._locks[zid]
        acquired = zone_lock.acquire(blocking=blocking)
        if not acquired:
            raise ServiceError("zone_busy", f"zone {zid!r} occupée")
        try:
            destination = self._destinations[zid]
            try:
                with destination.operation_lock(exclusive=exclusive, blocking=blocking):
                    with self._operation_state_lock:
                        self._operation_state[zid] = kind
                    try:
                        yield self._zone_cfg[zid], destination
                    finally:
                        with self._operation_state_lock:
                            if self._operation_state.get(zid) == kind:
                                self._operation_state.pop(zid, None)
            except DestinationBusyError as exc:
                raise ServiceError("zone_busy", str(exc)) from exc
        finally:
            zone_lock.release()

    def _prepare_upload(
        self,
        data: bytes,
        declared_mime: str | None,
        filename_hint: str | None,
        preserve_filename: bool,
    ):
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
        return info, target_filename

    def _store_prepared_upload(
        self,
        zid: str,
        zone: ZoneConfig,
        destination: LocalDestination,
        data: bytes,
        info,
        target_filename: str | None,
        allow_replace: bool,
    ) -> StoredImage:
        try:
            device = destination.device_id
        except (DestinationError, OSError) as exc:
            raise ServiceError("destination_error", str(exc)) from exc
        try:
            with self._space_locks[device].locked():
                destination.ensure_space(len(data), zone.min_free_percent)
                stored = destination.save(
                    data,
                    info,
                    filename=target_filename,
                    allow_replace=allow_replace,
                )
                destination.apply_retention(zone.retain, stored.filename)
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
            "upload zone=%s fichier=%s kind=%s %d octets",
            zid,
            stored.filename,
            stored.kind,
            stored.size,
        )
        return stored

    # ---------------------------------------------------------------- zones

    @property
    def auth_enabled(self) -> bool:
        return self.cfg.auth.enabled

    def has_zone(self, zid: str) -> bool:
        return zid in self._zone_cfg

    def overview(self, *, blocking: bool = True) -> dict:
        zones = []
        for zid, zone in self._zone_cfg.items():
            busy = False
            try:
                items = self.history(zid, blocking=blocking)
            except ServiceError as exc:
                if exc.code != "zone_busy":
                    raise
                busy = True
                items = []
            zones.append(
                {
                    "id": zid,
                    "label": zone.label,
                    "color": zone.color,
                    "retain": zone.retain,
                    "count": None if busy else len(items),
                    "groups": list(self._zone_groups[zid]),
                    "busy": busy,
                    "reference_prefix": zone.reference_prefix,
                    "reference_suffix": zone.reference_suffix,
                    "reference_list_prefix": zone.reference_list_prefix,
                    "reference_list_suffix": zone.reference_list_suffix,
                    "reference_separator": zone.reference_separator,
                    "allow_zip_download": zone.allow_zip_download,
                }
            )
        return {
            "auth_enabled": self.auth_enabled,
            "max_upload_bytes": self.cfg.max_upload_bytes,
            "max_image_pixels": self.cfg.max_image_pixels,
            "zones": zones,
            "groups": self.group_overview(),
        }

    def group_overview(self) -> list[dict]:
        """Retourne les groupes sans lire les destinations de stockage."""
        groups = []
        for group in self.cfg.groups:
            zone_ids = self._group_zone_ids[group.name]
            groups.append(
                {
                    "name": group.name,
                    "selection": group.selection,
                    "pattern": list(group.pattern),
                    "layout": group.layout,
                    "zone_ids": list(zone_ids),
                    "hide_empty": group.hide_empty,
                    "show_count": group.show_count,
                    "zone_count": len(zone_ids),
                }
            )
        return groups

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
        blocking: bool = True,
    ) -> dict:
        if zid not in self._zone_cfg:
            raise ServiceError("unknown_zone", f"zone inconnue : {zid}")
        info, target_filename = self._prepare_upload(
            data, declared_mime, filename_hint, preserve_filename
        )
        with self.zone_operation(
            zid, kind="upload", exclusive=True, blocking=blocking
        ) as (zone, destination):
            stored = self._store_prepared_upload(
                zid,
                zone,
                destination,
                data,
                info,
                target_filename,
                allow_replace,
            )
        return self.item_payload(zid, stored)

    # ------------------------------------------------------------ historique

    def history(self, zid: str, *, blocking: bool = True) -> list[dict]:
        try:
            with self.zone_operation(
                zid, kind="history", exclusive=False, blocking=blocking
            ) as (_zone, destination):
                items = destination.list()
        except ServiceError:
            raise
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
        blocking: bool = True,
    ) -> None:
        """Supprime une image connue (fichier + sidecar) de la zone."""
        if zid not in self._zone_cfg:
            raise ServiceError("unknown_zone", f"zone inconnue : {zid}")
        if not valid_filename(filename):
            raise ServiceError("unknown_image", "nom de fichier invalide")
        try:
            with self.zone_operation(
                zid, kind="delete", exclusive=True, blocking=blocking
            ) as (_zone, destination):
                destination.delete(
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

    def delete_many(
        self,
        zid: str,
        filenames: list[str],
        *,
        blocking: bool = True,
    ) -> dict:
        """Supprime un lot sous un verrou exclusif de zone."""
        if zid not in self._zone_cfg:
            raise ServiceError("unknown_zone", f"zone inconnue : {zid}")
        if not filenames:
            raise ServiceError("invalid_request", "aucun fichier à supprimer")
        if len(set(filenames)) != len(filenames):
            raise ServiceError("invalid_request", "noms de fichiers dupliqués")
        for filename in filenames:
            if not isinstance(filename, str) or not valid_filename(filename):
                raise ServiceError("invalid_filename", "nom de fichier invalide")
        deleted: list[str] = []
        failed: list[dict] = []
        try:
            with self.zone_operation(
                zid, kind="delete_batch", exclusive=True, blocking=blocking
            ) as (_zone, destination):
                for filename in filenames:
                    try:
                        destination.delete(filename)
                    except UnknownImageError as exc:
                        failed.append(
                            {"filename": filename, "code": "unknown_image", "message": str(exc)}
                        )
                    except StorageConflictError as exc:
                        failed.append(
                            {
                                "filename": filename,
                                "code": "storage_conflict",
                                "message": str(exc),
                            }
                        )
                    except (DestinationError, OSError) as exc:
                        failed.append(
                            {
                                "filename": filename,
                                "code": "destination_error",
                                "message": str(exc),
                            }
                        )
                    else:
                        deleted.append(filename)
                        log.info("suppression zone=%s fichier=%s", zid, filename)
        except ServiceError:
            raise
        return {"deleted": deleted, "failed": failed}

    def rename(self, zid: str, source: str, target: str, *, blocking: bool = True) -> dict:
        """Renomme une paire gérée (fichier + sidecar) dans une zone."""
        if zid not in self._zone_cfg:
            raise ServiceError("unknown_zone", f"zone inconnue : {zid}")
        if not valid_filename(source) or not valid_filename(target) or source == target:
            raise ServiceError("invalid_filename", "nom source ou cible invalide")
        try:
            with self.zone_operation(
                zid, kind="rename", exclusive=True, blocking=blocking
            ) as (_zone, destination):
                stored = destination.rename(source, target)
        except UnknownImageError as exc:
            raise ServiceError("unknown_image", str(exc)) from exc
        except StorageConflictError as exc:
            raise ServiceError("storage_conflict", str(exc)) from exc
        except (DestinationError, OSError) as exc:
            raise ServiceError("destination_error", str(exc)) from exc
        log.info("renommage zone=%s source=%s cible=%s", zid, source, target)
        return self.item_payload(zid, stored)

    def preview(
        self, zid: str, filename: str, *, blocking: bool = True
    ) -> tuple[bytes, str]:
        """Contenu binaire + MIME ; n'accepte que les fichiers connus."""
        if zid not in self._zone_cfg:
            raise ServiceError("unknown_zone", f"zone inconnue : {zid}")
        if not valid_filename(filename):
            raise ServiceError("unknown_image", "nom de fichier invalide")
        try:
            with self.zone_operation(
                zid, kind="preview", exclusive=False, blocking=blocking
            ) as (_zone, destination):
                known = {
                    item.filename: item
                    for item in destination.list()
                }
                item = known.get(filename)
                if item is None:
                    raise ServiceError("unknown_image", "fichier inconnu dans cette zone")
                if item.size > self.cfg.max_upload_bytes:
                    raise ServiceError("too_large", "preview trop grande à servir")
                data = destination.read(filename)
        except ServiceError:
            raise
        except UnknownImageError as exc:
            raise ServiceError("unknown_image", str(exc)) from exc
        except (DestinationError, OSError) as exc:
            raise ServiceError("destination_error", str(exc)) from exc
        return data, item.mime

    @contextmanager
    def archive_files(
        self,
        zid: str,
        filenames: list[str],
        *,
        blocking: bool = True,
    ):
        """Expose les fichiers d'une archive pendant un verrou de zone."""
        if zid not in self._zone_cfg:
            raise ServiceError("unknown_zone", f"zone inconnue : {zid}")
        if not filenames:
            raise ServiceError("invalid_request", "aucun fichier à archiver")
        if len(set(filenames)) != len(filenames):
            raise ServiceError("invalid_request", "noms de fichiers dupliqués")
        for filename in filenames:
            if not isinstance(filename, str) or not valid_filename(filename):
                raise ServiceError("invalid_filename", "nom de fichier invalide")
        with self.zone_operation(
            zid, kind="archive", exclusive=True, blocking=blocking
        ) as (zone, destination):
            if not zone.allow_zip_download:
                raise ServiceError(
                    "zip_disabled",
                    "le téléchargement ZIP est désactivé pour cette zone",
                )
            known = {item.filename: item for item in destination.list()}
            selected = []
            try:
                for filename in filenames:
                    item = known.get(filename)
                    if item is None:
                        raise ServiceError(
                            "unknown_image",
                            f"fichier inconnu dans cette zone : {filename}",
                        )
                    # Vérifie chaque paire avant d'envoyer les en-têtes HTTP.
                    with destination.open_read(filename):
                        pass
                    selected.append(item)
            except ServiceError:
                raise
            except UnknownImageError as exc:
                raise ServiceError("unknown_image", str(exc)) from exc
            except (DestinationError, OSError) as exc:
                raise ServiceError("destination_error", str(exc)) from exc
            yield destination, selected

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
            "preview_url": public_path(
                self.cfg.url_prefix,
                f"/previews/{quote(zid, safe='')}/{quote(item.filename, safe='')}",
            ),
            "reference": f"{zone.reference_prefix}{reference_path}{zone.reference_suffix}",
        }
