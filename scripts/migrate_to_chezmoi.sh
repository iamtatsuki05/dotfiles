#!/usr/bin/zsh

set -euo pipefail

readonly SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DEFAULT_REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

REPO_ROOT="$DEFAULT_REPO_ROOT"
APPLY=0
PREFER_HOME_ZSHRC=0

usage() {
  cat <<EOF
Usage:
  zsh scripts/migrate_to_chezmoi.sh [--dry-run]
  zsh scripts/migrate_to_chezmoi.sh --apply

Options:
  --apply            Write .chezmoiroot and home/ source state files.
  --dry-run          Show planned changes without writing files. This is the default.
  --prefer-home-zshrc
                     Use ~/.zshrc even when dotfiles/.zshrc exists.
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
      --prefer-home-zshrc)
        PREFER_HOME_ZSHRC=1
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

copy_file_with_home_fallback() {
  local source_relative_path="$1"
  local home_relative_path="$2"
  local target_relative_path="$3"
  local source="$REPO_ROOT/$source_relative_path"
  local home_source="$HOME/$home_relative_path"
  local target="$REPO_ROOT/$target_relative_path"

  if [[ -f "$source" ]]; then
    copy_file "$source_relative_path" "$target_relative_path"
    return
  fi

  if [[ ! -f "$home_source" ]]; then
    echo "ERROR: required source file not found: $source_relative_path or ~/$home_relative_path" >&2
    return 1
  fi

  log_action "copy ~/$home_relative_path ->" "$target_relative_path"
  if (( APPLY )); then
    ensure_parent_dir "$target"
    cp "$home_source" "$target"
  fi
}

copy_zshrc() {
  if (( PREFER_HOME_ZSHRC )); then
    copy_file_with_home_fallback "__missing_dotfiles_zshrc__" ".zshrc" "home/dot_zshrc"
    return
  fi

  copy_file_with_home_fallback "dotfiles/.zshrc" ".zshrc" "home/dot_zshrc"
}

remove_stale_generated_paths() {
  if (( ! APPLY )); then
    return
  fi

  rm -rf "$REPO_ROOT/home/dot_config"
  rm -f \
    "$REPO_ROOT/home/dot_Brewfile.tmpl" \
    "$REPO_ROOT/home/.chezmoitemplates/Brewfile" \
    "$REPO_ROOT/home/.chezmoitemplates/Brewfile.cli"
}

generate_chezmoi_source_state() {
  local mise_template
  local bashrc_template
  local bash_profile_template

  mise_template='{{- $repoRoot := .chezmoi.sourceDir -}}
{{- if ne (env "DOTFILES_REPO_ROOT") "" -}}
{{- $repoRoot = env "DOTFILES_REPO_ROOT" -}}
{{- end -}}
{{ include ".chezmoitemplates/mise-config.toml" | replace "__DOTFILES_REPO_ROOT__" $repoRoot }}'

  bashrc_template='{{- $repoRoot := .chezmoi.sourceDir -}}
{{- if ne (env "DOTFILES_REPO_ROOT") "" -}}
{{- $repoRoot = env "DOTFILES_REPO_ROOT" -}}
{{- end -}}
{{ include ".chezmoitemplates/bashrc" | replace "__DOTFILES_REPO_ROOT__" $repoRoot }}'

  bash_profile_template='{{ include ".chezmoitemplates/bash_profile" }}'

  remove_stale_generated_paths
  write_file ".chezmoiroot" "home"
  copy_zshrc
  copy_file "config/shell/bashrc.tmpl" "home/.chezmoitemplates/bashrc"
  copy_file "config/shell/bash_profile.tmpl" "home/.chezmoitemplates/bash_profile"
  write_file "home/dot_bashrc.tmpl" "$bashrc_template"
  write_file "home/dot_bash_profile.tmpl" "$bash_profile_template"
  copy_file "dotfiles/.tmux.conf" "home/dot_tmux.conf"
  copy_file "config/mise/config.toml" "home/.chezmoitemplates/mise-config.toml"
  copy_file "config/alacritty/alacritty.toml" "home/private_dot_config/alacritty/alacritty.toml"
  copy_file "config/ghostty/config" "home/private_dot_config/ghostty/config"
  copy_file "config/nix/nix.conf" "home/private_dot_config/nix/nix.conf"
  write_file "home/private_dot_config/mise/private_config.toml.tmpl" "$mise_template"
  copy_file "config/nvim/init.vim" "home/private_dot_config/nvim/init.vim"
  copy_file "config/shell/secrets.env.example" "home/private_dot_config/shell/create_private_secrets.env"
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
