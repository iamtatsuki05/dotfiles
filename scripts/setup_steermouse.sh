#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
source_dir="$repo_root/config/mouse/steermouse"
target="$HOME/Library/Application Support/SteerMouse & CursorSense"
dry_run=0
case "${1-}" in
  '') [[ $# == 0 ]] || exit 1 ;;
  --dry-run) [[ $# == 1 ]] || exit 1; dry_run=1 ;;
  -h|--help)
    printf 'Usage: bash scripts/setup_steermouse.sh [--dry-run]\n'
    exit 0 ;;
  *) printf 'ERROR: unknown argument: %s\n' "$1" >&2; exit 1 ;;
esac

if [[ "$(uname -s)" != Darwin ]]; then
  printf 'SKIP: SteerMouse setup is macOS-only\n'
  exit 0
fi
if [[ ! -f "$source_dir/Device.smsetting" || ! -s "$source_dir/Device.smsetting" || -L "$source_dir" || -L "$source_dir/Device.smsetting" ]]; then
  printf 'ERROR: expected a nonempty regular settings source: %s/Device.smsetting\n' "$source_dir" >&2
  exit 1
fi
if [[ -L "$target" ]]; then
  if [[ "$(readlink "$target")" == "$source_dir" ]]; then
    printf 'Already linked: %s\n' "$target"
    exit 0
  fi
  printf 'ERROR: refusing to replace an unmanaged symlink: %s\n' "$target" >&2
  exit 1
fi
if [[ -e "$target" ]]; then
  if [[ ! -d "$target" ]]; then
    printf 'ERROR: expected a directory: %s\n' "$target" >&2
    exit 1
  fi
  extra="$(find "$target" -mindepth 1 ! -type d ! -path "$target/Device.smsetting" -print)"
  if [[ -n "$extra" || -L "$target/Device.smsetting" ]]; then
    printf 'ERROR: existing folder contains additional data; reconcile it before linking: %s\n' "$target" >&2
    exit 1
  fi
  if [[ -e "$target/Device.smsetting" ]] && ! cmp -s "$target/Device.smsetting" "$source_dir/Device.smsetting"; then
    printf 'ERROR: live settings differ from the repository; preserve and reconcile them before linking: %s\n' "$target/Device.smsetting" >&2
    exit 1
  fi
fi
if (( dry_run )); then
  printf 'Would back up any existing directory, then link: %s -> %s\n' "$target" "$source_dir"
  printf 'Apply requires SteerMouse and CursorSense to be stopped.\n'
  exit 0
fi
if pgrep -x 'SteerMouse|SteerMouse Manager|CursorSense|CursorSense Manager' >/dev/null; then
  printf 'ERROR: quit SteerMouse and CursorSense (including their managers) before linking settings.\n' >&2
  exit 1
else
  process_status=$?
  if [[ "$process_status" != 1 ]]; then
    printf 'ERROR: could not check whether SteerMouse or CursorSense is running.\n' >&2
    exit "$process_status"
  fi
fi

mkdir -p "$(dirname "$target")"
backup=""
if [[ -d "$target" ]]; then
  backup="$(mktemp -d "$(dirname "$target")/SteerMouse-backup.XXXXXX")"
  mv "$target" "$backup/settings"
  printf 'Backup: %s/settings\n' "$backup"
fi
# Core Data can replace Device.smsetting when saving, so link its directory rather than the file.
if ! ln -s "$source_dir" "$target"; then
  if [[ -n "$backup" && ! -e "$target" && ! -L "$target" ]]; then
    mv "$backup/settings" "$target"
    rmdir "$backup"
  fi
  exit 1
fi
printf 'Linked: %s -> %s\n' "$target" "$source_dir"
printf 'Open SteerMouse and use Reconfigure in the Device tab. Keep this repository checkout in place.\n'
