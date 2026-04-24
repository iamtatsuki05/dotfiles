#!/usr/bin/zsh

set -euo pipefail

readonly SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
readonly REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
readonly LIB_DIR="$SCRIPT_DIR/lib"
readonly XDG_CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
readonly PROFILE_FILE="$XDG_CONFIG_HOME/dotfiles/profile"
readonly MANAGED_MARKER="# dotfiles managed git hook"

source "$LIB_DIR/setup_profile.sh"

write_profile() {
  local profile="$1"

  mkdir -p "$(dirname "$PROFILE_FILE")"
  print -r -- "$profile" > "$PROFILE_FILE"
}

hook_dir() {
  git -C "$REPO_ROOT" rev-parse --absolute-git-dir 2>/dev/null | sed 's#$#/hooks#'
}

install_hook() {
  local hook_name="$1"
  local hooks_dir="$2"
  local hook_file="$hooks_dir/$hook_name"

  if [[ -e "$hook_file" ]] && ! grep -qF "$MANAGED_MARKER" "$hook_file" 2>/dev/null; then
    echo "Skipped unmanaged existing hook: $hook_file"
    return 0
  fi

  cat > "$hook_file" <<EOF
#!/usr/bin/zsh
$MANAGED_MARKER

repo_root="$REPO_ROOT"
hook_name="$hook_name"
log_file="\${TMPDIR:-/tmp}/dotfiles-git-hooks.log"

if [[ "\$hook_name" == "post-checkout" && "\${3:-}" != "1" ]]; then
  exit 0
fi

{
  echo "===> \$(date '+%Y-%m-%d %H:%M:%S') \$hook_name"
  zsh "\$repo_root/scripts/apply_updates.sh" --from-hook
} >> "\$log_file" 2>&1 || true

exit 0
EOF

  chmod +x "$hook_file"
  echo "Installed $hook_file"
}

main() {
  dotfiles_parse_profile_args "scripts/setup_git_hooks.sh" "$@"
  local profile="$DOTFILES_PROFILE"
  local hooks_dir

  hooks_dir="$(hook_dir)" || {
    echo "Skipped: $REPO_ROOT is not a git repository"
    return 0
  }

  mkdir -p "$hooks_dir"
  write_profile "$profile"
  install_hook post-merge "$hooks_dir"
  install_hook post-rewrite "$hooks_dir"
  install_hook post-checkout "$hooks_dir"
}

main "$@"
