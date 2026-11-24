#!/usr/bin/env zsh

set -euo pipefail

readonly SCRIPT_DIR="${0:A:h}"
readonly REPO_ROOT="${SCRIPT_DIR:h}"
readonly LIB_DIR="$SCRIPT_DIR/lib"
readonly HOMEBREW_FALLBACK_CONFIG="${DOTFILES_HOMEBREW_FALLBACK_CONFIG:-$REPO_ROOT/config/nix/homebrew-fallback.nix}"

source "$LIB_DIR/homebrew.sh"

APPLY_FIXES=0
RUN_BREW_UPDATE=1

usage() {
  cat <<EOF
Usage:
  zsh scripts/doctor_homebrew_fallback.sh [--fix] [--no-update]

Checks every brews/casks entry in config/nix/homebrew-fallback.nix against
Homebrew, so "Cask 'X' is unavailable" failures in package-update can be
diagnosed before running brew bundle.

Options:
  --fix        Rewrite renamed entries in homebrew-fallback.nix in place.
  --no-update  Skip "brew update" (stale metadata may hide new casks).
  -h, --help   Show this help.

Exit status:
  0  All entries resolve.
  1  Renamed or missing entries remain.
  2  Environment or usage error.
EOF
}

while (($#)); do
  case "$1" in
    --fix) APPLY_FIXES=1 ;;
    --no-update) RUN_BREW_UPDATE=0 ;;
    -h|--help) usage; exit 0 ;;
    *) print -u2 -r -- "ERROR: unknown option: $1"; usage >&2; exit 2 ;;
  esac
  shift
done

if [[ ! -f "$HOMEBREW_FALLBACK_CONFIG" ]]; then
  print -u2 -r -- "ERROR: config not found: $HOMEBREW_FALLBACK_CONFIG"
  exit 2
fi

if ! dotfiles_find_homebrew >/dev/null 2>&1; then
  print -u2 -r -- "ERROR: brew executable not found"
  exit 2
fi
readonly BREW_BIN="$REPLY"

fallback_entries() {
  local setting_name="$1"

  awk -v target="$setting_name" '
    BEGIN { in_section = 0 }
    $0 ~ "^[[:space:]]*" target "[[:space:]]*=" { in_section = 1; next }
    in_section && /^[[:space:]]*[A-Za-z0-9_]+[[:space:]]*=/ { in_section = 0 }
    in_section {
      line = $0
      sub(/#.*/, "", line)
      while (match(line, /"([^"\\]|\\.)*"/)) {
        value = substr(line, RSTART + 1, RLENGTH - 2)
        gsub(/\\"/, "\"", value)
        print value
        line = substr(line, RSTART + RLENGTH)
      }
    }
  ' "$HOMEBREW_FALLBACK_CONFIG"
}

if (( RUN_BREW_UPDATE )); then
  print -r -- "===> Refreshing Homebrew metadata (brew update)"
  if ! "$BREW_BIN" update --quiet >/dev/null 2>&1; then
    print -u2 -r -- "WARNING: brew update failed; checking against possibly stale metadata"
  fi
fi

typeset -A KNOWN_FORMULAE KNOWN_CASKS INSTALLED_TAPS
typeset name
for name in ${(f)"$("$BREW_BIN" formulae 2>/dev/null)"}; do
  if [[ -n "$name" ]]; then KNOWN_FORMULAE[$name]=1; fi
done
for name in ${(f)"$("$BREW_BIN" casks 2>/dev/null)"}; do
  if [[ -n "$name" ]]; then KNOWN_CASKS[$name]=1; fi
done
for name in ${(f)"$("$BREW_BIN" tap 2>/dev/null)"}; do
  if [[ -n "$name" ]]; then INSTALLED_TAPS[$name]=1; fi
done

typeset -a RENAMED_ENTRIES MISSING_ENTRIES SKIPPED_ENTRIES
OK_COUNT=0

print_suggestions() {
  local kind_flag="$1" token="$2"
  local suggestions

  suggestions="$("$BREW_BIN" search "$kind_flag" -- "$token" 2>/dev/null \
    | grep -v '^==>' | grep -v '^[[:space:]]*$' | head -5 || true)"
  if [[ -n "$suggestions" ]]; then
    print -r -- "    candidates: ${(j:, :)${(f)suggestions}}"
  fi
  return 0
}

check_entry() {
  local kind="$1" token="$2"
  local kind_flag="--$kind"
  local info_output new_name tap_name

  if [[ "$kind" == "formula" && -n "${KNOWN_FORMULAE[$token]:-}" ]] \
    || [[ "$kind" == "cask" && -n "${KNOWN_CASKS[$token]:-}" ]]; then
    (( OK_COUNT += 1 ))
    return 0
  fi

  if [[ "$token" == */*/* ]]; then
    tap_name="${token%/*}"
    if [[ -z "${INSTALLED_TAPS[$tap_name]:-}" ]]; then
      SKIPPED_ENTRIES+=("$token (tap $tap_name is not tapped locally)")
      return 0
    fi
  fi

  if info_output="$("$BREW_BIN" info "$kind_flag" -- "$token" 2>&1)"; then
    new_name="$(print -r -- "$info_output" | awk '/^==>/ { print $2; exit }')"
    new_name="${new_name%:}"
    if [[ -n "$new_name" && "$new_name" != "$token" && "$new_name" != "${token##*/}" ]]; then
      RENAMED_ENTRIES+=("$token:$new_name")
    else
      (( OK_COUNT += 1 ))
    fi
    return 0
  fi

  if [[ "$info_output" == *"untrusted tap"* || "$info_output" == *"is not trusted"* ]]; then
    SKIPPED_ENTRIES+=("$token (tap is not trusted for brew info; brew bundle trusts it separately)")
    return 0
  fi

  MISSING_ENTRIES+=("$kind:$token")
  print -r -- "MISSING ($kind): $token"
  print -r -- "    $(print -r -- "$info_output" | head -1)"
  print_suggestions "$kind_flag" "$token"
  return 0
}

print -r -- "===> Checking entries in ${HOMEBREW_FALLBACK_CONFIG#$REPO_ROOT/}"
for name in ${(f)"$(fallback_entries brews)"}; do
  if [[ -n "$name" ]]; then check_entry formula "$name"; fi
done
for name in ${(f)"$(fallback_entries casks)"}; do
  if [[ -n "$name" ]]; then check_entry cask "$name"; fi
done

typeset entry old_name new_name replacement old_pattern fix_temp
for entry in "${RENAMED_ENTRIES[@]}"; do
  old_name="${entry%%:*}"
  new_name="${entry#*:}"
  replacement="$new_name"
  if [[ "$old_name" == */* && "$new_name" != */* ]]; then
    replacement="${old_name%/*}/$new_name"
  fi
  if (( APPLY_FIXES )) && [[ -n "$new_name" ]]; then
    old_pattern="${old_name//./\\.}"
    fix_temp="$(mktemp)"
    sed "s|\"${old_pattern}\"|\"${replacement}\"|g" "$HOMEBREW_FALLBACK_CONFIG" > "$fix_temp"
    # cat preserves the config file's permissions and inode; mv would replace them with mktemp's 0600.
    cat "$fix_temp" > "$HOMEBREW_FALLBACK_CONFIG"
    rm -f "$fix_temp"
    print -r -- "FIXED: renamed $old_name -> $replacement"
  else
    print -r -- "RENAMED: $old_name -> $replacement (run with --fix to rewrite)"
  fi
done

for entry in "${SKIPPED_ENTRIES[@]}"; do
  print -r -- "SKIPPED: $entry"
done

print -r -- "===> Result: $OK_COUNT ok, ${#RENAMED_ENTRIES[@]} renamed, ${#MISSING_ENTRIES[@]} missing, ${#SKIPPED_ENTRIES[@]} skipped"

if (( ${#MISSING_ENTRIES[@]} > 0 )); then
  print -r -- "Remove or replace the missing entries in ${HOMEBREW_FALLBACK_CONFIG#$REPO_ROOT/} manually."
  exit 1
fi
if (( ${#RENAMED_ENTRIES[@]} > 0 )) && (( ! APPLY_FIXES )); then
  exit 1
fi
exit 0
