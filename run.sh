#!/bin/sh
# Wrapper de compatibilité : l'exécutable public est désormais bin/pasteberth.
REPO_DIR="$(CDPATH= cd -- "$(dirname -- "$(readlink -f "$0")")" && pwd)"
export PASTEBERTH_REPO_ROOT="$REPO_DIR"
export PYTHONPATH="$REPO_DIR${PYTHONPATH:+:$PYTHONPATH}"
exec python3 -m pasteberth "$@"
