#!/usr/bin/zsh

set -euo pipefail

readonly SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DEFAULT_REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

REPO_ROOT="$DEFAULT_REPO_ROOT"
APPLY=0

usage() {
  cat <<EOF
Usage:
  zsh scripts/migrate_to_chezmoi.sh [--dry-run]
  zsh scripts/migrate_to_chezmoi.sh --apply

Options:
  --apply            Write .chezmoiroot and home/ source state files.
  --dry-run          Show planned changes without writing files. This is the default.
  --repo-root PATH   Override repository root. Intended for tests.
  -h, --help         Show this help.
EOF
}

parse_args() {
  while (($#)); do
    case "$1" in
      --apply)
        APPLY=1
        ;;
      --dry-run)
        APPLY=0
        ;;
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
}

log_action() {
  local action="$1"
  local relative_path="$2"

  if (( APPLY )); then
    echo "$action $relative_path"
  else
    echo "DRY-RUN: $action $relative_path"
  fi
}

ensure_parent_dir() {
  local target_path="$1"
  local parent="${target_path:h}"

  if (( APPLY )); then
    mkdir -p "$parent"
  fi
}

write_file() {
  local relative_path="$1"
  local content="$2"
  local target="$REPO_ROOT/$relative_path"

  log_action "write" "$relative_path"
  if (( APPLY )); then
    ensure_parent_dir "$target"
    print -r -- "$content" > "$target"
  fi
}

copy_file() {
  local source_relative_path="$1"
  local target_relative_path="$2"
  local source="$REPO_ROOT/$source_relative_path"
  local target="$REPO_ROOT/$target_relative_path"

  if [[ ! -f "$source" ]]; then
    echo "ERROR: required source file not found: $source_relative_path" >&2
    return 1
  fi

  log_action "copy $source_relative_path ->" "$target_relative_path"
  if (( APPLY )); then
    ensure_parent_dir "$target"
    cp "$source" "$target"
  fi
}

generate_chezmoi_source_state() {
  local brewfile_template
  local mise_template

  brewfile_template='{{- if eq (env "DOTFILES_PROFILE") "cli" -}}
{{ include ".chezmoitemplates/Brewfile.cli" -}}
{{- else if eq .chezmoi.os "darwin" -}}
{{ include ".chezmoitemplates/Brewfile" -}}
{{- else -}}
{{ include ".chezmoitemplates/Brewfile.cli" -}}
{{- end -}}'

  mise_template='{{- $repoRoot := .chezmoi.sourceDir -}}
{{- if ne (env "DOTFILES_REPO_ROOT") "" -}}
{{- $repoRoot = env "DOTFILES_REPO_ROOT" -}}
{{- end -}}
{{ include ".chezmoitemplates/mise-config.toml" | replace "__DOTFILES_REPO_ROOT__" $repoRoot }}'

  write_file ".chezmoiroot" "home"
  copy_file "dotfiles/.zshrc" "home/dot_zshrc"
  copy_file "dotfiles/.tmux.conf" "home/dot_tmux.conf"
  copy_file "dotfiles/.Brewfile" "home/.chezmoitemplates/Brewfile"
  copy_file "dotfiles/.Brewfile.cli" "home/.chezmoitemplates/Brewfile.cli"
  copy_file "config/mise-config.toml" "home/.chezmoitemplates/mise-config.toml"
  write_file "home/dot_Brewfile.tmpl" "$brewfile_template"
  copy_file "config/alacritty.toml" "home/dot_config/alacritty/alacritty.toml"
  copy_file "config/ghostty/config" "home/dot_config/ghostty/config"
  write_file "home/dot_config/mise/private_config.toml.tmpl" "$mise_template"
  copy_file "config/init.vim" "home/dot_config/nvim/init.vim"
  copy_file "config/shell/secrets.env.example" "home/dot_config/shell/create_private_secrets.env"
}

main() {
  parse_args "$@"

  if (( APPLY )); then
    echo "Generating chezmoi source state in $REPO_ROOT"
  else
    echo "DRY-RUN: chezmoi source state migration plan for $REPO_ROOT"
  fi

  generate_chezmoi_source_state
}

main "$@"
