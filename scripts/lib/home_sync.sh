#!/usr/bin/zsh

dotfiles_sync_home_tree() {
  local source_dir="$1"
  local target_dir="${2:-$HOME}"

  if [[ ! -d "$source_dir" ]]; then
    echo "ERROR: source directory not found: $source_dir" >&2
    return 1
  fi

  mkdir -p "$target_dir"

  # tar replaces Home Manager symlinks with real files instead of writing into
  # the symlink target under /nix/store.
  tar -C "$source_dir" -cf - . | tar -C "$target_dir" -xf -
}
