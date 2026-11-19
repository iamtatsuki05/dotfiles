#!/usr/bin/env bash

set -euo pipefail

readonly INSTALLER_URL="https://dev.meta.ai/install.sh"

temp_dir="$(mktemp -d)"
trap 'rm -rf "$temp_dir"' EXIT

curl --fail --silent --show-error --location --proto '=https' --tlsv1.2 \
  --output "$temp_dir/install.sh" "$INSTALLER_URL"

# dotfiles が shell の PATH を管理するため、installer には rc ファイルを書き換えさせない。
MUSE_NO_MODIFY_PATH=1 bash "$temp_dir/install.sh"
