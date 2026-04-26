#!/usr/bin/zsh

set -euo pipefail

# =============================================================================
# Copy application configuration files to their appropriate locations
# =============================================================================

readonly SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DEFAULT_REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$DEFAULT_REPO_ROOT"
CONFIG_DIR="$REPO_ROOT/config"
readonly XDG_CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"

usage() {
  cat <<EOF
Usage:
  zsh scripts/setup_config.sh [--repo-root PATH]

Options:
  --repo-root PATH  Override repository root. Intended for tests.
  -h, --help        Show this help.
EOF
}

parse_args() {
  while (($#)); do
    case "$1" in
      --repo-root)
        shift
        if ((! $#)); then
          echo "ERROR: --repo-root requires a value" >&2
          return 1
        fi
        REPO_ROOT="$1"
        ;;
      --repo-root=*)
        REPO_ROOT="${1#--repo-root=}"
        ;;
      -h|--help)
        usage
        exit 0
        ;;
      *)
        echo "ERROR: unknown argument: $1" >&2
        usage >&2
        return 1
        ;;
    esac
    shift
  done

  REPO_ROOT="$(cd "$REPO_ROOT" && pwd)"
  CONFIG_DIR="$REPO_ROOT/config"
}

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

render_repo_root_template() {
  local source_file="$1"
  local target_file="$2"
  local app_name="$3"
  local target_dir="${target_file%/*}"
  local tmp
  local line

  echo "Installing $app_name..."
  mkdir -p "$target_dir"
  tmp="$(mktemp)"

  while IFS= read -r line || [[ -n "$line" ]]; do
    print -r -- "${line//__DOTFILES_REPO_ROOT__/$REPO_ROOT}"
  done < "$source_file" > "$tmp"

  mv "$tmp" "$target_file"
  echo "  $source_file -> $target_file"
}

# -----------------------------------------------------------------------------
# Main execution
# -----------------------------------------------------------------------------
main() {
  parse_args "$@"

  install_config \
    "Alacritty" \
    "$CONFIG_DIR/alacritty/alacritty.toml" \
    "$XDG_CONFIG_HOME/alacritty/alacritty.toml"

  install_config \
    "Ghostty" \
    "$CONFIG_DIR/ghostty/config" \
    "$XDG_CONFIG_HOME/ghostty/config"

  install_config \
    "Nix" \
    "$CONFIG_DIR/nix/nix.conf" \
    "$XDG_CONFIG_HOME/nix/nix.conf"

  install_mise_config
  install_shell_startup_files

  # リポジトリ側の secrets.env を先にマイグレーションしてから ~/.config/ にコピーする
  migrate_secrets_env
  install_config \
    "shell secrets" \
    "$CONFIG_DIR/shell/secrets.env" \
    "$XDG_CONFIG_HOME/shell/secrets.env"
  chmod 600 "$XDG_CONFIG_HOME/shell/secrets.env"

  echo "All configs installed successfully"
}

install_mise_config() {
  render_repo_root_template \
    "$CONFIG_DIR/mise/config.toml" \
    "$XDG_CONFIG_HOME/mise/config.toml" \
    "mise config"
}

install_shell_startup_files() {
  render_repo_root_template \
    "$CONFIG_DIR/shell/dotfiles-shell-common.tmpl" \
    "$XDG_CONFIG_HOME/shell/dotfiles-shell-common.sh" \
    "shared shell config"

  render_repo_root_template \
    "$CONFIG_DIR/shell/bashrc.tmpl" \
    "$HOME/.bashrc" \
    "bashrc"

  render_repo_root_template \
    "$CONFIG_DIR/shell/bash_profile.tmpl" \
    "$HOME/.bash_profile" \
    "bash profile"
}

migrate_secrets_env() {
  local secrets_file="$CONFIG_DIR/shell/secrets.env"

  if [[ ! -f "$secrets_file" ]]; then
    return 0
  fi

  # DEVIN_BEARER_TOKEN (値に "Bearer " プレフィックスあり) → DEVIN_API_KEY (rawトークン) に移行
  if grep -qE "^(export )?DEVIN_BEARER_TOKEN=" "$secrets_file" 2>/dev/null && \
     ! grep -qE "^(export )?DEVIN_API_KEY=" "$secrets_file" 2>/dev/null; then
    python3 - "$secrets_file" << 'PYEOF'
import re, sys

path = sys.argv[1]
with open(path) as f:
    content = f.read()

def migrate_devin(m):
    prefix = m.group(1) or ''   # "export " or ""
    value  = m.group(2)         # トークン値 (Bearer xxx または xxx)
    raw    = re.sub(r'^Bearer\s+', '', value, flags=re.IGNORECASE)
    return f'{prefix}DEVIN_API_KEY={raw}'

content = re.sub(
    r'^(export )?DEVIN_BEARER_TOKEN=(.+)$',
    migrate_devin,
    content,
    flags=re.MULTILINE
)

with open(path, 'w') as f:
    f.write(content)
PYEOF
    echo "  DEVIN_BEARER_TOKEN -> DEVIN_API_KEY in $secrets_file"
  else
    echo "  skip: $secrets_file already has DEVIN_API_KEY or no DEVIN_BEARER_TOKEN"
  fi
}

main "$@"
