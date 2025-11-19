#!/usr/bin/zsh

set -euo pipefail

# =============================================================================
# Copy application configuration files to their appropriate locations
# =============================================================================

readonly SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
readonly REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
readonly CONFIG_DIR="$REPO_ROOT/config"
readonly XDG_CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"

# -----------------------------------------------------------------------------
# Helper function
# -----------------------------------------------------------------------------
install_config() {
  local app_name="$1"
  local source_file="$2"
  local target_file="$3"
  local target_dir="${target_file%/*}"

  echo "Installing $app_name config..."
  mkdir -p "$target_dir"
  cp "$source_file" "$target_file"
  echo "  $source_file -> $target_file"
}

# -----------------------------------------------------------------------------
# Main execution
# -----------------------------------------------------------------------------
main() {
  install_config \
    "Alacritty" \
    "$CONFIG_DIR/alacritty.toml" \
    "$XDG_CONFIG_HOME/alacritty/alacritty.toml"

  install_config \
    "mise" \
    "$CONFIG_DIR/mise-config.toml" \
    "$XDG_CONFIG_HOME/mise/config.toml"

  echo "All configs installed successfully"
}

main "$@"
