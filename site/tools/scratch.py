"""Shared scratch location for site QA; generated assets and reports stay in site/."""
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def scratch_directory() -> Path:
    work = REPO / 'work/tmp/site'
    # Reject symlinks before creating anything, including dangling parent links.
    for path in (REPO / 'work', REPO / 'work/tmp', work):
        if path.is_symlink() or (path.exists() and not path.is_dir()):
            raise ValueError(f'Scratch path must be a real directory: {path}')
    work.mkdir(parents=True, exist_ok=True)
    return work


if __name__ == '__main__':
    scratch_directory()
