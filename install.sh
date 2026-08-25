#!/bin/sh
# Installation utilisateur (sans root, sans pip) pour Pasteberth.
set -eu

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
BIN_DIR="${HOME}/.local/bin"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/pasteberth"

mkdir -p "$BIN_DIR"
ln -sfn "$REPO_DIR/run.sh" "$BIN_DIR/pasteberth"
chmod +x "$REPO_DIR/run.sh"

echo "Wrapper installé : $BIN_DIR/pasteberth -> $REPO_DIR/run.sh"
case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) echo "note : ajoutez $BIN_DIR à votre PATH si besoin." ;;
esac

if [ ! -f "$CONFIG_DIR/config.toml" ]; then
  mkdir -p "$CONFIG_DIR"
  cp "$REPO_DIR/config.example.toml" "$CONFIG_DIR/config.toml"
  chmod 600 "$CONFIG_DIR/config.toml"
  echo "Configuration d'exemple copiée dans $CONFIG_DIR/config.toml — adaptez vos zones."
else
  echo "Configuration existante conservée : $CONFIG_DIR/config.toml"
fi

UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
mkdir -p "$UNIT_DIR"
UNIT_PATH="$UNIT_DIR/pasteberth.service"
if [ ! -e "$UNIT_PATH" ]; then
  cp "$REPO_DIR/deploy/pasteberth.service" "$UNIT_PATH"
elif cmp -s "$REPO_DIR/deploy/pasteberth.service" "$UNIT_PATH"; then
  :
else
  cp "$REPO_DIR/deploy/pasteberth.service" "$UNIT_PATH.pasteberth-new"
  echo "note : unité existante conservée ; nouvelle version dans $UNIT_PATH.pasteberth-new"
fi
if command -v systemctl >/dev/null 2>&1; then
  if ! systemctl --user daemon-reload; then
    echo "note : daemon-reload systemd utilisateur impossible ; rechargez-le manuellement." >&2
  fi
else
  echo "note : systemctl absent ; unité copiée mais non rechargée." >&2
fi

cat <<EOF

Prochaines étapes :
  1. éditez $CONFIG_DIR/config.toml (zones = répertoires réels) ;
  2. $BIN_DIR/pasteberth passwd        # définit le mot de passe ;
  3. activez [auth] enabled = true dans la config ;
  4. systemctl --user enable --now pasteberth.service
     (ou lancez directement : pasteberth serve).
EOF
