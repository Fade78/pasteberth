#!/usr/bin/env python3
"""Record the exact source bundle used by a Pasteberth deployment."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
import tomllib
from datetime import datetime, timezone
from pathlib import Path


VERSION_RE = re.compile(r'^__version__\s*=\s*["\']([^"\']+)["\']\s*$', re.MULTILINE)
NOFOLLOW_FLAGS = getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)


def git(root: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return result.stdout.strip() or None


def _bundle_paths(root: Path):
    for path in sorted(root.rglob("*")):
        if "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        try:
            mode = os.lstat(path).st_mode
        except OSError as exc:
            raise SystemExit(f"could not inspect bundle file: {path}") from exc
        if stat.S_ISLNK(mode):
            raise SystemExit(f"bundle contains a symlink: {path.relative_to(root)}")
        if stat.S_ISREG(mode):
            yield path
        elif not stat.S_ISDIR(mode):
            raise SystemExit(f"bundle contains a non-regular file: {path.relative_to(root)}")


def _open_regular_file(path: Path) -> tuple[int, int]:
    try:
        fd = os.open(path, os.O_RDONLY | NOFOLLOW_FLAGS)
        mode = os.fstat(fd).st_mode
    except OSError as exc:
        raise SystemExit(f"could not open bundle file: {path}") from exc
    if not stat.S_ISREG(mode):
        os.close(fd)
        raise SystemExit(f"bundle file is not regular: {path}")
    return fd, mode


def _file_digest(path: Path) -> str:
    fd, _ = _open_regular_file(path)
    digest = hashlib.sha256()
    try:
        with os.fdopen(fd, "rb") as stream:
            fd = -1
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    finally:
        if fd >= 0:
            os.close(fd)
    return digest.hexdigest()


def file_digests(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): _file_digest(path)
        for path in _bundle_paths(root)
    }


def file_modes(root: Path) -> dict[str, int]:
    modes = {}
    for path in _bundle_paths(root):
        fd, mode = _open_regular_file(path)
        os.close(fd)
        modes[path.relative_to(root).as_posix()] = stat.S_IMODE(mode)
    return modes


def validate_regular_tree(root: Path) -> None:
    for path in root.rglob("*"):
        if path.is_symlink():
            raise SystemExit(f"bundle contains a symlink: {path.relative_to(root)}")
        if not path.is_dir() and not path.is_file():
            raise SystemExit(f"bundle contains a non-regular file: {path.relative_to(root)}")


def source_checkout_dirty(root: Path) -> bool:
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise SystemExit("could not determine source checkout status") from exc
    return bool(result.stdout.strip())


def absolute_without_symlinks(path: Path, label: str) -> Path:
    absolute = Path(os.path.abspath(path))
    current = absolute
    while True:
        try:
            if stat.S_ISLNK(os.lstat(current).st_mode):
                raise SystemExit(f"{label} path contains a symlink: {current}")
        except FileNotFoundError:
            pass
        if current.parent == current:
            break
        current = current.parent
    return absolute


def runtime_version(source: Path) -> str:
    text = (source / "runtime" / "__init__.py").read_text(encoding="utf-8")
    match = VERSION_RE.search(text)
    if not match:
        raise SystemExit("could not determine the runtime version")
    return match.group(1)


def project_version(repo: Path) -> str:
    with (repo / "pyproject.toml").open("rb") as stream:
        return tomllib.load(stream)["project"]["version"]


def validate_release_identity(runtime: str, project: str, tag: str | None) -> None:
    if runtime != project:
        raise SystemExit(
            f"runtime version {runtime!r} does not match pyproject version {project!r}"
        )
    expected_tag = f"v{runtime}"
    if tag != expected_tag:
        raise SystemExit(f"expected exact release tag {expected_tag!r}, got {tag!r}")


def validate_source_bundle(repo: Path, source: Path, source_files: dict[str, str]) -> None:
    source_name = source.relative_to(repo).as_posix().rstrip("/")
    prefix = source_name + "/"
    listing = git(
        repo,
        "ls-tree",
        "-r",
        "-z",
        "--format=%(objectmode) %(objectname) %(path)",
        "HEAD",
        "--",
        source_name,
    )
    if listing is None:
        raise SystemExit("could not determine tagged source bundle")
    tracked_entries = {}
    for record in listing.split("\0"):
        if not record:
            continue
        mode, object_name, path = record.split(" ", 2)
        if path.startswith(prefix):
            tracked_entries[path[len(prefix):]] = (stat.S_IMODE(int(mode, 8)), object_name)
    tracked_files = set(tracked_entries)
    actual_files = set(source_files)
    if actual_files != tracked_files:
        raise SystemExit(
            "source bundle is not the tagged Git tree: "
            f"missing={sorted(tracked_files - actual_files)}, "
            f"extra={sorted(actual_files - tracked_files)}"
        )
    tracked_modes = {path: values[0] for path, values in tracked_entries.items()}
    actual_modes = file_modes(source)
    if actual_modes != tracked_modes:
        missing = sorted(set(tracked_modes) - set(actual_modes))
        extra = sorted(set(actual_modes) - set(tracked_modes))
        changed = sorted(
            name for name in set(actual_modes) & set(tracked_modes)
            if actual_modes[name] != tracked_modes[name]
        )
        raise SystemExit(
            "source bundle file modes differ from tagged Git tree: "
            f"missing={missing}, extra={extra}, changed={changed}"
        )
    changed = []
    for path, (_, object_name) in tracked_entries.items():
        current_object = git(repo, "hash-object", "--no-filters", "--", f"{source_name}/{path}")
        if current_object != object_name:
            changed.append(path)
    if changed:
        raise SystemExit(
            "source bundle content differs from tagged Git tree: "
            f"changed={sorted(changed)}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    source = absolute_without_symlinks(args.source, "source")
    destination = absolute_without_symlinks(args.destination, "destination")
    if not source.is_dir() or not destination.is_dir():
        raise SystemExit("source and destination must be existing directories")

    validate_regular_tree(source)
    validate_regular_tree(destination)
    source_files = file_digests(source)
    repo = source.parent
    validate_source_bundle(repo, source, source_files)
    destination_files = {
        name: digest
        for name, digest in file_digests(destination).items()
        if name != "BUILD_INFO.json"
    }
    if source_files != destination_files:
        missing = sorted(set(source_files) - set(destination_files))
        extra = sorted(set(destination_files) - set(source_files))
        changed = sorted(
            name for name in set(source_files) & set(destination_files)
            if source_files[name] != destination_files[name]
        )
        raise SystemExit(
            "deployment bundle differs from source: "
            f"missing={missing}, extra={extra}, changed={changed}"
        )
    source_modes = file_modes(source)
    destination_modes = {
        name: mode
        for name, mode in file_modes(destination).items()
        if name != "BUILD_INFO.json"
    }
    if source_modes != destination_modes:
        missing = sorted(set(source_modes) - set(destination_modes))
        extra = sorted(set(destination_modes) - set(source_modes))
        changed = sorted(
            name for name in set(source_modes) & set(destination_modes)
            if source_modes[name] != destination_modes[name]
        )
        raise SystemExit(
            "deployment bundle file modes differ from source: "
            f"missing={missing}, extra={extra}, changed={changed}"
        )

    version = runtime_version(source)
    tag = git(repo, "describe", "--tags", "--exact-match", "HEAD")
    validate_release_identity(version, project_version(repo), tag)
    source_commit = git(repo, "rev-parse", "HEAD")
    if not source_commit or not re.fullmatch(r"[0-9a-f]{40,64}", source_commit):
        raise SystemExit("could not determine full source commit")
    info = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "version": version,
        "source_commit": source_commit,
        "source_tag": tag,
        "source_dirty": source_checkout_dirty(repo),
        "bundle_files": source_files,
    }
    temp_path = None
    try:
        fd, temp_name = tempfile.mkstemp(prefix=".BUILD_INFO.", dir=destination)
        temp_path = Path(temp_name)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(info, indent=2, sort_keys=True) + "\n")
        os.replace(temp_path, destination / "BUILD_INFO.json")
    except OSError as exc:
        raise SystemExit("could not write build manifest") from exc
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
    print(json.dumps({key: value for key, value in info.items() if key != "bundle_files"}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
