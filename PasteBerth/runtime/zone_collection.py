"""Read-only discovery of sidecar-backed zone collections."""
from __future__ import annotations

import hashlib
import logging
import os
import re
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


def _directory_key(path: Path) -> Hashable:
    try:
        info = path.stat()
    except OSError:
        return ("path", os.path.normcase(os.path.normpath(str(path))))
    if info.st_ino:
        return ("identity", info.st_dev, info.st_ino)
    return ("path", os.path.normcase(os.path.normpath(str(path))))


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


def _candidate_subtree_ok(path: Path) -> tuple[bool, str | None]:
    """Reject candidates containing any subdirectory."""
    try:
        entries = sorted(os.scandir(path), key=lambda entry: entry.name)
    except OSError as exc:
        return False, f"cannot inspect subtree {path}: {exc}"
    for entry in entries:
        try:
            if entry.is_dir(follow_symlinks=True):
                return False, f"contains user subdirectory {entry.name!r}"
        except OSError as exc:
            return False, f"cannot inspect directory entry {entry.name!r}: {exc}"
    return True, None


def _scan_collection(
    rule: ZoneCollectionConfig,
    rule_index: int,
) -> tuple[list[tuple[Path, str, Hashable]], list[str]]:
    prefix = f"zone collection #{rule_index + 1}"
    try:
        base = rule.base_directory.resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        return [], [f"{prefix}: base directory is unavailable: {rule.base_directory} ({exc})"]
    if not base.is_dir():
        return [], [f"{prefix}: base directory is not a directory: {base}"]

    try:
        expression = re.compile(rule.pattern)
    except re.error as exc:
        return [], [f"{prefix}: invalid pattern {rule.pattern!r}: {exc}"]

    matches: list[tuple[Path, str, Hashable]] = []
    diagnostics: list[str] = []
    base_key = _directory_key(base)
    stack: list[tuple[Path, frozenset[object]]] = [(base, frozenset({base_key}))]
    visited: set[object] = set()
    while stack:
        current, ancestors = stack.pop()
        current_key = _directory_key(current)
        if current_key in visited:
            continue
        visited.add(current_key)
        try:
            entries = sorted(os.scandir(current), key=lambda entry: entry.name, reverse=True)
        except OSError as exc:
            diagnostics.append(f"{prefix}: cannot inspect {current}: {exc}")
            continue
        for entry in entries:
            try:
                if not entry.is_dir(follow_symlinks=True):
                    continue
            except OSError as exc:
                diagnostics.append(f"{prefix}: cannot inspect {entry.name!r}: {exc}")
                continue
            lexical = Path(entry.path)
            try:
                resolved = lexical.resolve(strict=True)
            except (OSError, RuntimeError, ValueError) as exc:
                diagnostics.append(f"{prefix}: cannot resolve {lexical}: {exc}")
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
            child_key = _directory_key(resolved)
            if expression.fullmatch(relative):
                valid, reason = _candidate_subtree_ok(resolved)
                if valid:
                    matches.append((resolved, relative, child_key))
                else:
                    diagnostics.append(f"{prefix}: candidate {relative!r} ignored: {reason}")
            if depth < rule.max_depth and child_key not in ancestors:
                stack.append((resolved, ancestors | {child_key}))
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
) -> set[tuple[str, Hashable]]:
    zones = static_zones.values() if isinstance(static_zones, Mapping) else static_zones
    paths: set[tuple[str, Hashable]] = set()
    for zone in zones:
        try:
            path = zone.directory.resolve()
        except (OSError, RuntimeError, ValueError):
            continue
        paths.add((os.path.normcase(os.path.normpath(str(path))), _directory_key(path)))
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
    static = _static_directory_keys(static_zones)
    static_ids = set(static_zones)
    diagnostics: list[str] = []
    records_by_identity: dict[
        Hashable, list[tuple[int, ZoneCollectionConfig, Path, str]]
    ] = {}
    for rule_index, rule in enumerate(rules):
        matches, rule_diagnostics = _scan_collection(rule, rule_index)
        diagnostics.extend(rule_diagnostics)
        for path, relative, identity in sorted(matches, key=lambda match: match[1]):
            normalized = os.path.normcase(os.path.normpath(str(path)))
            if any(normalized == static_path or identity == static_identity for static_path, static_identity in static):
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
