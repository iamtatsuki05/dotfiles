#!/usr/bin/zsh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CONFIG_DIR="$REPO_ROOT/config"

VIM_AUTOLOAD="$HOME/.vim/autoload"
NVIM_DIR="$HOME/.config/nvim"
NVIM_BUNDLE_DIR="$NVIM_DIR/bundle"
THEME_DIR="$NVIM_BUNDLE_DIR/vim-code-dark.git"
NVIM_COLORS_DIR="$NVIM_DIR/colors"
INIT_VIM_SOURCE="$CONFIG_DIR/init.vim"
INIT_VIM_TARGET="$NVIM_DIR/init.vim"

# Install vim-plug for both vim and neovim
curl -fLo "$VIM_AUTOLOAD/plug.vim" --create-dirs \
    https://raw.githubusercontent.com/junegunn/vim-plug/master/plug.vim

# Ensure color scheme repo is present only once
if [[ ! -d "$THEME_DIR" ]]; then
    mkdir -p "$NVIM_BUNDLE_DIR"
    git clone https://github.com/tomasiser/vim-code-dark.git "$THEME_DIR"
fi

mkdir -p "$NVIM_COLORS_DIR"
ln -sf "$THEME_DIR/colors/codedark.vim" "$NVIM_COLORS_DIR/codedark.vim"

# Copy init.vim from repository config
mkdir -p "$NVIM_DIR"
cp "$INIT_VIM_SOURCE" "$INIT_VIM_TARGET"

# Install Vim plugins automatically if nvim is available
if command -v nvim >/dev/null 2>&1; then
    nvim -c 'PlugInstall' -c 'qa'
else
    echo "[setup_nvim] Neovim is not installed. Install it and rerun scripts/setup_nvim.sh to finish plugin setup." >&2
fi

cat <<'EOF'
Optional: Install GitHub Copilot
Run the following commands:
# terminal
git clone https://github.com/github/copilot.vim \
   ~/.config/nvim/pack/github/start/copilot.vim
# neovim
nvim -c 'Copilot setup'
EOF
