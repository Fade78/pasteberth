"""Read-only discovery of directory-backed dynamic zones."""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Hashable, Iterable, Mapping

from .config import AutoZoneConfig, GroupConfig, ZoneConfig


log = logging.getLogger("pasteberth.autozone")

_RESERVED_DIRECTORIES = frozenset({"incoming", ".pasteberth"})
_ZONE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


@dataclass(frozen=True)
class AutoZoneCandidate:
    """One accepted directory and the autozone groups that selected it."""

    zone: ZoneConfig
    groups: tuple[str, ...]
    rule_indexes: tuple[int, ...]


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
    """Reject user directories while allowing the two Pasteberth subtrees."""
    root_key = _directory_key(path)
    stack: list[tuple[Path, bool, frozenset[object]]] = [(path, False, frozenset({root_key}))]
    while stack:
        current, inside_reserved, ancestors = stack.pop()
        try:
            entries = sorted(os.scandir(current), key=lambda entry: entry.name)
        except OSError as exc:
            return False, f"cannot inspect subtree {current}: {exc}"
        for entry in entries:
            try:
                if not entry.is_dir(follow_symlinks=True):
                    continue
            except OSError as exc:
                return False, f"cannot inspect directory entry {entry.name!r}: {exc}"
            allowed = inside_reserved or entry.name in _RESERVED_DIRECTORIES
            if not allowed:
                return False, f"contains user subdirectory {entry.name!r}"
            child = Path(entry.path)
            try:
                resolved = child.resolve(strict=True)
            except (OSError, RuntimeError, ValueError) as exc:
                return False, f"cannot resolve directory {child}: {exc}"
            try:
                resolved.relative_to(path)
            except ValueError:
                return False, f"reserved directory escapes candidate: {child}"
            child_key = _directory_key(resolved)
            if child_key in ancestors:
                continue
            stack.append(
                (
                    resolved,
                    inside_reserved or entry.name in _RESERVED_DIRECTORIES,
                    ancestors | {child_key},
                )
            )
    return True, None


def _scan_rule(
    rule: AutoZoneConfig,
    rule_index: int,
) -> tuple[list[tuple[Path, str, Hashable]], list[str]]:
    prefix = f"autozone #{rule_index + 1}"
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
                # Following a link outside the rule base is safe but cannot
                # produce a path relative to this rule.
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


def _zone_from_candidate(rule: AutoZoneConfig, path: Path, relative: str) -> ZoneConfig | None:
    zone_id = "-".join(relative.split("/")).lower()
    if not _ZONE_ID_RE.fullmatch(zone_id):
        return None
    label = relative if rule.label_mode == "relative" else _git_label(path, relative)
    return ZoneConfig(
        id=zone_id,
        label=label,
        directory=path,
        retain=rule.max_items,
        reference_prefix=rule.reference_prefix,
        reference_suffix=rule.reference_suffix,
        reference_list_prefix=rule.reference_list_prefix,
        reference_list_suffix=rule.reference_list_suffix,
        reference_separator=rule.reference_separator,
        allow_zip_download=rule.allow_zip_download,
        color=rule.color,
        create_directory=False,
        min_free_percent=rule.min_free_percent,
        storage_mode=rule.storage_mode,
        max_items=rule.max_items,
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


def discover_autozones(
    rules: Iterable[AutoZoneConfig],
    static_zones: Mapping[str, ZoneConfig] | Iterable[ZoneConfig] = (),
) -> tuple[list[AutoZoneCandidate], list[str]]:
    """Return a deterministic dynamic-zone snapshot and diagnostics."""
    static_zone_values = tuple(
        static_zones.values() if isinstance(static_zones, Mapping) else static_zones
    )
    static_zones = {zone.id: zone for zone in static_zone_values}
    static = _static_directory_keys(static_zones)
    static_ids = set(static_zones)
    diagnostics: list[str] = []
    records_by_identity: dict[Hashable, list[tuple[int, AutoZoneConfig, Path, str]]] = {}
    for rule_index, rule in enumerate(rules):
        matches, rule_diagnostics = _scan_rule(rule, rule_index)
        diagnostics.extend(rule_diagnostics)
        for path, relative, identity in sorted(matches, key=lambda match: match[1]):
            normalized = os.path.normcase(os.path.normpath(str(path)))
            if any(normalized == static_path or identity == static_identity for static_path, static_identity in static):
                diagnostics.append(
                    f"autozone #{rule_index + 1}: candidate {relative!r} ignored: "
                    "static zone has precedence"
                )
                continue
            records_by_identity.setdefault(identity, []).append(
                (rule_index, rule, path, relative)
            )

    canonical: list[
        tuple[int, AutoZoneConfig, Path, str, Hashable, tuple[str, ...], tuple[int, ...]]
    ] = []
    for identity, records in records_by_identity.items():
        records.sort(key=lambda record: (record[3], record[0], str(record[2])))
        rule_index, rule, path, relative = records[0]
        if len(records) > 1:
            aliases = ", ".join(record[3] for record in records[1:])
            diagnostics.append(
                f"autozone #{rule_index + 1}: candidate {relative!r} is canonical; "
                f"resolved aliases ignored: {aliases}"
            )
        groups = tuple(dict.fromkeys(record[1].group for record in records))
        rule_indexes = tuple(dict.fromkeys(record[0] for record in records))
        canonical.append((rule_index, rule, path, relative, identity, groups, rule_indexes))

    by_id: dict[str, list[AutoZoneCandidate]] = {}
    for rule_index, rule, path, relative, identity, groups, rule_indexes in canonical:
        zone = _zone_from_candidate(rule, path, relative)
        zone_id = "-".join(relative.split("/")).lower()
        if zone is None:
            diagnostics.append(
                f"autozone #{rule_index + 1}: candidate {relative!r} ignored: "
                f"generated zone ID {zone_id!r} is invalid or longer than 64 characters"
            )
            continue
        if zone.id in static_ids:
            diagnostics.append(
                f"autozone #{rule_index + 1}: candidate {relative!r} ignored: "
                f"generated zone ID {zone.id!r} collides with static zone"
            )
            continue
        by_id.setdefault(zone.id, []).append(
            AutoZoneCandidate(zone=zone, groups=groups, rule_indexes=rule_indexes)
        )

    result: list[AutoZoneCandidate] = []
    for zone_id, candidates in sorted(by_id.items()):
        if len(candidates) > 1:
            for candidate in candidates:
                diagnostics.append(
                    f"autozone candidate {candidate.zone.directory} ignored: "
                    f"generated zone ID {zone_id!r} collides with another autozone"
                )
            continue
        result.append(candidates[0])
    return result, diagnostics


def merge_autozone_groups(
    configured_groups: Iterable[GroupConfig],
    rules: Iterable[AutoZoneConfig],
    zones: Mapping[str, ZoneConfig],
    candidates: Iterable[AutoZoneCandidate],
) -> tuple[tuple[GroupConfig, ...], list[str]]:
    """Add dynamic memberships and generate groups for unnamed rules."""
    configured = tuple(configured_groups)
    rules = tuple(rules)
    active_ids = set(zones)
    members: dict[str, list[str]] = {}
    for candidate in candidates:
        if candidate.zone.id not in active_ids:
            continue
        for group_name in candidate.groups:
            group_members = members.setdefault(group_name, [])
            if candidate.zone.id not in group_members:
                group_members.append(candidate.zone.id)

    def ordered_members(group_name: str) -> tuple[str, ...]:
        selected = set(members.get(group_name, ()))
        return tuple(zone_id for zone_id in zones if zone_id in selected)

    ordinary_names = {group.name for group in configured}
    groups = [
        GroupConfig(
            name=group.name,
            selection=group.selection,
            pattern=group.pattern,
            pattern_defined=group.pattern_defined,
            layout=group.layout,
            hide_empty=group.hide_empty,
            show_count=group.show_count,
            members=ordered_members(group.name),
        )
        for group in configured
    ]
    generated: dict[str, GroupConfig] = {}
    diagnostics: list[str] = []
    for rule_index, rule in enumerate(rules):
        if rule.group in ordinary_names:
            continue
        existing = generated.get(rule.group)
        if existing is None:
            generated[rule.group] = GroupConfig(
                name=rule.group,
                selection="autozone",
                members=ordered_members(rule.group),
                layout=rule.group_layout,
                hide_empty=rule.group_hide_empty,
                show_count=rule.group_show_count,
            )
            continue
        if (
            existing.layout != rule.group_layout
            or existing.hide_empty != rule.group_hide_empty
            or existing.show_count != rule.group_show_count
        ):
            diagnostics.append(
                f"autozone #{rule_index + 1}: generated group {rule.group!r} "
                "options differ from the first rule; first rule wins"
            )
        generated[rule.group] = GroupConfig(
            name=existing.name,
            selection=existing.selection,
            pattern=existing.pattern,
            pattern_defined=existing.pattern_defined,
            layout=existing.layout,
            hide_empty=existing.hide_empty,
            show_count=existing.show_count,
            members=ordered_members(rule.group),
        )
    groups.extend(generated.values())
    return tuple(groups), diagnostics
