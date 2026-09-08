"""Business logic: upload -> validation -> storage -> retention.

The service is the only path from the web layer to destinations; it serializes
operations per zone to keep retention coherent under concurrency while leaving
zones independent from one another.
"""
from __future__ import annotations

import hashlib
import logging
import os
import threading
from contextlib import ExitStack, contextmanager
from pathlib import Path
from urllib.parse import quote

from .zone_collection import (
    ZoneCollectionCandidate,
    discover_zone_collections,
    resolve_collection_members,
)
from .config import Config, GroupConfig, ZoneConfig, public_path, resolve_group_zone_ids
from .content import classify
from .images import (
    InvalidImageError,
    mime_allowed,
    mime_syntax_allowed,
)
from .platformfs import platform_fs
from .storage import (
    CREATION_METHODS,
    DestinationError,
    DestinationBusyError,
    LocalDestination,
    ReplacementRequiredError,
    RetentionError,
    StorageConflictError,
    StorageLowError,
    StoredImage,
    UnknownImageError,
    validate_comment,
    portable_filename,
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
        self._zone_collection_refresh_lock = threading.Lock()
        self._zone_collection_refresh_condition = threading.Condition()
        self._zone_collection_refresh_in_progress = False
        self._zone_collection_refresh_generation = 0
        self._zone_collection_refresh_closed = False
        self._zone_cfg: dict[str, ZoneConfig] = {}
        self._destinations: dict[str, LocalDestination] = {}
        self._locks: dict[str, threading.RLock] = {}
        self._space_locks: dict[int, _DeviceSpaceLock] = {}
        self._operation_state: dict[str, str] = {}
        self._operation_state_lock = threading.Lock()
        self._zone_collection_diagnostics: tuple[str, ...] = ()
        self._install_registry(cfg.zones, (), initial=True)
        self._refresh_zone_collections()

    def _new_destination(self, zone: ZoneConfig) -> LocalDestination:
        common = {
            "limits": self.cfg.limits,
            "max_image_pixels": self.cfg.max_image_pixels,
            "file_group": zone.file_group,
        }
        return LocalDestination(
            zone.directory,
            create_directory=zone.create_directory,
            **common,
        )

    def _destination_matches(self, destination: LocalDestination, zone: ZoneConfig) -> bool:
        if not isinstance(destination, LocalDestination):
            return False
        if destination.directory != zone.directory.resolve():
            return False
        return (
            destination.create_directory == zone.create_directory
            and destination.limits == self.cfg.limits
            and destination.max_image_pixels == self.cfg.max_image_pixels
            and destination.file_group == zone.file_group
        )

    def _effective_groups(
        self,
        zones: dict[str, ZoneConfig],
        candidates: tuple[ZoneCollectionCandidate, ...],
    ) -> tuple[dict[str, tuple[str, ...]], tuple[GroupConfig, ...]]:
        return (
            resolve_collection_members(candidates, zones),
            self.cfg.groups,
        )

    def _install_registry(
        self,
        static_zones: dict[str, ZoneConfig],
        candidates: tuple[ZoneCollectionCandidate, ...],
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
                log.warning("zone collection destination unavailable: %s", zone.directory)
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
                log.warning(
                    "zone collection filesystem unavailable: %s",
                    active_zones[zid].directory,
                )
                destinations.pop(zid, None)
                active_zones.pop(zid, None)
                continue
            self._space_locks.setdefault(device, _DeviceSpaceLock(device))
        active_candidates = tuple(
            candidate for candidate in candidates if candidate.zone.id in active_zones
        )
        collection_members, effective_groups = self._effective_groups(
            active_zones, active_candidates
        )
        group_zone_ids = resolve_group_zone_ids(
            effective_groups,
            active_zones,
            collection_members,
        )
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

    def _perform_zone_collection_refresh(self) -> None:
        with self._zone_collection_refresh_lock:
            candidates, diagnostics = discover_zone_collections(
                self.cfg.zone_collections,
                self.cfg.zones,
            )
            candidate_tuple = tuple(candidates)
            self._install_registry(self.cfg.zones, candidate_tuple)
            diagnostic_tuple = tuple(diagnostics)
            if diagnostic_tuple != self._zone_collection_diagnostics:
                for message in diagnostic_tuple:
                    log.warning("%s", message)
                self._zone_collection_diagnostics = diagnostic_tuple

    def _finish_zone_collection_refresh(self) -> None:
        with self._zone_collection_refresh_condition:
            self._zone_collection_refresh_in_progress = False
            self._zone_collection_refresh_generation += 1
            self._zone_collection_refresh_condition.notify_all()

    def _background_zone_collection_refresh(self) -> None:
        try:
            self._perform_zone_collection_refresh()
        except Exception:
            log.exception("background zone collection refresh failed")
        finally:
            self._finish_zone_collection_refresh()

    def _refresh_zone_collections(self, *, background: bool = False) -> None:
        if not self.cfg.zone_collections:
            return
        with self._zone_collection_refresh_condition:
            if self._zone_collection_refresh_closed:
                return
            generation = self._zone_collection_refresh_generation
            if self._zone_collection_refresh_in_progress:
                owner = False
            else:
                self._zone_collection_refresh_in_progress = True
                owner = True
        if not owner:
            if not background:
                with self._zone_collection_refresh_condition:
                    while (
                        self._zone_collection_refresh_in_progress
                        and self._zone_collection_refresh_generation == generation
                    ):
                        self._zone_collection_refresh_condition.wait()
            return
        if background:
            try:
                thread = threading.Thread(
                    target=self._background_zone_collection_refresh,
                    name="pasteberth-zone-discovery",
                    daemon=True,
                )
                thread.start()
            except BaseException:
                self._finish_zone_collection_refresh()
                raise
            return
        try:
            self._perform_zone_collection_refresh()
        finally:
            self._finish_zone_collection_refresh()

    def close(self) -> None:
        """Stop new background discovery and wait for an active scan."""
        with self._zone_collection_refresh_condition:
            self._zone_collection_refresh_closed = True
            while self._zone_collection_refresh_in_progress:
                self._zone_collection_refresh_condition.wait()

    def _valid_filename(self, name: object) -> bool:
        return valid_filename(
            name,
            max_length=self.cfg.limits.max_filename_length,
            max_bytes=self.cfg.limits.max_filename_bytes,
        )

    def _new_filename(self, name: object) -> bool:
        return portable_filename(
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
                with destination.operation_lock(
                    exclusive=exclusive,
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
            self._refresh_zone_collections()
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

    @contextmanager
    def transfer_operation(
        self,
        source_zid: str,
        target_zid: str,
        *,
        blocking: bool = True,
    ):
        """Lock two distinct zones in a stable order for an internal transfer."""
        if source_zid == target_zid:
            raise ServiceError("invalid_request", "source and target zones must differ")
        self._refresh_zone_collections()
        with self._registry_lock:
            snapshots = {
                zid: (
                    self._zone_cfg.get(zid),
                    self._destinations.get(zid),
                    self._locks.get(zid),
                )
                for zid in (source_zid, target_zid)
            }
        if any(value[0] is None or value[1] is None or value[2] is None for value in snapshots.values()):
            missing = source_zid if snapshots[source_zid][0] is None else target_zid
            raise ServiceError("unknown_zone", f"unknown zone: {missing}")

        ordered = sorted(
            [
                (
                    zid,
                    snapshots[zid][0],
                    snapshots[zid][1],
                    snapshots[zid][2],
                )
                for zid in (source_zid, target_zid)
            ],
            key=lambda entry: (str(entry[2].directory), entry[0]),
        )
        acquired_locks = []
        try:
            for zid, _zone, _destination, zone_lock in ordered:
                if not zone_lock.acquire(blocking=blocking):
                    raise ServiceError("zone_busy", f"zone {zid!r} is busy")
                acquired_locks.append(zone_lock)
            try:
                with ExitStack() as stack:
                    for zid, _zone, destination, _zone_lock in ordered:
                        try:
                            stack.enter_context(
                                destination.operation_lock(
                                    exclusive=True,
                                    blocking=blocking,
                                )
                            )
                        except DestinationBusyError as exc:
                            raise ServiceError("zone_busy", str(exc)) from exc
                    with self._operation_state_lock:
                        for zid in (source_zid, target_zid):
                            self._operation_state[zid] = "transfer"
                    try:
                        yield {
                            source_zid: (snapshots[source_zid][0], snapshots[source_zid][1]),
                            target_zid: (snapshots[target_zid][0], snapshots[target_zid][1]),
                        }
                    finally:
                        with self._operation_state_lock:
                            for zid in (source_zid, target_zid):
                                if self._operation_state.get(zid) == "transfer":
                                    self._operation_state.pop(zid, None)
            except ServiceError:
                raise
            except (DestinationError, OSError) as exc:
                raise ServiceError("destination_error", str(exc)) from exc
        finally:
            for zone_lock in reversed(acquired_locks):
                zone_lock.release()

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
            if not filename_hint or not self._new_filename(filename_hint):
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

    def _upload_limit_bytes(self, zone: ZoneConfig, destination: LocalDestination) -> int:
        space_limit = destination.available_upload_bytes(zone.min_free_percent)
        if self.cfg.max_upload_bytes is None:
            return space_limit
        return min(self.cfg.max_upload_bytes, space_limit)

    def _store_prepared_upload(
        self,
        zid: str,
        zone: ZoneConfig,
        destination: LocalDestination,
        data: bytes,
        info,
        target_filename: str | None,
        allow_replace: bool,
        creation_method: str,
    ) -> tuple[StoredImage, list[str], bool]:
        content_sha256 = hashlib.sha256(data).hexdigest()
        try:
            device = destination.device_id
        except (DestinationError, OSError) as exc:
            raise ServiceError("destination_error", str(exc)) from exc
        try:
            with self._space_locks[device].locked():
                if target_filename is None:
                    duplicate = destination.find_duplicate(content_sha256, len(data))
                    if duplicate is not None:
                        return duplicate, [], True
                destination.ensure_space(len(data), zone.min_free_percent)
                stored = destination.save(
                    data,
                    info,
                    filename=target_filename,
                    allow_replace=allow_replace,
                    sha256=content_sha256,
                    creation_method=creation_method,
                )
                retention_deleted = destination.apply_retention(zone.retain, stored.filename)
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
            "upload zone=%s file=%s kind=%s %d bytes",
            zid,
            stored.filename,
            stored.kind,
            stored.size,
        )
        return stored, retention_deleted, False

    # ---------------------------------------------------------------- zones

    @property
    def auth_enabled(self) -> bool:
        return self.cfg.auth.enabled

    def has_zone(self, zid: str) -> bool:
        self._refresh_zone_collections()
        with self._registry_lock:
            return zid in self._zone_cfg

    def active_zone_count(self) -> int:
        """Return the number of zones in the current registry snapshot."""
        with self._registry_lock:
            return len(self._zone_cfg)

    def zone_id_for_directory(self, directory: str | Path) -> str:
        """Resolve a client path against the daemon's configured zones."""
        try:
            target = Path(directory).expanduser().resolve()
        except (OSError, RuntimeError, ValueError) as exc:
            raise ServiceError("invalid_request", f"invalid zone directory: {directory}") from exc
        target_key = os.path.normcase(os.path.normpath(str(target)))
        self._refresh_zone_collections()
        with self._registry_lock:
            zones = tuple(self._zone_cfg.items())
        for zid, zone in zones:
            try:
                configured = zone.directory.resolve()
            except (OSError, RuntimeError, ValueError):
                continue
            configured_key = os.path.normcase(os.path.normpath(str(configured)))
            if target_key == configured_key:
                return zid
        raise ServiceError(
            "unknown_zone",
            f"target directory does not match any configured zone: {target}",
        )

    def _group_overview_from_registry(self) -> list[dict]:
        with self._registry_lock:
            groups = tuple(self._group_zone_ids.items())
            configured = {
                group.name: group
                for group in self._effective_group_configs
            }
        return self._group_overview_snapshot(groups, configured)

    @staticmethod
    def _group_overview_snapshot(groups, configured) -> list[dict]:
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
        self._refresh_zone_collections(background=True)
        with self._registry_lock:
            group_snapshot = tuple(self._group_zone_ids.items())
            group_configs = {
                group.name: group
                for group in self._effective_group_configs
            }
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
            upload_limit_bytes = None
            dynamic = zid not in self.cfg.zones
            try:
                items = self.history(
                    zid,
                    blocking=blocking,
                    _refresh=False,
                    _snapshot=(zone, destination, zone_lock),
                )
            except ServiceError as exc:
                if exc.code != "zone_busy" and not (
                    dynamic and exc.code == "destination_error"
                ):
                    raise
                busy = True
                items = []
            except (DestinationError, OSError) as exc:
                if not dynamic:
                    raise ServiceError("destination_error", str(exc)) from exc
                busy = True
                items = []
            if not busy:
                try:
                    upload_limit_bytes = self._upload_limit_bytes(zone, destination)
                except (DestinationError, OSError) as exc:
                    if not dynamic:
                        raise ServiceError("destination_error", str(exc)) from exc
                    busy = True
                    items = []
            zones.append(
                {
                    "id": zid,
                    "label": zone.label,
                    "color": zone.color,
                    "retain": zone.retain,
                    "count": None if busy else len(items),
                    "images": [] if busy else items,
                    "upload_limit_bytes": upload_limit_bytes,
                    "groups": list(groups),
                    "busy": busy,
                    "storage_mode": zone.storage_mode,
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
            "groups": self._group_overview_snapshot(group_snapshot, group_configs),
        }

    def group_overview(self) -> list[dict]:
        """Return groups without reading storage destinations."""
        self._refresh_zone_collections(background=True)
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
        creation_method: str = "filesystem_drop",
        blocking: bool = True,
    ) -> dict:
        if not self.has_zone(zid):
            raise ServiceError("unknown_zone", f"unknown zone: {zid}")
        if not isinstance(creation_method, str) or creation_method not in CREATION_METHODS:
            raise ServiceError("invalid_request", "invalid creation method")
        info, target_filename = self._prepare_upload(
            data, declared_mime, filename_hint, preserve_filename
        )
        with self.zone_operation(
            zid, kind="upload", exclusive=True, blocking=blocking
        ) as (zone, destination):
            stored, retention_deleted, duplicate = self._store_prepared_upload(
                zid,
                zone,
                destination,
                data,
                info,
                target_filename,
                allow_replace,
                creation_method,
            )
        payload = self.item_payload(zid, stored, zone=zone, destination=destination)
        if retention_deleted:
            payload["retention_deleted"] = retention_deleted
        if duplicate:
            payload["duplicate"] = True
        return payload

    def regularize_staged_upload(
        self,
        zid: str,
        stage_name: str,
        filename: str,
        declared_mime: str | None,
        *,
        allow_replace: bool = False,
        blocking: bool = True,
    ) -> dict:
        """Turn a locally staged file into a managed data/sidecar pair."""
        if not self.has_zone(zid):
            raise ServiceError("unknown_zone", f"unknown zone: {zid}")
        with self.zone_operation(
            zid, kind="upload", exclusive=True, blocking=blocking
        ) as (zone, destination):
            try:
                data, stage_identity = destination.read_direct_drop(
                    stage_name,
                    self.cfg.max_upload_bytes,
                )
                info, target_filename = self._prepare_upload(
                    data,
                    declared_mime,
                    filename,
                    preserve_filename=True,
                )
                stored, retention_deleted, duplicate = self._store_prepared_upload(
                    zid,
                    zone,
                    destination,
                    data,
                    info,
                    target_filename,
                    allow_replace,
                    "filesystem_drop",
                )
                destination.discard_direct_drop(stage_name, stage_identity)
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
        payload = self.item_payload(zid, stored, zone=zone, destination=destination)
        if retention_deleted:
            payload["retention_deleted"] = retention_deleted
        if duplicate:
            payload["duplicate"] = True
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

    def transfer(
        self,
        source_zid: str,
        target_zid: str,
        filenames: list[str],
        *,
        mode: str = "move",
        blocking: bool = True,
    ) -> dict:
        """Copy or move coherent managed pairs between two zones."""
        if mode not in {"copy", "move"}:
            raise ServiceError("invalid_request", "transfer mode must be 'copy' or 'move'")
        if not isinstance(filenames, list) or not filenames:
            raise ServiceError("invalid_request", "no files to transfer")
        if len(set(filenames)) != len(filenames):
            raise ServiceError("invalid_request", "duplicate filenames")
        for filename in filenames:
            if not isinstance(filename, str) or not self._valid_filename(filename):
                raise ServiceError("invalid_filename", "invalid filename")

        with self.transfer_operation(
            source_zid,
            target_zid,
            blocking=blocking,
        ) as snapshots:
            source_zone, source_destination = snapshots[source_zid]
            target_zone, target_destination = snapshots[target_zid]
            try:
                source_items = {
                    item.filename: item for item in source_destination.list()
                }
            except (DestinationError, OSError) as exc:
                raise ServiceError("destination_error", str(exc)) from exc
            selected = []
            for filename in filenames:
                item = source_items.get(filename)
                if item is None:
                    raise ServiceError(
                        "unknown_image",
                        f"file is unknown in source zone: {filename}",
                    )
                selected.append(item)

            for item in selected:
                try:
                    target_destination.ensure_transfer_target_available(item.filename)
                except StorageConflictError as exc:
                    raise ServiceError("storage_conflict", str(exc)) from exc
                except (DestinationError, OSError) as exc:
                    raise ServiceError("destination_error", str(exc)) from exc

            total_bytes = sum(item.size for item in selected)
            if self.cfg.max_upload_bytes is not None:
                oversized = next(
                    (item for item in selected if item.size > self.cfg.max_upload_bytes),
                    None,
                )
                if oversized is not None:
                    raise ServiceError(
                        "too_large",
                        f"content is too large ({oversized.filename!r})",
                    )
            transferred: list[str] = []
            failed: list[dict] = []
            items: list[dict] = []
            retention_deleted: list[str] = []
            published_items: list[tuple[StoredImage, StoredImage]] = []
            try:
                target_device = target_destination.device_id
            except (DestinationError, OSError) as exc:
                raise ServiceError("destination_error", str(exc)) from exc
            with self._space_locks[target_device].locked():
                try:
                    target_destination.ensure_space(
                        total_bytes,
                        target_zone.min_free_percent,
                    )
                except StorageLowError as exc:
                    raise ServiceError("storage_low", str(exc)) from exc
                except (DestinationError, OSError) as exc:
                    raise ServiceError("destination_error", str(exc)) from exc
                for item in selected:
                    target_published = False
                    try:
                        data = source_destination.read(item.filename)
                        if item.sha256 is not None and hashlib.sha256(data).hexdigest() != item.sha256:
                            raise StorageConflictError(
                                f"source content changed: {item.filename!r}"
                            )
                        stored = target_destination.save_managed(data, item)
                        target_published = True
                        removed = target_destination.apply_retention(
                            target_zone.retain,
                            stored.filename,
                        )
                        retention_deleted.extend(removed)
                        # Retention for a later item may remove an earlier
                        # target. Reconcile all published items against the
                        # final target state before reporting success or
                        # deleting any move source.
                        published_items.append((item, stored))
                        log.info(
                            "%s source=%s target=%s file=%s",
                            mode,
                            source_zid,
                            target_zid,
                            item.filename,
                        )
                    except StorageLowError as exc:
                        failed.append(
                            {
                                "filename": item.filename,
                                "code": "storage_low",
                                "message": str(exc),
                                "target_published": target_published,
                            }
                        )
                    except StorageConflictError as exc:
                        failed.append(
                            {
                                "filename": item.filename,
                                "code": "storage_conflict",
                                "message": str(exc),
                                "target_published": target_published,
                            }
                        )
                    except RetentionError as exc:
                        failed.append(
                            {
                                "filename": item.filename,
                                "code": "retention_error",
                                "message": str(exc),
                                "target_published": target_published,
                            }
                        )
                    except UnknownImageError as exc:
                        failed.append(
                            {
                                "filename": item.filename,
                                "code": "unknown_image",
                                "message": str(exc),
                                "target_published": target_published,
                            }
                        )
                    except (DestinationError, OSError) as exc:
                        failed.append(
                            {
                                "filename": item.filename,
                                "code": "destination_error",
                                "message": str(exc),
                                "target_published": target_published,
                            }
                        )

                if published_items:
                    try:
                        remaining_targets = {
                            item.filename for item in target_destination.list()
                        }
                    except (DestinationError, OSError) as exc:
                        raise ServiceError("destination_error", str(exc)) from exc
                    for source_item, stored in published_items:
                        if stored.filename not in remaining_targets:
                            failed.append(
                                {
                                    "filename": source_item.filename,
                                    "code": "retention_error",
                                    "message": "target item was removed by retention",
                                    "target_published": True,
                                }
                            )
                            continue
                        if mode == "move":
                            try:
                                source_destination.delete(source_item.filename)
                            except UnknownImageError as exc:
                                failed.append(
                                    {
                                        "filename": source_item.filename,
                                        "code": "unknown_image",
                                        "message": str(exc),
                                        "target_published": True,
                                    }
                                )
                            except (DestinationError, OSError) as exc:
                                failed.append(
                                    {
                                        "filename": source_item.filename,
                                        "code": "destination_error",
                                        "message": str(exc),
                                        "target_published": True,
                                    }
                                )
                                continue
                        transferred.append(source_item.filename)
                        items.append(
                            self.item_payload(
                                target_zid,
                                stored,
                                zone=target_zone,
                                destination=target_destination,
                            )
                        )

            return {
                "source_zone": source_zid,
                "target_zone": target_zid,
                "mode": mode,
                "transferred": transferred,
                "failed": failed,
                "items": items,
                "retention_deleted": retention_deleted,
            }

    def rename(self, zid: str, source: str, target: str, *, blocking: bool = True) -> dict:
        """Rename a managed pair (file + sidecar) in a zone."""
        if not self.has_zone(zid):
            raise ServiceError("unknown_zone", f"unknown zone: {zid}")
        if not self._valid_filename(source) or not self._new_filename(target) or source == target:
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
                total_bytes = sum(item.size for item in selected)
                max_archive_bytes = self.cfg.limits.max_archive_bytes
                if max_archive_bytes is not None and total_bytes > max_archive_bytes:
                    raise ServiceError(
                        "too_large",
                        "selected files exceed the archive size limit",
                    )
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
            "creation_method": item.creation_method,
            "replaced": item.replaced,
            "comment": item.comment,
            "preview_url": public_path(
                self.cfg.url_prefix,
                f"/previews/{quote(zid, safe='')}/{quote(item.filename, safe='')}",
            ),
            "reference": f"{zone.reference_prefix}{reference_path}{zone.reference_suffix}",
        }
