"""Business logic: upload -> validation -> storage -> retention.

The service is the only path from the web layer to destinations; it serializes
operations per zone to keep retention coherent under concurrency while leaving
zones independent from one another.
"""
from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from urllib.parse import quote

from .autozone import (
    AutoZoneCandidate,
    discover_autozones,
    merge_autozone_groups,
)
from .config import Config, ZoneConfig, public_path, resolve_group_zone_ids
from .content import classify
from .images import (
    InvalidImageError,
    mime_allowed,
    mime_syntax_allowed,
)
from .platformfs import platform_fs
from .storage import (
    DestinationError,
    DestinationBusyError,
    DirectoryDestination,
    LocalDestination,
    ReplacementRequiredError,
    RetentionError,
    StorageConflictError,
    StorageLimitError,
    StorageLowError,
    StoredImage,
    UnknownImageError,
    validate_comment,
    valid_filename,
)

log = logging.getLogger("pasteberth.service")


class _DeviceSpaceLock:
    """Lock shared by local threads and processes for one filesystem."""

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
    """Business error with a code usable by the HTTP layer."""

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
        "storage_limit": 507,
        "retention_error": 503,
        "storage_conflict": 409,
        "replacement_required": 428,
        "zone_busy": 423,
        "zip_disabled": 403,
        "invalid_request": 400,
        "invalid_comment": 400,
        "destination_error": 500,
    }

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.status = self.STATUS.get(code, 400)


class PasteService:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._registry_lock = threading.RLock()
        self._autozone_refresh_lock = threading.Lock()
        self._zone_cfg: dict[str, ZoneConfig] = {}
        self._destinations: dict[str, LocalDestination] = {}
        self._locks: dict[str, threading.RLock] = {}
        self._space_locks: dict[int, _DeviceSpaceLock] = {}
        self._operation_state: dict[str, str] = {}
        self._operation_state_lock = threading.Lock()
        self._autozone_diagnostics: tuple[str, ...] = ()
        self._install_registry(cfg.zones, (), initial=True)
        self._refresh_autozones()

    def _new_destination(self, zone: ZoneConfig) -> LocalDestination:
        common = {
            "limits": self.cfg.limits,
            "max_image_pixels": self.cfg.max_image_pixels,
        }
        if zone.storage_mode == "directory":
            if zone.max_items is None:
                raise DestinationError(
                    f"directory zone {zone.id!r} has no max_items limit"
                )
            return DirectoryDestination(
                zone.directory,
                max_items=zone.max_items,
                **common,
            )
        return LocalDestination(
            zone.directory,
            create_directory=zone.create_directory,
            **common,
        )

    @staticmethod
    def _destination_matches(destination: LocalDestination, zone: ZoneConfig) -> bool:
        if getattr(destination, "directory", None) != zone.directory.resolve():
            return False
        if zone.storage_mode == "directory":
            return (
                isinstance(destination, DirectoryDestination)
                and destination.max_items == zone.max_items
            )
        return isinstance(destination, LocalDestination) and not isinstance(
            destination, DirectoryDestination
        )

    def _effective_groups(
        self,
        zones: dict[str, ZoneConfig],
        candidates: tuple[AutoZoneCandidate, ...],
    ):
        return merge_autozone_groups(
            self.cfg.groups,
            self.cfg.autozones,
            zones,
            candidates,
        )

    def _install_registry(
        self,
        static_zones: dict[str, ZoneConfig],
        candidates: tuple[AutoZoneCandidate, ...],
        *,
        initial: bool = False,
    ) -> None:
        zones = dict(static_zones)
        for candidate in candidates:
            if candidate.zone.id not in zones:
                zones[candidate.zone.id] = candidate.zone

        with self._registry_lock:
            old_destinations = self._destinations
            old_locks = self._locks
        destinations: dict[str, LocalDestination] = {}
        for zid, zone in zones.items():
            old = old_destinations.get(zid)
            if old is not None and self._destination_matches(old, zone):
                try:
                    old._ensure_dir()  # type: ignore[attr-defined]
                except DestinationError:
                    if zid in static_zones or initial:
                        raise
                else:
                    destinations[zid] = old
                    continue
            try:
                destinations[zid] = self._new_destination(zone)
            except (DestinationError, OSError):
                if zid in static_zones or initial:
                    raise
                log.warning("autozone destination unavailable: %s", zone.directory)
                continue

        active_zones = {
            zid: zone for zid, zone in zones.items() if zid in destinations
        }
        for zid, destination in tuple(destinations.items()):
            try:
                device = destination.device_id
            except (DestinationError, OSError):
                if zid in static_zones or initial:
                    raise
                log.warning("autozone filesystem unavailable: %s", active_zones[zid].directory)
                destinations.pop(zid, None)
                active_zones.pop(zid, None)
                continue
            self._space_locks.setdefault(device, _DeviceSpaceLock(device))
        active_candidates = tuple(
            candidate for candidate in candidates if candidate.zone.id in active_zones
        )
        effective_groups, group_diagnostics = self._effective_groups(
            active_zones, active_candidates
        )
        group_zone_ids = resolve_group_zone_ids(effective_groups, active_zones)
        zone_groups: dict[str, list[str]] = {zid: [] for zid in active_zones}
        for group in effective_groups:
            for zid in group_zone_ids[group.name]:
                zone_groups[zid].append(group.name)

        locks = {zid: old_locks.get(zid, threading.RLock()) for zid in active_zones}
        with self._registry_lock:
            self._zone_cfg = active_zones
            self._destinations = destinations
            self._locks = locks
            self._effective_group_configs = effective_groups
            self._group_zone_ids = group_zone_ids
            self._zone_groups = {
                zid: tuple(groups) for zid, groups in zone_groups.items()
            }
        if group_diagnostics:
            log.warning("%s", "; ".join(group_diagnostics))

    def _refresh_autozones(self) -> None:
        if not self.cfg.autozones:
            return
        with self._autozone_refresh_lock:
            candidates, diagnostics = discover_autozones(
                self.cfg.autozones,
                self.cfg.zones,
            )
            candidate_tuple = tuple(candidates)
            self._install_registry(self.cfg.zones, candidate_tuple)
            diagnostic_tuple = tuple(diagnostics)
            if diagnostic_tuple != self._autozone_diagnostics:
                for message in diagnostic_tuple:
                    log.warning("%s", message)
                self._autozone_diagnostics = diagnostic_tuple

    def _valid_filename(self, name: object) -> bool:
        return valid_filename(
            name,
            max_length=self.cfg.limits.max_filename_length,
            max_bytes=self.cfg.limits.max_filename_bytes,
        )

    @contextmanager
    def _zone_operation_snapshot(
        self,
        zid: str,
        zone: ZoneConfig,
        destination: LocalDestination,
        zone_lock: threading.RLock,
        *,
        kind: str,
        exclusive: bool,
        blocking: bool,
    ):
        """Run an operation against an already captured registry snapshot."""
        with self._operation_state_lock:
            active = self._operation_state.get(zid)
        if active in {"delete_batch", "archive"}:
            raise ServiceError(
                "zone_busy",
                f"zone {zid!r} is busy with a {active} operation",
            )

        acquired = zone_lock.acquire(blocking=blocking)
        if not acquired:
            raise ServiceError("zone_busy", f"zone {zid!r} is busy")
        try:
            try:
                # Directory publication may promote files from ``incoming`` while listing.
                # Serialize those reads with writers so publication remains a single operation.
                directory_exclusive = isinstance(destination, DirectoryDestination)
                with destination.operation_lock(
                    exclusive=exclusive or directory_exclusive,
                    blocking=blocking,
                ):
                    with self._operation_state_lock:
                        self._operation_state[zid] = kind
                    try:
                        yield zone, destination
                    finally:
                        with self._operation_state_lock:
                            if self._operation_state.get(zid) == kind:
                                self._operation_state.pop(zid, None)
            except DestinationBusyError as exc:
                raise ServiceError("zone_busy", str(exc)) from exc
        finally:
            zone_lock.release()

    @contextmanager
    def zone_operation(
        self,
        zid: str,
        *,
        kind: str,
        exclusive: bool,
        blocking: bool = True,
        refresh: bool = True,
    ):
        """Coordinate a zone operation in this process and on disk."""
        if refresh:
            self._refresh_autozones()
        with self._registry_lock:
            zone = self._zone_cfg.get(zid)
            destination = self._destinations.get(zid)
            zone_lock = self._locks.get(zid)
        if zone is None or destination is None or zone_lock is None:
            raise ServiceError("unknown_zone", f"unknown zone: {zid}")
        with self._zone_operation_snapshot(
            zid,
            zone,
            destination,
            zone_lock,
            kind=kind,
            exclusive=exclusive,
            blocking=blocking,
        ) as snapshot:
            yield snapshot

    def _prepare_upload(
        self,
        data: bytes,
        declared_mime: str | None,
        filename_hint: str | None,
        preserve_filename: bool,
    ):
        if not data:
            raise ServiceError("empty_upload", "no data received")
        if self.cfg.max_upload_bytes is not None and len(data) > self.cfg.max_upload_bytes:
            raise ServiceError(
                "too_large",
                f"content is too large ({len(data)} > {self.cfg.max_upload_bytes} bytes)",
            )
        target_filename = None
        if preserve_filename:
            if not filename_hint or not self._valid_filename(filename_hint):
                raise ServiceError(
                    "invalid_filename",
                    "the dropped filename is invalid",
                )
            target_filename = filename_hint
        if not mime_syntax_allowed(
            declared_mime,
            max_length=self.cfg.limits.max_mime_length,
        ):
            raise ServiceError(
                "unsupported_media_type",
                f"declared Content-Type is invalid: {declared_mime!r}",
            )
        # A drag-and-drop carries a real file name, so its declared MIME may be
        # an arbitrary vendor type. Content classification remains authoritative.
        if not mime_allowed(declared_mime) and target_filename is None:
            raise ServiceError(
                "unsupported_media_type",
                f"declared Content-Type is not allowed: {declared_mime!r}",
            )
        try:
            info = classify(
                data,
                declared_mime,
                filename_hint,
                max_pixels=self.cfg.max_image_pixels,
                max_dimension=self.cfg.limits.max_image_dimension,
                max_raw_bytes=self.cfg.limits.max_image_raw_bytes,
                max_png_chunks=self.cfg.limits.max_png_chunks,
                max_jpeg_segments=self.cfg.limits.max_jpeg_segments,
                max_webp_chunks=self.cfg.limits.max_webp_chunks,
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
                f"{info.kind} content is rejected by configuration (accept_*)",
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
        adopt_existing: bool,
    ) -> tuple[StoredImage, list[str]]:
        try:
            device = destination.device_id
        except (DestinationError, OSError) as exc:
            raise ServiceError("destination_error", str(exc)) from exc
        try:
            with self._space_locks[device].locked():
                # Adoption only allocates the bounded JSON sidecar; the data
                # file is already present in the zone.
                incoming_bytes = 0 if adopt_existing else len(data)
                destination.ensure_space(incoming_bytes, zone.min_free_percent)
                stored = destination.save(
                    data,
                    info,
                    filename=target_filename,
                    allow_replace=allow_replace,
                    adopt_existing=adopt_existing,
                )
                retention_deleted = destination.apply_retention(zone.retain, stored.filename)
        except StorageLowError as exc:
            raise ServiceError("storage_low", str(exc)) from exc
        except StorageLimitError as exc:
            raise ServiceError("storage_limit", str(exc)) from exc
        except RetentionError as exc:
            raise ServiceError("retention_error", str(exc)) from exc
        except ReplacementRequiredError as exc:
            raise ServiceError("replacement_required", str(exc)) from exc
        except StorageConflictError as exc:
            raise ServiceError("storage_conflict", str(exc)) from exc
        except (DestinationError, OSError) as exc:
            raise ServiceError("destination_error", str(exc)) from exc
        log.info(
            "upload zone=%s file=%s kind=%s %d bytes",
            zid,
            stored.filename,
            stored.kind,
            stored.size,
        )
        return stored, retention_deleted

    # ---------------------------------------------------------------- zones

    @property
    def auth_enabled(self) -> bool:
        return self.cfg.auth.enabled

    def has_zone(self, zid: str) -> bool:
        self._refresh_autozones()
        with self._registry_lock:
            return zid in self._zone_cfg

    def _group_overview_from_registry(self) -> list[dict]:
        with self._registry_lock:
            groups = tuple(self._group_zone_ids.items())
            configured = {
                group.name: group
                for group in self._effective_group_configs
            }
        return [
            {
                "name": name,
                "selection": configured[name].selection,
                "pattern": list(configured[name].pattern),
                "layout": configured[name].layout,
                "zone_ids": list(zone_ids),
                "hide_empty": configured[name].hide_empty,
                "show_count": configured[name].show_count,
                "zone_count": len(zone_ids),
            }
            for name, zone_ids in groups
        ]

    def overview(self, *, blocking: bool = True) -> dict:
        self._refresh_autozones()
        with self._registry_lock:
            snapshot = tuple(
                (
                    zid,
                    zone,
                    self._destinations[zid],
                    self._locks[zid],
                    self._zone_groups.get(zid, ()),
                )
                for zid, zone in self._zone_cfg.items()
            )
        zones = []
        for zid, zone, destination, zone_lock, groups in snapshot:
            busy = False
            try:
                items = self.history(
                    zid,
                    blocking=blocking,
                    _refresh=False,
                    _snapshot=(zone, destination, zone_lock),
                )
            except ServiceError as exc:
                if exc.code != "zone_busy":
                    raise
                busy = True
                items = []
            except (DestinationError, OSError) as exc:
                raise ServiceError("destination_error", str(exc)) from exc
            blocked = False
            block_reason = None
            if not busy and isinstance(destination, DirectoryDestination):
                try:
                    blocked, block_reason = destination.capacity_status(
                        len(items), zone.min_free_percent
                    )
                except (DestinationError, OSError) as exc:
                    block_reason = f"capacity unavailable: {exc}"
            zones.append(
                {
                    "id": zid,
                    "label": zone.label,
                    "color": zone.color,
                    "retain": zone.retain,
                    "count": None if busy else len(items),
                    "groups": list(groups),
                    "busy": busy,
                    "storage_mode": zone.storage_mode,
                    "max_items": zone.max_items,
                    "blocked": blocked,
                    "block_reason": block_reason,
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
            "show_full_path": self.cfg.show_full_path,
            "zones": zones,
            "groups": self._group_overview_from_registry(),
        }

    def group_overview(self) -> list[dict]:
        """Return groups without reading storage destinations."""
        self._refresh_autozones()
        return self._group_overview_from_registry()

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
        adopt_existing: bool = False,
        blocking: bool = True,
    ) -> dict:
        if not self.has_zone(zid):
            raise ServiceError("unknown_zone", f"unknown zone: {zid}")
        info, target_filename = self._prepare_upload(
            data, declared_mime, filename_hint, preserve_filename
        )
        with self.zone_operation(
            zid, kind="upload", exclusive=True, blocking=blocking
        ) as (zone, destination):
            stored, retention_deleted = self._store_prepared_upload(
                zid,
                zone,
                destination,
                data,
                info,
                target_filename,
                allow_replace,
                adopt_existing,
            )
        payload = self.item_payload(zid, stored, zone=zone, destination=destination)
        if retention_deleted:
            payload["retention_deleted"] = retention_deleted
        return payload

    # ------------------------------------------------------------ historique

    def history(
        self,
        zid: str,
        *,
        blocking: bool = True,
        _refresh: bool = True,
        _snapshot: tuple[ZoneConfig, LocalDestination, threading.RLock] | None = None,
    ) -> list[dict]:
        try:
            if _snapshot is None:
                operation = self.zone_operation(
                    zid,
                    kind="history",
                    exclusive=False,
                    blocking=blocking,
                    refresh=_refresh,
                )
            else:
                zone, destination, zone_lock = _snapshot
                operation = self._zone_operation_snapshot(
                    zid,
                    zone,
                    destination,
                    zone_lock,
                    kind="history",
                    exclusive=False,
                    blocking=blocking,
                )
            with operation as (zone, destination):
                items = destination.list()
        except ServiceError:
            raise
        except (DestinationError, OSError) as exc:
            raise ServiceError("destination_error", str(exc)) from exc
        return [
            self.item_payload(zid, item, zone=zone, destination=destination)
            for item in items
        ]

    # --------------------------------------------------------------- preview

    def delete(
        self,
        zid: str,
        filename: str,
        *,
        allow_stale_sidecar: bool = False,
        blocking: bool = True,
    ) -> None:
        """Delete a known image (file + sidecar) from a zone."""
        if not self.has_zone(zid):
            raise ServiceError("unknown_zone", f"unknown zone: {zid}")
        if not self._valid_filename(filename):
            raise ServiceError("unknown_image", "invalid filename")
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
        log.info("delete zone=%s file=%s", zid, filename)

    def delete_many(
        self,
        zid: str,
        filenames: list[str],
        *,
        blocking: bool = True,
    ) -> dict:
        """Delete a batch under an exclusive zone lock."""
        if not self.has_zone(zid):
            raise ServiceError("unknown_zone", f"unknown zone: {zid}")
        if not filenames:
            raise ServiceError("invalid_request", "no files to delete")
        if len(set(filenames)) != len(filenames):
            raise ServiceError("invalid_request", "duplicate filenames")
        for filename in filenames:
            if not isinstance(filename, str) or not self._valid_filename(filename):
                raise ServiceError("invalid_filename", "invalid filename")
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
                        log.info("delete zone=%s file=%s", zid, filename)
        except ServiceError:
            raise
        return {"deleted": deleted, "failed": failed}

    def rename(self, zid: str, source: str, target: str, *, blocking: bool = True) -> dict:
        """Rename a managed pair (file + sidecar) in a zone."""
        if not self.has_zone(zid):
            raise ServiceError("unknown_zone", f"unknown zone: {zid}")
        if not self._valid_filename(source) or not self._valid_filename(target) or source == target:
            raise ServiceError("invalid_filename", "invalid source or target filename")
        try:
            with self.zone_operation(
                zid, kind="rename", exclusive=True, blocking=blocking
            ) as (zone, destination):
                stored = destination.rename(source, target)
        except UnknownImageError as exc:
            raise ServiceError("unknown_image", str(exc)) from exc
        except StorageConflictError as exc:
            raise ServiceError("storage_conflict", str(exc)) from exc
        except (DestinationError, OSError) as exc:
            raise ServiceError("destination_error", str(exc)) from exc
        log.info("rename zone=%s source=%s target=%s", zid, source, target)
        return self.item_payload(zid, stored, zone=zone, destination=destination)

    def update_comment(self, zid: str, filename: str, comment: object) -> dict:
        """Update a managed item's short comment without changing its data."""
        if not self.has_zone(zid):
            raise ServiceError("unknown_zone", f"unknown zone: {zid}")
        if not self._valid_filename(filename):
            raise ServiceError("unknown_image", "invalid filename")
        try:
            comment = validate_comment(
                comment,
                max_length=self.cfg.limits.max_comment_length,
                max_bytes=self.cfg.limits.max_comment_bytes,
            )
        except ValueError as exc:
            raise ServiceError("invalid_comment", str(exc)) from exc
        try:
            with self.zone_operation(
                zid, kind="comment", exclusive=True, blocking=True
            ) as (zone, destination):
                stored = destination.update_comment(filename, comment)
        except UnknownImageError as exc:
            raise ServiceError("unknown_image", str(exc)) from exc
        except StorageConflictError as exc:
            raise ServiceError("storage_conflict", str(exc)) from exc
        except (DestinationError, OSError) as exc:
            raise ServiceError("destination_error", str(exc)) from exc
        log.info("comment updated zone=%s filename=%s", zid, filename)
        return self.item_payload(zid, stored, zone=zone, destination=destination)

    def preview(
        self, zid: str, filename: str, *, blocking: bool = True
    ) -> tuple[bytes, str]:
        """Return binary content and MIME for known files only."""
        if not self.has_zone(zid):
            raise ServiceError("unknown_zone", f"unknown zone: {zid}")
        if not self._valid_filename(filename):
            raise ServiceError("unknown_image", "invalid filename")
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
                    raise ServiceError("unknown_image", "file is unknown in this zone")
                if (
                    self.cfg.max_upload_bytes is not None
                    and item.size > self.cfg.max_upload_bytes
                ):
                    raise ServiceError("too_large", "preview is too large to serve")
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
        """Expose archive files while holding a zone lock."""
        if not self.has_zone(zid):
            raise ServiceError("unknown_zone", f"unknown zone: {zid}")
        if not filenames:
            raise ServiceError("invalid_request", "no files to archive")
        if len(set(filenames)) != len(filenames):
            raise ServiceError("invalid_request", "duplicate filenames")
        for filename in filenames:
            if not isinstance(filename, str) or not self._valid_filename(filename):
                raise ServiceError("invalid_filename", "invalid filename")
        with self.zone_operation(
            zid, kind="archive", exclusive=True, blocking=blocking
        ) as (zone, destination):
            if not zone.allow_zip_download:
                raise ServiceError(
                    "zip_disabled",
                    "ZIP downloads are disabled for this zone",
                )
            known = {item.filename: item for item in destination.list()}
            selected = []
            try:
                for filename in filenames:
                    item = known.get(filename)
                    if item is None:
                        raise ServiceError(
                            "unknown_image",
                            f"file is unknown in this zone: {filename}",
                        )
                    # Verify every pair before sending HTTP headers.
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

    def item_payload(
        self,
        zid: str,
        item: StoredImage,
        *,
        zone: ZoneConfig | None = None,
        destination: LocalDestination | None = None,
    ) -> dict:
        if zone is None or destination is None:
            with self._registry_lock:
                zone = self._zone_cfg.get(zid)
                destination = self._destinations.get(zid)
            if zone is None or destination is None:
                raise ServiceError("unknown_zone", f"unknown zone: {zid}")
        reference_path = destination.reference_path(item.filename)
        return {
            "id": item.filename,
            "filename": item.filename,
            "created_at": item.created_at.isoformat(timespec="microseconds"),
            "changed_at": (
                item.changed_at.isoformat(timespec="microseconds")
                if item.changed_at is not None
                else None
            ),
            "width": item.width,
            "height": item.height,
            "size": item.size,
            "format": item.fmt,
            "kind": item.kind,
            "mime": item.mime,
            "comment": item.comment,
            "preview_url": public_path(
                self.cfg.url_prefix,
                f"/previews/{quote(zid, safe='')}/{quote(item.filename, safe='')}",
            ),
            "reference": f"{zone.reference_prefix}{reference_path}{zone.reference_suffix}",
        }
