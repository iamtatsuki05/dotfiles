#!/usr/bin/env bash

set -euo pipefail

# The download page and the Sparkle appcast hand out expiring signed CDN URLs, so
# the dmg cannot be pinned in Nix; the appcast URL itself is stable.
readonly APPCAST_URL="https://www.facebook.com/endo/release/appcast.xml?channel=production"
readonly TEAM_ID="V9WTTPBFK9"
readonly APP_NAME="Muse.app"
readonly APPLICATIONS_DIR="${MUSE_APP_INSTALL_DIR:-/Applications}"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "ERROR: Muse for Mac requires macOS." >&2
  exit 1
fi

if [[ -e "$APPLICATIONS_DIR/$APP_NAME" ]]; then
  echo "$APPLICATIONS_DIR/$APP_NAME already exists; the app updates itself through Sparkle."
  exit 0
fi

temp_dir="$(mktemp -d)"
mount_point="$temp_dir/mount"
cleanup() {
  hdiutil detach "$mount_point" -quiet >/dev/null 2>&1 || true
  rm -rf "$temp_dir"
}
trap cleanup EXIT

curl --fail --silent --show-error --location --proto '=https' --tlsv1.2 \
  --output "$temp_dir/appcast.xml" "$APPCAST_URL"
dmg_url="$(python3 -I - "$temp_dir/appcast.xml" <<'PY'
import sys
import xml.etree.ElementTree as ET

enclosure = ET.parse(sys.argv[1]).getroot().find("./channel/item/enclosure")
if enclosure is None or not enclosure.get("url", "").startswith("https://"):
    raise SystemExit("ERROR: Muse appcast has no https enclosure")
print(enclosure.get("url"))
PY
)"

curl --fail --silent --show-error --location --proto '=https' --tlsv1.2 \
  --output "$temp_dir/Muse.dmg" "$dmg_url"
mkdir -p "$mount_point"
hdiutil attach -nobrowse -readonly -quiet -mountpoint "$mount_point" "$temp_dir/Muse.dmg"

app="$mount_point/$APP_NAME"
codesign --verify --deep --strict "$app"
team_id="$(codesign -dv "$app" 2>&1 | sed -n 's/^TeamIdentifier=//p')"
if [[ "$team_id" != "$TEAM_ID" ]]; then
  echo "ERROR: $APP_NAME is signed by team '$team_id', expected Meta ($TEAM_ID)." >&2
  exit 1
fi
spctl --assess --type execute "$app"

ditto "$app" "$APPLICATIONS_DIR/$APP_NAME"
echo "Installed $APPLICATIONS_DIR/$APP_NAME"
