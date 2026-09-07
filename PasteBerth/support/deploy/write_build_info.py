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


def file_digests(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
        and "__pycache__" not in path.parts
        and path.suffix != ".pyc"
    }


def file_modes(root: Path) -> dict[str, int]:
    return {
        path.relative_to(root).as_posix(): stat.S_IMODE(path.stat().st_mode)
        for path in sorted(root.rglob("*"))
        if path.is_file()
        and "__pycache__" not in path.parts
        and path.suffix != ".pyc"
    }


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
    listing = git(repo, "ls-tree", "-r", "--name-only", "HEAD", "--", source_name)
    prefix = source_name + "/"
    tracked_files = {
        path[len(prefix):]
        for path in (listing or "").splitlines()
        if path.startswith(prefix)
    }
    actual_files = set(source_files)
    if actual_files != tracked_files:
        raise SystemExit(
            "source bundle is not the tagged Git tree: "
            f"missing={sorted(tracked_files - actual_files)}, "
            f"extra={sorted(actual_files - tracked_files)}"
        )
    mode_listing = git(
        repo,
        "ls-tree",
        "-r",
        "--format=%(objectmode) %(path)",
        "HEAD",
        "--",
        source_name,
    )
    if mode_listing is None:
        raise SystemExit("could not determine tagged source file modes")
    tracked_modes = {}
    for line in mode_listing.splitlines():
        mode, path = line.split(" ", 1)
        if path.startswith(prefix):
            tracked_modes[path[len(prefix):]] = stat.S_IMODE(int(mode, 8))
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
    result = subprocess.run(
        ["git", "diff", "--quiet", "HEAD", "--", source_name],
        cwd=repo,
        check=False,
    )
    if result.returncode != 0:
        raise SystemExit("source bundle has tracked changes after the release tag")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    source = args.source.resolve()
    destination = args.destination.resolve()
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
