#!/usr/bin/env bash

set -euo pipefail

# Darwin pipe limits can block a large heredoc before the Python reader starts.
exec python3 -I -c '
import runpy
import sys
from pathlib import Path
implementation = Path(sys.argv.pop(1)).resolve().with_suffix(".py")
sys.argv[0] = str(implementation)
runpy.run_path(str(implementation), run_name="__main__")
' "$0" "$@"
