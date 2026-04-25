#!/usr/bin/zsh

set -euo pipefail

readonly SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DEFAULT_REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

REPO_ROOT="$DEFAULT_REPO_ROOT"
BREWFILE=""
APPLY=0

usage() {
  cat <<EOF
Usage:
  zsh scripts/migrate_brew_to_nix.sh [--apply] [--brewfile PATH]

Options:
  --apply           Write Nix package lists and migration reports.
  --dry-run         Show the migration summary without writing files. This is the default.
  --brewfile PATH   Brewfile to migrate. If omitted, the current Homebrew state is dumped to a temporary Brewfile.
  --repo-root PATH  Override repository root. Intended for tests.
  -h, --help        Show this help.
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
      --brewfile)
        shift
        if ((! $#)); then
          echo "ERROR: --brewfile requires a value" >&2
          return 1
        fi
        BREWFILE="$1"
        ;;
      --brewfile=*)
        BREWFILE="${1#--brewfile=}"
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

cleanup_temp_brewfile() {
  [[ -z "${TEMP_BREWFILE_DIR:-}" ]] || rm -rf "$TEMP_BREWFILE_DIR"
}

dump_current_homebrew_state() {
  if ! command -v brew >/dev/null 2>&1; then
    echo "ERROR: --brewfile was not specified and brew is not available." >&2
    echo "Pass a Brewfile with --brewfile PATH, or run this on a machine that still has Homebrew." >&2
    return 1
  fi

  TEMP_BREWFILE_DIR="$(mktemp -d)"
  BREWFILE="$TEMP_BREWFILE_DIR/Brewfile"

  echo "Dumping current Homebrew state to temporary Brewfile..."
  HOMEBREW_BUNDLE_DUMP_NO_GO="${HOMEBREW_BUNDLE_DUMP_NO_GO:-1}" \
    HOMEBREW_BUNDLE_DUMP_NO_CARGO="${HOMEBREW_BUNDLE_DUMP_NO_CARGO:-1}" \
    HOMEBREW_BUNDLE_DUMP_NO_KREW="${HOMEBREW_BUNDLE_DUMP_NO_KREW:-1}" \
    HOMEBREW_BUNDLE_DUMP_NO_NPM="${HOMEBREW_BUNDLE_DUMP_NO_NPM:-1}" \
    brew bundle dump --file="$BREWFILE" --force
}

resolve_brewfile() {
  if [[ -n "$BREWFILE" ]]; then
    return
  fi

  dump_current_homebrew_state
}

load_mapping_file() {
  local map_file="$1"
  local target_assoc="$2"
  local brew_name
  local nix_name

  if [[ ! -f "$map_file" ]]; then
    echo "ERROR: mapping file not found: $map_file" >&2
    return 1
  fi

  while IFS=$'\t' read -r brew_name nix_name _rest; do
    [[ -n "$brew_name" ]] || continue
    [[ "$brew_name" == \#* ]] && continue
    [[ -n "$nix_name" ]] || continue
    eval "${target_assoc}[\$brew_name]=\"\$nix_name\""
  done < "$map_file"
}

load_cask_mapping() {
  local map_file="$REPO_ROOT/config/nix/cask-to-nix.tsv"
  local cask_name
  local nix_name
  local nix_scope
  local _homebrew_scope

  if [[ ! -f "$map_file" ]]; then
    echo "ERROR: mapping file not found: $map_file" >&2
    return 1
  fi

  while IFS=$'\t' read -r cask_name nix_name nix_scope _homebrew_scope; do
    [[ -n "$cask_name" ]] || continue
    [[ "$cask_name" == \#* ]] && continue
    [[ -n "$nix_name" ]] || continue
    NIX_BY_CASK[$cask_name]="$nix_name"
    NIX_SCOPE_BY_CASK[$cask_name]="${nix_scope:-common}"
  done < "$map_file"
}

load_mas_to_nix_mapping() {
  local map_file="$REPO_ROOT/config/nix/mas-to-nix.tsv"
  local app_name
  local app_id
  local nix_name
  local nix_scope

  [[ -f "$map_file" ]] || return 0

  while IFS=$'\t' read -r app_name app_id nix_name nix_scope _rest; do
    [[ -n "$app_name" ]] || continue
    [[ "$app_name" == \#* ]] && continue
    [[ -n "$nix_name" ]] || continue
    NIX_BY_MAS_NAME[$app_name]="$nix_name"
    NIX_SCOPE_BY_MAS_NAME[$app_name]="${nix_scope:-macos}"
    if [[ -n "$app_id" ]]; then
      NIX_BY_MAS_ID[$app_id]="$nix_name"
      NIX_SCOPE_BY_MAS_ID[$app_id]="${nix_scope:-macos}"
    fi
  done < "$map_file"
}

load_mas_to_cask_mapping() {
  local map_file="$REPO_ROOT/config/nix/mas-to-cask.tsv"
  local app_name
  local app_id
  local cask_name

  [[ -f "$map_file" ]] || return 0

  while IFS=$'\t' read -r app_name app_id cask_name _rest; do
    [[ -n "$app_name" ]] || continue
    [[ "$app_name" == \#* ]] && continue
    [[ -n "$cask_name" ]] || continue
    CASK_BY_MAS_NAME[$app_name]="$cask_name"
    if [[ -n "$app_id" ]]; then
      CASK_BY_MAS_ID[$app_id]="$cask_name"
    fi
  done < "$map_file"
}

load_mappings() {
  load_mapping_file "$REPO_ROOT/config/nix/brew-to-nix.tsv" NIX_BY_BREW
  load_cask_mapping
  load_mas_to_nix_mapping
  load_mas_to_cask_mapping
}

quote_nix_string() {
  local value="$1"

  value="${value//\\/\\\\}"
  value="${value//\"/\\\"}"
  print -r -- "\"$value\""
}

append_unique() {
  local array_name="$1"
  local value="$2"
  local existing

  eval "for existing in \"\${${array_name}[@]:-}\"; do [[ \"\$existing\" == \"\$value\" ]] && return 0; done"
  eval "${array_name}+=(\"\$value\")"
}

append_unmapped() {
  local kind="$1"
  local name="$2"
  local reason="$3"

  append_unique UNMAPPED_HOMEBREW_LINES "$kind"$'\t'"$name"$'\t'"$reason"
}

append_homebrew_fallback() {
  local kind="$1"
  local name="$2"

  case "$kind" in
    tap)
      append_unique HOMEBREW_FALLBACK_TAPS "$name"
      ;;
    brew)
      append_unique HOMEBREW_FALLBACK_BREWS "$name"
      ;;
    cask)
      append_unique HOMEBREW_FALLBACK_CASKS "$name"
      ;;
    vscode)
      append_unique HOMEBREW_FALLBACK_VSCODE "$name"
      ;;
    uv)
      append_unique HOMEBREW_FALLBACK_UNSUPPORTED_UV "$name"
      ;;
  esac
}

append_mas_app() {
  local name="$1"
  local app_id="$2"

  append_unique MAS_APP_NAMES "$name"
  MAS_APP_IDS[$name]="$app_id"
}

append_gui_package_by_scope() {
  local nix_name="$1"
  local nix_scope="$2"

  case "$nix_scope" in
    common|all)
      append_unique GUI_COMMON_PACKAGE_NAMES "$nix_name"
      ;;
    macos|darwin)
      append_unique GUI_MACOS_PACKAGE_NAMES "$nix_name"
      ;;
    linux)
      append_unique GUI_LINUX_PACKAGE_NAMES "$nix_name"
      ;;
    *)
      echo "ERROR: unsupported Nix MAS scope for $nix_name: $nix_scope" >&2
      return 1
      ;;
  esac
}

register_mas_app_by_priority() {
  local name="$1"
  local app_id="$2"
  local nix_name
  local nix_scope
  local cask_name

  nix_name="${NIX_BY_MAS_NAME[$name]:-${NIX_BY_MAS_ID[$app_id]:-}}"
  if [[ -n "$nix_name" ]]; then
    nix_scope="${NIX_SCOPE_BY_MAS_NAME[$name]:-${NIX_SCOPE_BY_MAS_ID[$app_id]:-macos}}"
    append_gui_package_by_scope "$nix_name" "$nix_scope"
    append_unique MIGRATED_MAS_APPS "$name"$'\t'"nix"$'\t'"$nix_name"
    return
  fi

  cask_name="${CASK_BY_MAS_NAME[$name]:-${CASK_BY_MAS_ID[$app_id]:-}}"
  if [[ -n "$cask_name" ]]; then
    append_homebrew_fallback cask "$cask_name"
    append_unique MIGRATED_MAS_APPS "$name"$'\t'"brew"$'\t'"$cask_name"
    return
  fi

  append_mas_app "$name" "$app_id"
}

parse_brewfile() {
  local line
  local kind
  local name
  local nix_name
  local nix_scope

  if [[ ! -f "$BREWFILE" ]]; then
    echo "ERROR: Brewfile not found: $BREWFILE" >&2
    return 1
  fi

  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ -n "$line" ]] || continue
    [[ "$line" == \#* ]] && continue

    if [[ "$line" =~ '^mas "([^"]+)", id: ([0-9]+)' ]]; then
      register_mas_app_by_priority "${match[1]}" "${match[2]}"
      continue
    fi

    if [[ "$line" =~ '^(tap|brew|cask|vscode|uv) "([^"]+)"' ]]; then
      kind="${match[1]}"
      name="${match[2]}"
    else
      append_unmapped "raw" "$line" "unparsed-brewfile-line"
      continue
    fi

    case "$kind" in
      tap)
        append_unmapped "$kind" "$name" "homebrew-tap-not-managed-by-nix"
        append_homebrew_fallback "$kind" "$name"
        ;;
      brew)
        if [[ -n "${NIX_BY_BREW[$name]:-}" ]]; then
          nix_name="${NIX_BY_BREW[$name]}"
          append_unique NIX_PACKAGE_NAMES "$nix_name"
          append_unique MIGRATED_BREW_FORMULAE "$name"
        else
          append_unmapped "$kind" "$name" "no-nixpkg-mapping"
          append_homebrew_fallback "$kind" "$name"
        fi
        ;;
      cask)
        if [[ -n "${NIX_BY_CASK[$name]:-}" ]]; then
          nix_name="${NIX_BY_CASK[$name]}"
          nix_scope="${NIX_SCOPE_BY_CASK[$name]:-common}"
          case "$nix_scope" in
            common|all)
              append_unique GUI_COMMON_PACKAGE_NAMES "$nix_name"
              append_unique MIGRATED_BREW_CASKS "$name"
              ;;
            macos|darwin)
              append_unique GUI_MACOS_PACKAGE_NAMES "$nix_name"
              append_unique MIGRATED_BREW_CASKS "$name"
              ;;
            linux)
              append_unique GUI_LINUX_PACKAGE_NAMES "$nix_name"
              append_unmapped "$kind" "$name" "nix-package-is-linux-only"
              append_homebrew_fallback "$kind" "$name"
              ;;
            *)
              echo "ERROR: unsupported Nix cask scope for $name: $nix_scope" >&2
              return 1
              ;;
          esac
        else
          append_unmapped "$kind" "$name" "no-nixpkg-mapping"
          append_homebrew_fallback "$kind" "$name"
        fi
        ;;
      vscode|uv)
        append_unmapped "$kind" "$name" "managed-outside-nix-package-set"
        append_homebrew_fallback "$kind" "$name"
        ;;
    esac
  done < "$BREWFILE"
}

write_string_list() {
  local target="$1"
  local array_name="$2"
  local name

  mkdir -p "${target:h}"
  {
    echo "["
    eval "for name in \"\${${array_name}[@]}\"; do echo \"  \$(quote_nix_string \"\$name\")\"; done"
    echo "]"
  } > "$target"
}

write_package_names() {
  write_string_list "$REPO_ROOT/config/nix/package-names.nix" NIX_PACKAGE_NAMES
}

write_gui_package_names() {
  write_string_list "$REPO_ROOT/config/nix/gui-common-package-names.nix" GUI_COMMON_PACKAGE_NAMES
  write_string_list "$REPO_ROOT/config/nix/gui-macos-package-names.nix" GUI_MACOS_PACKAGE_NAMES
  write_string_list "$REPO_ROOT/config/nix/gui-linux-package-names.nix" GUI_LINUX_PACKAGE_NAMES

  local legacy_target="$REPO_ROOT/config/nix/gui-package-names.nix"
  [[ ! -f "$legacy_target" ]] || rm -f "$legacy_target"
}

write_line_list() {
  local target="$1"
  local array_name="$2"
  local line

  mkdir -p "${target:h}"
  {
    eval "for line in \"\${${array_name}[@]}\"; do print -r -- \"\$line\"; done"
  } > "$target"
}

write_migration_reports() {
  write_line_list "$REPO_ROOT/config/nix/migrated-brew-formulae.txt" MIGRATED_BREW_FORMULAE
  write_line_list "$REPO_ROOT/config/nix/migrated-brew-casks.txt" MIGRATED_BREW_CASKS
  write_line_list "$REPO_ROOT/config/nix/migrated-mas-apps.tsv" MIGRATED_MAS_APPS

  local target="$REPO_ROOT/config/nix/unmapped-homebrew.tsv"
  local line
  mkdir -p "${target:h}"
  {
    echo $'# kind\tname\treason'
    for line in "${UNMAPPED_HOMEBREW_LINES[@]}"; do
      print -r -- "$line"
    done
  } > "$target"
}

write_homebrew_fallback_config() {
  local target="$REPO_ROOT/config/nix/homebrew-fallback.nix"

  mkdir -p "${target:h}"
  {
    echo "{"
    write_nix_attr_string_list "taps" HOMEBREW_FALLBACK_TAPS
    echo ""
    write_nix_attr_string_list "brews" HOMEBREW_FALLBACK_BREWS
    echo ""
    write_nix_attr_string_list "casks" HOMEBREW_FALLBACK_CASKS
    echo ""
    write_nix_attr_string_list "vscode" HOMEBREW_FALLBACK_VSCODE
    echo ""
    write_nix_attr_string_list "unsupportedUvPackages" HOMEBREW_FALLBACK_UNSUPPORTED_UV
    echo "}"
  } > "$target"
}

write_mas_apps_config() {
  local target="$REPO_ROOT/config/nix/mas-apps.nix"
  local name
  local app_id

  mkdir -p "${target:h}"
  {
    echo "{"
    for name in "${MAS_APP_NAMES[@]}"; do
      app_id="${MAS_APP_IDS[$name]}"
      echo "  $(quote_nix_string "$name") = $app_id;"
    done
    echo "}"
  } > "$target"
}

write_nix_attr_string_list() {
  local attr_name="$1"
  local array_name="$2"
  local name

  echo "  $attr_name = ["
  eval "for name in \"\${${array_name}[@]}\"; do echo \"    \$(quote_nix_string \"\$name\")\"; done"
  echo "  ];"
}

remove_legacy_homebrew_fallbacks() {
  rm -f \
    "$REPO_ROOT/config/homebrew/fallback.Brewfile" \
    "$REPO_ROOT/config/homebrew/macos-casks.Brewfile"

  rmdir "$REPO_ROOT/config/homebrew" 2>/dev/null || true
}

print_summary() {
  echo "Brewfile: $BREWFILE"
  echo "nix packages: ${#NIX_PACKAGE_NAMES[@]}"
  echo "nix GUI common packages: ${#GUI_COMMON_PACKAGE_NAMES[@]}"
  echo "nix GUI macOS packages: ${#GUI_MACOS_PACKAGE_NAMES[@]}"
  echo "nix GUI Linux packages: ${#GUI_LINUX_PACKAGE_NAMES[@]}"
  echo "migrated Homebrew formulae: ${#MIGRATED_BREW_FORMULAE[@]}"
  echo "migrated Homebrew casks: ${#MIGRATED_BREW_CASKS[@]}"
  echo "unmapped Homebrew entries: ${#UNMAPPED_HOMEBREW_LINES[@]}"
  echo "Homebrew fallback taps: ${#HOMEBREW_FALLBACK_TAPS[@]}"
  echo "Homebrew fallback formulae: ${#HOMEBREW_FALLBACK_BREWS[@]}"
  echo "Homebrew fallback casks: ${#HOMEBREW_FALLBACK_CASKS[@]}"
  echo "Homebrew fallback VS Code extensions: ${#HOMEBREW_FALLBACK_VSCODE[@]}"
  echo "Mac App Store apps: ${#MAS_APP_NAMES[@]}"
  echo "unsupported uv tool entries: ${#HOMEBREW_FALLBACK_UNSUPPORTED_UV[@]}"

  if (( ! APPLY )); then
    echo "DRY-RUN: no files written"
  fi
}

main() {
  typeset -gA NIX_BY_BREW=()
  typeset -gA NIX_BY_CASK=()
  typeset -gA NIX_SCOPE_BY_CASK=()
  typeset -gA NIX_BY_MAS_NAME=()
  typeset -gA NIX_BY_MAS_ID=()
  typeset -gA NIX_SCOPE_BY_MAS_NAME=()
  typeset -gA NIX_SCOPE_BY_MAS_ID=()
  typeset -gA CASK_BY_MAS_NAME=()
  typeset -gA CASK_BY_MAS_ID=()
  typeset -ga NIX_PACKAGE_NAMES=()
  typeset -ga GUI_COMMON_PACKAGE_NAMES=()
  typeset -ga GUI_MACOS_PACKAGE_NAMES=()
  typeset -ga GUI_LINUX_PACKAGE_NAMES=()
  typeset -ga MIGRATED_BREW_FORMULAE=()
  typeset -ga MIGRATED_BREW_CASKS=()
  typeset -ga MIGRATED_MAS_APPS=()
  typeset -ga UNMAPPED_HOMEBREW_LINES=()
  typeset -ga HOMEBREW_FALLBACK_TAPS=()
  typeset -ga HOMEBREW_FALLBACK_BREWS=()
  typeset -ga HOMEBREW_FALLBACK_CASKS=()
  typeset -ga HOMEBREW_FALLBACK_VSCODE=()
  typeset -ga HOMEBREW_FALLBACK_UNSUPPORTED_UV=()
  typeset -gA MAS_APP_IDS=()
  typeset -ga MAS_APP_NAMES=()

  parse_args "$@"
  resolve_brewfile
  load_mappings
  parse_brewfile
  print_summary

  if (( APPLY )); then
    write_package_names
    write_gui_package_names
    write_migration_reports
    write_homebrew_fallback_config
    write_mas_apps_config
    remove_legacy_homebrew_fallbacks
  fi
}

trap cleanup_temp_brewfile EXIT

main "$@"
