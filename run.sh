#!/bin/sh
# Compatibility wrapper: the public executable is now bin/pasteberth.
REPO_DIR="$(CDPATH= cd -- "$(dirname -- "$(readlink -f "$0")")" && pwd)"
export PASTEBERTH_REPO_ROOT="$REPO_DIR"
export PYTHONPATH="$REPO_DIR"
exec python3 -P -m pasteberth "$@"
