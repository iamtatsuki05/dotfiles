#!/usr/bin/env bash

set -euo pipefail

readonly CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/claude-account"
readonly LOGIN_PROFILES_FILE="$CONFIG_DIR/login-profiles.json"
readonly LOGIN_LOCK_FILE="$CONFIG_DIR/full-login.lock"
AUTH_ENV_COMMAND=(env)

usage() {
  cat <<'EOF'
Usage:
  claude-account auth-login <profile>
  claude-account list
  claude-account <profile> [claude arguments...]

The default launch path uses the single full-scope `claude auth login`
credential in macOS Keychain. Run `auth-login` with every Claude process
closed before switching accounts.

Profiles may contain letters, numbers, dots, underscores, and hyphens.
EOF
}

validate_profile() {
  local profile="$1"

  case "$profile" in
    ""|.*|-*|*[!A-Za-z0-9._-]*)
      echo "ERROR: invalid profile name: $profile" >&2
      return 2
      ;;
    auth-login|list|help|add|add-token|token|__auth-login|__run-login)
      echo "ERROR: reserved profile name: $profile" >&2
      return 2
      ;;
  esac
}

run_with_login_lock() {
  local lock_mode="$1"
  shift
  local lock_runner_code

  umask 077
  mkdir -p "$CONFIG_DIR"
  chmod 700 "$CONFIG_DIR"
  lock_runner_code="$(cat <<'PY'
import fcntl
import os
import signal
import subprocess
import sys

lock_path = sys.argv[1]
lock_mode = sys.argv[2]
script = sys.argv[3]
arguments = sys.argv[4:]
descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
operation = fcntl.LOCK_EX if lock_mode == "exclusive" else fcntl.LOCK_SH
try:
    fcntl.flock(descriptor, operation | fcntl.LOCK_NB)
except BlockingIOError:
    print(
        "ERROR: shared login lock is busy; exit managed Claude sessions and retry",
        file=sys.stderr,
    )
    raise SystemExit(1)

environment = os.environ.copy()
environment["CLAUDE_ACCOUNT_LOCK_HELD"] = lock_mode
process = subprocess.Popen([script, *arguments], env=environment)


def forward_signal(signum, _frame):
    if process.poll() is None:
        process.send_signal(signum)


signal.signal(signal.SIGINT, lambda _signum, _frame: None)
signal.signal(signal.SIGTERM, forward_signal)
signal.signal(signal.SIGHUP, forward_signal)
raise SystemExit(process.wait())
PY
)"
  exec python3 -c "$lock_runner_code" "$LOGIN_LOCK_FILE" "$lock_mode" "$0" "$@"
}

require_login_lock() {
  local expected_mode="$1"

  if [[ "${CLAUDE_ACCOUNT_LOCK_HELD:-}" != "$expected_mode" ]]; then
    echo "ERROR: internal claude-account command requires the $expected_mode login lock" >&2
    return 1
  fi
}

check_settings_for_auth_overrides() {
  local claude_config_dir="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"

  python3 - "$claude_config_dir" "$PWD" <<'PY'
import json
import sys
from pathlib import Path


def is_auth_env(name):
    exact = {
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_CUSTOM_HEADERS",
        "ANTHROPIC_FEDERATION_RULE_ID",
        "ANTHROPIC_UNIX_SOCKET",
        "ANTHROPIC_ORGANIZATION_ID",
        "ANTHROPIC_PROFILE",
        "ANTHROPIC_WORKSPACE_ID",
        "AWS_BEARER_TOKEN_BEDROCK",
        "CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST",
        "CLAUDE_CODE_HOST_AUTH_ENV_VAR",
        "CLAUDE_CODE_MANAGED_SETTINGS_PATH",
        "CLAUDE_CODE_SIMPLE",
    }
    prefixes = (
        "ANTHROPIC_AWS_",
        "ANTHROPIC_BEDROCK_",
        "ANTHROPIC_FOUNDRY_",
        "ANTHROPIC_GOOGLE_CLOUD_",
        "ANTHROPIC_VERTEX_",
        "CCR_OAUTH_TOKEN_",
        "CLAUDE_CODE_API_KEY_",
        "CLAUDE_CODE_HOST_AUTH_",
        "CLAUDE_CODE_MANAGED_SETTINGS_",
        "CLAUDE_CODE_OAUTH_",
    )
    provider_selectors = {
        "CLAUDE_CODE_USE_ANTHROPIC_AWS",
        "CLAUDE_CODE_USE_ANTHROPIC_GOOGLE_CLOUD",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_FOUNDRY",
        "CLAUDE_CODE_USE_GATEWAY",
        "CLAUDE_CODE_USE_MANTLE",
        "CLAUDE_CODE_USE_VERTEX",
    }
    return name in exact or name in provider_selectors or name.startswith(prefixes)


claude_config_dir = Path(sys.argv[1]).expanduser()
cwd = Path(sys.argv[2]).resolve()
paths = [claude_config_dir / "settings.json"]
for directory in (cwd, *cwd.parents):
    paths.extend(
        (
            directory / ".claude" / "settings.json",
            directory / ".claude" / "settings.local.json",
        )
    )

for managed_root in (
    Path("/Library/Application Support/ClaudeCode"),
    Path("/etc/claude-code"),
):
    paths.append(managed_root / "managed-settings.json")
    paths.extend(sorted((managed_root / "managed-settings.d").glob("*.json")))

seen = set()
for path in paths:
    try:
        resolved = path.resolve()
    except OSError:
        resolved = path
    if resolved in seen or not path.is_file():
        continue
    seen.add(resolved)
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        print(f"ERROR: cannot validate Claude settings: {path}: {exc}", file=sys.stderr)
        raise SystemExit(1)

    keys = []
    if data.get("apiKeyHelper"):
        keys.append("apiKeyHelper")
    if data.get("policyHelper"):
        keys.append("policyHelper")
    env = data.get("env")
    if isinstance(env, dict):
        keys.extend(name for name in env if is_auth_env(name))
    if keys:
        print(
            f"ERROR: authentication override in Claude settings: {path}: {', '.join(sorted(keys))}",
            file=sys.stderr,
        )
        raise SystemExit(1)
PY
}

validate_claude_arguments() {
  local argument

  for argument in "$@"; do
    case "$argument" in
      --bare)
        echo "ERROR: --bare does not use subscription login" >&2
        return 2
        ;;
      --settings|--settings=*|--setting-sources|--setting-sources=*)
        echo "ERROR: --settings is not supported by claude-account because it can override authentication" >&2
        return 2
        ;;
      --managed-settings|--managed-settings=*)
        echo "ERROR: managed settings flags are not supported by claude-account" >&2
        return 2
        ;;
    esac
  done
}

build_auth_env_command() {
  local env_name

  AUTH_ENV_COMMAND=(env)
  while IFS='=' read -r env_name _; do
    case "$env_name" in
      ANTHROPIC_API_KEY|ANTHROPIC_AUTH_TOKEN|ANTHROPIC_BASE_URL|ANTHROPIC_AWS_*|ANTHROPIC_BEDROCK_*|ANTHROPIC_CUSTOM_HEADERS|ANTHROPIC_FEDERATION_RULE_ID|ANTHROPIC_FOUNDRY_*|ANTHROPIC_GOOGLE_CLOUD_*|ANTHROPIC_ORGANIZATION_ID|ANTHROPIC_PROFILE|ANTHROPIC_UNIX_SOCKET|ANTHROPIC_VERTEX_*|ANTHROPIC_WORKSPACE_ID|AWS_BEARER_TOKEN_BEDROCK|CCR_OAUTH_TOKEN_*|CLAUDE_CODE_API_KEY_*|CLAUDE_CODE_HOST_AUTH_*|CLAUDE_CODE_MANAGED_SETTINGS_*|CLAUDE_CODE_OAUTH_*|CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST|CLAUDE_CODE_SIMPLE|CLAUDE_CODE_USE_ANTHROPIC_AWS|CLAUDE_CODE_USE_ANTHROPIC_GOOGLE_CLOUD|CLAUDE_CODE_USE_BEDROCK|CLAUDE_CODE_USE_FOUNDRY|CLAUDE_CODE_USE_GATEWAY|CLAUDE_CODE_USE_MANTLE|CLAUDE_CODE_USE_VERTEX)
        AUTH_ENV_COMMAND+=( -u "$env_name" )
        ;;
    esac
  done < <(env)
}

full_login_identity() {
  local auth_status

  if ! auth_status="$("${AUTH_ENV_COMMAND[@]}" claude auth status --json 2>/dev/null)"; then
    echo "ERROR: failed to read the shared Claude login" >&2
    return 1
  fi
  printf '%s' "$auth_status" | python3 -c '
import hashlib
import json
import sys

status = json.load(sys.stdin)
email = status.get("email")
organization = status.get("orgId")
subscription = status.get("subscriptionType")
valid = (
    status.get("loggedIn") is True
    and status.get("authMethod") == "claude.ai"
    and status.get("apiProvider") == "firstParty"
    and not status.get("apiKeySource")
    and isinstance(email, str)
    and bool(email.strip())
    and isinstance(organization, str)
    and bool(organization.strip())
    and isinstance(subscription, str)
    and bool(subscription.strip())
)
if not valid:
    raise SystemExit(1)
identity_material = f"{email.strip().lower()}\0{organization.strip()}"
identity = hashlib.sha256(identity_material.encode()).hexdigest()
print(f"{identity}\t{subscription.strip().lower()}")
' || {
    echo "ERROR: shared Claude login is not a full subscription login" >&2
    return 1
  }
}

read_registered_profile() {
  local profile="$1"

  python3 - "$LOGIN_PROFILES_FILE" "$profile" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
profile = sys.argv[2]
if not path.is_file():
    raise SystemExit(1)
data = json.loads(path.read_text())
record = (data.get("profiles") or {}).get(profile)
if not isinstance(record, dict):
    raise SystemExit(1)
identity = record.get("identitySha256")
subscription = record.get("subscriptionType")
if not identity or not subscription:
    raise SystemExit(1)
print(f"{identity}\t{subscription}")
PY
}

write_registered_profile() {
  local profile="$1"
  local identity_sha256="$2"
  local subscription_type="$3"

  umask 077
  mkdir -p "$CONFIG_DIR"
  chmod 700 "$CONFIG_DIR"
  python3 - "$LOGIN_PROFILES_FILE" "$profile" "$identity_sha256" "$subscription_type" <<'PY'
import json
import os
import sys
import tempfile
from pathlib import Path

path = Path(sys.argv[1])
profile = sys.argv[2]
identity = sys.argv[3]
subscription = sys.argv[4]
data = {"version": 1, "profiles": {}}
if path.is_file():
    data = json.loads(path.read_text())
profiles = data.setdefault("profiles", {})
profiles[profile] = {
    "identitySha256": identity,
    "subscriptionType": subscription,
}
descriptor, temporary = tempfile.mkstemp(prefix=".login-profiles.", dir=path.parent)
try:
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        json.dump(data, stream, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
except Exception:
    try:
        os.unlink(temporary)
    except FileNotFoundError:
        pass
    raise
PY
  chmod 600 "$LOGIN_PROFILES_FILE"
}

running_claude_process_count() {
  local processes

  processes="$(pgrep -x claude 2>/dev/null || true)"
  if [[ -z "$processes" ]]; then
    echo 0
  else
    printf '%s\n' "$processes" | awk 'NF { count += 1 } END { print count + 0 }'
  fi
}

auth_login_profile() {
  local profile="$1"
  local process_count
  local existing_info=""
  local existing_identity=""
  local login_info
  local identity_sha256
  local subscription_type

  validate_profile "$profile"
  check_settings_for_auth_overrides
  process_count="$(running_claude_process_count)"
  if (( process_count > 0 )); then
    echo "ERROR: $process_count Claude processes are still running; exit all Claude sessions before switching the shared login" >&2
    return 1
  fi

  if existing_info="$(read_registered_profile "$profile" 2>/dev/null)"; then
    existing_identity="${existing_info%%$'\t'*}"
  fi

  build_auth_env_command
  "${AUTH_ENV_COMMAND[@]}" claude auth login
  if ! login_info="$(full_login_identity)"; then
    return 1
  fi
  identity_sha256="${login_info%%$'\t'*}"
  subscription_type="${login_info#*$'\t'}"
  if [[ -n "$existing_identity" && "$existing_identity" != "$identity_sha256" ]]; then
    echo "ERROR: login identity does not match the registered profile: $profile" >&2
    echo "The shared login changed, but the existing profile mapping was preserved. Re-run auth-login and choose the account originally registered for this profile." >&2
    return 1
  fi

  write_registered_profile "$profile" "$identity_sha256" "$subscription_type"
  echo "Registered full-login profile: $profile ($subscription_type)"
}

run_login_profile() {
  local profile="$1"
  shift
  local registered_info
  local registered_identity
  local login_info
  local login_identity

  validate_profile "$profile"
  validate_claude_arguments "$@"
  check_settings_for_auth_overrides
  if ! registered_info="$(read_registered_profile "$profile" 2>/dev/null)"; then
    echo "ERROR: full-login profile is not registered: $profile" >&2
    echo "Run: claude-account auth-login $profile" >&2
    return 1
  fi
  registered_identity="${registered_info%%$'\t'*}"

  build_auth_env_command
  if ! login_info="$(full_login_identity)"; then
    echo "Run: claude-account auth-login $profile" >&2
    return 1
  fi
  login_identity="${login_info%%$'\t'*}"
  if [[ "$registered_identity" != "$login_identity" ]]; then
    echo "ERROR: shared Claude login does not match profile: $profile" >&2
    echo "Exit all Claude sessions, then run: claude-account auth-login $profile" >&2
    return 1
  fi

  exec "${AUTH_ENV_COMMAND[@]}" \
    DISABLE_LOGIN_COMMAND=1 \
    DISABLE_LOGOUT_COMMAND=1 \
    CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1 \
    claude "$@"
}

list_profiles() {
  local login_info=""
  local current_identity=""

  if [[ ! -f "$LOGIN_PROFILES_FILE" ]]; then
    echo "No full-login profiles registered."
    return 0
  fi
  build_auth_env_command
  if login_info="$(full_login_identity 2>/dev/null)"; then
    current_identity="${login_info%%$'\t'*}"
  fi
  python3 - "$LOGIN_PROFILES_FILE" "$current_identity" <<'PY'
import json
import sys
from pathlib import Path

data = json.loads(Path(sys.argv[1]).read_text())
current = sys.argv[2]
for name, record in sorted((data.get("profiles") or {}).items()):
    state = "current login" if record.get("identitySha256") == current else "registered"
    print(f"{name}\t{state}\t{record.get('subscriptionType', 'unknown')}")
PY
}

main() {
  local command_name="${1:-}"

  case "$command_name" in
    __auth-login)
      require_login_lock exclusive
      if [[ $# -ne 2 ]]; then
        return 2
      fi
      auth_login_profile "$2"
      ;;
    __run-login)
      require_login_lock shared
      if [[ $# -lt 2 ]]; then
        return 2
      fi
      shift
      run_login_profile "$@"
      ;;
    auth-login)
      if [[ $# -ne 2 ]]; then
        usage >&2
        return 2
      fi
      run_with_login_lock exclusive __auth-login "$2"
      ;;
    list)
      if [[ $# -ne 1 ]]; then
        usage >&2
        return 2
      fi
      list_profiles
      ;;
    add|add-token|token)
      echo "ERROR: unknown command: $command_name" >&2
      usage >&2
      return 2
      ;;
    help|-h|--help|"")
      usage
      ;;
    *)
      shift
      run_with_login_lock shared __run-login "$command_name" "$@"
      ;;
  esac
}

main "$@"
