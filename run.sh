#!/bin/sh
# Wrapper d'exécution : permet `pasteberth` sans pip ni installation Python.
# Installé (symlink) dans ~/.local/bin par install.sh ; readlink -f résout
# le symlink vers ce fichier, donc le répertoire calculé est la racine du dépôt.
REPO_DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
export PYTHONPATH="$REPO_DIR${PYTHONPATH:+:$PYTHONPATH}"
exec python3 -m pasteberth "$@"
