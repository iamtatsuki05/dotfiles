#!/usr/bin/env bash
set -euo pipefail

payload="$(cat -)"
python3 - "$payload" <<'PY'
import json
import re
import sys

payload = json.loads(sys.argv[1])
secret_path = re.compile(
    r"(^|/)(\.env(\..*)?|secrets\.env|credentials\.json|secrets\.json|id_rsa|id_ed25519)$"
    r"|(\.key|\.pem)$"
)
terminal_secret_read = re.compile(
    r"\b(cat|less|more|head|tail|sed|awk|grep|rg|cp|mv|open)\b[^\n;|&]*"
    r"(\.env(\.[^\s;&|]*)?|secrets\.env|credentials\.json|secrets\.json|id_rsa|id_ed25519|\.key|\.pem)"
)


def contains_secret_path(value):
    if isinstance(value, str):
        return bool(secret_path.search(value))
    if isinstance(value, list):
        return any(contains_secret_path(item) for item in value)
    if isinstance(value, dict):
        return any(contains_secret_path(item) for item in value.values())
    return False


tool_name = payload.get("tool_name") or ""
tool_input = payload.get("tool_input") or {}

blocked = contains_secret_path(tool_input)
if tool_name == "terminal":
    command = str(tool_input.get("command", ""))
    blocked = blocked or bool(terminal_secret_read.search(command))

if blocked:
    print(json.dumps({
        "action": "block",
        "message": "Refusing to access secret-like files from Hermes Agent",
    }))
else:
    print("{}")
PY
