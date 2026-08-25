#!/bin/sh
# Wrapper d'exécution : permet `pasteberth` sans pip ni installation Python.
# Installé (symlink) dans ~/.local/bin par install.sh.
REPO_DIR="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
export PYTHONPATH="$REPO_DIR${PYTHONPATH:+:$PYTHONPATH}"
exec python3 -m pasteberth "$@"
