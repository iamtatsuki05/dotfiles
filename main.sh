#!/usr/bin/zsh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$SCRIPT_DIR"
SCRIPTS_DIR="$REPO_ROOT/scripts"
DOTFILES_DIR="$REPO_ROOT/dotfiles"

# copy dotfiles (use dotfiles/. to include hidden files and avoid zsh glob "no matches" errors)
cp -r "$DOTFILES_DIR"/. ~/

# Install Homebrew
sh "$SCRIPTS_DIR/brew_install.sh"

# Setup default settings
sh "$SCRIPTS_DIR/default_setup.sh"

# Setup config settings
sh "$SCRIPTS_DIR/setup_config.sh"

# Setup Neovim
sh "$SCRIPTS_DIR/setup_nvim.sh"

echo "Please restart your terminal."
