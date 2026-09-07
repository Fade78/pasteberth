#!/usr/bin/env python3
"""Record the exact source bundle used by a Pasteberth deployment."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import stat
import subprocess
import tomllib
from datetime import datetime, timezone
from pathlib import Path


VERSION_RE = re.compile(r'^__version__\s*=\s*["\']([^"\']+)["\']\s*$', re.MULTILINE)
NOFOLLOW_FLAGS = getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
DIRECTORY_FLAGS = getattr(os, "O_DIRECTORY", 0)


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


def git_required(root: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise SystemExit(f"could not run git {' '.join(args)}") from exc
    return result.stdout


def _open_directory(path: Path) -> int:
    try:
        fd = os.open(path, os.O_RDONLY | DIRECTORY_FLAGS | NOFOLLOW_FLAGS)
    except OSError as exc:
        raise SystemExit(f"could not open bundle directory: {path}") from exc
    if not stat.S_ISDIR(os.fstat(fd).st_mode):
        os.close(fd)
        raise SystemExit(f"bundle root is not a directory: {path}")
    return fd


def _bundle_metadata(
    root: Path,
    object_format: str = "sha1",
) -> tuple[dict[str, str], dict[str, int], dict[str, str]]:
    if object_format not in ("sha1", "sha256"):
        raise SystemExit(f"unsupported Git object format: {object_format}")
    digests = {}
    modes = {}
    objects = {}
    root_fd = _open_directory(root)

    def walk(directory_fd: int, relative: Path) -> None:
        entries = []
        try:
            with os.scandir(directory_fd) as scanner:
                entries = sorted(scanner, key=lambda entry: entry.name)
            for entry in entries:
                entry_relative = relative / entry.name
                try:
                    entry_mode = entry.stat(follow_symlinks=False).st_mode
                except OSError as exc:
                    raise SystemExit(f"could not inspect bundle file: {entry_relative}") from exc
                if stat.S_ISLNK(entry_mode):
                    raise SystemExit(f"bundle contains a symlink: {entry_relative}")
                if stat.S_ISDIR(entry_mode):
                    try:
                        child_fd = os.open(
                            entry.name,
                            os.O_RDONLY | DIRECTORY_FLAGS | NOFOLLOW_FLAGS,
                            dir_fd=directory_fd,
                        )
                    except OSError as exc:
                        raise SystemExit(f"could not open bundle directory: {entry_relative}") from exc
                    try:
                        walk(child_fd, entry_relative)
                    finally:
                        os.close(child_fd)
                    continue
                if not stat.S_ISREG(entry_mode):
                    raise SystemExit(f"bundle contains a non-regular file: {entry_relative}")
                if "__pycache__" in entry_relative.parts or entry_relative.suffix == ".pyc":
                    continue
                try:
                    file_fd = os.open(
                        entry.name,
                        os.O_RDONLY | NOFOLLOW_FLAGS,
                        dir_fd=directory_fd,
                    )
                    actual_mode = os.fstat(file_fd).st_mode
                except OSError as exc:
                    raise SystemExit(f"could not open bundle file: {entry_relative}") from exc
                if not stat.S_ISREG(actual_mode):
                    os.close(file_fd)
                    raise SystemExit(f"bundle file is not regular: {entry_relative}")
                digest = hashlib.sha256()
                blob_digest = hashlib.new(object_format, usedforsecurity=False)
                blob_digest.update(f"blob {os.fstat(file_fd).st_size}\0".encode())
                try:
                    with os.fdopen(file_fd, "rb") as stream:
                        file_fd = -1
                        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                            digest.update(chunk)
                            blob_digest.update(chunk)
                finally:
                    if file_fd >= 0:
                        os.close(file_fd)
                name = entry_relative.as_posix()
                digests[name] = digest.hexdigest()
                modes[name] = stat.S_IMODE(actual_mode)
                objects[name] = blob_digest.hexdigest()
        except OSError as exc:
            raise SystemExit(f"could not inspect bundle directory: {relative}") from exc

    try:
        walk(root_fd, Path())
    finally:
        os.close(root_fd)
    return digests, modes, objects


def file_digests(root: Path) -> dict[str, str]:
    return _bundle_metadata(root)[0]


def file_modes(root: Path) -> dict[str, int]:
    return _bundle_metadata(root)[1]


def file_objects(root: Path, object_format: str = "sha1") -> dict[str, str]:
    return _bundle_metadata(root, object_format)[2]


def _git_object_for_file(
    root: Path,
    relative: str,
    object_format: str,
) -> tuple[int, str] | None:
    directory_fds = [_open_directory(root)]
    file_fd = -1
    try:
        current_fd = directory_fds[0]
        components = Path(relative).parts
        for component in components[:-1]:
            current_fd = os.open(
                component,
                os.O_RDONLY | DIRECTORY_FLAGS | NOFOLLOW_FLAGS,
                dir_fd=current_fd,
            )
            directory_fds.append(current_fd)
        file_fd = os.open(
            components[-1],
            os.O_RDONLY | NOFOLLOW_FLAGS,
            dir_fd=current_fd,
        )
        file_mode = os.fstat(file_fd).st_mode
        if not stat.S_ISREG(file_mode):
            return None
        blob_digest = hashlib.new(object_format, usedforsecurity=False)
        blob_digest.update(f"blob {os.fstat(file_fd).st_size}\0".encode())
        with os.fdopen(file_fd, "rb") as stream:
            file_fd = -1
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                blob_digest.update(chunk)
        return file_mode, blob_digest.hexdigest()
    except OSError:
        return None
    finally:
        if file_fd >= 0:
            os.close(file_fd)
        for directory_fd in reversed(directory_fds):
            os.close(directory_fd)


def validate_regular_tree(root: Path) -> None:
    _bundle_metadata(root)


def source_checkout_dirty(root: Path) -> bool:
    try:
        object_format = git_required(root, "rev-parse", "--show-object-format").strip()
        if object_format not in ("sha1", "sha256"):
            raise SystemExit(f"unsupported Git object format: {object_format}")
        entries = tagged_tree_entries(root)
        for path, (mode, object_name) in entries.items():
            if mode not in (0o100644, 0o100755):
                return True
            working = _git_object_for_file(root, path, object_format)
            if working is None:
                return True
            working_mode, current_object = working
            if stat.S_IMODE(working_mode) != stat.S_IMODE(mode):
                return True
            if current_object != object_name:
                return True
        if git_required(root, "diff", "--cached", "--name-only", "HEAD").strip():
            return True
        return bool(git_required(root, "ls-files", "--others", "--exclude-standard", "-z"))
    except SystemExit as exc:
        raise SystemExit("could not determine source checkout status") from exc


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


def reject_overlapping_paths(source: Path, destination: Path) -> None:
    if source == destination or source in destination.parents or destination in source.parents:
        raise SystemExit("source and destination must be separate, non-overlapping directories")


def tagged_tree_entries(repo: Path, pathspec: str | None = None) -> dict[str, tuple[int, str]]:
    args = [
        "ls-tree",
        "-r",
        "-z",
        "--format=%(objectmode) %(objectname) %(path)",
        "HEAD",
    ]
    if pathspec is not None:
        args.extend(["--", pathspec])
    listing = git_required(repo, *args)
    entries = {}
    for record in listing.split("\0"):
        if not record:
            continue
        mode, object_name, path = record.split(" ", 2)
        entries[path] = (int(mode, 8), object_name)
    return entries


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
    object_format = git_required(repo, "rev-parse", "--show-object-format").strip()
    tagged_entries = tagged_tree_entries(repo, source_name)
    tracked_entries = {
        path[len(prefix):]: values
        for path, values in tagged_entries.items()
        if path.startswith(prefix)
    }
    tracked_files = set(tracked_entries)
    actual_files = set(source_files)
    if actual_files != tracked_files:
        raise SystemExit(
            "source bundle is not the tagged Git tree: "
            f"missing={sorted(tracked_files - actual_files)}, "
            f"extra={sorted(actual_files - tracked_files)}"
        )
    unsupported = sorted(
        path for path, values in tracked_entries.items()
        if values[0] not in (0o100644, 0o100755)
    )
    if unsupported:
        raise SystemExit(
            "tagged source tree contains unsupported file modes: "
            f"paths={unsupported}"
        )
    tracked_modes = {path: stat.S_IMODE(values[0]) for path, values in tracked_entries.items()}
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
    actual_objects = file_objects(source, object_format)
    changed = []
    for path, (_, object_name) in tracked_entries.items():
        if actual_objects.get(path) != object_name:
            changed.append(path)
    if changed:
        raise SystemExit(
            "source bundle content differs from tagged Git tree: "
            f"changed={sorted(changed)}"
        )


def write_manifest(destination: Path, info: dict) -> None:
    destination_fd = _open_directory(destination)
    temp_fd = -1
    temp_name = None
    try:
        for _ in range(10):
            candidate = f".BUILD_INFO.{secrets.token_hex(12)}"
            try:
                temp_fd = os.open(
                    candidate,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | NOFOLLOW_FLAGS,
                    0o600,
                    dir_fd=destination_fd,
                )
            except FileExistsError:
                continue
            temp_name = candidate
            break
        if temp_fd < 0 or temp_name is None:
            raise OSError("could not create a temporary build manifest")
        with os.fdopen(temp_fd, "w", encoding="utf-8") as stream:
            temp_fd = -1
            stream.write(json.dumps(info, indent=2, sort_keys=True) + "\n")
        os.replace(
            temp_name,
            "BUILD_INFO.json",
            src_dir_fd=destination_fd,
            dst_dir_fd=destination_fd,
        )
        temp_name = None
    except OSError as exc:
        raise SystemExit("could not write build manifest") from exc
    finally:
        if temp_fd >= 0:
            os.close(temp_fd)
        if temp_name is not None:
            try:
                os.unlink(temp_name, dir_fd=destination_fd)
            except FileNotFoundError:
                pass
        os.close(destination_fd)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    source = absolute_without_symlinks(args.source, "source")
    destination = absolute_without_symlinks(args.destination, "destination")
    reject_overlapping_paths(source, destination)
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
    if not source_commit or not re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", source_commit):
        raise SystemExit("could not determine full source commit")
    info = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "version": version,
        "source_commit": source_commit,
        "source_tag": tag,
        "source_dirty": source_checkout_dirty(repo),
        "bundle_files": source_files,
    }
    write_manifest(destination, info)
    print(json.dumps({key: value for key, value in info.items() if key != "bundle_files"}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
