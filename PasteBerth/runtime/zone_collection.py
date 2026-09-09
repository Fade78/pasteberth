"""Read-only discovery of sidecar-backed zone collections."""
from __future__ import annotations

import hashlib
import logging
import os
import re
import stat
import time
from collections.abc import Hashable, Iterable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path

from .config import ZoneCollectionConfig, ZoneConfig


log = logging.getLogger("pasteberth.zone_collection")

_ZONE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_ZONE_COLLECTION_COLORS = (
    "#243447", "#304c61", "#3f5f75", "#4d426b",
    "#5c3f63", "#633f4b", "#65452f", "#5d542f",
    "#3f5e45", "#2f5e5d", "#3e506b", "#51405f",
)


@dataclass(frozen=True)
class ZoneCollectionCandidate:
    """One accepted directory and the collections that contain it."""

    zone: ZoneConfig
    collection_ids: tuple[str, ...]
    rule_indexes: tuple[int, ...]


def _zone_settings_signature(rule: ZoneCollectionConfig) -> tuple[object, ...]:
    """Return the zone behavior controlled by a collection rule."""
    return (
        rule.label_mode,
        rule.storage_mode,
        rule.retain,
        rule.file_group,
        rule.min_free_percent,
        rule.reference_prefix,
        rule.reference_suffix,
        rule.reference_list_prefix,
        rule.reference_list_suffix,
        rule.reference_separator,
        rule.allow_zip_download,
        rule.color,
    )


class _DiscoveryPass:
    """Filesystem observations shared by rules, never by discovery calls."""

    def __init__(self) -> None:
        self.resolutions: dict[Path, tuple[Path | None, str | None]] = {}
        self.stats: dict[Path, os.stat_result | None] = {}
        self.entries: dict[
            Path, tuple[tuple[tuple[str, bool, str | None], ...], str | None]
        ] = {}
        self.subtrees: dict[Path, tuple[bool, str | None]] = {}

    def resolve(self, path: Path) -> tuple[Path | None, str | None]:
        if path not in self.resolutions:
            try:
                # A cached parent alone cannot prove its ancestors are still
                # free of symlinks. Keep strict, full resolution for new paths.
                resolved = path.resolve(strict=True)
            except (OSError, RuntimeError, ValueError) as exc:
                self.resolutions[path] = (None, str(exc))
            else:
                self.resolutions[path] = (resolved, None)
                self.resolutions.setdefault(resolved, (resolved, None))
        return self.resolutions[path]

    def stat(self, path: Path) -> os.stat_result | None:
        if path not in self.stats:
            try:
                self.stats[path] = path.stat()
            except OSError:
                self.stats[path] = None
        return self.stats[path]

    def directory_key(self, path: Path) -> Hashable:
        info = self.stat(path)
        if info is not None and info.st_ino:
            return ("identity", info.st_dev, info.st_ino)
        return ("path", os.path.normcase(os.path.normpath(str(path))))

    def inspect(
        self, path: Path, *, leaf_only: bool = False,
    ) -> tuple[tuple[tuple[str, bool, str | None], ...], str | None]:
        # Key by canonical PATH, not inode: bind aliases can have different
        # children/mounts, and their entry paths must remain relative to this base.
        if path not in self.entries:
            entries: list[tuple[str, bool, str | None]] = []
            try:
                with os.scandir(path) as scan:
                    for entry in sorted(scan, key=lambda entry: entry.name):
                        try:
                            is_directory = entry.is_dir(follow_symlinks=True)
                        except OSError as exc:
                            entries.append((entry.name, False, str(exc)))
                        else:
                            if is_directory:
                                entries.append((entry.name, True, None))
                        if leaf_only and entries:
                            # Reject at the first directory/error without checking
                            # the tail. This partial probe is NOT traversal data.
                            return (tuple(entries), None)
            except OSError as exc:
                self.entries[path] = ((), str(exc))
            else:
                self.entries[path] = (tuple(entries), None)
        return self.entries[path]


def _relative_path(path: Path, base: Path) -> str | None:
    try:
        relative = path.relative_to(base)
    except ValueError:
        return None
    return "/".join(part for part in relative.parts if part not in ("", "."))


def _git_label(path: Path, relative: str) -> str:
    current = path
    while True:
        marker = current / ".git"
        try:
            if marker.is_dir() or marker.is_file():
                return current.name or relative
        except OSError:
            pass
        parent = current.parent
        if parent == current:
            break
        current = parent
    return relative


def _candidate_subtree_ok(
    path: Path, context: _DiscoveryPass,
) -> tuple[bool, str | None]:
    """Reject candidates containing any subdirectory."""
    if path not in context.subtrees:
        entries, error = context.inspect(path, leaf_only=True)
        result: tuple[bool, str | None] = (True, None)
        if error is not None:
            result = (False, f"cannot inspect subtree {path}: {error}")
        else:
            for name, is_directory, entry_error in entries:
                if entry_error is not None:
                    result = (False, f"cannot inspect directory entry {name!r}: {entry_error}")
                    break
                if is_directory:
                    result = (False, f"contains user subdirectory {name!r}")
                    break
        context.subtrees[path] = result
    return context.subtrees[path]


def _scan_collection(
    rule: ZoneCollectionConfig,
    rule_index: int,
    context: _DiscoveryPass,
) -> tuple[list[tuple[Path, str, Hashable]], list[str]]:
    prefix = f"zone collection #{rule_index + 1}"
    base, error = context.resolve(rule.base_directory)
    if base is None:
        return [], [f"{prefix}: base directory is unavailable: {rule.base_directory} ({error})"]
    base_info = context.stat(base)
    if base_info is None or not stat.S_ISDIR(base_info.st_mode):
        return [], [f"{prefix}: base directory is not a directory: {base}"]

    try:
        expression = re.compile(rule.pattern)
    except re.error as exc:
        return [], [f"{prefix}: invalid pattern {rule.pattern!r}: {exc}"]

    matches: list[tuple[Path, str, Hashable]] = []
    diagnostics: list[str] = []
    base_key = context.directory_key(base)
    stack: list[tuple[Path, Hashable, frozenset[object]]] = [
        (base, base_key, frozenset({base_key}))
    ]
    visited: set[object] = set()
    while stack:
        current, current_key, ancestors = stack.pop()
        if current_key in visited:
            continue
        visited.add(current_key)
        entries, error = context.inspect(current)
        if error is not None:
            diagnostics.append(f"{prefix}: cannot inspect {current}: {error}")
            continue
        for name, is_directory, entry_error in reversed(entries):
            if entry_error is not None:
                diagnostics.append(f"{prefix}: cannot inspect {name!r}: {entry_error}")
                continue
            if not is_directory:
                continue
            lexical = current / name
            resolved, error = context.resolve(lexical)
            if resolved is None:
                diagnostics.append(f"{prefix}: cannot resolve {lexical}: {error}")
                continue
            if lexical != resolved:
                diagnostics.append(
                    f"{prefix}: directory alias {lexical} resolves to {resolved}"
                )
            relative = _relative_path(resolved, base)
            if relative is None:
                # Following a link outside the collection base is safe but cannot
                # produce a path relative to this collection.
                continue
            depth = len(relative.split("/")) if relative else 0
            if depth == 0 or depth > rule.max_depth:
                continue
            child_key = context.directory_key(resolved)
            if expression.fullmatch(relative):
                valid, reason = _candidate_subtree_ok(resolved, context)
                if valid:
                    matches.append((resolved, relative, child_key))
                else:
                    diagnostics.append(f"{prefix}: candidate {relative!r} ignored: {reason}")
            if depth < rule.max_depth and child_key not in ancestors:
                stack.append((resolved, child_key, ancestors | {child_key}))
    return matches, diagnostics


def _zone_collection_color(path: Path, collection_id: str) -> str:
    key = os.path.normcase(os.path.normpath(str(path))) + "\x00" + collection_id
    digest = hashlib.sha256(key.encode("utf-8")).digest()
    index = int.from_bytes(digest[:8], "big") % len(_ZONE_COLLECTION_COLORS)
    return _ZONE_COLLECTION_COLORS[index]


def _distinct_zone_collection_color(
    path: Path, collection_id: str, used: set[str]
) -> str:
    """Choose a stable readable color that is not already used by the collection."""
    key = os.path.normcase(os.path.normpath(str(path))) + "\x00" + collection_id
    digest = hashlib.sha256(key.encode("utf-8")).digest()
    start = int.from_bytes(digest[:8], "big") % len(_ZONE_COLLECTION_COLORS)
    for offset in range(len(_ZONE_COLLECTION_COLORS)):
        color = _ZONE_COLLECTION_COLORS[(start + offset) % len(_ZONE_COLLECTION_COLORS)]
        if color not in used:
            return color

    # Keep producing dark, high-contrast colors when a collection has more
    # entries than the curated palette. The salt makes the fallback deterministic.
    salt = 0
    while True:
        fallback = hashlib.sha256(f"{key}\x00{salt}".encode("utf-8")).digest()
        color = "#" + "".join(f"{24 + (value % 80):02x}" for value in fallback[:3])
        if color not in used:
            return color
        salt += 1


def _assign_distinct_zone_collection_colors(
    candidates: list[ZoneCollectionCandidate],
    rules: tuple[ZoneCollectionConfig, ...],
) -> list[ZoneCollectionCandidate]:
    """Make generated colors distinct within every zone collection."""
    used_by_collection: dict[str, set[str]] = {}
    generated: list[tuple[int, ZoneCollectionCandidate]] = []
    for index, candidate in enumerate(candidates):
        explicit = (
            bool(candidate.rule_indexes)
            and rules[candidate.rule_indexes[0]].color is not None
        )
        for collection_id in candidate.collection_ids:
            used_by_collection.setdefault(collection_id, set())
            if explicit:
                used_by_collection[collection_id].add(candidate.zone.color)
        if not explicit:
            generated.append((index, candidate))

    for index, candidate in generated:
        blocked = set().union(
            *(used_by_collection[collection_id] for collection_id in candidate.collection_ids)
        )
        collection_id = candidate.collection_ids[0] if candidate.collection_ids else ""
        color = _distinct_zone_collection_color(candidate.zone.directory, collection_id, blocked)
        candidates[index] = replace(
            candidate,
            zone=replace(candidate.zone, color=color),
        )
        for collection_id in candidate.collection_ids:
            used_by_collection[collection_id].add(color)
    return candidates


def _zone_from_candidate(
    rule: ZoneCollectionConfig, path: Path, relative: str
) -> ZoneConfig | None:
    zone_id = "-".join(relative.split("/")).lower()
    if not _ZONE_ID_RE.fullmatch(zone_id):
        return None
    if rule.label_mode == "first-directory":
        label = relative.split("/", 1)[0]
    elif rule.label_mode == "relative":
        label = relative
    else:
        label = _git_label(path, relative)
    return ZoneConfig(
        id=zone_id,
        label=label,
        directory=path,
        retain=rule.retain,
        reference_prefix=rule.reference_prefix,
        reference_suffix=rule.reference_suffix,
        reference_list_prefix=rule.reference_list_prefix,
        reference_list_suffix=rule.reference_list_suffix,
        reference_separator=rule.reference_separator,
        allow_zip_download=rule.allow_zip_download,
        color=rule.color if rule.color is not None else _zone_collection_color(path, rule.id),
        create_directory=False,
        min_free_percent=rule.min_free_percent,
        storage_mode="sidecar",
        file_group=rule.file_group,
    )


def _static_directory_keys(
    static_zones: Mapping[str, ZoneConfig] | Iterable[ZoneConfig],
    context: _DiscoveryPass,
) -> set[tuple[str, Hashable]]:
    zones = static_zones.values() if isinstance(static_zones, Mapping) else static_zones
    paths: set[tuple[str, Hashable]] = set()
    for zone in zones:
        try:
            path = zone.directory.resolve()
        except (OSError, RuntimeError, ValueError):
            continue
        paths.add((os.path.normcase(os.path.normpath(str(path))), context.directory_key(path)))
    return paths


def discover_zone_collections(
    rules: Iterable[ZoneCollectionConfig],
    static_zones: Mapping[str, ZoneConfig] | Iterable[ZoneConfig] = (),
) -> tuple[list[ZoneCollectionCandidate], list[str]]:
    """Return a deterministic dynamic-zone snapshot and diagnostics."""
    rules = tuple(rules)
    static_zone_values = tuple(
        static_zones.values() if isinstance(static_zones, Mapping) else static_zones
    )
    static_zones = {zone.id: zone for zone in static_zone_values}
    context = _DiscoveryPass()
    static = _static_directory_keys(static_zones, context)
    static_paths = {path for path, _ in static}
    static_identities = {identity for _, identity in static}
    static_ids = set(static_zones)
    diagnostics: list[str] = []
    records_by_identity: dict[
        Hashable, list[tuple[int, ZoneCollectionConfig, Path, str]]
    ] = {}
    for rule_index, rule in enumerate(rules):
        started = time.perf_counter()
        counts = (len(context.resolutions), len(context.stats), len(context.entries))
        matches, rule_diagnostics = _scan_collection(rule, rule_index, context)
        log.debug(
            "zone collection #%d (%s): scan %.3fs, %d matches; "
            "new cached paths: resolution=%d stat=%d enumeration=%d",
            rule_index + 1, rule.id, time.perf_counter() - started, len(matches),
            len(context.resolutions) - counts[0],
            len(context.stats) - counts[1],
            len(context.entries) - counts[2],
        )
        diagnostics.extend(rule_diagnostics)
        for path, relative, identity in sorted(matches, key=lambda match: match[1]):
            normalized = os.path.normcase(os.path.normpath(str(path)))
            if normalized in static_paths or identity in static_identities:
                diagnostics.append(
                    f"zone collection #{rule_index + 1}: candidate {relative!r} ignored: "
                    "static zone has precedence"
                )
                continue
            records_by_identity.setdefault(identity, []).append(
                (rule_index, rule, path, relative)
            )

    canonical: list[
        tuple[
            int,
            ZoneCollectionConfig,
            Path,
            str,
            Hashable,
            tuple[str, ...],
            tuple[int, ...],
        ]
    ] = []
    for identity, records in records_by_identity.items():
        records.sort(key=lambda record: (record[3], record[0], str(record[2])))
        rule_index, rule, path, relative = records[0]
        settings = {_zone_settings_signature(record[1]) for record in records}
        if len(settings) > 1:
            collection_ids = ", ".join(
                sorted(dict.fromkeys(record[1].id for record in records))
            )
            diagnostics.append(
                f"zone collection candidate {path} ignored: collections "
                f"{collection_ids} define conflicting zone settings"
            )
            continue
        if len(records) > 1:
            aliases = ", ".join(record[3] for record in records[1:])
            diagnostics.append(
                f"zone collection #{rule_index + 1}: candidate {relative!r} is canonical; "
                f"resolved aliases ignored: {aliases}"
            )
        collection_ids = tuple(dict.fromkeys(record[1].id for record in records))
        rule_indexes = tuple(dict.fromkeys(record[0] for record in records))
        canonical.append((rule_index, rule, path, relative, identity, collection_ids, rule_indexes))

    by_id: dict[str, list[ZoneCollectionCandidate]] = {}
    for rule_index, rule, path, relative, identity, collection_ids, rule_indexes in canonical:
        zone = _zone_from_candidate(rule, path, relative)
        zone_id = "-".join(relative.split("/")).lower()
        if zone is None:
            diagnostics.append(
                f"zone collection #{rule_index + 1}: candidate {relative!r} ignored: "
                f"generated zone ID {zone_id!r} is invalid or longer than 64 characters"
            )
            continue
        if zone.id in static_ids:
            diagnostics.append(
                f"zone collection #{rule_index + 1}: candidate {relative!r} ignored: "
                f"generated zone ID {zone.id!r} collides with static zone"
            )
            continue
        by_id.setdefault(zone.id, []).append(
            ZoneCollectionCandidate(
                zone=zone,
                collection_ids=collection_ids,
                rule_indexes=rule_indexes,
            )
        )

    result: list[ZoneCollectionCandidate] = []
    for zone_id, candidates in sorted(by_id.items()):
        if len(candidates) > 1:
            for candidate in candidates:
                diagnostics.append(
                    f"zone collection candidate {candidate.zone.directory} ignored: "
                    f"generated zone ID {zone_id!r} collides with another zone collection"
                )
            continue
        result.append(candidates[0])
    return _assign_distinct_zone_collection_colors(result, tuple(rules)), diagnostics


def resolve_collection_members(
    candidates: Iterable[ZoneCollectionCandidate],
    zone_ids: Iterable[str],
) -> dict[str, tuple[str, ...]]:
    """Build ordered collection membership from the active candidate snapshot."""
    ordered_zone_ids = tuple(zone_ids)
    active_ids = set(ordered_zone_ids)
    members: dict[str, list[str]] = {}
    for candidate in candidates:
        zone_id = candidate.zone.id
        if zone_id not in active_ids:
            continue
        for collection_id in candidate.collection_ids:
            collection_members = members.setdefault(collection_id, [])
            if zone_id not in collection_members:
                collection_members.append(zone_id)
    return {
        collection_id: tuple(
            zone_id for zone_id in ordered_zone_ids if zone_id in set(collection_zone_ids)
        )
        for collection_id, collection_zone_ids in members.items()
    }
