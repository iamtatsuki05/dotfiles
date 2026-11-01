#!/usr/bin/env bash
set -euo pipefail
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
package_dir="$repo_root/dotfiles/.agent"
case "$*" in
  '') ;;
  --dry-run) printf 'Install Hallmark browser dependencies from %s/package-lock.json\n' "$package_dir"; exit 0 ;;
  -h|--help) echo 'Usage: bash scripts/setup_hallmark.sh [--dry-run]'; exit 0 ;;
  *) echo "ERROR: unknown arguments: $*" >&2; exit 1 ;;
esac
[[ -f "$package_dir/package.json" && -f "$package_dir/package-lock.json" ]] || {
  echo 'ERROR: Hallmark dependency manifest and lockfile are required' >&2
  exit 1
}
npm ci --ignore-scripts --engine-strict --no-audit --no-fund --prefix "$package_dir"
echo 'Hallmark browser dependencies installed. Rendering also requires Chrome or Chromium.'
