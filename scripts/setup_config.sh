#!/usr/bin/zsh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CONFIG_DIR="$REPO_ROOT/config"

# alacritty config
mkdir -p ~/.config/alacritty
cp "$CONFIG_DIR/alacritty.toml" ~/.config/alacritty/alacritty.toml

# mise config
mkdir -p ~/.config/mise
cp "$CONFIG_DIR/mise-config.toml" ~/.config/mise/config.toml
