#!/usr/bin/env zsh

dotfiles_list_nix_setting_has_entries() {
  local file_path="$1"
  local setting_name="$2"

  [[ -f "$file_path" ]] || return 1
  awk -v target="$setting_name" '
    BEGIN { in_section = 0 }
    $0 ~ "^[[:space:]]*" target "[[:space:]]*=" { in_section = 1; next }
    in_section && /^[[:space:]]*[A-Za-z0-9_]+[[:space:]]*=/ { in_section = 0 }
    in_section && /^[[:space:]]*"[^"]+"/ { found = 1 }
    END { exit found ? 0 : 1 }
  ' "$file_path"
}

dotfiles_homebrew_fallback_has_cli_entries() {
  dotfiles_list_nix_setting_has_entries "$HOMEBREW_FALLBACK_CONFIG" "brews"
}

dotfiles_homebrew_fallback_has_gui_entries() {
  dotfiles_list_nix_setting_has_entries "$HOMEBREW_FALLBACK_CONFIG" "casks" \
    || dotfiles_list_nix_setting_has_entries "$HOMEBREW_FALLBACK_CONFIG" "vscode"
}

dotfiles_profile_requires_homebrew() {
  local profile_name="$1"

  dotfiles_is_macos || return 1

  if dotfiles_homebrew_fallback_has_cli_entries; then
    return 0
  fi

  [[ "$profile_name" == "full" ]] && dotfiles_homebrew_fallback_has_gui_entries
}
