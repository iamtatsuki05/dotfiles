#!/usr/bin/env bash

set -euo pipefail

readonly KEYCHAIN_SERVICE="dotfiles.claude-account.setup-token"
readonly CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/claude-account"
readonly PROFILES_FILE="$CONFIG_DIR/profiles"

usage() {
  cat <<'EOF'
Usage:
  claude-account add <profile>
  claude-account list
  claude-account <profile> [claude arguments...]

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
    add|list|help)
      echo "ERROR: reserved profile name: $profile" >&2
      return 2
      ;;
  esac
}

require_keychain() {
  if ! command -v security >/dev/null 2>&1; then
    echo "ERROR: macOS Keychain command not found: security" >&2
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

add_profile() {
  local profile="$1"

  validate_profile "$profile"
  require_keychain

  echo "Paste the setup-token for '$profile' at the macOS Keychain prompt."
  security add-generic-password \
    -U \
    -a "$profile" \
    -s "$KEYCHAIN_SERVICE" \
    -l "Claude Code setup-token ($profile)" \
    -w

  umask 077
  mkdir -p "$CONFIG_DIR"
  chmod 700 "$CONFIG_DIR"
  touch "$PROFILES_FILE"
  if ! grep -Fxq -- "$profile" "$PROFILES_FILE"; then
    printf '%s\n' "$profile" >> "$PROFILES_FILE"
  fi
  chmod 600 "$PROFILES_FILE"

  echo "Saved Claude Code account profile: $profile"
}

list_profiles() {
  local profile
  local state

  require_keychain
  if [[ ! -s "$PROFILES_FILE" ]]; then
    echo "No Claude Code account profiles registered."
    return 0
  fi

  while IFS= read -r profile; do
    [[ -n "$profile" ]] || continue
    if security find-generic-password -a "$profile" -s "$KEYCHAIN_SERVICE" >/dev/null 2>&1; then
      state="ready"
    else
      state="missing token"
    fi
    printf '%s\t%s\n' "$profile" "$state"
  done < "$PROFILES_FILE"
}

run_profile() {
  local profile="$1"
  shift
  local argument
  local auth_status
  local env_name
  local -a auth_env_command=(env)
  local token

  validate_profile "$profile"
  for argument in "$@"; do
    case "$argument" in
      --bare)
        echo "ERROR: --bare does not support setup-token profiles" >&2
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
  check_settings_for_auth_overrides
  require_keychain

  while IFS='=' read -r env_name _; do
    case "$env_name" in
      ANTHROPIC_API_KEY|ANTHROPIC_AUTH_TOKEN|ANTHROPIC_BASE_URL|ANTHROPIC_AWS_*|ANTHROPIC_BEDROCK_*|ANTHROPIC_CUSTOM_HEADERS|ANTHROPIC_FEDERATION_RULE_ID|ANTHROPIC_FOUNDRY_*|ANTHROPIC_GOOGLE_CLOUD_*|ANTHROPIC_ORGANIZATION_ID|ANTHROPIC_PROFILE|ANTHROPIC_UNIX_SOCKET|ANTHROPIC_VERTEX_*|ANTHROPIC_WORKSPACE_ID|AWS_BEARER_TOKEN_BEDROCK|CCR_OAUTH_TOKEN_*|CLAUDE_CODE_API_KEY_*|CLAUDE_CODE_HOST_AUTH_*|CLAUDE_CODE_MANAGED_SETTINGS_*|CLAUDE_CODE_OAUTH_*|CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST|CLAUDE_CODE_SIMPLE|CLAUDE_CODE_USE_ANTHROPIC_AWS|CLAUDE_CODE_USE_ANTHROPIC_GOOGLE_CLOUD|CLAUDE_CODE_USE_BEDROCK|CLAUDE_CODE_USE_FOUNDRY|CLAUDE_CODE_USE_GATEWAY|CLAUDE_CODE_USE_MANTLE|CLAUDE_CODE_USE_VERTEX)
        auth_env_command+=( -u "$env_name" )
        ;;
    esac
  done < <(env)

  case "$-" in
    *x*) set +x ;;
  esac
  if ! token="$(security find-generic-password -a "$profile" -s "$KEYCHAIN_SERVICE" -w 2>/dev/null)"; then
    echo "ERROR: profile not found in macOS Keychain: $profile" >&2
    return 1
  fi
  if [[ -z "$token" ]]; then
    echo "ERROR: setup-token is empty for profile: $profile" >&2
    return 1
  fi

  if ! auth_status="$(
    "${auth_env_command[@]}" \
      CLAUDE_CODE_OAUTH_TOKEN="$token" \
      DISABLE_LOGIN_COMMAND=1 \
      DISABLE_LOGOUT_COMMAND=1 \
      claude auth status --json 2>/dev/null
  )"; then
    echo "ERROR: failed to verify the selected Claude Code account profile" >&2
    return 1
  fi
  if ! printf '%s' "$auth_status" | python3 -c '
import json
import sys

status = json.load(sys.stdin)
valid = (
    status.get("loggedIn") is True
    and status.get("authMethod") == "oauth_token"
    and status.get("apiProvider") == "firstParty"
    and not status.get("apiKeySource")
    and status.get("authTokenSource") in (None, "", "CLAUDE_CODE_OAUTH_TOKEN")
    and status.get("oauthTokenSource") in (None, "", "CLAUDE_CODE_OAUTH_TOKEN")
)
raise SystemExit(0 if valid else 1)
'; then
    echo "ERROR: Claude Code selected an unexpected authentication route; check settings env and apiKeyHelper" >&2
    return 1
  fi

  exec "${auth_env_command[@]}" \
    CLAUDE_CODE_OAUTH_TOKEN="$token" \
    DISABLE_LOGIN_COMMAND=1 \
    DISABLE_LOGOUT_COMMAND=1 \
    claude "$@"
}

main() {
  local command_name="${1:-}"

  case "$command_name" in
    add)
      if [[ $# -ne 2 ]]; then
        usage >&2
        return 2
      fi
      add_profile "$2"
      ;;
    list)
      if [[ $# -ne 1 ]]; then
        usage >&2
        return 2
      fi
      list_profiles
      ;;
    help|-h|--help|"")
      usage
      ;;
    *)
      shift
      run_profile "$command_name" "$@"
      ;;
  esac
}

main "$@"
