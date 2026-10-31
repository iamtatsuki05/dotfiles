#!/usr/bin/env bash

dotfiles_load_features() {
  local repo_root="$1"
  local data_file="$repo_root/home/.chezmoidata.toml"
  local feature_value

  unset DOTFILES_MACOS_FEATURES
  # Bootstrap runs before Nix/Python may exist; only this literal boolean is read here.
  if ! feature_value="$(LC_ALL=C awk '
    {
      line = $0
      sub(/\r$/, "", line)
      sub(/^[ \t]*/, "", line)
      sub(/[ \t]*#.*/, "", line)
      sub(/[ \t]*$/, "", line)
      if (line == "") next

      if (line ~ /^\[/) {
        in_features = 0
        if (line ~ /^\[[ \t]*features[ \t]*\]$/) {
          tables++
          in_features = 1
        } else {
          header = line
          gsub(/\[/, "", header)
          gsub(/\]/, "", header)
          gsub(/["\047 \t]/, "", header)
          if (header ~ /^features(\.|$)/) invalid = 1
        }
        next
      }
      if (in_features) {
        if (line !~ /^macos[ \t]*=[ \t]*(true|false)$/) {
          invalid = 1
        } else {
          values++
          value = line
          sub(/^macos[ \t]*=[ \t]*/, "", value)
        }
      } else {
        key = line
        sub(/=.*/, "", key)
        gsub(/["\047 \t]/, "", key)
        if (key ~ /^features(\.|$)/) invalid = 1
      }
    }
    END {
      if (invalid || tables != 1 || values != 1) exit 2
      print value
    }
  ' "$data_file")"; then
    printf 'ERROR: cannot read a valid [features] macos boolean from %s\n' "$data_file" >&2
    return 2
  fi
  DOTFILES_MACOS_FEATURES="$feature_value"
}

dotfiles_macos_features_enabled() {
  case "${DOTFILES_MACOS_FEATURES-}" in
    true) return 0 ;;
    false) return 1 ;;
    *)
      printf 'ERROR: feature flags have not been loaded successfully\n' >&2
      return 2
      ;;
  esac
}
