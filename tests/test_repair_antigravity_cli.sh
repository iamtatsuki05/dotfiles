#!/usr/bin/env bash
set -euo pipefail

script="$(cd "$(dirname "$0")/../scripts" && pwd)/repair_antigravity_cli.sh"
fixture="$(mktemp -d)"
trap 'rm -rf "$fixture"' EXIT
export PREFIX="$fixture/brew"
mkdir -p "$PREFIX/bin" "$PREFIX/var" "$PREFIX/Caskroom/antigravity-cli" "$fixture/stubs"
export PATH="$fixture/stubs:/usr/bin:/bin"
target="$PREFIX/bin/agy"

cat > "$fixture/stubs/brew" <<'STUB'
#!/bin/sh
set -eu
case "$*" in
  --prefix) echo "$PREFIX" ;;
  "--caskroom antigravity-cli") echo "$PREFIX/Caskroom/antigravity-cli" ;;
  "list --cask --versions antigravity-cli") test -x "$PREFIX/Caskroom/antigravity-cli/wrapper" ;;
  "install --cask antigravity-cli")
    [ ! -e "$PREFIX/bin/agy" ] || exit 1
    [ -z "${FAIL_INSTALL:-}" ] || exit 1
    wrapper="$PREFIX/Caskroom/antigravity-cli/wrapper"
    printf '#!/bin/sh\nexit 0\n' > "$wrapper"
    chmod +x "$wrapper"
    ln -s "$wrapper" "$PREFIX/bin/agy"
    ;;
  *) exit 2 ;;
esac
STUB
cat > "$fixture/stubs/codesign" <<'STUB'
#!/bin/sh
test "$(cat "$PREFIX/bin/agy")" = signed
STUB
chmod +x "$fixture/stubs/"*
echo unsigned > "$target"
chmod +x "$target"
if bash "$script"; then exit 1; fi
[[ "$(cat "$target")" == unsigned ]]
echo signed > "$target"
if FAIL_INSTALL=1 bash "$script"; then exit 1; fi
[[ "$(cat "$target")" == signed ]]
bash "$script"
[[ -L "$target" && -x "$target" ]]
bash "$script"
