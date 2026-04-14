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

install_template_if_missing() {
  local app_name="$1"
  local source_file="$2"
  local target_file="$3"
  local target_dir="${target_file%/*}"

  echo "Preparing $app_name template..."
  mkdir -p "$target_dir"

  if [[ -e "$target_file" ]]; then
    echo "  skip: $target_file already exists"
    return 0
  fi

  cp "$source_file" "$target_file"
  chmod 600 "$target_file"
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
    "Ghostty" \
    "$CONFIG_DIR/ghostty/config" \
    "$XDG_CONFIG_HOME/ghostty/config"

  install_config \
    "mise" \
    "$CONFIG_DIR/mise-config.toml" \
    "$XDG_CONFIG_HOME/mise/config.toml"

  install_template_if_missing \
    "shell secrets" \
    "$CONFIG_DIR/shell/secrets.env.example" \
    "$XDG_CONFIG_HOME/shell/secrets.env"

  # claude/codex/gemini の settings.json 等は sync.sh のシンボリックリンクで管理。
  # codex_hooks の feature flag のみセットアップ時に設定する。
  setup_codex_feature_flag

  echo "All configs installed successfully"
}

setup_codex_feature_flag() {
  local config_file="$HOME/.codex/config.toml"

  if [[ -f "$config_file" ]] && grep -q "codex_hooks" "$config_file" 2>/dev/null; then
    echo "  skip: codex_hooks already in $config_file"
    return 0
  fi

  if [[ -f "$config_file" ]] && grep -q "^\[features\]" "$config_file" 2>/dev/null; then
    local tmp
    tmp="$(mktemp)"
    awk '/^\[features\]/{print; print "codex_hooks = true"; next}1' "$config_file" > "$tmp" && mv "$tmp" "$config_file"
  else
    printf '\n[features]\ncodex_hooks = true\n' >> "$config_file"
  fi
  echo "  codex_hooks = true -> $config_file"
}

main "$@"
