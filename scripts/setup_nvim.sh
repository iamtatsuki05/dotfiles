#!/usr/bin/zsh

set -euo pipefail

# =============================================================================
# Setup Neovim with vim-plug, color scheme, and plugins
# =============================================================================

readonly SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
readonly REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
readonly CONFIG_DIR="$REPO_ROOT/config"

# Paths
readonly VIM_AUTOLOAD="$HOME/.vim/autoload"
readonly NVIM_DIR="$HOME/.config/nvim"
readonly NVIM_BUNDLE_DIR="$NVIM_DIR/bundle"
readonly NVIM_COLORS_DIR="$NVIM_DIR/colors"

# Theme configuration
readonly THEME_REPO="https://github.com/tomasiser/vim-code-dark.git"
readonly THEME_DIR="$NVIM_BUNDLE_DIR/vim-code-dark.git"
readonly THEME_FILE="$THEME_DIR/colors/codedark.vim"

# Config files
readonly INIT_VIM_SOURCE="$CONFIG_DIR/init.vim"
readonly INIT_VIM_TARGET="$NVIM_DIR/init.vim"

# URLs
readonly VIM_PLUG_URL="https://raw.githubusercontent.com/junegunn/vim-plug/master/plug.vim"

# -----------------------------------------------------------------------------
# Setup steps
# -----------------------------------------------------------------------------
install_vim_plug() {
  echo "Installing vim-plug..."
  curl -fLo "$VIM_AUTOLOAD/plug.vim" --create-dirs "$VIM_PLUG_URL"
  echo "vim-plug installed to $VIM_AUTOLOAD/plug.vim"
}

install_color_scheme() {
  echo "Setting up color scheme..."

  if [[ -d "$THEME_DIR" ]]; then
    echo "Color scheme already cloned"
  else
    mkdir -p "$NVIM_BUNDLE_DIR"
    git clone "$THEME_REPO" "$THEME_DIR"
    echo "Color scheme cloned to $THEME_DIR"
  fi

  mkdir -p "$NVIM_COLORS_DIR"
  ln -sf "$THEME_FILE" "$NVIM_COLORS_DIR/codedark.vim"
  echo "Color scheme linked to $NVIM_COLORS_DIR"
}

copy_init_vim() {
  echo "Copying init.vim..."
  mkdir -p "$NVIM_DIR"
  cp "$INIT_VIM_SOURCE" "$INIT_VIM_TARGET"
  echo "init.vim copied to $INIT_VIM_TARGET"
}

install_plugins() {
  echo "Installing Neovim plugins..."

  if ! command -v nvim >/dev/null 2>&1; then
    echo "WARNING: Neovim is not installed" >&2
    echo "Install Neovim and run this script again to complete plugin setup" >&2
    return 1
  fi

  nvim -c 'PlugInstall' -c 'qa'
  echo "Plugins installed successfully"
}

show_optional_setup() {
  cat <<'EOF'

=============================================================================
Optional: GitHub Copilot Setup
=============================================================================
To install GitHub Copilot, run:

# Clone the Copilot plugin
git clone https://github.com/github/copilot.vim \
  ~/.config/nvim/pack/github/start/copilot.vim

# Setup Copilot in Neovim
nvim -c 'Copilot setup'
=============================================================================
EOF
}

# -----------------------------------------------------------------------------
# Main execution
# -----------------------------------------------------------------------------
main() {
  install_vim_plug
  install_color_scheme
  copy_init_vim
  install_plugins || true  # Continue even if nvim is not installed
  show_optional_setup

  echo "Neovim setup complete"
}

main "$@"
