#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
fixture="$(mktemp -d)"
trap 'rm -rf "$fixture"' EXIT
mkdir -p "$fixture/repo/scripts" "$fixture/repo/config/mouse/steermouse" "$fixture/home" "$fixture/bin"
source_dir="$fixture/repo/config/mouse/steermouse"
target="$fixture/home/Library/Application Support/SteerMouse & CursorSense"
printf 'original settings\n' > "$source_dir/Device.smsetting"
cat > "$fixture/bin/uname" <<'EOF'
#!/bin/sh
printf '%s\n' "${FIXTURE_OS:-Darwin}"
EOF
cat > "$fixture/bin/pgrep" <<'EOF'
#!/bin/sh
exit "${FIXTURE_PGREP_STATUS:-1}"
EOF
chmod +x "$fixture/bin/"*
if [[ ! -f "$repo_root/scripts/setup_steermouse.sh" ]]; then
  echo 'FAIL: SteerMouse setup entrypoint is missing' >&2
  exit 1
fi
cp "$repo_root/scripts/setup_steermouse.sh" "$fixture/repo/scripts/"
run_setup() {
  HOME="$fixture/home" PATH="$fixture/bin:/usr/bin:/bin" \
    bash "$fixture/repo/scripts/setup_steermouse.sh" "$@" > "$fixture/output" 2>&1
}
fail() { cat "$fixture/output" >&2; echo "FAIL: $*" >&2; exit 1; }
reject_setup() { if run_setup "$@"; then fail 'unsafe setup was accepted'; fi; }

run_setup --dry-run || fail 'dry run failed'
[[ ! -e "$fixture/home/Library" ]] || fail 'dry run created home files'
FIXTURE_OS=Linux run_setup || fail 'Linux skip failed'
[[ ! -e "$fixture/home/Library" ]] || fail 'Linux setup wrote home files'
FIXTURE_PGREP_STATUS=0 reject_setup
[[ ! -e "$fixture/home/Library" ]] || fail 'running app changed home files'
FIXTURE_PGREP_STATUS=2 reject_setup
run_setup || fail 'fresh setup failed'
[[ -L "$target" && "$target" -ef "$source_dir" ]] || fail 'directory not linked'
FIXTURE_PGREP_STATUS=0 run_setup || fail 'existing managed link is not idempotent'

# A save that replaces the data file must still update the repository through the directory link.
printf 'updated settings\n' > "$target/new-settings"
mv "$target/new-settings" "$target/Device.smsetting"
[[ -L "$target" && "$(cat "$source_dir/Device.smsetting")" == 'updated settings' ]] || fail 'replacement broke synchronization'
rm "$target"
mkdir -p "$target/Device Definitions"
cp "$source_dir/Device.smsetting" "$target/Device.smsetting"
run_setup --dry-run || fail 'existing directory dry run failed'
[[ -d "$target" && ! -L "$target" ]] || fail 'dry run replaced directory'
printf '#!/bin/sh\nexit 1\n' > "$fixture/bin/ln"
chmod +x "$fixture/bin/ln"
reject_setup
[[ -d "$target" && ! -L "$target" ]] && cmp "$target/Device.smsetting" "$source_dir/Device.smsetting" || fail 'failed link did not restore original directory'
rm "$fixture/bin/ln"
run_setup || fail 'matching directory migration failed'
[[ -L "$target" ]] || fail 'matching directory not linked'
backups=("$fixture/home/Library/Application Support/"SteerMouse-backup.*/settings/Device.smsetting)
[[ ${#backups[@]} == 1 ]] && cmp "${backups[0]}" "$source_dir/Device.smsetting" || fail 'backup missing or modified'

rm "$target"
mkdir "$target"
printf 'different live settings\n' > "$target/Device.smsetting"
reject_setup
[[ ! -L "$target" && "$(cat "$target/Device.smsetting")" == 'different live settings' ]] || fail 'different settings overwritten'
cp "$source_dir/Device.smsetting" "$target/Device.smsetting"
printf 'custom definition\n' > "$target/custom-device"
reject_setup
[[ -f "$target/custom-device" && ! -L "$target" ]] || fail 'extra data lost'
rm "$target/custom-device"
ln -s "$source_dir/Device.smsetting" "$target/foreign-link"
reject_setup
rm "$target/foreign-link" "$target/Device.smsetting"
rmdir "$target"
ln -s "$fixture/missing" "$target"
reject_setup
[[ "$(readlink "$target")" == "$fixture/missing" ]] || fail 'foreign broken link replaced'
rm "$target"
printf 'not a directory\n' > "$target"
reject_setup
[[ -f "$target" && ! -L "$target" ]] || fail 'file target replaced'
rm "$target"
rm "$source_dir/Device.smsetting"
reject_setup
[[ ! -e "$target" ]] || fail 'missing source created link'
reject_setup --unknown
echo 'SteerMouse setup tests passed'
