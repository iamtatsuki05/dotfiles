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

  # claude/codex/gemini の settings.json 等は sync.sh のシンボリックリンクで管理。
  # codex は動的更新される config.toml を持つため、base 設定のマージとfeature flagのみここで処理する。
  setup_codex_config
  setup_codex_feature_flag
  setup_gemini_env

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

setup_codex_config() {
  local base_file="$CONFIG_DIR/codex/config.toml.base"
  local config_file="$HOME/.codex/config.toml"

  if [[ ! -f "$base_file" ]]; then
    echo "  skip: $base_file not found"
    return 0
  fi

  if [[ ! -f "$config_file" ]]; then
    # 新規マシン: base をそのままコピー
    mkdir -p "$(dirname "$config_file")"
    cp "$base_file" "$config_file"
    chmod 600 "$config_file"
    echo "  $base_file -> $config_file (new)"
    return 0
  fi

  # 既存 config.toml: http_headers / env_http_headers を bearer_token_env_var に移行
  if grep -qE "(http_headers|env_http_headers)" "$config_file" 2>/dev/null && ! grep -q "bearer_token_env_var" "$config_file" 2>/dev/null; then
    python3 - "$config_file" << 'PYEOF'
import re, sys

path = sys.argv[1]
with open(path) as f:
    content = f.read()

# http_headers または env_http_headers の Authorization 行を bearer_token_env_var に置き換え
content = re.sub(
    r'^(env_)?http_headers\s*=\s*\{[^\n]*"Authorization"[^\n]*\}',
    'bearer_token_env_var = "DEVIN_API_KEY"',
    content,
    flags=re.MULTILINE
)

with open(path, 'w') as f:
    f.write(content)
PYEOF
    echo "  http_headers/env_http_headers -> bearer_token_env_var in $config_file"
  else
    echo "  skip: $config_file already uses bearer_token_env_var or has no http_headers"
  fi

  python3 - "$config_file" << 'PYEOF'
import re, sys

path = sys.argv[1]
with open(path) as f:
    content = f.read()

original = content

if re.search(r'^sandbox_mode\s*=.*$', content, flags=re.MULTILINE):
    content = re.sub(
        r'^sandbox_mode\s*=.*$',
        'sandbox_mode = "workspace-write"',
        content,
        count=1,
        flags=re.MULTILINE,
    )
else:
    content = 'sandbox_mode = "workspace-write"\n' + content

section_pattern = r'(?ms)^\[sandbox_workspace_write\]\n.*?(?=^\[|\Z)'
match = re.search(section_pattern, content)
if match:
    section = match.group(0)
    if re.search(r'^network_access\s*=.*$', section, flags=re.MULTILINE):
        updated = re.sub(
            r'^network_access\s*=.*$',
            'network_access = true',
            section,
            count=1,
            flags=re.MULTILINE,
        )
    else:
        updated = section.rstrip() + '\nnetwork_access = true\n'
    content = content[:match.start()] + updated + content[match.end():]
else:
    if not content.endswith('\n'):
        content += '\n'
    content += '\n[sandbox_workspace_write]\nnetwork_access = true\n'

if content != original:
    with open(path, 'w') as f:
        f.write(content)
PYEOF
  echo "  ensured workspace-write sandbox defaults in $config_file"
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

setup_gemini_env() {
  local secrets_file="$CONFIG_DIR/shell/secrets.env"
  local gemini_env_file="$HOME/.gemini/.env"

  if [[ ! -f "$secrets_file" ]]; then
    echo "  skip: $secrets_file not found, skipping ~/.gemini/.env setup"
    return 0
  fi

  mkdir -p "$(dirname "$gemini_env_file")"

  # secrets.env から Gemini CLI が必要な変数を抽出して ~/.gemini/.env に書き出す
  # (export プレフィックスを除去し、KEY=VALUE 形式で出力)
  local vars=("DEVIN_API_KEY")
  local tmp
  tmp="$(mktemp)"

  for var in "${vars[@]}"; do
    local line
    line=$(grep -E "^(export )?${var}=" "$secrets_file" 2>/dev/null | tail -1 | sed 's/^export //')
    if [[ -n "$line" ]]; then
      echo "$line" >> "$tmp"
    fi
  done

  if [[ ! -s "$tmp" ]]; then
    rm -f "$tmp"
    echo "  skip: no relevant vars (${vars[*]}) found in $secrets_file"
    return 0
  fi

  mv "$tmp" "$gemini_env_file"
  chmod 600 "$gemini_env_file"
  echo "  updated $gemini_env_file (${vars[*]})"
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
