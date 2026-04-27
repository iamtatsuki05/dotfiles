#!/usr/bin/zsh

set -euo pipefail

readonly SCRIPT_DIR="${0:A:h}"
REPO_ROOT="${SCRIPT_DIR:h}"
FLAKE_FILE=""
COMMAND=""
TOOL=""
PACKAGE_FILE_URL=""
readonly MANAGED_BLOCK_START='        # BEGIN managed by scripts/manage_nix_package_version_override.sh'
readonly MANAGED_BLOCK_END='        # END managed by scripts/manage_nix_package_version_override.sh'

usage() {
  cat <<EOF
Usage:
  zsh scripts/manage_nix_package_version_override.sh [--repo-root PATH] pin-latest TOOL
  zsh scripts/manage_nix_package_version_override.sh [--repo-root PATH] unpin TOOL
  zsh scripts/manage_nix_package_version_override.sh [--repo-root PATH] show [TOOL]

Commands:
  pin-latest TOOL   Pin a supported Nix package to the latest version published in nixpkgs master.
  unpin TOOL        Remove the explicit override for TOOL and fall back to the locked nixpkgs input.
  show [TOOL]       Print the current override file, or list override files when TOOL is omitted.

Supported tools:
  codex
EOF
}

fail() {
  echo "ERROR: $*" >&2
  exit 1
}

log() {
  echo "$*"
}

resolve_flake_file() {
  FLAKE_FILE="$REPO_ROOT/flake.nix"
}

parse_args() {
  while (($#)); do
    case "$1" in
      --repo-root)
        shift
        (($#)) || fail "--repo-root requires a path"
        REPO_ROOT="$1"
        ;;
      -h|--help)
        usage
        exit 0
        ;;
      pin-latest|unpin|show)
        COMMAND="$1"
        shift
        break
        ;;
      *)
        fail "unknown argument: $1"
        ;;
    esac
    shift
  done

  [[ -n "$COMMAND" ]] || fail "a command is required"

  if [[ "$COMMAND" == "show" ]]; then
    TOOL="${1:-}"
    return 0
  fi

  (($#)) || fail "$COMMAND requires a tool name"
  TOOL="$1"
}

ensure_supported_tool() {
  case "$TOOL" in
    codex)
      PACKAGE_FILE_URL="https://raw.githubusercontent.com/NixOS/nixpkgs/master/pkgs/by-name/co/codex/package.nix"
      ;;
    *)
      fail "unsupported tool: $TOOL"
      ;;
  esac
}

temporary_directory_root() {
  REPLY="${TMPDIR:-/tmp}"
}

create_unique_temp_file() {
  local temp_root="$1"
  local prefix="$2"
  local suffix_index=0
  local candidate

  while ((suffix_index < 1024)); do
    candidate="$temp_root/${prefix}.$$.$suffix_index"
    if [[ ! -e "$candidate" ]]; then
      : > "$candidate"
      REPLY="$candidate"
      return 0
    fi
    suffix_index=$((suffix_index + 1))
  done

  fail "failed to create a temporary file in $temp_root"
}

extract_first_double_quoted_value() {
  local pattern="$1"
  local file_path="$2"
  local line

  while IFS= read -r line || [[ -n "$line" ]]; do
    case "$line" in
      *"$pattern"*)
        line="${line#*"$pattern"}"
        REPLY="${line%%\"*}"
        return 0
        ;;
    esac
  done < "$file_path"

  fail "failed to extract pattern: $pattern"
}

extract_tag_prefix() {
  local file_path="$1"
  local line

  while IFS= read -r line || [[ -n "$line" ]]; do
    case "$line" in
      *'tag = "'*)
        line="${line#*tag = \"}"
        REPLY="${line%%\$\{*}"
        return 0
        ;;
    esac
  done < "$file_path"

  fail "failed to extract tag prefix"
}

replace_managed_block() {
  local replacement_file="$1"
  python3 - "$FLAKE_FILE" "$replacement_file" "$MANAGED_BLOCK_START" "$MANAGED_BLOCK_END" <<'PY'
from pathlib import Path
import sys

flake_path = Path(sys.argv[1])
replacement_path = Path(sys.argv[2])
start_marker = sys.argv[3]
end_marker = sys.argv[4]

lines = flake_path.read_text().splitlines()
replacement_lines = replacement_path.read_text().splitlines()

try:
    start_index = lines.index(start_marker)
    end_index = lines.index(end_marker)
except ValueError as exc:
    raise SystemExit(f"ERROR: failed to find managed block marker: {exc}") from exc

updated_lines = lines[: start_index + 1] + replacement_lines + lines[end_index:]
flake_path.write_text("\n".join(updated_lines) + "\n")
PY
}

write_override_file() {
  local version="$1"
  local owner="$2"
  local repo="$3"
  local tag="$4"
  local src_hash="$5"
  local cargo_hash="$6"
  local source_root="$7"
  local replacement_file="$8"

  cat > "$replacement_file" <<EOF
        $TOOL = {
          owner = "$owner";
          repo = "$repo";
          version = "$version";
          tag = "$tag";
          srcHash = "$src_hash";
          cargoHash = "$cargo_hash";
          sourceRoot = "$source_root";
        };
EOF
}

pin_latest() {
  local temp_root
  local package_file
  local version
  local owner
  local repo
  local tag_prefix
  local src_hash
  local cargo_hash
  local source_root
  local replacement_file

  ensure_supported_tool
  resolve_flake_file
  temporary_directory_root
  temp_root="$REPLY"
  create_unique_temp_file "$temp_root" "dotfiles-nix-package"
  package_file="$REPLY"

  curl -fsSL "$PACKAGE_FILE_URL" > "$package_file"

  extract_first_double_quoted_value 'version = "' "$package_file"
  version="$REPLY"
  extract_first_double_quoted_value 'owner = "' "$package_file"
  owner="$REPLY"
  extract_first_double_quoted_value 'repo = "' "$package_file"
  repo="$REPLY"
  extract_tag_prefix "$package_file"
  tag_prefix="$REPLY"
  extract_first_double_quoted_value 'hash = "' "$package_file"
  src_hash="$REPLY"
  extract_first_double_quoted_value 'cargoHash = "' "$package_file"
  cargo_hash="$REPLY"
  extract_first_double_quoted_value 'sourceRoot = "${finalAttrs.src.name}/' "$package_file"
  source_root="$REPLY"
  create_unique_temp_file "$temp_root" "dotfiles-nix-package-override"
  replacement_file="$REPLY"

  write_override_file "$version" "$owner" "$repo" "${tag_prefix}${version}" "$src_hash" "$cargo_hash" "$source_root" "$replacement_file"
  replace_managed_block "$replacement_file"

  rm -f "$package_file"
  rm -f "$replacement_file"
  log "Pinned $TOOL to $version in $FLAKE_FILE"
}

unpin() {
  local temp_root
  local replacement_file

  ensure_supported_tool
  resolve_flake_file
  temporary_directory_root
  temp_root="$REPLY"
  create_unique_temp_file "$temp_root" "dotfiles-nix-package-override"
  replacement_file="$REPLY"

  : > "$replacement_file"
  replace_managed_block "$replacement_file"
  rm -f "$replacement_file"
  log "Removed explicit latest pin for $TOOL"
}

show_override() {
  local line
  local in_block=0

  resolve_flake_file

  while IFS= read -r line || [[ -n "$line" ]]; do
    if [[ "$line" == "$MANAGED_BLOCK_START" ]]; then
      in_block=1
      continue
    fi
    if [[ "$line" == "$MANAGED_BLOCK_END" ]]; then
      break
    fi
    if [[ "$in_block" == "1" ]]; then
      print -r -- "$line"
    fi
  done < "$FLAKE_FILE"
}

main() {
  parse_args "$@"

  case "$COMMAND" in
    pin-latest)
      pin_latest
      ;;
    unpin)
      unpin
      ;;
    show)
      show_override
      ;;
  esac
}

main "$@"
