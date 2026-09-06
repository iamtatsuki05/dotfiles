#!/usr/bin/env bash

set -euo pipefail
TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
if [[ "$(uname -s)" != Darwin ]]; then
  printf 'SKIP: macOS fixture sandbox canaries require macOS\n'
  exit 0
fi
source "$TEST_DIR/lib/isolated.sh"
fixture="$(mktemp -d "${TMPDIR:-/tmp}/dotfiles-isolation.XXXXXX")"
trap 'rm -rf "$fixture"' EXIT HUP INT TERM
mkdir "$fixture/allowed"
printf '%s\n' keep > "$fixture/outside"
python_bin="${DOTFILES_TEST_PYTHON:-$(command -v python3)}"
# An environment variable must never be enough to bypass the kernel boundary.
DOTFILES_TEST_SANDBOX_ROOT="$fixture/allowed" DOTFILES_TEST_PYTHON=/usr/bin/true \
  run_isolated "$fixture/allowed" "$python_bin" - "$fixture/allowed" "$fixture/outside" <<'PY'
import errno
from pathlib import Path
import socket
import subprocess
import sys

allowed, outside = map(Path, sys.argv[1:])
(allowed / "writable").write_text("ok")
try:
    outside.write_text("changed")
except PermissionError:
    pass
else:
    raise SystemExit("sandbox allowed a write outside the fixture")
try:
    with socket.socket() as sock:
        sock.settimeout(0.2)
        sock.connect(("192.0.2.1", 9))
except OSError as exc:
    if exc.errno not in (errno.EPERM, errno.EACCES):
        raise SystemExit("network failed without a sandbox denial")
else:
    raise SystemExit("sandbox allowed an external network connection")
try:
    subprocess.run(["/usr/bin/sudo", "--version"], capture_output=True)
except PermissionError:
    pass
else:
    raise SystemExit("sandbox allowed real sudo execution")
PY
[[ -f "$fixture/allowed/writable" ]] || {
  printf 'ERROR: fixture isolation canary did not execute\n' >&2
  exit 1
}
[[ "$(cat "$fixture/outside")" == keep ]]
printf 'fixture sandbox canaries passed\n'
