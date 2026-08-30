#!/usr/bin/env zsh

set -euo pipefail

readonly TEST_DIR="$(cd "$(dirname "$0")" && pwd)"
readonly REPO_ROOT="$(cd "$TEST_DIR/.." && pwd)"
readonly MIGRATION_SCRIPT="$REPO_ROOT/scripts/migrate_brew_to_nix.sh"
readonly INSTALL_SCRIPT="$REPO_ROOT/scripts/nix_install.sh"
readonly NIX_PORTABLE_INSTALL_SCRIPT="$REPO_ROOT/scripts/nix_portable_install.sh"
readonly ROOTLESS_NIX_INSTALL_SCRIPT="$REPO_ROOT/scripts/nix_rootless_install.sh"
readonly REMOVE_HOMEBREW_SCRIPT="$REPO_ROOT/scripts/remove_homebrew.sh"
readonly CLEANUP_PACKAGE_CACHES_SCRIPT="$REPO_ROOT/scripts/cleanup_package_caches.sh"
readonly INSTALL_HOMEBREW_SCRIPT="$REPO_ROOT/scripts/install_homebrew.sh"
readonly INSTALL_MAS_APPS_SCRIPT="$REPO_ROOT/scripts/install_mas_apps.sh"
readonly UPDATE_MANAGED_VERSIONS_SCRIPT="$REPO_ROOT/scripts/update_managed_versions.sh"
readonly APPLY_UPDATES_SCRIPT="$REPO_ROOT/scripts/apply_updates.sh"
readonly SETUP_GIT_HOOKS_SCRIPT="$REPO_ROOT/scripts/setup_git_hooks.sh"
readonly MAIN_SCRIPT="$REPO_ROOT/main.sh"
readonly HOMEBREW_LIB="$REPO_ROOT/scripts/lib/homebrew.sh"
readonly HOMEBREW_FALLBACK_LIB="$REPO_ROOT/scripts/lib/homebrew_fallback.sh"
readonly RUNTIME_LIB="$REPO_ROOT/scripts/lib/runtime.sh"
readonly COMMAND_LIB="$REPO_ROOT/scripts/lib/command.sh"
readonly FLAKE_FILE="$REPO_ROOT/flake.nix"
readonly BASHRC_TEMPLATE_FILE="$REPO_ROOT/config/shell/bashrc.tmpl"
readonly BASH_PROFILE_TEMPLATE_FILE="$REPO_ROOT/config/shell/bash_profile.tmpl"
readonly SHELL_COMMON_TEMPLATE_FILE="$REPO_ROOT/config/shell/dotfiles-shell-common.tmpl"
readonly MISE_CONFIG="$REPO_ROOT/config/mise/config.toml"
readonly WAZA_AGENT_EVAL_FILE="$REPO_ROOT/dotfiles/.agent/evals/markdown-docs/eval.yaml"
readonly WAZA_MARKDOWN_DOCS_MODEL_EVAL_FILE="$REPO_ROOT/dotfiles/.agent/evals/markdown-docs/model.yaml"
readonly WAZA_AUTO_DEBUGGER_EVAL_FILE="$REPO_ROOT/dotfiles/.agent/evals/auto-debugger/eval.yaml"
readonly WAZA_AUTO_DEBUGGER_MODEL_EVAL_FILE="$REPO_ROOT/dotfiles/.agent/evals/auto-debugger/model.yaml"
readonly WAZA_GIT_GITHUB_FLOW_EVAL_FILE="$REPO_ROOT/dotfiles/.agent/evals/git-github-flow/eval.yaml"
readonly WAZA_GIT_GITHUB_FLOW_MODEL_EVAL_FILE="$REPO_ROOT/dotfiles/.agent/evals/git-github-flow/model.yaml"
readonly WAZA_SECURITY_CHECK_EVAL_FILE="$REPO_ROOT/dotfiles/.agent/evals/security-check/eval.yaml"
readonly WAZA_SECURITY_CHECK_MODEL_EVAL_FILE="$REPO_ROOT/dotfiles/.agent/evals/security-check/model.yaml"
readonly WAZA_ALL_EVAL_SCRIPT="$REPO_ROOT/scripts/waza_eval_all.sh"
readonly WAZA_MODEL_EVAL_SCRIPT="$REPO_ROOT/scripts/waza_eval_model.sh"
readonly WAZA_CLI_AGENT_EVAL_SCRIPT="$REPO_ROOT/scripts/waza_eval_cli_agent.sh"
readonly WAZA_ALL_EVAL_IMPL="$REPO_ROOT/scripts/agent/waza_eval_all.sh"
readonly WAZA_MODEL_EVAL_IMPL="$REPO_ROOT/scripts/agent/waza_eval_model.sh"
readonly WAZA_CLI_AGENT_EVAL_IMPL="$REPO_ROOT/scripts/agent/waza_eval_cli_agent.sh"
readonly WAZA_EVAL_ROOT="$REPO_ROOT/dotfiles/.agent/evals"
readonly AGENT_README="$REPO_ROOT/dotfiles/.agent/README.md"
readonly AGENT_README_JA="$REPO_ROOT/dotfiles/.agent/README_JA.md"
readonly HOME_MANAGER_MODULE="$REPO_ROOT/config/nix/home-manager/default.nix"
readonly HOME_MANAGER_PACKAGES_MODULE="$REPO_ROOT/config/nix/home-manager/packages.nix"
readonly HOME_MANAGER_ZSH_MODULE="$REPO_ROOT/config/nix/home-manager/zsh.nix"
readonly HOME_MANAGER_NEOVIM_MODULE="$REPO_ROOT/config/nix/home-manager/neovim.nix"
readonly HOME_MANAGER_AUTO_UPDATE_MODULE="$REPO_ROOT/config/nix/home-manager/auto-update.nix"
readonly HOME_MANAGER_SESSION_MODULE="$REPO_ROOT/config/nix/home-manager/session.nix"
readonly DARWIN_MODULE="$REPO_ROOT/config/nix/darwin/default.nix"
readonly DARWIN_BASE_MODULE="$REPO_ROOT/config/nix/darwin/base.nix"
readonly DARWIN_DEFAULTS_MODULE="$REPO_ROOT/config/nix/darwin/defaults.nix"
readonly DARWIN_HOMEBREW_MODULE="$REPO_ROOT/config/nix/darwin/homebrew.nix"
readonly DARWIN_AUTO_UPDATE_MODULE="$REPO_ROOT/config/nix/darwin/auto-update.nix"
readonly NIX_PACKAGE_NAMES_FILE="$REPO_ROOT/config/nix/package-names.nix"
readonly DOTFILES_PACKAGES_FILE="$REPO_ROOT/config/nix/dotfiles-packages.nix"
readonly NIX_GUI_COMMON_PACKAGE_NAMES_FILE="$REPO_ROOT/config/nix/gui-common-package-names.nix"
readonly NIX_GUI_MACOS_PACKAGE_NAMES_FILE="$REPO_ROOT/config/nix/gui-macos-package-names.nix"
readonly NIX_GUI_LINUX_PACKAGE_NAMES_FILE="$REPO_ROOT/config/nix/gui-linux-package-names.nix"
readonly UNMAPPED_HOMEBREW_FILE="$REPO_ROOT/config/nix/unmapped-homebrew.tsv"
readonly HOMEBREW_FALLBACK_FILE="$REPO_ROOT/config/nix/homebrew-fallback.nix"
readonly MAS_APPS_FILE="$REPO_ROOT/config/nix/mas-apps.nix"
readonly MIGRATED_FORMULAE_FILE="$REPO_ROOT/config/nix/migrated-brew-formulae.txt"
readonly MIGRATED_CASKS_FILE="$REPO_ROOT/config/nix/migrated-brew-casks.txt"
readonly MIGRATED_MAS_APPS_FILE="$REPO_ROOT/config/nix/migrated-mas-apps.tsv"
readonly TEST_ZSH_BIN="${DOTFILES_TEST_ZSH_BIN:-/bin/zsh}"

source "$TEST_DIR/lib/assertions.sh"

emit_matrix_result() {
  local line="$1"

  print -r -- "$line"
  if [[ -n "${MATRIX_RESULT_LOG_DIR:-}" ]]; then
    mkdir -p "$MATRIX_RESULT_LOG_DIR"
    print -r -- "$line" >> "$MATRIX_RESULT_LOG_DIR/matrix-results.log"
  fi
}

copy_script_libs() {
  local repo="$1"

  mkdir -p "$repo/scripts/lib"
  cp "$REPO_ROOT/scripts/lib/setup_profile.sh" "$repo/scripts/lib/setup_profile.sh"
  cp "$COMMAND_LIB" "$repo/scripts/lib/command.sh"
  cp "$HOMEBREW_LIB" "$repo/scripts/lib/homebrew.sh"
  cp "$HOMEBREW_FALLBACK_LIB" "$repo/scripts/lib/homebrew_fallback.sh"
  cp "$RUNTIME_LIB" "$repo/scripts/lib/runtime.sh"
}

write_strict_fake_sudo() {
  local bin_dir="$1"

  cat > "$bin_dir/sudo" <<'EOF'
#!/bin/zsh
set -euo pipefail
event_log="${NIX_TEST_EVENT_LOG:-${DOTFILES_TEST_EVENT_LOG:-}}"
fixture_root="${NIX_TEST_FIXTURE_ROOT:?}"
fixture_root="$(cd -P "$fixture_root" && pwd -P)"
event_log_parent="$(cd -P "${event_log:h}" 2>/dev/null && pwd -P)"
[[ "$event_log_parent" == "$fixture_root" || "$event_log_parent" == "$fixture_root"/* ]] || {
  print -u2 -- "rejected fake sudo event log: $event_log"
  exit 125
}

reject_sudo() {
  print -u2 -- "rejected fake sudo command: $*"
  exit 125
}

is_fixture_path() {
  local candidate="$1"
  local candidate_parent

  [[ "$candidate" == /* ]] || return 1
  [[ "$candidate" != *'/../'* && "$candidate" != */.. && "$candidate" != *'/./'* && "$candidate" != */. ]] || return 1
  candidate_parent="$(cd -P "${candidate:h}" 2>/dev/null && pwd -P)" || return 1
  [[ "$candidate_parent" == "$fixture_root" || "$candidate_parent" == "$fixture_root"/* ]]
}

is_allowed_flake_ref() {
  local flake_ref="$1"
  local flake_path="${flake_ref%%#*}"
  local flake_attr="${flake_ref#*#}"

  [[ "$flake_attr" == (aarch64|x86_64)-(darwin|linux)-(cli|full) ]] || return 1
  if [[ "$flake_path" == path:"$fixture_root" || "$flake_path" == "$fixture_root" ]]; then
    return 0
  fi
  [[ "${NIX_TEST_ALLOW_GENERATED_FLAKE:-0}" == 1 \
    && "$flake_path" == path:/private/tmp/dotfiles-flake.<->.<-> ]]
}

if (( $# == 8 )) && [[ "$1" == env && "$2" == HOME=/var/root && "$3" == DOTFILES_USERNAME=* \
  && "$4" == darwin-rebuild && "$5" == switch && "$6" == --impure && "$7" == --flake ]]; then
  flake_ref="$8"
  is_allowed_flake_ref "$flake_ref" || reject_sudo "$*"
  darwin_rebuild="$fixture_root/bin/darwin-rebuild"
  [[ -x "$darwin_rebuild" && -f "$darwin_rebuild" && ! -L "$darwin_rebuild" ]] || reject_sudo "$*"
  print -r -- "sudo:env HOME=/var/root $3 darwin-rebuild switch --impure --flake $flake_ref" >> "$event_log"
  "$darwin_rebuild" switch --impure --flake "$flake_ref"
  exit $?
fi

if (( $# == 14 )) && [[ "$1" == env && "$2" == HOME=/var/root && "$3" == DOTFILES_USERNAME=* \
  && "$5" == --extra-experimental-features \
  && "$6" == "nix-command flakes" && "$7" == run && "$8" == --impure \
  && "${10}" == -- && "${11}" == switch && "${12}" == --impure \
  && "${13}" == --flake ]]; then
  nix_bin="$4"
  [[ "${nix_bin:t}" == nix ]] && is_fixture_path "$nix_bin" \
    && [[ -x "$nix_bin" && -f "$nix_bin" && ! -L "$nix_bin" ]] || reject_sudo "$*"
  [[ "$9" == path:"$fixture_root"#darwin-rebuild || "$9" == "$fixture_root"#darwin-rebuild ]] \
    || reject_sudo "$*"
  is_allowed_flake_ref "${14}" || reject_sudo "$*"
  [[ "${14}" == "${9%#darwin-rebuild}"#(aarch64|x86_64)-(darwin|linux)-(cli|full) ]] \
    || reject_sudo "$*"
  print -r -- "sudo:env HOME=/var/root $3 nix ${5} ${6} ${7} ${8} ${9} -- ${11} ${12} ${13} ${14}" >> "$event_log"
  "$nix_bin" "$5" "$6" "$7" "$8" "$9" "${10}" "${11}" "${12}" "${13}" "${14}"
  exit $?
fi

if (( $# == 3 )) && [[ "$1" == mv ]] && is_fixture_path "$2" && is_fixture_path "$3" \
  && [[ "$3" == "$2.before-nix-darwin" ]] && [[ ! -L "$2" && ! -L "$3" ]]; then
  mv_bin="/bin/mv"
  [[ -x "$mv_bin" ]] || mv_bin="/usr/bin/mv"
  [[ -x "$mv_bin" ]] || reject_sudo "$*"
  print -r -- "sudo:mv $2 $3" >> "$event_log"
  "$mv_bin" -- "$2" "$3"
  exit $?
fi

reject_sudo "$*"
EOF
  chmod +x "$bin_dir/sudo"
}

write_strict_fake_find() {
  local bin_dir="$1"

  cat > "$bin_dir/find" <<'EOF'
#!/bin/zsh
set -euo pipefail
event_log="${NIX_TEST_EVENT_LOG:-${DOTFILES_TEST_EVENT_LOG:-}}"
forbidden_home="${NIX_TEST_FORBIDDEN_HOME:?}"
forbidden_config="${NIX_TEST_FORBIDDEN_XDG_CONFIG:?}"
event_log_parent="$(cd -P "${event_log:h}" 2>/dev/null && pwd -P)"
fixture_root="${NIX_TEST_FIXTURE_ROOT:?}"
fixture_root="$(cd -P "$fixture_root" && pwd -P)"
[[ "$event_log_parent" == "$fixture_root" || "$event_log_parent" == "$fixture_root"/* ]] || {
  print -u2 -- "rejected fake find event log: $event_log"
  exit 125
}

candidate="${1:-}"
[[ "$candidate" == "$fixture_root" || "$candidate" == "$fixture_root"/* \
  || ( "$candidate" != "$forbidden_home" && "$candidate" != "$forbidden_home"/* \
  && "$candidate" != "$forbidden_config" && "$candidate" != "$forbidden_config"/* ) ]] || {
  print -u2 -- "rejected host HOME/config find path: $candidate"
  exit 124
}

find_bin="/usr/bin/find"
[[ -x "$find_bin" ]] || find_bin="/bin/find"
[[ -x "$find_bin" ]] || {
  print -u2 -- 'no system find available for fixture'
  exit 125
}
print -r -- "find:$candidate" >> "$event_log"
exec "$find_bin" "$@"
EOF
  chmod +x "$bin_dir/find"
}

write_strict_fake_bash() {
  local bin_dir="$1"

  cat > "$bin_dir/bash" <<'EOF'
#!/bin/zsh
set -euo pipefail
event_log="${NIX_TEST_EVENT_LOG:-${DOTFILES_TEST_EVENT_LOG:-}}"
target_bash="${NIX_TEST_TARGET_BASH:-${DOTFILES_TEST_TARGET_BASH:-}}"
fixture_root="${NIX_TEST_FIXTURE_ROOT:?}"
fixture_root="$(cd -P "$fixture_root" && pwd -P)"
allowed_script="${NIX_TEST_ALLOWED_BASH_SCRIPT:?}"
event_log_parent="$(cd -P "${event_log:h}" 2>/dev/null && pwd -P)"
[[ "$event_log_parent" == "$fixture_root" || "$event_log_parent" == "$fixture_root"/* ]] || {
  print -u2 -- "rejected fake bash event log: $event_log"
  exit 126
}
[[ "$target_bash" == /* && "${target_bash:t}" == bash && -x "$target_bash" && ! -d "$target_bash" ]] || {
  print -u2 -- "rejected fake bash target: $target_bash"
  exit 126
}
(( $# >= 1 )) || {
  print -u2 -- 'rejected fake bash invocation without a script'
  exit 126
}
script_path="$1"
shift
[[ "$script_path" == "$allowed_script" ]] || {
  print -u2 -- "rejected fake bash script: $script_path"
  exit 126
}
[[ "$script_path" == /* && "$script_path" != *'/../'* && "$script_path" != */.. \
  && "$script_path" != *'/./'* && "$script_path" != */. ]] || exit 126
script_parent="$(cd -P "${script_path:h}" 2>/dev/null && pwd -P)" || exit 126
[[ "$script_parent" == "$fixture_root" || "$script_parent" == "$fixture_root"/* ]] || exit 126
resolve_existing_path() {
  local candidate="$1"
  local candidate_dir
  local link_target

  while [[ -L "$candidate" ]]; do
    candidate_dir="$(cd -P "${candidate:h}" 2>/dev/null && pwd -P)" || return 1
    link_target="$(readlink "$candidate")" || return 1
    if [[ "$link_target" == /* ]]; then
      candidate="$link_target"
    else
      candidate="$candidate_dir/$link_target"
    fi
  done
  candidate_dir="$(cd -P "${candidate:h}" 2>/dev/null && pwd -P)" || return 1
  print -r -- "$candidate_dir/${candidate:t}"
}
resolved_script="$(resolve_existing_path "$script_path")" || exit 126
[[ "$resolved_script" == "$fixture_root"/* && -f "$resolved_script" ]] || exit 126

case "$script_path" in
  */nix_install.sh)
    case "$#" in
      1) [[ "$1" == --help ]] || exit 126; print -r -- "bash:$script_path --help" >> "$event_log"; exec "$target_bash" "$script_path" --help ;;
      2) [[ "$1" == --profile && ( "$2" == cli || "$2" == full ) ]] || exit 126; print -r -- "bash:$script_path --profile $2" >> "$event_log"; exec "$target_bash" "$script_path" --profile "$2" ;;
      3) [[ "$1" == --profile && "$2" == full && "$3" == --with-gui-apps ]] || exit 126; print -r -- "bash:$script_path --profile full --with-gui-apps" >> "$event_log"; exec "$target_bash" "$script_path" --profile full --with-gui-apps ;;
      *) exit 126 ;;
    esac
    ;;
  */setup_hermes_agent.sh)
    [[ "$#" == 1 && "$1" == --update-only ]] || exit 126
    print -r -- "bash:$script_path --update-only" >> "$event_log"
    exec "$target_bash" "$script_path" --update-only
    ;;
  */remove_homebrew.sh)
    [[ "$#" == 2 && "$1" == --apply && "$2" == --confirm-nix-ready ]] || exit 126
    print -r -- "bash:$script_path --apply --confirm-nix-ready" >> "$event_log"
    exec "$target_bash" "$script_path" --apply --confirm-nix-ready
    ;;
  *)
    print -u2 -- "rejected fake bash script: $script_path"
    exit 126
    ;;
esac
EOF
  chmod +x "$bin_dir/bash"
}

is_test_macos() {
  [[ "$OSTYPE" == darwin* ]]
}

bash_major_version() {
  local bash_bin="$1"

  "$bash_bin" -c 'printf "%s\n" "${BASH_VERSINFO[0]}"'
}

select_bash_for_major() {
  local expected_major="$1"
  local candidate
  local actual_major
  local -a candidates=()

  if [[ "$expected_major" == "3" ]]; then
    if [[ -n "${BASH32_BIN:-}" ]]; then
      candidates=("$BASH32_BIN")
    elif is_test_macos; then
      candidates=(/bin/bash)
    fi
  else
    if [[ -n "${BASH5_BIN:-}" ]]; then
      candidates=("$BASH5_BIN")
    else
      candidates=(/run/current-system/sw/bin/bash /opt/homebrew/bin/bash /usr/local/bin/bash /bin/bash)
    fi
  fi

  for candidate in "${candidates[@]}"; do
    [[ "$candidate" == /* && -x "$candidate" ]] || continue
    actual_major="$(bash_major_version "$candidate" 2>/dev/null)" || continue
    if [[ "$actual_major" == "$expected_major" ]]; then
      REPLY="$candidate"
      return 0
    fi
  done

  return 1
}

matrix_os_name() {
  if is_test_macos; then
    print -r -- "macos"
  else
    print -r -- "linux"
  fi
}

emit_required_bash_skip() {
  local target="$1"
  local expected_major="$2"

  emit_matrix_result "MATRIX_RESULT|os=$(matrix_os_name)|shell=bash${expected_major}|target=${target}|status=SKIP|requirement=required|reason=bash${expected_major}-unavailable"
}

emit_not_applicable_skip() {
  local target="$1"

  emit_matrix_result "MATRIX_RESULT|os=$(matrix_os_name)|shell=any|target=${target}|status=SKIP|requirement=not-applicable|reason=macos-only"
}

skip_unless_macos() {
  local test_name="$1"

  if is_test_macos; then
    return 0
  fi

  echo "SKIP: $test_name requires macOS"
  return 1
}

create_fixture_repo() {
  local repo="$1"

  mkdir -p "$repo/config/nix" "$repo/input"

  cat > "$repo/config/nix/brew-to-nix.tsv" <<'EOF'
# brew	nix
git	git
gnu-sed	gnused
mise	dotfiles.mise
EOF

  cat > "$repo/config/nix/mise-managed-homebrew.tsv" <<'EOF'
# kind	name	mise tool
brew	claude-code	claude-code
brew	codex	codex
brew	herdr	github:ogulcancelik/herdr
brew	hermes-agent	external:scripts/setup_hermes_agent.sh
brew	opencode	opencode
cask	claude-code@latest	claude-code
cask	codex	codex
cask	cursor-cli	cursor-agent
EOF

  cat > "$repo/config/nix/cask-to-nix.tsv" <<'EOF'
# cask	nix	nix scope
slack	slack	common
alacritty	alacritty	common
ghostty	ghostty	linux
rancher	dotfiles.rancher-desktop	macos
raycast	raycast	macos
EOF

  cat > "$repo/config/nix/mas-to-nix.tsv" <<'EOF'
# mas app name	app store id	nix	nix scope
EOF

  cat > "$repo/config/nix/mas-to-cask.tsv" <<'EOF'
# mas app name	app store id	cask
Affinity Photo	824183456	affinity-photo
EOF

  cat > "$repo/input/Brewfile" <<'EOF'
tap "example/tap"
brew "git"
brew "gnu-sed"
brew "mise"
brew "claude-code"
brew "codex"
brew "herdr"
brew "hermes-agent"
brew "opencode"
brew "private-tool"
cask "slack"
cask "alacritty"
cask "claude-code@latest"
cask "codex"
cask "cursor-cli"
cask "ghostty"
cask "rancher"
cask "raycast"
cask "private-app"
mas "Affinity Photo", id: 824183456
mas "Xcode", id: 497799835
vscode "example.extension"
uv "claude-monitor"
EOF
}

test_brewfile_migration_writes_nix_lists_and_unmapped_report() {
  local repo
  local trusted_casks
  make_temp_dir
  repo="$REPLY"
  create_fixture_repo "$repo"

  cat > "$repo/config/nix/homebrew-fallback.nix" <<'EOF'
{
  taps = [
  ];

  brews = [
  ];

  casks = [
  ];

  trustedCasks = [
    "private-app"
    "removed-app"
  ];

  vscode = [
  ];

  unsupportedUvPackages = [
  ];
}
EOF

  "$TEST_ZSH_BIN" "$MIGRATION_SCRIPT" \
    --repo-root "$repo" \
    --brewfile "$repo/input/Brewfile" \
    --apply >/dev/null

  assert_contains "$repo/config/nix/package-names.nix" '"git"'
  assert_contains "$repo/config/nix/package-names.nix" '"gnused"'
  assert_contains "$repo/config/nix/package-names.nix" '"dotfiles.mise"'
  assert_not_contains "$repo/config/nix/package-names.nix" '"gemini-cli"'
  assert_not_contains "$repo/config/nix/package-names.nix" '"claude-code"'
  assert_not_contains "$repo/config/nix/package-names.nix" '"codex"'
  assert_not_contains "$repo/config/nix/package-names.nix" '"herdr"'
  assert_not_contains "$repo/config/nix/package-names.nix" '"hermes-agent"'
  assert_not_contains "$repo/config/nix/package-names.nix" '"opencode"'
  assert_contains "$repo/config/nix/gui-common-package-names.nix" '"slack"'
  assert_contains "$repo/config/nix/gui-common-package-names.nix" '"alacritty"'
  assert_not_contains "$repo/config/nix/gui-common-package-names.nix" '"copilot-cli"'
  assert_not_contains "$repo/config/nix/gui-common-package-names.nix" '"cursor-cli"'
  assert_not_contains "$repo/config/nix/gui-common-package-names.nix" '"claude-code"'
  assert_not_contains "$repo/config/nix/gui-common-package-names.nix" '"codex"'
  assert_contains "$repo/config/nix/gui-linux-package-names.nix" '"ghostty"'
  assert_contains "$repo/config/nix/gui-macos-package-names.nix" '"raycast"'
  assert_contains "$repo/config/nix/gui-macos-package-names.nix" '"dotfiles.rancher-desktop"'
  assert_contains "$repo/config/nix/migrated-brew-formulae.txt" "gnu-sed"
  assert_not_contains "$repo/config/nix/migrated-brew-formulae.txt" "gemini-cli"
  assert_not_contains "$repo/config/nix/migrated-brew-formulae.txt" "claude-code"
  assert_not_contains "$repo/config/nix/migrated-brew-formulae.txt" "codex"
  assert_not_contains "$repo/config/nix/migrated-brew-formulae.txt" "herdr"
  assert_contains "$repo/config/nix/migrated-brew-casks.txt" "slack"
  assert_contains "$repo/config/nix/migrated-brew-casks.txt" "rancher"
  assert_not_contains "$repo/config/nix/migrated-brew-casks.txt" "claude-code@latest"
  assert_not_contains "$repo/config/nix/migrated-brew-casks.txt" "codex"
  assert_not_contains "$repo/config/nix/migrated-brew-casks.txt" "cursor-cli"
  assert_contains "$repo/config/nix/unmapped-homebrew.tsv" $'brew	claude-code	managed-by-mise:claude-code'
  assert_contains "$repo/config/nix/unmapped-homebrew.tsv" $'brew	codex	managed-by-mise:codex'
  assert_contains "$repo/config/nix/unmapped-homebrew.tsv" $'brew	herdr	managed-by-mise:github:ogulcancelik/herdr'
  assert_contains "$repo/config/nix/unmapped-homebrew.tsv" $'brew	hermes-agent	managed-externally:scripts/setup_hermes_agent.sh'
  assert_not_contains "$repo/config/nix/homebrew-fallback.nix" '"hermes-agent"'
  assert_contains "$repo/config/nix/unmapped-homebrew.tsv" $'brew	opencode	managed-by-mise:opencode'
  assert_contains "$repo/config/nix/unmapped-homebrew.tsv" $'cask	claude-code@latest	managed-by-mise:claude-code'
  assert_contains "$repo/config/nix/unmapped-homebrew.tsv" $'cask	codex	managed-by-mise:codex'
  assert_contains "$repo/config/nix/unmapped-homebrew.tsv" $'cask	cursor-cli	managed-by-mise:cursor-agent'
  assert_contains "$repo/config/nix/unmapped-homebrew.tsv" $'brew	private-tool'
  assert_contains "$repo/config/nix/unmapped-homebrew.tsv" $'cask	private-app'
  assert_not_contains "$repo/config/nix/unmapped-homebrew.tsv" $'cask	rancher	'
  assert_contains "$repo/config/nix/unmapped-homebrew.tsv" $'vscode	example.extension'
  assert_contains "$repo/config/nix/unmapped-homebrew.tsv" $'uv	claude-monitor'
  assert_contains "$repo/config/nix/homebrew-fallback.nix" '"example/tap"'
  assert_contains "$repo/config/nix/homebrew-fallback.nix" '"private-tool"'
  assert_not_contains "$repo/config/nix/homebrew-fallback.nix" '"gemini-cli"'
  assert_not_contains "$repo/config/nix/homebrew-fallback.nix" '"claude-code"'
  assert_not_contains "$repo/config/nix/homebrew-fallback.nix" '"claude-code@latest"'
  assert_not_contains "$repo/config/nix/homebrew-fallback.nix" '"codex"'
  assert_contains "$repo/config/nix/homebrew-fallback.nix" '"ghostty"'
  assert_contains "$repo/config/nix/homebrew-fallback.nix" '"private-app"'
  assert_not_contains "$repo/config/nix/homebrew-fallback.nix" '"rancher"'
  assert_contains "$repo/config/nix/homebrew-fallback.nix" '"affinity-photo"'
  assert_contains "$repo/config/nix/homebrew-fallback.nix" 'trustedCasks = ['
  assert_not_contains "$repo/config/nix/homebrew-fallback.nix" '"removed-app"'
  trusted_casks="$(awk '/trustedCasks = \[/ { in_section = 1; next } in_section && /\];/ { in_section = 0 } in_section { print }' "$repo/config/nix/homebrew-fallback.nix")"
  assert_contains_text "$trusted_casks" '"private-app"'
  assert_not_contains_text "$trusted_casks" '"removed-app"'
  assert_contains "$repo/config/nix/homebrew-fallback.nix" '"example.extension"'
  assert_contains "$repo/config/nix/homebrew-fallback.nix" '"claude-monitor"'
  assert_contains "$repo/config/nix/mas-apps.nix" '"Xcode" = 497799835;'
  assert_not_contains "$repo/config/nix/mas-apps.nix" 'Affinity Photo'
  assert_contains "$repo/config/nix/migrated-mas-apps.tsv" $'Affinity Photo	brew	affinity-photo'
  assert_not_exists "$repo/config/homebrew/fallback.Brewfile"
  assert_not_exists "$repo/config/homebrew/macos-casks.Brewfile"

  rm -rf "$repo"
}

test_brewfile_migration_dry_run_does_not_write_outputs() {
  local repo
  local output
  make_temp_dir
  repo="$REPLY"
  output="$repo/dry-run.log"
  create_fixture_repo "$repo"

  "$TEST_ZSH_BIN" "$MIGRATION_SCRIPT" \
    --repo-root "$repo" \
    --brewfile "$repo/input/Brewfile" \
    --dry-run > "$output"

  assert_contains "$output" "DRY-RUN"
  assert_contains "$output" "nix packages"
  assert_not_exists "$repo/config/nix/package-names.nix"
  assert_not_exists "$repo/config/nix/gui-common-package-names.nix"
  assert_not_exists "$repo/config/nix/gui-macos-package-names.nix"
  assert_not_exists "$repo/config/nix/gui-linux-package-names.nix"
  assert_not_exists "$repo/config/nix/unmapped-homebrew.tsv"
  assert_not_exists "$repo/config/nix/homebrew-fallback.nix"
  assert_not_exists "$repo/config/nix/mas-apps.nix"
  assert_not_exists "$repo/config/nix/migrated-mas-apps.tsv"

  rm -rf "$repo"
}

test_repository_migration_moves_available_formulae_and_gui_apps_to_nix() {
  local nix_attr
  local cask
  local cli_attrs=(
    "agent-browser"
    "emacs-nox.pkgs.cask"
    "ghq"
    "gws"
    "dotfiles.e2b"
    "dotfiles.displayplacer"
    "dotfiles.mactop"
    "dotfiles.magika-cli"
    "dotfiles.mise"
    "dotfiles.z"
    "marp-cli"
    "libossp_uuid"
    "wireshark-cli"
  )
  local common_gui_attrs=(
    "_1password-cli"
    "alacritty"
    "firefox"
    "google-chrome"
    "slack"
    "vscode"
  )
  local macos_gui_attrs=(
    "alt-tab-macos"
    "betterdisplay"
    "daisydisk"
    "dotfiles.rancher-desktop"
    "iterm2"
    "mas"
    "raycast"
    "rectangle-pro"
  )
  local linux_gui_attrs=(
    "android-studio"
    "freefilesync"
    "ghostty"
    "pcloud"
    "vlc"
    "zed-editor"
  )
  local migrated_casks=(
    "1password-cli"
    "alacritty"
    "firefox"
    "google-chrome"
    "slack"
    "visual-studio-code"
  )

  for nix_attr in "${cli_attrs[@]}"; do
    assert_contains "$NIX_PACKAGE_NAMES_FILE" "\"$nix_attr\""
  done

  assert_not_contains "$NIX_PACKAGE_NAMES_FILE" '"codex"'
  assert_not_contains "$NIX_PACKAGE_NAMES_FILE" '"gemini-cli"'
  assert_not_contains "$NIX_GUI_COMMON_PACKAGE_NAMES_FILE" '"claude-code"'
  assert_not_contains "$NIX_GUI_MACOS_PACKAGE_NAMES_FILE" '"karabiner-elements"'
  assert_contains "$REPO_ROOT/config/nix/cask-to-nix.tsv" $'firefox\tfirefox\tcommon'
  assert_contains "$REPO_ROOT/config/nix/cask-to-nix.tsv" $'rancher\tdotfiles.rancher-desktop\tmacos'
  assert_contains "$REPO_ROOT/config/nix/cask-to-nix.tsv" $'zed\tzed-editor\tlinux'

  for nix_attr in "${common_gui_attrs[@]}"; do
    assert_contains "$NIX_GUI_COMMON_PACKAGE_NAMES_FILE" "\"$nix_attr\""
  done

  for nix_attr in "${macos_gui_attrs[@]}"; do
    assert_contains "$NIX_GUI_MACOS_PACKAGE_NAMES_FILE" "\"$nix_attr\""
  done

  for nix_attr in "${linux_gui_attrs[@]}"; do
    assert_contains "$NIX_GUI_LINUX_PACKAGE_NAMES_FILE" "\"$nix_attr\""
  done

  for cask in "${migrated_casks[@]}"; do
    assert_contains "$MIGRATED_CASKS_FILE" "$cask"
  done

  assert_contains "$UNMAPPED_HOMEBREW_FILE" $'cask	affinity'
  assert_not_contains "$UNMAPPED_HOMEBREW_FILE" $'cask	yoink'
  assert_contains "$UNMAPPED_HOMEBREW_FILE" $'vscode	adpyke.codesnap'
  assert_contains "$MIGRATED_FORMULAE_FILE" "mise"
  assert_not_contains "$MIGRATED_FORMULAE_FILE" "gemini-cli"
  assert_not_contains "$MIGRATED_CASKS_FILE" "claude-code@latest"
  assert_not_contains "$MIGRATED_CASKS_FILE" "codex"
  assert_not_contains "$MIGRATED_CASKS_FILE" "zed"
  assert_contains "$UNMAPPED_HOMEBREW_FILE" $'cask\tkarabiner-elements\trequires-macos-pkg-and-background-services'
  assert_not_contains "$UNMAPPED_HOMEBREW_FILE" $'cask\trancher\t'
  assert_contains "$UNMAPPED_HOMEBREW_FILE" $'cask	claude-code@latest	managed-by-mise:claude-code'
  assert_contains "$UNMAPPED_HOMEBREW_FILE" $'cask	codex	managed-by-mise:codex'
  assert_contains "$UNMAPPED_HOMEBREW_FILE" $'cask	zed	nix-package-is-linux-only'
  assert_contains "$UNMAPPED_HOMEBREW_FILE" $'tap	stablyai/orca	homebrew-tap-not-managed-by-nix'
  assert_contains "$UNMAPPED_HOMEBREW_FILE" $'cask	stablyai/orca/orca	no-nixpkg-mapping'
  assert_contains "$UNMAPPED_HOMEBREW_FILE" $'brew	claude-code	managed-by-mise:claude-code'
  assert_contains "$UNMAPPED_HOMEBREW_FILE" $'brew	codex	managed-by-mise:codex'
  assert_contains "$MIGRATED_MAS_APPS_FILE" $'Alfred	nix	alfred'
  assert_contains "$MIGRATED_MAS_APPS_FILE" $'Affinity Photo	brew	affinity-photo'
  assert_contains "$HOMEBREW_FALLBACK_FILE" 'taps = ['
  assert_contains "$HOMEBREW_FALLBACK_FILE" '"cloudflare/cloudflare"'
  assert_contains "$HOMEBREW_FALLBACK_FILE" '"stablyai/orca"'
  assert_contains "$HOMEBREW_FALLBACK_FILE" 'casks = ['
  assert_contains "$HOMEBREW_FALLBACK_FILE" '"affinity"'
  assert_contains "$HOMEBREW_FALLBACK_FILE" '"affinity-photo"'
  assert_not_contains "$HOMEBREW_FALLBACK_FILE" '"background-music"'
  assert_contains "$HOMEBREW_FALLBACK_FILE" '"ghostty"'
  assert_contains "$HOMEBREW_FALLBACK_FILE" '"karabiner-elements"'
  assert_contains "$HOMEBREW_FALLBACK_FILE" '"docker-desktop"'
  assert_not_contains "$HOMEBREW_FALLBACK_FILE" '"rancher"'
  assert_not_contains "$HOMEBREW_FALLBACK_FILE" '"messenger"'
  assert_contains "$HOMEBREW_FALLBACK_FILE" '"tailscale-app"'
  assert_not_contains "$HOMEBREW_FALLBACK_FILE" '"yoink"'
  assert_contains "$HOMEBREW_FALLBACK_FILE" '"zed"'
  assert_contains "$HOMEBREW_FALLBACK_FILE" '"stablyai/orca/orca"'
  assert_contains "$HOMEBREW_FALLBACK_FILE" 'trustedCasks = ['
  assert_contains "$HOMEBREW_FALLBACK_FILE" '"grishka/grishka/neardrop"'
  assert_contains "$HOMEBREW_FALLBACK_FILE" '"lyraphase/pcloud/pcloud-drive"'
  assert_contains "$HOMEBREW_FALLBACK_FILE" '"stablyai/orca/orca"'
  assert_contains "$HOMEBREW_FALLBACK_FILE" 'vscode = ['
  assert_contains "$HOMEBREW_FALLBACK_FILE" '"adpyke.codesnap"'
  assert_contains "$HOMEBREW_FALLBACK_FILE" 'unsupportedUvPackages = ['
  assert_contains "$HOMEBREW_FALLBACK_FILE" '"claude-monitor"'
  assert_contains "$DOTFILES_PACKAGES_FILE" 'pname = "rancher-desktop"'
  assert_contains "$DOTFILES_PACKAGES_FILE" 'Rancher.Desktop-${version}-mac.aarch64.zip'
  assert_contains "$DOTFILES_PACKAGES_FILE" 'Rancher.Desktop-${version}-mac.x86_64.zip'
  assert_file "$MAS_APPS_FILE"
  assert_not_contains "$MAS_APPS_FILE" '"Messenger"'
  assert_contains "$MAS_APPS_FILE" '"Xcode" = 497799835;'
  assert_not_contains "$MAS_APPS_FILE" '"Alfred"'
}

test_waza_is_integrated_for_agent_skill_evaluations() {
  assert_contains "$NIX_PACKAGE_NAMES_FILE" '"dotfiles.waza"'
  assert_contains "$DOTFILES_PACKAGES_FILE" 'pname = "waza"'
  assert_contains "$DOTFILES_PACKAGES_FILE" 'https://github.com/microsoft/waza/releases/download'
  assert_contains "$DOTFILES_PACKAGES_FILE" 'mainProgram = "waza"'
  assert_contains "$FLAKE_FILE" 'waza = dotfilesPackages.waza'
  assert_contains "$MISE_CONFIG" '[tasks.waza-check]'
  assert_contains "$MISE_CONFIG" '[tasks.waza-eval]'
  assert_contains "$MISE_CONFIG" '[tasks.waza-eval-all]'
  assert_contains "$MISE_CONFIG" '[tasks.waza-eval-model]'
  assert_not_contains "$MISE_CONFIG" '[tasks.waza-eval-codex]'
  assert_not_contains "$MISE_CONFIG" '[tasks.waza-eval-claude]'
  assert_not_contains "$MISE_CONFIG" '[tasks.waza-eval-gemini]'
  assert_not_contains "$MISE_CONFIG" '[tasks.waza-eval-copilot]'
  assert_not_contains "$MISE_CONFIG" '[tasks.waza-eval-devin]'
  assert_not_contains "$MISE_CONFIG" '[tasks.waza-eval-cursor]'
  assert_not_contains "$MISE_CONFIG" '[tasks.waza-eval-opencode]'
  assert_not_contains "$MISE_CONFIG" '[tasks.waza-eval-hermes]'
  assert_not_contains "$MISE_CONFIG" '[tasks.waza-eval-openclaw]'
  assert_not_contains "$MISE_CONFIG" '[tasks.waza-eval-agent-swarm]'
  assert_not_contains "$MISE_CONFIG" '[tasks.waza-eval-cli-agents]'
  assert_contains "$AGENT_README" 'mise run waza-eval-model -- --agent all --dry-run'
  assert_contains "$AGENT_README_JA" 'mise run waza-eval-model -- --agent all --dry-run'
  assert_contains "$AGENT_README" 'mise run waza-eval-model -- --agent openclaw --allow'
  assert_contains "$AGENT_README_JA" 'mise run waza-eval-model -- --agent openclaw --allow'
  assert_not_contains "$AGENT_README" 'mise run waza-eval-model -- --agent agent-swarm --allow'
  assert_not_contains "$AGENT_README_JA" 'mise run waza-eval-model -- --agent agent-swarm --allow'
  assert_not_contains "$AGENT_README" 'waza-eval-cli-agents'
  assert_not_contains "$AGENT_README_JA" 'waza-eval-cli-agents'
  assert_not_contains "$AGENT_README" 'waza-eval-codex'
  assert_not_contains "$AGENT_README_JA" 'waza-eval-codex'
  assert_contains "$MISE_CONFIG" '[tasks.waza-dashboard]'
  assert_contains "$MISE_CONFIG" 'nix run path:.#waza -- run'
  assert_contains "$WAZA_AGENT_EVAL_FILE" 'markdown-docs-eval'
  assert_contains "$WAZA_AGENT_EVAL_FILE" 'executor: mock'
  assert_contains "$WAZA_AUTO_DEBUGGER_EVAL_FILE" 'auto-debugger-eval'
  assert_contains "$WAZA_MARKDOWN_DOCS_MODEL_EVAL_FILE" 'duplicated_headings_detected'
  assert_contains "$WAZA_AUTO_DEBUGGER_MODEL_EVAL_FILE" 'string_concatenation_detected'
  assert_contains "$WAZA_GIT_GITHUB_FLOW_EVAL_FILE" 'git-github-flow-eval'
  assert_contains "$WAZA_SECURITY_CHECK_EVAL_FILE" 'security-check-eval'
  assert_contains "$WAZA_GIT_GITHUB_FLOW_MODEL_EVAL_FILE" 'executor: copilot-sdk'
  assert_contains "$WAZA_GIT_GITHUB_FLOW_MODEL_EVAL_FILE" 'force_push_requires_authorization'
  assert_contains "$WAZA_GIT_GITHUB_FLOW_MODEL_EVAL_FILE" 'empty_assignee_is_repaired'
  assert_contains "$WAZA_GIT_GITHUB_FLOW_MODEL_EVAL_FILE" 'pr_starts_as_draft'
  assert_contains "$WAZA_GIT_GITHUB_FLOW_MODEL_EVAL_FILE" 'ready_waits_for_checks'
  assert_contains "$WAZA_SECURITY_CHECK_MODEL_EVAL_FILE" 'executor: copilot-sdk'
  assert_contains "$WAZA_SECURITY_CHECK_MODEL_EVAL_FILE" 'sql_injection_detected'
  assert_executable "$WAZA_MODEL_EVAL_SCRIPT"
  assert_executable "$WAZA_CLI_AGENT_EVAL_SCRIPT"
  assert_executable "$WAZA_ALL_EVAL_IMPL"
  assert_executable "$WAZA_MODEL_EVAL_IMPL"
  assert_executable "$WAZA_CLI_AGENT_EVAL_IMPL"
  assert_contains "$WAZA_ALL_EVAL_SCRIPT" 'agent/waza_eval_all.sh'
  assert_contains "$WAZA_MODEL_EVAL_SCRIPT" 'agent/waza_eval_model.sh'
  assert_contains "$WAZA_CLI_AGENT_EVAL_SCRIPT" 'agent/waza_eval_cli_agent.sh'
  assert_contains "$WAZA_MODEL_EVAL_IMPL" 'DEFAULT_AGENT="codex"'
  assert_contains "$WAZA_MODEL_EVAL_IMPL" '--agent AGENT'
  assert_contains "$WAZA_MODEL_EVAL_IMPL" '--model AGENT'
  assert_contains "$WAZA_MODEL_EVAL_IMPL" 'zsh scripts/waza_eval_model.sh --allow'
  assert_contains "$WAZA_MODEL_EVAL_IMPL" 'waza_eval_cli_agent.sh" "$agent"'
  assert_contains "$WAZA_ALL_EVAL_IMPL" 'if [[ -d "$context_dir" ]]'
  assert_contains "$WAZA_CLI_AGENT_EVAL_IMPL" 'CLI agent evals require explicit --allow'
  assert_contains "$WAZA_CLI_AGENT_EVAL_IMPL" 'codex exec -C'
  assert_contains "$WAZA_CLI_AGENT_EVAL_IMPL" 'claude -p'
  assert_contains "$WAZA_CLI_AGENT_EVAL_IMPL" 'agy chat --mode agent'
  assert_contains "$WAZA_CLI_AGENT_EVAL_IMPL" 'copilot'
  assert_contains "$WAZA_CLI_AGENT_EVAL_IMPL" 'devin'
  assert_contains "$WAZA_CLI_AGENT_EVAL_IMPL" 'cursor-agent'
  assert_contains "$WAZA_CLI_AGENT_EVAL_IMPL" 'opencode run'
  assert_contains "$WAZA_CLI_AGENT_EVAL_IMPL" 'hermes'
  assert_contains "$WAZA_CLI_AGENT_EVAL_IMPL" 'openclaw agent'
  assert_not_contains "$WAZA_CLI_AGENT_EVAL_IMPL" 'npm:@desplega.ai/agent-swarm'
  assert_contains "$WAZA_CLI_AGENT_EVAL_IMPL" 'run_direct_or_mise'
  assert_contains "$WAZA_CLI_AGENT_EVAL_IMPL" 'run_direct_or_homebrew_cask'
  assert_contains "$WAZA_CLI_AGENT_EVAL_IMPL" '.waza-results/cli-agents'
  assert_contains "$WAZA_CLI_AGENT_EVAL_IMPL" 'canonical_agent_specs'
  assert_contains "$WAZA_CLI_AGENT_EVAL_IMPL" 'all_cli_agents'
}

test_waza_cli_agent_eval_script_is_guarded_and_can_dry_run() {
  local output
  make_temp_file
  output="$REPLY"

  if "$TEST_ZSH_BIN" "$WAZA_CLI_AGENT_EVAL_SCRIPT" codex >"$output" 2>&1; then
    fail "expected cli agent eval without --allow to fail"
  fi
  assert_output_contains "$output" "CLI agent evals require explicit --allow"
  assert_output_contains "$output" "zsh scripts/waza_eval_cli_agent.sh codex --allow"

  if "$TEST_ZSH_BIN" "$WAZA_MODEL_EVAL_SCRIPT" >"$output" 2>&1; then
    fail "expected model eval without --allow or --dry-run to fail"
  fi
  assert_output_contains "$output" "Waza model evals require explicit --allow"
  assert_output_contains "$output" "zsh scripts/waza_eval_model.sh --allow"

  "$TEST_ZSH_BIN" "$WAZA_MODEL_EVAL_SCRIPT" --dry-run --suite dotfiles/.agent/evals/markdown-docs/model.yaml >"$output"
  assert_output_contains "$output" "DRY-RUN codex"
  assert_output_contains "$output" "dotfiles/.agent/evals/markdown-docs/model.yaml"

  "$TEST_ZSH_BIN" "$WAZA_MODEL_EVAL_SCRIPT" --agent claude --dry-run --suite dotfiles/.agent/evals/security-check/model.yaml >"$output"
  assert_output_contains "$output" "DRY-RUN claude"
  assert_output_contains "$output" "dotfiles/.agent/evals/security-check/model.yaml"

  "$TEST_ZSH_BIN" "$WAZA_MODEL_EVAL_SCRIPT" --agent antigravity-cli --dry-run --suite dotfiles/.agent/evals/auto-debugger/model.yaml >"$output"
  assert_output_contains "$output" "DRY-RUN antigravity"
  assert_output_contains "$output" "dotfiles/.agent/evals/auto-debugger/model.yaml"

  "$TEST_ZSH_BIN" "$WAZA_MODEL_EVAL_SCRIPT" --model opencode --dry-run --suite dotfiles/.agent/evals/auto-debugger/model.yaml >"$output"
  assert_output_contains "$output" "DRY-RUN opencode"
  assert_output_contains "$output" "dotfiles/.agent/evals/auto-debugger/model.yaml"

  "$TEST_ZSH_BIN" "$WAZA_MODEL_EVAL_SCRIPT" --agent all --dry-run --suite dotfiles/.agent/evals/markdown-docs/model.yaml >"$output"
  assert_output_contains "$output" "DRY-RUN codex"
  assert_output_contains "$output" "DRY-RUN openclaw"
  assert_not_contains "$output" "DRY-RUN agent-swarm"

  "$TEST_ZSH_BIN" "$WAZA_CLI_AGENT_EVAL_SCRIPT" codex --dry-run >"$output"
  assert_output_contains "$output" "DRY-RUN codex"
  assert_output_contains "$output" "dotfiles/.agent/evals/markdown-docs/model.yaml"
  assert_output_contains "$output" "tasks/restructure-guide.yaml"

  "$TEST_ZSH_BIN" "$WAZA_CLI_AGENT_EVAL_SCRIPT" claude --dry-run --suite dotfiles/.agent/evals/security-check/model.yaml >"$output"
  assert_output_contains "$output" "DRY-RUN claude"
  assert_output_contains "$output" "dotfiles/.agent/evals/security-check/model.yaml"
  assert_output_contains "$output" "tasks/review-flask-handler.yaml"

  "$TEST_ZSH_BIN" "$WAZA_CLI_AGENT_EVAL_SCRIPT" agy --dry-run --suite dotfiles/.agent/evals/auto-debugger/model.yaml >"$output"
  assert_output_contains "$output" "DRY-RUN antigravity"
  assert_output_contains "$output" "dotfiles/.agent/evals/auto-debugger/model.yaml"
  assert_output_contains "$output" "tasks/pytest-typeerror.yaml"

  "$TEST_ZSH_BIN" "$WAZA_CLI_AGENT_EVAL_SCRIPT" opencode --dry-run --suite dotfiles/.agent/evals/auto-debugger/model.yaml >"$output"
  assert_output_contains "$output" "DRY-RUN opencode"
  assert_output_contains "$output" "dotfiles/.agent/evals/auto-debugger/model.yaml"
  assert_output_contains "$output" "tasks/pytest-typeerror.yaml"

  "$TEST_ZSH_BIN" "$WAZA_CLI_AGENT_EVAL_SCRIPT" copilot --dry-run --suite dotfiles/.agent/evals/markdown-docs/model.yaml >"$output"
  assert_output_contains "$output" "DRY-RUN copilot"

  "$TEST_ZSH_BIN" "$WAZA_CLI_AGENT_EVAL_SCRIPT" devin --dry-run --suite dotfiles/.agent/evals/markdown-docs/model.yaml >"$output"
  assert_output_contains "$output" "DRY-RUN devin"

  "$TEST_ZSH_BIN" "$WAZA_CLI_AGENT_EVAL_SCRIPT" cursor-agent --dry-run --suite dotfiles/.agent/evals/markdown-docs/model.yaml >"$output"
  assert_output_contains "$output" "DRY-RUN cursor"

  "$TEST_ZSH_BIN" "$WAZA_CLI_AGENT_EVAL_SCRIPT" opencode --dry-run --suite dotfiles/.agent/evals/markdown-docs/model.yaml >"$output"
  assert_output_contains "$output" "DRY-RUN opencode"

  "$TEST_ZSH_BIN" "$WAZA_CLI_AGENT_EVAL_SCRIPT" hermes-agent --dry-run --suite dotfiles/.agent/evals/markdown-docs/model.yaml >"$output"
  assert_output_contains "$output" "DRY-RUN hermes"

  "$TEST_ZSH_BIN" "$WAZA_CLI_AGENT_EVAL_SCRIPT" openclaw --dry-run --suite dotfiles/.agent/evals/markdown-docs/model.yaml >"$output"
  assert_output_contains "$output" "DRY-RUN openclaw"

  "$TEST_ZSH_BIN" "$WAZA_CLI_AGENT_EVAL_SCRIPT" all --dry-run --suite dotfiles/.agent/evals/markdown-docs/model.yaml >"$output"
  assert_output_contains "$output" "DRY-RUN codex"
  assert_output_contains "$output" "DRY-RUN antigravity"
  assert_output_contains "$output" "DRY-RUN copilot"
  assert_output_contains "$output" "DRY-RUN hermes"
  assert_output_contains "$output" "DRY-RUN openclaw"
  assert_not_contains "$output" "DRY-RUN agent-swarm"

  rm -f "$output"
}

test_waza_cli_agent_eval_script_preserves_cli_failure_status() {
  local fake_bin
  local output_dir
  local output
  local cli_status
  make_temp_dir
  fake_bin="$REPLY"
  make_temp_dir
  output_dir="$REPLY"
  make_temp_file
  output="$REPLY"

  cat > "$fake_bin/codex" <<'EOF'
#!/usr/bin/env zsh
exit 23
EOF
  chmod +x "$fake_bin/codex"

  set +e
  PATH="$fake_bin:$PATH" "$TEST_ZSH_BIN" "$WAZA_CLI_AGENT_EVAL_SCRIPT" codex \
    --allow \
    --suite dotfiles/.agent/evals/markdown-docs/model.yaml \
    --output-dir "$output_dir" >"$output" 2>&1
  cli_status=$?
  set -e

  [[ "$cli_status" -ne 0 ]] || fail "expected cli agent eval to fail when codex exits non-zero"
  assert_contains "$output_dir/codex/markdown-docs-model-eval/markdown-docs-restructure-guide-001/summary.txt" "CLI failed with status 23"

  rm -rf "$fake_bin" "$output_dir"
  rm -f "$output"
}

test_waza_cli_agent_eval_script_grades_successful_cli_output() {
  local fake_bin
  local output_dir
  local output
  make_temp_dir
  fake_bin="$REPLY"
  make_temp_dir
  output_dir="$REPLY"
  make_temp_file
  output="$REPLY"

  cat > "$fake_bin/codex" <<'EOF'
#!/usr/bin/env zsh
echo "This Markdown review identifies duplicate headings and explains structure, order, usage, troubleshooting, and heading cleanup with enough concrete detail to exceed the length threshold."
EOF
  chmod +x "$fake_bin/codex"

  PATH="$fake_bin:$PATH" "$TEST_ZSH_BIN" "$WAZA_CLI_AGENT_EVAL_SCRIPT" codex \
    --allow \
    --suite dotfiles/.agent/evals/markdown-docs/model.yaml \
    --output-dir "$output_dir" >"$output" 2>&1

  assert_contains "$output_dir/codex/markdown-docs-model-eval/markdown-docs-restructure-guide-001/summary.txt" "PASS regex_match: (?i)(duplicate|duplicated|repeated).*heading|two.*setup|setup.*twice"
  assert_contains "$output_dir/codex/markdown-docs-model-eval/markdown-docs-restructure-guide-001/summary.txt" "PASS regex_not_match: (?i)fatal error|crashed|exception occurred"
  assert_not_contains "$output_dir/codex/markdown-docs-model-eval/markdown-docs-restructure-guide-001/summary.txt" "type: text"
  assert_not_contains "$output_dir/codex/markdown-docs-model-eval/markdown-docs-restructure-guide-001/summary.txt" "FAIL"

  rm -rf "$fake_bin" "$output_dir"
  rm -f "$output"
}

test_waza_eval_suites_cover_all_regular_agent_skills() {
  local -a skills
  local -A superpower_eval_dirs
  local skill
  local eval_dir
  local eval_file
  local model_file
  local task_files

  skills=(
    agent-cli-consult
    agent-job-scheduler
    alphaxiv-paper-lookup
    api-design
    auto-debugger
    ci-cd
    compatibility-safety
    database-dev
    empirical-prompt-tuning
    go-dev
    goal-prompt-builder
    google-colab-cli
    gws
    html-preview-review
    magika
    markdown-docs
    markitdown
    missing-tools
    git-github-flow
    prompt-tuner
    python-dev
    retrospective-codify
    security-check
    shaping-japanese-longform
    terraform-dev
    typescript-dev
  )

  for skill in "${skills[@]}"; do
    eval_file="$WAZA_EVAL_ROOT/$skill/eval.yaml"
    model_file="$WAZA_EVAL_ROOT/$skill/model.yaml"
    assert_file "$REPO_ROOT/dotfiles/.agent/skills/$skill/SKILL.md"
    assert_contains "$eval_file" "name: $skill-eval"
    assert_contains "$eval_file" "skill: $skill"
    assert_contains "$eval_file" "executor: mock"
    assert_contains "$eval_file" 'tasks/*.yaml'
    assert_contains "$model_file" "name: $skill-model-eval"
    assert_contains "$model_file" "skill: $skill"
    assert_contains "$model_file" "executor: copilot-sdk"
    assert_contains "$model_file" "regex_match:"
    task_files=("$WAZA_EVAL_ROOT/$skill"/tasks/*.yaml(N))
    (( ${#task_files[@]} > 0 )) || fail "expected at least one task yaml for Waza eval skill: $skill"
  done

  superpower_eval_dirs=(
    superpowers-dispatching-parallel-agents "dispatching-parallel-agents"
    superpowers-test-driven-development "test-driven-development"
    superpowers-writing-skills "writing-skills"
  )

  for eval_dir skill in "${(@kv)superpower_eval_dirs}"; do
    eval_file="$WAZA_EVAL_ROOT/$eval_dir/eval.yaml"
    model_file="$WAZA_EVAL_ROOT/$eval_dir/model.yaml"
    assert_file "$REPO_ROOT/dotfiles/.agent/skills/${eval_dir#superpowers-}/SKILL.md"
    assert_contains "$eval_file" "name: $eval_dir-eval"
    assert_contains "$eval_file" "skill: \"$skill\""
    assert_contains "$eval_file" "executor: mock"
    assert_contains "$model_file" "name: $eval_dir-model-eval"
    assert_contains "$model_file" "skill: \"$skill\""
    assert_contains "$model_file" "executor: copilot-sdk"
    task_files=("$WAZA_EVAL_ROOT/$eval_dir"/tasks/*.yaml(N))
    (( ${#task_files[@]} > 0 )) || fail "expected at least one task yaml for Waza eval skill: $skill"
  done
}

test_agent_skills_use_supported_discovery_paths() {
  local skills_root="$REPO_ROOT/dotfiles/.agent/skills"
  local bundled_manifest="$skills_root/.bundled_manifest"
  local skill_file
  local skill_name
  local relative_path

  assert_file "$bundled_manifest"

  while IFS= read -r skill_file; do
    relative_path="${skill_file#$skills_root/}"
    [[ "$relative_path" == */*/* ]] || continue
    [[ "$relative_path" == .* ]] && continue
    skill_name="${relative_path:h:t}"
    grep -Eq "^${skill_name}:" "$bundled_manifest" \
      || fail "nested agent skill must be registered in .bundled_manifest: $relative_path"
  done < <(find "$skills_root" -type f -name SKILL.md -print | sort)
}

test_flake_exposes_nix_darwin_and_home_manager_profiles() {
  assert_contains "$FLAKE_FILE" 'nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable"'
  assert_contains "$FLAKE_FILE" 'url = "github:nix-darwin/nix-darwin"'
  assert_contains "$FLAKE_FILE" 'url = "github:nix-community/home-manager"'
  assert_contains "$FLAKE_FILE" 'builtins.getEnv "DOTFILES_USERNAME"'
  assert_contains "$FLAKE_FILE" 'builtins.getEnv "USER"'
  assert_contains "$FLAKE_FILE" 'DOTFILES_USERNAME or USER must be available while evaluating this flake'
  assert_not_contains "$FLAKE_FILE" 'username = "tatsuki-o"'
  assert_not_contains "$FLAKE_FILE" 'username = "tatsuki"'
  assert_contains "$FLAKE_FILE" 'darwinConfigurations'
  assert_contains "$FLAKE_FILE" 'homeConfigurations'
  assert_contains "$FLAKE_FILE" 'mkDarwinConfiguration'
  assert_contains "$FLAKE_FILE" 'mkHomeConfiguration'
  assert_contains "$FLAKE_FILE" 'homeManagerBackupExtension = "before-nix-darwin"'
  assert_contains "$FLAKE_FILE" 'home-manager.backupFileExtension = homeManagerBackupExtension'
  assert_contains "$FLAKE_FILE" 'aarch64-darwin-full'
  assert_contains "$FLAKE_FILE" 'aarch64-darwin-cli'
  assert_contains "$FLAKE_FILE" 'x86_64-linux-cli'
  assert_contains "$FLAKE_FILE" 'x86_64-linux-full'
  assert_contains "$FLAKE_FILE" 'dotfiles-full-packages'
  assert_contains "$FLAKE_FILE" 'dotfiles-cli-packages'
  assert_contains "$FLAKE_FILE" './config/nix/home-manager'
  assert_contains "$FLAKE_FILE" './config/nix/darwin'
  assert_not_contains "$FLAKE_FILE" "nix-homebrew"
  assert_not_contains "$FLAKE_FILE" './config/nix/modules/home-manager.nix'
  assert_not_contains "$FLAKE_FILE" './config/nix/modules/darwin.nix'
}

test_home_manager_and_darwin_modules_define_profiles_without_homebrew() {
  assert_contains "$HOME_MANAGER_MODULE" 'dotfiles.profile'
  assert_contains "$HOME_MANAGER_MODULE" 'dotfiles.enableGuiApps'
  assert_contains "$HOME_MANAGER_MODULE" 'targets.darwin.copyApps.enable = pkgs.stdenv.hostPlatform.isDarwin && config.dotfiles.enableGuiApps'
  assert_contains "$HOME_MANAGER_MODULE" 'targets.darwin.linkApps.enable = false'
  assert_contains "$HOME_MANAGER_MODULE" 'programs.home-manager.enable = true'
  assert_contains "$HOME_MANAGER_MODULE" './packages.nix'
  assert_contains "$HOME_MANAGER_MODULE" './zsh.nix'
  assert_contains "$HOME_MANAGER_MODULE" './neovim.nix'
  assert_contains "$HOME_MANAGER_MODULE" './auto-update.nix'
  assert_contains "$HOME_MANAGER_MODULE" './session.nix'

  assert_contains "$HOME_MANAGER_PACKAGES_MODULE" 'home.packages'
  assert_contains "$HOME_MANAGER_PACKAGES_MODULE" 'lib.optionals config.dotfiles.enableGuiApps guiPackages'
  assert_contains "$HOME_MANAGER_PACKAGES_MODULE" 'homeManagerProvidedPackageNames'
  assert_contains "$HOME_MANAGER_PACKAGES_MODULE" 'lib.getName pkg'

  assert_contains "$HOME_MANAGER_ZSH_MODULE" 'programs.zsh.enable = true'
  assert_contains "$HOME_MANAGER_ZSH_MODULE" 'programs.zsh.oh-my-zsh.enable = true'
  assert_contains "$HOME_MANAGER_ZSH_MODULE" 'lib.mkOrder 550'
  assert_contains "$HOME_MANAGER_ZSH_MODULE" '/opt/homebrew/share/zsh/site-functions/_brew'
  assert_contains "$HOME_MANAGER_ZSH_MODULE" 'PROMPT_MACHINE_EMOJI'
  assert_contains "$HOME_MANAGER_ZSH_MODULE" 'prompt-machine-emoji'
  assert_contains "$HOME_MANAGER_ZSH_MODULE" 'command mise activate zsh'
  assert_contains "$HOME_MANAGER_ZSH_MODULE" 'hm-session-vars.sh'
  assert_not_contains "$HOME_MANAGER_ZSH_MODULE" "brew shellenv"

  assert_contains "$HOME_MANAGER_NEOVIM_MODULE" 'programs.neovim.enable = true'
  assert_contains "$HOME_MANAGER_NEOVIM_MODULE" 'programs.neovim.plugins'
  assert_contains "$HOME_MANAGER_NEOVIM_MODULE" 'vim-code-dark'
  assert_contains "$HOME_MANAGER_NEOVIM_MODULE" 'vim-fern'
  assert_contains "$HOME_MANAGER_NEOVIM_MODULE" 'builtins.readFile ../../nvim/init.vim'

  assert_contains "$HOME_MANAGER_AUTO_UPDATE_MODULE" 'systemd.user.services.dotfiles-auto-update'
  assert_contains "$HOME_MANAGER_AUTO_UPDATE_MODULE" 'systemd.user.timers.dotfiles-auto-update'
  assert_contains "$HOME_MANAGER_AUTO_UPDATE_MODULE" 'config.dotfiles.profile == "full" && !pkgs.stdenv.hostPlatform.isDarwin'
  assert_contains "$HOME_MANAGER_AUTO_UPDATE_MODULE" 'OnCalendar = "*-*-* 06:00:00"'
  assert_contains "$HOME_MANAGER_AUTO_UPDATE_MODULE" 'Persistent = true'
  assert_contains "$HOME_MANAGER_AUTO_UPDATE_MODULE" '/tmp/dotfiles-git-pull.log'

  assert_contains "$HOME_MANAGER_SESSION_MODULE" 'home.sessionVariables'
  assert_not_contains "$HOME_MANAGER_MODULE" "brew shellenv"

  assert_contains "$DARWIN_MODULE" './base.nix'
  assert_contains "$DARWIN_MODULE" './defaults.nix'
  assert_contains "$DARWIN_MODULE" './homebrew.nix'
  assert_contains "$DARWIN_MODULE" './auto-update.nix'

  assert_contains "$DARWIN_BASE_MODULE" 'system.stateVersion'
  assert_contains "$DARWIN_BASE_MODULE" 'nix.enable = false'
  assert_contains "$DARWIN_BASE_MODULE" 'users.users.${username}.home'
  assert_not_contains "$DARWIN_BASE_MODULE" 'import ../gui-packages.nix'
  assert_not_contains "$DARWIN_BASE_MODULE" 'enableGuiApps && !pkgs.stdenv.hostPlatform.isDarwin'
  assert_not_contains "$DARWIN_BASE_MODULE" 'nix.settings'
  assert_not_contains "$DARWIN_BASE_MODULE" 'nix.optimise'

  assert_contains "$DARWIN_DEFAULTS_MODULE" 'security.pam.services.sudo_local = {'
  assert_contains "$DARWIN_DEFAULTS_MODULE" 'touchIdAuth = true'
  assert_contains "$DARWIN_DEFAULTS_MODULE" 'InitialKeyRepeat = 12'
  assert_contains "$DARWIN_DEFAULTS_MODULE" 'KeyRepeat = 1'
  assert_contains "$DARWIN_DEFAULTS_MODULE" 'screenshotsDirectory = "${homeDirectory}/SS"'
  assert_contains "$DARWIN_DEFAULTS_MODULE" 'wvous-tl-corner = 12'
  assert_contains "$DARWIN_DEFAULTS_MODULE" 'wvous-tr-corner = 2'
  assert_contains "$DARWIN_DEFAULTS_MODULE" 'wvous-bl-corner = 11'
  assert_contains "$DARWIN_DEFAULTS_MODULE" 'wvous-br-corner = 3'
  assert_contains "$DARWIN_DEFAULTS_MODULE" 'system.defaults.CustomUserPreferences."com.apple.dock" = {'
  assert_contains "$DARWIN_DEFAULTS_MODULE" '"wvous-tl-modifier" = 0'
  assert_contains "$DARWIN_DEFAULTS_MODULE" '"wvous-tr-modifier" = 0'
  assert_contains "$DARWIN_DEFAULTS_MODULE" '"wvous-bl-modifier" = 0'
  assert_contains "$DARWIN_DEFAULTS_MODULE" '"wvous-br-modifier" = 0'
  assert_contains "$DARWIN_DEFAULTS_MODULE" 'system.defaults.screencapture = {'
  assert_contains "$DARWIN_DEFAULTS_MODULE" 'location = screenshotsDirectory'
  assert_contains "$DARWIN_DEFAULTS_MODULE" '/bin/mkdir -p /Library/Preferences/FeatureFlags/Domain'
  assert_contains "$DARWIN_DEFAULTS_MODULE" '/usr/bin/defaults write /Library/Preferences/FeatureFlags/Domain/UIKit.plist'
  assert_contains "$DARWIN_DEFAULTS_MODULE" 'redesigned_text_cursor -dict-add Enabled -bool false'
  assert_not_contains "$DARWIN_DEFAULTS_MODULE" 'killall CursorUIViewService'
  assert_contains "$DARWIN_DEFAULTS_MODULE" 'current_wifi_dns='
  assert_contains "$DARWIN_DEFAULTS_MODULE" '[ "$current_wifi_dns" = "8.8.8.8 8.8.4.4" ]'
  assert_contains "$DARWIN_DEFAULTS_MODULE" 'networksetup -setdnsservers Wi-Fi 1.1.1.1 8.8.8.8 8.8.4.4 >/dev/null 2>&1 || true'
  assert_contains "$DARWIN_DEFAULTS_MODULE" 'tmutil addexclusion -p /nix'
  assert_contains "$DARWIN_DEFAULTS_MODULE" 'mdutil -i off /nix'

  assert_contains "$DARWIN_HOMEBREW_MODULE" 'import ../homebrew-fallback.nix'
  assert_contains "$DARWIN_HOMEBREW_MODULE" 'homebrewFallbackHasCliEntries = homebrewFallback.brews != [ ]'
  assert_contains "$DARWIN_HOMEBREW_MODULE" 'homebrewFallbackHasGuiEntries = homebrewFallback.casks != [ ] || homebrewFallback.vscode != [ ]'
  assert_contains "$DARWIN_HOMEBREW_MODULE" 'homebrewFallbackEnabled = homebrewFallbackHasCliEntries || (enableGuiApps && homebrewFallbackHasGuiEntries)'
  assert_contains "$DARWIN_HOMEBREW_MODULE" 'homebrewTrustedCasks = lib.optionals enableGuiApps (homebrewFallback.trustedCasks or [ ])'
  assert_contains "$DARWIN_HOMEBREW_MODULE" 'homebrew = lib.mkIf homebrewFallbackEnabled'
  assert_contains "$DARWIN_HOMEBREW_MODULE" 'enable = true'
  assert_contains "$DARWIN_HOMEBREW_MODULE" 'taps = homebrewFallback.taps'
  assert_contains "$DARWIN_HOMEBREW_MODULE" 'brews = homebrewFallback.brews'
  assert_contains "$DARWIN_HOMEBREW_MODULE" 'casks = lib.optionals enableGuiApps homebrewFallback.casks'
  assert_contains "$DARWIN_HOMEBREW_MODULE" 'masApps = { }'
  assert_not_contains "$DARWIN_HOMEBREW_MODULE" 'import ../mas-apps.nix'
  assert_contains "$DARWIN_HOMEBREW_MODULE" 'vscode = lib.optionals enableGuiApps homebrewFallback.vscode'
  assert_contains "$DARWIN_HOMEBREW_MODULE" 'cleanup = "none"'
  assert_contains "$DARWIN_HOMEBREW_MODULE" 'system.activationScripts.homebrew.text'
  assert_contains "$DARWIN_HOMEBREW_MODULE" 'lib.mkBefore'
  assert_contains "$DARWIN_HOMEBREW_MODULE" 'brew trust --cask'

  assert_contains "$DARWIN_AUTO_UPDATE_MODULE" 'launchd.user.agents.dotfiles-auto-update'
  assert_contains "$DARWIN_AUTO_UPDATE_MODULE" 'profile == "full"'
  assert_contains "$DARWIN_AUTO_UPDATE_MODULE" 'StartCalendarInterval'
  assert_contains "$DARWIN_AUTO_UPDATE_MODULE" 'Hour = 6'
  assert_contains "$DARWIN_AUTO_UPDATE_MODULE" 'Minute = 0'
  assert_contains "$DARWIN_AUTO_UPDATE_MODULE" '/tmp/dotfiles-git-pull.log'
  assert_contains "$DARWIN_AUTO_UPDATE_MODULE" 'system.activationScripts.postActivation.text = lib.mkAfter'
  assert_contains "$DARWIN_AUTO_UPDATE_MODULE" 'removed legacy dotfiles cron block'
  assert_contains "$DARWIN_AUTO_UPDATE_MODULE" '| sudo --user=${username} -- crontab -'
  assert_not_contains "$DARWIN_AUTO_UPDATE_MODULE" 'stripped_cron'
  assert_not_exists "$REPO_ROOT/config/nix/modules/darwin.nix"
  assert_not_exists "$REPO_ROOT/config/nix/modules/home-manager.nix"
  assert_not_contains "$MAIN_SCRIPT" 'default_setup.sh'
  assert_not_contains "$MAIN_SCRIPT" 'setup_cron.sh'
  assert_not_contains "$APPLY_UPDATES_SCRIPT" 'setup_cron.sh'
  assert_not_exists "$REPO_ROOT/scripts/default_setup.sh"
  assert_not_exists "$REPO_ROOT/scripts/setup_cron.sh"
  assert_not_exists "$REPO_ROOT/config/cron/crontab"
}

test_nix_install_script_switches_nix_darwin_or_home_manager() {
  assert_contains "$INSTALL_SCRIPT" '--profile full|cli'
  assert_contains "$INSTALL_SCRIPT" 'Select setup profile. Defaults to cli.'
  assert_contains "$INSTALL_SCRIPT" '--cli-only'
  assert_contains "$INSTALL_SCRIPT" '--with-gui-apps'
  assert_contains "$INSTALL_SCRIPT" '--uninstall-homebrew'
  assert_contains "$INSTALL_SCRIPT" 'darwin-rebuild'
  assert_contains "$INSTALL_SCRIPT" 'home-manager'
  assert_contains "$INSTALL_SCRIPT" 'switch --impure --flake'
  assert_contains "$INSTALL_SCRIPT" 'build --impure --flake'
  assert_contains "$INSTALL_SCRIPT" 'aarch64-darwin-full'
  assert_contains "$INSTALL_SCRIPT" 'x86_64-linux-cli'
  assert_contains "$INSTALL_SCRIPT" 'NIX_EXPERIMENTAL_ARGS=(--extra-experimental-features "nix-command flakes")'
  assert_contains "$INSTALL_SCRIPT" 'source "$SCRIPT_DIR/lib/runtime.sh"'
  assert_contains "$INSTALL_SCRIPT" 'dotfiles_resolve_command_from_path "nix-rootless"'
  assert_contains "$INSTALL_SCRIPT" 'HOME_MANAGER_BACKUP_EXTENSION="before-nix-darwin"'
  assert_contains "$INSTALL_SCRIPT" 'HOME_MANAGER_BACKUP_ARCHIVE_EPOCH'
  assert_contains "$INSTALL_SCRIPT" 'DOTFILES_DARWIN_SUDO_LOCAL_PATH'
  assert_contains "$INSTALL_SCRIPT" 'DARWIN_SUDO_LOCAL_BACKUP_PATH'
  assert_contains "$INSTALL_SCRIPT" 'DOTFILES_DARWIN_ETC_SHELL_RC_PATHS'
  assert_contains "$INSTALL_SCRIPT" 'DOTFILES_USERNAME="$flake_username"'
  assert_contains "$INSTALL_SCRIPT" '--impure --flake'
  assert_contains "$INSTALL_SCRIPT" 'archive_existing_home_manager_backups'
  assert_contains "$INSTALL_SCRIPT" 'backup_existing_darwin_sudo_local'
  assert_contains "$INSTALL_SCRIPT" 'backup_existing_darwin_etc_shell_rc_files'
  assert_contains "$INSTALL_SCRIPT" 'sudo mv "$DARWIN_SUDO_LOCAL_PATH" "$DARWIN_SUDO_LOCAL_BACKUP_PATH"'
  assert_contains "$INSTALL_SCRIPT" 'before nix-darwin manages shell startup files'
  assert_contains "$INSTALL_SCRIPT" 'switch -b "$HOME_MANAGER_BACKUP_EXTENSION" --impure --flake'
  assert_contains "$INSTALL_SCRIPT" '"${NIX_EXPERIMENTAL_ARGS[@]}"'
  assert_contains "$INSTALL_SCRIPT" 'dotfiles_create_unique_temp_directory'
  assert_contains "$INSTALL_SCRIPT" 'dotfiles_resolve_command_from_path'
  assert_contains "$INSTALL_SCRIPT" 'sudo env HOME=/var/root'
  assert_contains "$INSTALL_SCRIPT" 'scripts/remove_homebrew.sh'
  assert_contains "$INSTALL_SCRIPT" 'Run zsh scripts/install_homebrew.sh --profile $profile_name'
  assert_contains "$INSTALL_SCRIPT" '$REMOVE_HOMEBREW_SCRIPT" --apply --confirm-nix-ready'
  assert_contains "$INSTALL_SCRIPT" '--exclude result'
  assert_contains "$INSTALL_SCRIPT" '--exclude .agent'
  assert_not_contains "$INSTALL_SCRIPT" '< <('
  assert_not_contains "$INSTALL_SCRIPT" '$(nix_args)'
  assert_not_contains "$INSTALL_SCRIPT" 'brew bundle'
  assert_not_contains "$INSTALL_SCRIPT" 'fallback.Brewfile'
}

test_nix_install_script_defaults_to_cli_profile_on_macos() {
  skip_unless_macos "$funcstack[1]" || return 0

  local repo
  local bin_dir
  local log_file
  local output_file
  local home_dir
  local config_dir
  local forbidden_home="$HOME"
  local forbidden_config="${XDG_CONFIG_HOME:-$HOME/.config}"
  local guard_status=0
  local host_machine
  local expected_attr

  make_temp_dir

  repo="$REPLY"
  bin_dir="$repo/bin"
  home_dir="$repo/home"
  config_dir="$repo/config-home"
  log_file="$repo/commands.log"
  output_file="$repo/output.log"

  mkdir -p "$repo/scripts/lib" "$repo/config/nix" "$bin_dir" "$home_dir" "$config_dir"
  cp "$INSTALL_SCRIPT" "$repo/scripts/nix_install.sh"
  copy_script_libs "$repo"
  cat > "$repo/config/nix/homebrew-fallback.nix" <<'EOF'
{ taps = [ ]; brews = [ ]; casks = [ ]; vscode = [ ]; }
EOF
  cat > "$repo/config/nix/mas-apps.nix" <<'EOF'
{ }
EOF

  cat > "$bin_dir/uname" <<'EOF'
#!/usr/bin/env zsh
set -euo pipefail
if [[ "${1:-}" == "-s" ]]; then
  print -r -- "Darwin"
elif [[ "${1:-}" == "-m" ]]; then
  print -r -- "arm64"
else
  print -r -- "Darwin"
fi
EOF
  cat > "$bin_dir/darwin-rebuild" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
print -r -- "darwin-rebuild:\$*" >> "$log_file"
exit 0
EOF
  cat > "$bin_dir/nix" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
print -r -- "nix:\$*" >> "$log_file"
exit 0
EOF
  write_strict_fake_sudo "$bin_dir"
  write_strict_fake_find "$bin_dir"

  chmod +x "$bin_dir/uname" "$bin_dir/darwin-rebuild" "$bin_dir/nix" "$bin_dir/sudo" "$bin_dir/find"

  PATH="$bin_dir:/bin:/usr/bin:/usr/sbin:/sbin" \
    HOME="$home_dir" XDG_CONFIG_HOME="$config_dir" \
    NIX_TEST_EVENT_LOG="$log_file" NIX_TEST_FIXTURE_ROOT="$repo" \
    NIX_TEST_FORBIDDEN_HOME="$forbidden_home" NIX_TEST_FORBIDDEN_XDG_CONFIG="$forbidden_config" \
    DOTFILES_DARWIN_SUDO_LOCAL_PATH="$repo/etc/pam.d/sudo_local" \
    DOTFILES_DARWIN_ETC_SHELL_RC_PATHS="$repo/etc/bashrc:$repo/etc/zshrc" \
    "$TEST_ZSH_BIN" "$repo/scripts/nix_install.sh" > "$output_file"

  assert_output_contains "$output_file" 'Nix profile: cli'
  host_machine="$(/usr/bin/uname -m)" || fail 'real /usr/bin/uname -m failed'
  case "$host_machine" in
    arm64)
      expected_attr='aarch64-darwin-cli'
      ;;
    x86_64)
      expected_attr='x86_64-darwin-cli'
      ;;
    *)
      fail "unsupported macOS fixture architecture: $host_machine"
      ;;
  esac
  assert_output_contains "$output_file" "Flake output: $expected_attr"
  assert_contains "$log_file" "find:$home_dir"
  assert_contains "$log_file" "find:$config_dir"
  assert_contains "$log_file" 'darwin-rebuild:switch --impure --flake'
  assert_not_contains "$output_file" 'aarch64-darwin-full'

  PATH="$bin_dir:/bin:/usr/bin:/usr/sbin:/sbin" \
    NIX_TEST_EVENT_LOG="$log_file" NIX_TEST_FIXTURE_ROOT="$repo" \
    "$bin_dir/sudo" env HOME=/var/root DOTFILES_USERNAME=dotfiles-test darwin-rebuild switch \
    --impure --flake "$repo/etc" > "$repo/sudo-guard.log" 2>&1 || guard_status=$?
  (( guard_status != 0 )) || fail "fake sudo must reject an unlisted flake path"
  guard_status=0
  PATH="$bin_dir:/bin:/usr/bin:/usr/sbin:/sbin" \
    NIX_TEST_EVENT_LOG="$log_file" NIX_TEST_FIXTURE_ROOT="$repo" \
    "$bin_dir/sudo" /bin/true > "$repo/sudo-argv-guard.log" 2>&1 || guard_status=$?
  (( guard_status != 0 )) || fail "fake sudo must reject unknown command argv"

  rm -rf "$repo"
}

test_nix_install_script_uses_nix_run_impure_after_subcommand_when_darwin_rebuild_is_missing() {
  skip_unless_macos "$funcstack[1]" || return 0

  local repo
  local bin_dir
  local log_file
  local output_file
  local home_dir
  local config_dir
  local forbidden_home="$HOME"
  local forbidden_config="${XDG_CONFIG_HOME:-$HOME/.config}"

  make_temp_dir

  repo="$REPLY"
  bin_dir="$repo/bin"
  home_dir="$repo/home"
  config_dir="$repo/config-home"
  log_file="$repo/commands.log"
  output_file="$repo/output.log"

  mkdir -p "$repo/scripts/lib" "$repo/config/nix" "$bin_dir" "$home_dir" "$config_dir"
  cp "$INSTALL_SCRIPT" "$repo/scripts/nix_install.sh"
  copy_script_libs "$repo"
  cat > "$repo/config/nix/homebrew-fallback.nix" <<'EOF'
{ taps = [ ]; brews = [ ]; casks = [ ]; vscode = [ ]; }
EOF
  cat > "$repo/config/nix/mas-apps.nix" <<'EOF'
{ }
EOF

  cat > "$bin_dir/uname" <<'EOF'
#!/usr/bin/env zsh
set -euo pipefail
if [[ "${1:-}" == "-s" ]]; then
  print -r -- "Darwin"
elif [[ "${1:-}" == "-m" ]]; then
  print -r -- "arm64"
else
  print -r -- "Darwin"
fi
EOF
  cat > "$bin_dir/nix" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
print -r -- "nix:\$*" >> "$log_file"
exit 0
EOF
  write_strict_fake_sudo "$bin_dir"
  write_strict_fake_find "$bin_dir"

  chmod +x "$bin_dir/uname" "$bin_dir/nix" "$bin_dir/sudo" "$bin_dir/find"

  PATH="$bin_dir:/bin:/usr/bin:/usr/sbin:/sbin" \
    HOME="$home_dir" XDG_CONFIG_HOME="$config_dir" \
    NIX_TEST_EVENT_LOG="$log_file" NIX_TEST_FIXTURE_ROOT="$repo" \
    NIX_TEST_FORBIDDEN_HOME="$forbidden_home" NIX_TEST_FORBIDDEN_XDG_CONFIG="$forbidden_config" \
    DOTFILES_DARWIN_SUDO_LOCAL_PATH="$repo/etc/pam.d/sudo_local" \
    DOTFILES_DARWIN_ETC_SHELL_RC_PATHS="$repo/etc/bashrc:$repo/etc/zshrc" \
    "$TEST_ZSH_BIN" "$repo/scripts/nix_install.sh" --profile full > "$output_file"

  assert_contains "$log_file" "find:$home_dir"
  assert_contains "$log_file" "find:$config_dir"
  assert_contains "$log_file" 'sudo:env HOME=/var/root DOTFILES_USERNAME='
  assert_contains "$log_file" 'nix:--extra-experimental-features nix-command flakes run --impure'
  assert_not_contains "$log_file" 'nix:--extra-experimental-features nix-command flakes --impure run'

  rm -rf "$repo"
}

test_nix_install_script_backs_up_existing_sudo_local_before_darwin_switch() {
  skip_unless_macos "$funcstack[1]" || return 0

  local repo
  local bin_dir
  local etc_dir
  local log_file
  local output_file
  local sudo_local
  local backup_file
  local home_dir
  local config_dir
  local forbidden_home="$HOME"
  local forbidden_config="${XDG_CONFIG_HOME:-$HOME/.config}"

  make_temp_dir

  repo="$REPLY"
  bin_dir="$repo/bin"
  etc_dir="$repo/etc/pam.d"
  home_dir="$repo/home"
  config_dir="$repo/config-home"
  log_file="$repo/commands.log"
  output_file="$repo/output.log"
  sudo_local="$etc_dir/sudo_local"
  backup_file="${sudo_local}.before-nix-darwin"

  mkdir -p "$repo/scripts/lib" "$bin_dir" "$etc_dir" "$home_dir" "$config_dir"
  cp "$INSTALL_SCRIPT" "$repo/scripts/nix_install.sh"
  copy_script_libs "$repo"

  cat > "$bin_dir/uname" <<'EOF'
#!/usr/bin/env zsh
set -euo pipefail
if [[ "${1:-}" == "-s" ]]; then
  print -r -- "Darwin"
elif [[ "${1:-}" == "-m" ]]; then
  print -r -- "arm64"
else
  print -r -- "Darwin"
fi
EOF
  cat > "$bin_dir/nix" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
print -r -- "nix:\$*" >> "$log_file"
exit 0
EOF
  cat > "$bin_dir/darwin-rebuild" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
print -r -- "darwin-rebuild:\$*" >> "$log_file"
exit 0
EOF
  write_strict_fake_sudo "$bin_dir"
  write_strict_fake_find "$bin_dir"

  chmod +x "$bin_dir/uname" "$bin_dir/nix" "$bin_dir/darwin-rebuild" "$bin_dir/sudo" "$bin_dir/find"

  cat > "$sudo_local" <<'EOF'
# sudo_local: local config file which survives system update and is included for sudo
# uncomment following line to enable Touch ID for sudo
auth sufficient pam_tid.so
EOF

  PATH="$bin_dir:/bin:/usr/bin:/usr/sbin:/sbin" \
    HOME="$home_dir" XDG_CONFIG_HOME="$config_dir" \
    NIX_TEST_EVENT_LOG="$log_file" NIX_TEST_FIXTURE_ROOT="$repo" \
    NIX_TEST_FORBIDDEN_HOME="$forbidden_home" NIX_TEST_FORBIDDEN_XDG_CONFIG="$forbidden_config" \
    DOTFILES_DARWIN_SUDO_LOCAL_PATH="$sudo_local" \
    DOTFILES_DARWIN_ETC_SHELL_RC_PATHS="$repo/etc/bashrc:$repo/etc/zshrc" \
    "$TEST_ZSH_BIN" "$repo/scripts/nix_install.sh" --profile full > "$output_file"

  assert_contains "$log_file" "find:$home_dir"
  assert_contains "$log_file" "find:$config_dir"
  assert_output_contains "$output_file" "Backing up existing $sudo_local to $backup_file before nix-darwin manages sudo Touch ID."
  assert_contains "$log_file" "sudo:mv $sudo_local $backup_file"
  assert_contains "$log_file" 'sudo:env HOME=/var/root DOTFILES_USERNAME='
  assert_contains "$log_file" 'darwin-rebuild switch --impure --flake'
  assert_contains "$log_file" 'darwin-rebuild:switch --impure --flake'
  assert_file "$backup_file"
  assert_not_exists "$sudo_local"

  rm -rf "$repo"
}

test_nix_install_script_backs_up_existing_etc_shell_rc_before_darwin_switch() {
  skip_unless_macos "$funcstack[1]" || return 0

  local repo
  local bin_dir
  local etc_dir
  local log_file
  local output_file
  local bashrc
  local zshrc
  local home_dir
  local config_dir
  local forbidden_home="$HOME"
  local forbidden_config="${XDG_CONFIG_HOME:-$HOME/.config}"

  make_temp_dir

  repo="$REPLY"
  bin_dir="$repo/bin"
  etc_dir="$repo/etc"
  home_dir="$repo/home"
  config_dir="$repo/config-home"
  log_file="$repo/commands.log"
  output_file="$repo/output.log"
  bashrc="$etc_dir/bashrc"
  zshrc="$etc_dir/zshrc"

  mkdir -p "$repo/scripts/lib" "$bin_dir" "$etc_dir" "$home_dir" "$config_dir"
  cp "$INSTALL_SCRIPT" "$repo/scripts/nix_install.sh"
  copy_script_libs "$repo"

  cat > "$bin_dir/uname" <<'EOF'
#!/usr/bin/env zsh
set -euo pipefail
if [[ "${1:-}" == "-s" ]]; then
  print -r -- "Darwin"
elif [[ "${1:-}" == "-m" ]]; then
  print -r -- "arm64"
else
  print -r -- "Darwin"
fi
EOF
  cat > "$bin_dir/nix" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
print -r -- "nix:\$*" >> "$log_file"
exit 0
EOF
  cat > "$bin_dir/darwin-rebuild" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
print -r -- "darwin-rebuild:\$*" >> "$log_file"
exit 0
EOF
  write_strict_fake_sudo "$bin_dir"
  write_strict_fake_find "$bin_dir"

  chmod +x "$bin_dir/uname" "$bin_dir/nix" "$bin_dir/darwin-rebuild" "$bin_dir/sudo" "$bin_dir/find"

  print -r -- "# legacy bashrc" > "$bashrc"
  print -r -- "# legacy zshrc" > "$zshrc"

  PATH="$bin_dir:/bin:/usr/bin:/usr/sbin:/sbin" \
    HOME="$home_dir" XDG_CONFIG_HOME="$config_dir" \
    NIX_TEST_EVENT_LOG="$log_file" NIX_TEST_FIXTURE_ROOT="$repo" \
    NIX_TEST_FORBIDDEN_HOME="$forbidden_home" NIX_TEST_FORBIDDEN_XDG_CONFIG="$forbidden_config" \
    DOTFILES_DARWIN_ETC_SHELL_RC_PATHS="$bashrc:$zshrc" \
    "$TEST_ZSH_BIN" "$repo/scripts/nix_install.sh" --profile full > "$output_file"

  assert_contains "$log_file" "find:$home_dir"
  assert_contains "$log_file" "find:$config_dir"
  assert_output_contains "$output_file" "Backing up existing $bashrc to $bashrc.before-nix-darwin before nix-darwin manages shell startup files."
  assert_output_contains "$output_file" "Backing up existing $zshrc to $zshrc.before-nix-darwin before nix-darwin manages shell startup files."
  assert_contains "$log_file" "sudo:mv $bashrc $bashrc.before-nix-darwin"
  assert_contains "$log_file" "sudo:mv $zshrc $zshrc.before-nix-darwin"
  assert_contains "$log_file" 'sudo:env HOME=/var/root DOTFILES_USERNAME='
  assert_contains "$log_file" 'darwin-rebuild switch --impure --flake'
  assert_file "$bashrc.before-nix-darwin"
  assert_file "$zshrc.before-nix-darwin"
  assert_not_exists "$bashrc"
  assert_not_exists "$zshrc"

  rm -rf "$repo"
}

test_nix_install_script_archives_existing_home_manager_backups_before_switch() {
  skip_unless_macos "$funcstack[1]" || return 0

  local repo
  local bin_dir
  local home_dir
  local config_dir
  local log_file
  local output_file
  local old_zshrc_backup
  local old_xdg_backup
  local archived_zshrc_backup
  local archived_xdg_backup

  make_temp_dir

  repo="$REPLY"
  bin_dir="$repo/bin"
  home_dir="$repo/home"
  config_dir="$repo/xdg"
  log_file="$repo/commands.log"
  output_file="$repo/output.log"
  old_zshrc_backup="$home_dir/.zshrc.before-nix-darwin"
  old_xdg_backup="$config_dir/mise/config.toml.before-nix-darwin"
  archived_zshrc_backup="${old_zshrc_backup}.stale-1700000000"
  archived_xdg_backup="${old_xdg_backup}.stale-1700000000"

  mkdir -p "$repo/scripts/lib" "$bin_dir" "$home_dir" "$config_dir/mise"
  cp "$INSTALL_SCRIPT" "$repo/scripts/nix_install.sh"
  copy_script_libs "$repo"

  cat > "$bin_dir/uname" <<'EOF'
#!/usr/bin/env zsh
set -euo pipefail
if [[ "${1:-}" == "-s" ]]; then
  print -r -- "Darwin"
elif [[ "${1:-}" == "-m" ]]; then
  print -r -- "arm64"
else
  print -r -- "Darwin"
fi
EOF
  cat > "$bin_dir/nix" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
print -r -- "nix:\$*" >> "$log_file"
exit 0
EOF
  cat > "$bin_dir/darwin-rebuild" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
print -r -- "darwin-rebuild:\$*" >> "$log_file"
exit 0
EOF
  write_strict_fake_sudo "$bin_dir"

  chmod +x "$bin_dir/uname" "$bin_dir/nix" "$bin_dir/darwin-rebuild" "$bin_dir/sudo"

  cat > "$old_zshrc_backup" <<'EOF'
legacy zshrc backup
EOF
  cat > "$old_xdg_backup" <<'EOF'
legacy xdg backup
EOF

  HOME="$home_dir" XDG_CONFIG_HOME="$config_dir" PATH="$bin_dir:/bin:/usr/bin:/usr/sbin:/sbin" \
    NIX_TEST_EVENT_LOG="$log_file" NIX_TEST_FIXTURE_ROOT="$repo" \
    DOTFILES_HOME_MANAGER_BACKUP_ARCHIVE_EPOCH="1700000000" \
    DOTFILES_DARWIN_SUDO_LOCAL_PATH="$repo/etc/pam.d/sudo_local" \
    DOTFILES_DARWIN_ETC_SHELL_RC_PATHS="$repo/etc/bashrc:$repo/etc/zshrc" \
    "$TEST_ZSH_BIN" "$repo/scripts/nix_install.sh" --profile full > "$output_file"

  assert_output_contains "$output_file" "Archiving existing Home Manager backup $old_zshrc_backup to $archived_zshrc_backup before activation."
  assert_output_contains "$output_file" "Archiving existing Home Manager backup $old_xdg_backup to $archived_xdg_backup before activation."
  assert_contains "$log_file" 'sudo:env HOME=/var/root DOTFILES_USERNAME='
  assert_contains "$log_file" 'darwin-rebuild switch --impure --flake'
  assert_file "$archived_zshrc_backup"
  assert_file "$archived_xdg_backup"
  assert_not_exists "$old_zshrc_backup"
  assert_not_exists "$old_xdg_backup"

  rm -rf "$repo"
}

test_nix_install_script_handles_dirty_worktree_without_hanging() {
  skip_unless_macos "$funcstack[1]" || return 0

  local repo
  local bin_dir
  local log_file
  local output_file
  local home_dir
  local config_dir
  local forbidden_home="$HOME"
  local forbidden_config="${XDG_CONFIG_HOME:-$HOME/.config}"

  make_temp_dir

  repo="$REPLY"
  bin_dir="$repo/bin"
  home_dir="$repo/home"
  config_dir="$repo/config-home"
  log_file="$repo/commands.log"
  output_file="$repo/output.log"

  mkdir -p "$repo/scripts/lib" "$repo/config/nix" "$bin_dir" "$home_dir" "$config_dir"
  cp "$INSTALL_SCRIPT" "$repo/scripts/nix_install.sh"
  copy_script_libs "$repo"
  cat > "$repo/flake.nix" <<'EOF'
{ }
EOF
  cat > "$repo/flake.lock" <<'EOF'
{ }
EOF

  cat > "$bin_dir/git" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
if [[ "\$*" == *'rev-parse --is-inside-work-tree'* ]]; then
  exit 0
fi
if [[ "\$*" == *'ls-files --others --exclude-standard --'* ]]; then
  print -r -- "flake.lock"
  exit 0
fi
print -r -- "git:\$*" >> "$log_file"
exit 0
EOF
  cat > "$bin_dir/uname" <<'EOF'
#!/usr/bin/env zsh
set -euo pipefail
if [[ "${1:-}" == "-s" ]]; then
  print -r -- "Darwin"
elif [[ "${1:-}" == "-m" ]]; then
  print -r -- "arm64"
else
  print -r -- "Darwin"
fi
EOF
  cat > "$bin_dir/nix" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
print -r -- "nix:\$*" >> "$log_file"
exit 0
EOF
  cat > "$bin_dir/darwin-rebuild" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
print -r -- "darwin-rebuild:\$*" >> "$log_file"
exit 0
EOF
  write_strict_fake_sudo "$bin_dir"
  write_strict_fake_find "$bin_dir"

  chmod +x "$bin_dir/git" "$bin_dir/uname" "$bin_dir/nix" "$bin_dir/darwin-rebuild" "$bin_dir/sudo" "$bin_dir/find"

  PATH="$bin_dir:/bin:/usr/bin:/usr/sbin:/sbin" \
    HOME="$home_dir" XDG_CONFIG_HOME="$config_dir" \
    NIX_TEST_EVENT_LOG="$log_file" NIX_TEST_FIXTURE_ROOT="$repo" NIX_TEST_ALLOW_GENERATED_FLAKE=1 \
    NIX_TEST_FORBIDDEN_HOME="$forbidden_home" NIX_TEST_FORBIDDEN_XDG_CONFIG="$forbidden_config" \
    DOTFILES_DARWIN_SUDO_LOCAL_PATH="$repo/etc/pam.d/sudo_local" \
    DOTFILES_DARWIN_ETC_SHELL_RC_PATHS="$repo/etc/bashrc:$repo/etc/zshrc" \
    "$TEST_ZSH_BIN" "$repo/scripts/nix_install.sh" --profile full > "$output_file"

  assert_contains "$log_file" "find:$home_dir"
  assert_contains "$log_file" "find:$config_dir"
  assert_output_contains "$output_file" 'Flake path: /private/tmp/dotfiles-flake.'
  assert_contains "$log_file" 'sudo:env HOME=/var/root DOTFILES_USERNAME='
  assert_contains "$log_file" 'darwin-rebuild switch --impure --flake path:/private/tmp/dotfiles-flake.'
  assert_contains "$log_file" 'darwin-rebuild:switch --impure --flake path:/private/tmp/dotfiles-flake.'

  rm -rf "$repo"
}

test_nix_install_script_uses_git_aware_flake_ref_for_tracked_worktree() {
  skip_unless_macos "$funcstack[1]" || return 0

  local repo
  local repo_abs
  local bin_dir
  local log_file
  local output_file
  local home_dir
  local config_dir
  local forbidden_home="$HOME"
  local forbidden_config="${XDG_CONFIG_HOME:-$HOME/.config}"

  make_temp_dir

  repo="$REPLY"
  repo_abs="${repo:A}"
  bin_dir="$repo/bin"
  home_dir="$repo/home"
  config_dir="$repo/config-home"
  log_file="$repo/commands.log"
  output_file="$repo/output.log"

  mkdir -p "$repo/scripts/lib" "$repo/config/nix" "$bin_dir" "$home_dir" "$config_dir"
  cp "$INSTALL_SCRIPT" "$repo/scripts/nix_install.sh"
  copy_script_libs "$repo"
  cat > "$repo/flake.nix" <<'EOF'
{ }
EOF
  cat > "$repo/flake.lock" <<'EOF'
{ }
EOF
  cat > "$repo/.git" <<'EOF'
gitdir: /tmp/example-worktree-git-dir
EOF

  cat > "$bin_dir/git" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
if [[ "\$*" == *'rev-parse --is-inside-work-tree'* ]]; then
  exit 0
fi
if [[ "\$*" == *'ls-files --others --exclude-standard --'* ]]; then
  exit 0
fi
print -r -- "git:\$*" >> "$log_file"
exit 0
EOF
  cat > "$bin_dir/uname" <<'EOF'
#!/usr/bin/env zsh
set -euo pipefail
if [[ "${1:-}" == "-s" ]]; then
  print -r -- "Darwin"
elif [[ "${1:-}" == "-m" ]]; then
  print -r -- "arm64"
else
  print -r -- "Darwin"
fi
EOF
  cat > "$bin_dir/nix" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
print -r -- "nix:\$*" >> "$log_file"
exit 0
EOF
  cat > "$bin_dir/darwin-rebuild" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
print -r -- "darwin-rebuild:\$*" >> "$log_file"
exit 0
EOF
  write_strict_fake_sudo "$bin_dir"
  write_strict_fake_find "$bin_dir"

  chmod +x "$bin_dir/git" "$bin_dir/uname" "$bin_dir/nix" "$bin_dir/darwin-rebuild" "$bin_dir/sudo" "$bin_dir/find"

  PATH="$bin_dir:/bin:/usr/bin:/usr/sbin:/sbin" \
    HOME="$home_dir" XDG_CONFIG_HOME="$config_dir" \
    NIX_TEST_EVENT_LOG="$log_file" NIX_TEST_FIXTURE_ROOT="$repo" \
    NIX_TEST_FORBIDDEN_HOME="$forbidden_home" NIX_TEST_FORBIDDEN_XDG_CONFIG="$forbidden_config" \
    DOTFILES_DARWIN_SUDO_LOCAL_PATH="$repo/etc/pam.d/sudo_local" \
    DOTFILES_DARWIN_ETC_SHELL_RC_PATHS="$repo/etc/bashrc:$repo/etc/zshrc" \
    "$TEST_ZSH_BIN" "$repo/scripts/nix_install.sh" --profile full > "$output_file"

  assert_contains "$log_file" "find:$home_dir"
  assert_contains "$log_file" "find:$config_dir"
  assert_output_contains "$output_file" "Flake path: $repo_abs"
  assert_contains "$log_file" "darwin-rebuild switch --impure --flake $repo_abs#"
  assert_contains "$log_file" "darwin-rebuild:switch --impure --flake $repo_abs#"
  assert_not_contains "$log_file" "path:$repo_abs"

  rm -rf "$repo"
}

test_rootless_nix_install_script_supports_no_sudo_linux() {
  assert_contains "$ROOTLESS_NIX_INSTALL_SCRIPT" 'nix-user-chroot'
  assert_contains "$ROOTLESS_NIX_INSTALL_SCRIPT" 'unshare --user --pid true'
  assert_contains "$ROOTLESS_NIX_INSTALL_SCRIPT" 'ROOTLESS_NIX_DIR'
  assert_contains "$ROOTLESS_NIX_INSTALL_SCRIPT" 'experimental-features = nix-command flakes'
  assert_contains "$ROOTLESS_NIX_INSTALL_SCRIPT" 'curl -L https://nixos.org/nix/install'
  assert_contains "$ROOTLESS_NIX_INSTALL_SCRIPT" '--no-daemon'
  assert_contains "$ROOTLESS_NIX_INSTALL_SCRIPT" 'nix-rootless'
  assert_contains "$ROOTLESS_NIX_INSTALL_SCRIPT" 'rootless-nix-shell'
  assert_contains "$ROOTLESS_NIX_INSTALL_SCRIPT" '--run'
  assert_contains "$ROOTLESS_NIX_INSTALL_SCRIPT" '--shell'
}

test_nix_portable_install_script_supports_no_sudo_nix_main_path() {
  local tmp_dir
  local log_file
  local output_file
  make_temp_dir
  tmp_dir="$REPLY"
  log_file="$tmp_dir/nix-portable.log"
  output_file="$tmp_dir/output.log"

  cat > "$tmp_dir/nix-portable" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
{
  printf 'NP_RUNTIME=%s\n' "${NP_RUNTIME:-}"
  printf 'ARGS=%s\n' "$*"
} >> "$NIX_PORTABLE_TEST_LOG"

if [[ "$*" == "nix --version" ]]; then
  printf 'nix (Nix) fake\n'
elif [[ "$*" == nix\ shell* ]]; then
  printf 'fake nix shell\n'
fi
EOF
  chmod +x "$tmp_dir/nix-portable"

  NIX_PORTABLE_BIN_DIR="$tmp_dir" NIX_PORTABLE_TEST_LOG="$log_file" \
    "$TEST_ZSH_BIN" "$NIX_PORTABLE_INSTALL_SCRIPT" > "$output_file"
  assert_output_contains "$output_file" "nix-portable is ready."
  assert_output_contains "$log_file" "NP_RUNTIME=proot"
  assert_output_contains "$log_file" "ARGS=nix --version"
  assert_executable "$tmp_dir/nixp"
  assert_executable "$tmp_dir/dotfiles-nix-shell"
  assert_executable "$tmp_dir/dotfiles-nix-run"

  : > "$log_file"
  NIX_PORTABLE_BIN_DIR="$tmp_dir" NIX_PORTABLE_TEST_LOG="$log_file" \
    "$tmp_dir/nixp" --version > "$output_file"
  assert_output_contains "$log_file" "ARGS=nix --version"

  : > "$log_file"
  NIX_PORTABLE_BIN_DIR="$tmp_dir" NIX_PORTABLE_TEST_LOG="$log_file" \
    "$tmp_dir/dotfiles-nix-run" echo ok > "$output_file"
  assert_output_contains "$log_file" "ARGS=nix shell path:$REPO_ROOT#dotfiles-cli-packages -c echo ok"

  : > "$log_file"
  NIX_PORTABLE_BIN_DIR="$tmp_dir" NIX_PORTABLE_TEST_LOG="$log_file" \
    "$TEST_ZSH_BIN" "$NIX_PORTABLE_INSTALL_SCRIPT" --with-gui-apps --run echo ok > "$output_file"
  assert_output_contains "$log_file" "ARGS=nix shell path:$REPO_ROOT#dotfiles-full-packages -c echo ok"

  rm -rf "$tmp_dir"
}

test_remove_homebrew_script_is_explicit_and_dry_run_first() {
  assert_contains "$REMOVE_HOMEBREW_SCRIPT" '--dry-run'
  assert_contains "$REMOVE_HOMEBREW_SCRIPT" '--apply'
  assert_contains "$REMOVE_HOMEBREW_SCRIPT" '--confirm-nix-ready'
  assert_contains "$REMOVE_HOMEBREW_SCRIPT" '--force'
  assert_contains "$REMOVE_HOMEBREW_SCRIPT" 'homebrew-fallback.nix'
  assert_contains "$REMOVE_HOMEBREW_SCRIPT" 'taps|brews|casks|vscode'
  assert_not_contains "$REMOVE_HOMEBREW_SCRIPT" 'mas_apps_has_entries'
  assert_contains "$REMOVE_HOMEBREW_SCRIPT" 'Refusing to remove Homebrew'
  assert_contains "$REMOVE_HOMEBREW_SCRIPT" 'source "$LIB_DIR/command.sh"'
  assert_contains "$REMOVE_HOMEBREW_SCRIPT" 'dotfiles_print_raw_command_block'
  assert_contains "$REMOVE_HOMEBREW_SCRIPT" 'Homebrew uninstall command'
  assert_contains "$REMOVE_HOMEBREW_SCRIPT" 'raw.githubusercontent.com/Homebrew/install/HEAD/uninstall.sh'
  assert_contains "$MAIN_SCRIPT" 'install_homebrew.sh'
}

test_cleanup_package_caches_script_supports_safe_nix_and_homebrew_cleanup() {
  local repo
  local repo_real
  local bin_dir
  local log_file
  local output_file

  make_temp_dir

  repo="$REPLY"
  repo_real="$(cd "$repo" && pwd)"
  bin_dir="$repo/bin"
  log_file="$repo/cleanup.log"
  output_file="$repo/output.log"

  assert_contains "$MISE_CONFIG" '[tasks.package-cleanup]'
  assert_contains "$MISE_CONFIG" 'alias = "nix-brew-cleanup"'
  assert_contains "$MISE_CONFIG" 'run = "zsh scripts/cleanup_package_caches.sh"'
  assert_contains "$CLEANUP_PACKAGE_CACHES_SCRIPT" '--older-than Nd'
  assert_contains "$CLEANUP_PACKAGE_CACHES_SCRIPT" '--apply'
  assert_contains "$CLEANUP_PACKAGE_CACHES_SCRIPT" 'source "$LIB_DIR/command.sh"'
  assert_contains "$CLEANUP_PACKAGE_CACHES_SCRIPT" 'dotfiles_run_or_print'
  assert_contains "$CLEANUP_PACKAGE_CACHES_SCRIPT" 'dotfiles_print_command'
  assert_contains "$CLEANUP_PACKAGE_CACHES_SCRIPT" 'nix profile wipe-history'
  assert_contains "$CLEANUP_PACKAGE_CACHES_SCRIPT" '--profile "$profile"'
  assert_contains "$CLEANUP_PACKAGE_CACHES_SCRIPT" 'nix store gc'
  assert_contains "$CLEANUP_PACKAGE_CACHES_SCRIPT" 'nix store optimise'
  assert_contains "$CLEANUP_PACKAGE_CACHES_SCRIPT" 'brew cleanup --prune=all --scrub'
  assert_contains "$CLEANUP_PACKAGE_CACHES_SCRIPT" '--include-mise'
  assert_contains "$CLEANUP_PACKAGE_CACHES_SCRIPT" 'mise prune'
  assert_contains "$CLEANUP_PACKAGE_CACHES_SCRIPT" 'mise cache prune'

  mkdir -p "$repo/scripts/lib" "$bin_dir" "$repo/home/.local/state/nix/profiles"
  touch "$repo/home/.local/state/nix/profiles/profile" "$repo/home/.nix-profile"
  cp "$CLEANUP_PACKAGE_CACHES_SCRIPT" "$repo/scripts/cleanup_package_caches.sh"
  copy_script_libs "$repo"

  cat > "$bin_dir/nix" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
print -r -- "nix:\$*" >> "$log_file"
EOF
  cat > "$bin_dir/brew" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
print -r -- "brew:\$*" >> "$log_file"
if [[ "\${1:-}" == "--prefix" ]]; then
  print -r -- "/opt/homebrew"
fi
EOF
  cat > "$bin_dir/mise" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
print -r -- "mise:\$*:MISE_GLOBAL_CONFIG_FILE=\${MISE_GLOBAL_CONFIG_FILE:-}" >> "$log_file"
EOF

  chmod +x "$repo/scripts/cleanup_package_caches.sh" "$bin_dir/nix" "$bin_dir/brew" "$bin_dir/mise"

  HOME="$repo/home" HOMEBREW_PREFIX= PATH="$bin_dir:/bin:/usr/bin:/usr/sbin:/sbin" \
    "$TEST_ZSH_BIN" "$repo/scripts/cleanup_package_caches.sh" > "$output_file"

  assert_output_contains "$output_file" 'DRY-RUN: package caches were not removed'
  assert_output_contains "$output_file" "nix profile wipe-history --profile $repo/home/.local/state/nix/profiles/profile --older-than 30d"
  assert_output_contains "$output_file" "nix profile wipe-history --profile $repo/home/.nix-profile --older-than 30d"
  assert_output_contains "$output_file" 'nix store gc'
  assert_output_contains "$output_file" 'nix store optimise'
  assert_output_contains "$output_file" 'brew cleanup --prune=all --scrub'
  grep -Fq -- 'mise prune' "$output_file" && fail "expected mise cleanup to be opt-in"
  assert_not_exists "$log_file"

  HOME="$repo/home" HOMEBREW_PREFIX= PATH="$bin_dir:/bin:/usr/bin:/usr/sbin:/sbin" \
    "$TEST_ZSH_BIN" "$repo/scripts/cleanup_package_caches.sh" --include-mise > "$output_file"

  assert_output_contains "$output_file" "env MISE_GLOBAL_CONFIG_FILE=$repo_real/config/mise/config.toml mise prune --dry-run --tools"
  assert_output_contains "$output_file" 'mise cache prune --dry-run'
  assert_not_exists "$log_file"

  HOME="$repo/home" HOMEBREW_PREFIX= PATH="$bin_dir:/bin:/usr/bin:/usr/sbin:/sbin" \
    "$TEST_ZSH_BIN" "$repo/scripts/cleanup_package_caches.sh" --apply --include-mise > "$output_file"

  assert_contains "$log_file" "nix:profile wipe-history --profile $repo/home/.local/state/nix/profiles/profile --older-than 30d"
  assert_contains "$log_file" "nix:profile wipe-history --profile $repo/home/.nix-profile --older-than 30d"
  assert_contains "$log_file" 'nix:store gc'
  assert_contains "$log_file" 'nix:store optimise'
  assert_contains "$log_file" 'brew:cleanup --prune=all --scrub'
  assert_contains "$log_file" "mise:prune --yes --tools:MISE_GLOBAL_CONFIG_FILE=$repo_real/config/mise/config.toml"
  assert_contains "$log_file" 'mise:cache prune --yes:MISE_GLOBAL_CONFIG_FILE='

  rm -rf "$repo"
}

test_install_homebrew_script_supports_required_profiles() {
  assert_contains "$INSTALL_HOMEBREW_SCRIPT" '--dry-run'
  assert_contains "$INSTALL_HOMEBREW_SCRIPT" 'source "$LIB_DIR/homebrew_fallback.sh"'
  assert_contains "$INSTALL_HOMEBREW_SCRIPT" 'dotfiles_profile_requires_homebrew'
  assert_contains "$INSTALL_HOMEBREW_SCRIPT" 'raw.githubusercontent.com/Homebrew/install/HEAD/install.sh'
  assert_contains "$INSTALL_HOMEBREW_SCRIPT" 'dotfiles_prepend_homebrew_to_path'
  assert_contains "$INSTALL_HOMEBREW_SCRIPT" 'Skipping Homebrew install because the selected profile does not require it'
  assert_contains "$HOMEBREW_LIB" 'dotfiles_find_homebrew'
  assert_contains "$HOMEBREW_LIB" '/opt/homebrew/bin/brew'
  assert_contains "$HOMEBREW_LIB" '/usr/local/bin/brew'
  assert_not_contains "$HOMEBREW_LIB" '$(dotfiles_find_homebrew'
  assert_not_contains "$HOMEBREW_LIB" '$(dirname "$brew_path")'
  assert_contains "$HOMEBREW_LIB" 'path_rest="$PATH"'
  assert_not_contains "$HOMEBREW_LIB" 'for candidate_dir in $PATH'
  assert_contains "$HOMEBREW_FALLBACK_LIB" 'dotfiles_homebrew_fallback_has_cli_entries'
  assert_contains "$HOMEBREW_FALLBACK_LIB" 'dotfiles_homebrew_fallback_has_gui_entries'
  assert_contains "$HOMEBREW_FALLBACK_LIB" 'dotfiles_profile_requires_homebrew'
  assert_contains "$HOMEBREW_FALLBACK_LIB" 'dotfiles_list_nix_setting_has_entries'
  assert_contains "$RUNTIME_LIB" 'dotfiles_resolve_command_from_path'
  assert_contains "$RUNTIME_LIB" 'dotfiles_create_unique_temp_directory'
  assert_contains "$RUNTIME_LIB" 'dotfiles_create_unique_temp_file'
  assert_not_contains "$REPO_ROOT/scripts/lib/setup_profile.sh" '$(dotfiles_default_profile)'
  assert_not_contains "$REPO_ROOT/scripts/lib/setup_profile.sh" '$(uname -s)'
}

test_main_mise_shell_and_hooks_use_nix_as_the_setup_path() {
  assert_contains "$MAIN_SCRIPT" 'nix_install.sh'
  assert_contains "$MAIN_SCRIPT" 'setup_agent_files.sh'
  assert_contains "$MAIN_SCRIPT" 'install_mas_apps.sh'
  assert_contains "$MAIN_SCRIPT" 'install_mas_apps_best_effort'
  assert_contains "$MAIN_SCRIPT" 'prepare_sudo_authentication'
  assert_contains "$MAIN_SCRIPT" 'sudo -v'
  assert_contains "$MAIN_SCRIPT" 'sudo -n true'
  assert_contains "$MAIN_SCRIPT" 'DOTFILES_SKIP_SUDO_KEEPALIVE'
  assert_contains "$MAIN_SCRIPT" 'install_rosetta_if_needed'
  assert_contains "$MAIN_SCRIPT" 'DOTFILES_SKIP_ROSETTA_INSTALL'
  assert_contains "$MAIN_SCRIPT" 'softwareupdate --install-rosetta --agree-to-license'
  assert_not_contains "$MAIN_SCRIPT" 'dotfiles/.agent/sync.sh'
  assert_contains "$MAIN_SCRIPT" 'install_homebrew.sh'
  assert_contains "$MAIN_SCRIPT" '--profile "$profile"'
  assert_contains "$MISE_CONFIG" '[tasks.nix-apply]'
  assert_contains "$MISE_CONFIG" '[tasks.chezmoi-status]'
  assert_contains "$MISE_CONFIG" 'run = "zsh scripts/chezmoi_apply.sh --verify"'
  assert_contains "$MISE_CONFIG" 'run = "zsh scripts/nix_install.sh --cli-only"'
  assert_not_contains "$MISE_CONFIG" '[tasks.nix-apply-cli]'
  assert_contains "$MISE_CONFIG" '[tasks.nix-apply-with-gui-apps]'
  assert_contains "$MISE_CONFIG" 'run = "zsh scripts/nix_install.sh --with-gui-apps"'
  assert_contains "$MISE_CONFIG" '[tasks.nix-portable-install]'
  assert_contains "$MISE_CONFIG" 'run = "zsh scripts/nix_portable_install.sh"'
  assert_contains "$MISE_CONFIG" '[tasks.nix-portable-shell]'
  assert_contains "$MISE_CONFIG" 'run = "zsh scripts/nix_portable_install.sh --shell"'
  assert_contains "$MISE_CONFIG" 'run = "zsh scripts/remove_homebrew.sh --apply --confirm-nix-ready"'
  assert_not_contains "$MISE_CONFIG" '[tasks.homebrew-dump]'
  assert_not_contains "$MISE_CONFIG" 'brew_dump.sh'
  assert_contains "$HOME_MANAGER_ZSH_MODULE" 'programs.zsh.enable = true'
  assert_contains "$HOME_MANAGER_ZSH_MODULE" 'dotfiles-shell-common.sh'
  assert_contains "$HOME_MANAGER_ZSH_MODULE" 'command mise activate zsh'
  assert_contains "$HOME_MANAGER_ZSH_MODULE" 'programs.zsh.oh-my-zsh.enable = true'
  assert_not_contains "$HOME_MANAGER_ZSH_MODULE" 'HOMEBREW_PREFIX'
  assert_not_contains "$HOME_MANAGER_ZSH_MODULE" 'brew shellenv'
  assert_contains "$APPLY_UPDATES_SCRIPT" 'setup_agent_files.sh'
  assert_contains "$APPLY_UPDATES_SCRIPT" 'chezmoi_apply.sh'
  assert_not_contains "$APPLY_UPDATES_SCRIPT" 'dotfiles/.agent/sync.sh'
  assert_not_contains "$APPLY_UPDATES_SCRIPT" "sync_nix_profile"
}

test_main_script_runs_homebrew_before_nix_setup() {
  skip_unless_macos "$funcstack[1]" || return 0

  local repo
  local home_dir
  local bin_dir
  local log_file
  local install_line
  local nix_line

  make_temp_dir

  repo="$REPLY"
  home_dir="$repo/home"
  bin_dir="$repo/bin"
  log_file="$repo/main.log"

  mkdir -p "$repo/scripts/lib" "$repo/dotfiles/.agent" "$home_dir" "$bin_dir"
  cp "$MAIN_SCRIPT" "$repo/main.sh"
  copy_script_libs "$repo"

  cat > "$repo/scripts/install_homebrew.sh" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
print -r -- "install_homebrew:\$*" >> "$log_file"
EOF
  cat > "$repo/scripts/nix_install.sh" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
print -r -- "nix_install:\$*" >> "$log_file"
EOF
  cat > "$repo/scripts/chezmoi_apply.sh" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
print -r -- "chezmoi_apply:\$*" >> "$log_file"
EOF
  cat > "$repo/scripts/setup_git_hooks.sh" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
print -r -- "setup_git_hooks:\$*" >> "$log_file"
EOF
  cat > "$repo/scripts/setup_agent_files.sh" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
print -r -- "sync_agent" >> "$log_file"
EOF
  cat > "$repo/scripts/install_mas_apps.sh" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
print -r -- "install_mas_apps:\$*" >> "$log_file"
EOF
  cat > "$bin_dir/mise" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
print -r -- "mise:\$*" >> "$log_file"
EOF
  cat > "$bin_dir/nix" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
print -r -- "nix:\$*" >> "$log_file"
EOF

  chmod +x \
    "$repo/scripts/install_homebrew.sh" \
    "$repo/scripts/nix_install.sh" \
    "$repo/scripts/chezmoi_apply.sh" \
    "$repo/scripts/setup_agent_files.sh" \
    "$repo/scripts/install_mas_apps.sh" \
    "$repo/scripts/setup_git_hooks.sh" \
    "$bin_dir/nix" \
    "$bin_dir/mise"

  HOME="$home_dir" USER=dotfiles-test PATH="$bin_dir:/bin:/usr/bin:/usr/sbin:/sbin" \
    DOTFILES_SKIP_SUDO_KEEPALIVE=1 \
    DOTFILES_SKIP_ROSETTA_INSTALL=1 \
    "$TEST_ZSH_BIN" "$repo/main.sh" --profile full > "$repo/output.log"

  assert_output_contains "$repo/output.log" "Setup completed successfully!"
  assert_contains "$log_file" 'sync_agent'
  assert_contains "$log_file" 'install_homebrew:--profile full'
  assert_contains "$log_file" 'nix_install:--profile full'
  assert_contains "$log_file" 'chezmoi_apply:--profile full --mark-default'
  assert_contains "$log_file" 'setup_git_hooks:--profile full'
  assert_contains "$log_file" 'mise:install'

  install_line="$(grep -n 'install_homebrew:--profile full' "$log_file" | cut -d: -f1)"
  nix_line="$(grep -n 'nix_install:--profile full' "$log_file" | cut -d: -f1)"
  [[ -n "$install_line" && -n "$nix_line" && "$install_line" -lt "$nix_line" ]] || \
    fail "expected Homebrew install step to run before nix_install"

  rm -rf "$repo"
}

test_main_script_can_skip_mas_apps_for_full_profile() {
  skip_unless_macos "$funcstack[1]" || return 0

  local repo
  local home_dir
  local bin_dir
  local log_file

  make_temp_dir

  repo="$REPLY"
  home_dir="$repo/home"
  bin_dir="$repo/bin"
  log_file="$repo/main.log"

  mkdir -p "$repo/scripts/lib" "$repo/dotfiles/.agent" "$home_dir" "$bin_dir"
  cp "$MAIN_SCRIPT" "$repo/main.sh"
  copy_script_libs "$repo"

  cat > "$repo/scripts/install_homebrew.sh" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
print -r -- "install_homebrew:\$*" >> "$log_file"
EOF
  cat > "$repo/scripts/nix_install.sh" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
print -r -- "nix_install:\$*" >> "$log_file"
EOF
  cat > "$repo/scripts/chezmoi_apply.sh" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
print -r -- "chezmoi_apply:\$*" >> "$log_file"
EOF
  cat > "$repo/scripts/setup_git_hooks.sh" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
print -r -- "setup_git_hooks:\$*" >> "$log_file"
EOF
  cat > "$repo/scripts/setup_agent_files.sh" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
print -r -- "sync_agent" >> "$log_file"
EOF
  cat > "$repo/scripts/install_mas_apps.sh" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
print -r -- "install_mas_apps:\${*}:skip=\${DOTFILES_SKIP_MAS_APPS:-0}" >> "$log_file"
EOF
  cat > "$bin_dir/mise" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
print -r -- "mise:\$*" >> "$log_file"
EOF
  cat > "$bin_dir/nix" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
print -r -- "nix:\$*" >> "$log_file"
EOF

  chmod +x \
    "$repo/scripts/install_homebrew.sh" \
    "$repo/scripts/nix_install.sh" \
    "$repo/scripts/chezmoi_apply.sh" \
    "$repo/scripts/setup_agent_files.sh" \
    "$repo/scripts/install_mas_apps.sh" \
    "$repo/scripts/setup_git_hooks.sh" \
    "$bin_dir/nix" \
    "$bin_dir/mise"

  HOME="$home_dir" USER=dotfiles-test PATH="$bin_dir:/bin:/usr/bin:/usr/sbin:/sbin" \
    DOTFILES_SKIP_SUDO_KEEPALIVE=1 \
    DOTFILES_SKIP_ROSETTA_INSTALL=1 \
    "$TEST_ZSH_BIN" "$repo/main.sh" --full --skip-mas-apps > "$repo/output.log"

  assert_output_contains "$repo/output.log" "Profile: full"
  assert_output_contains "$repo/output.log" "Setup completed successfully!"
  assert_contains "$log_file" 'nix_install:--profile full'
  assert_contains "$log_file" 'install_mas_apps:--profile full:skip=1'

  rm -rf "$repo"
}

test_main_script_uses_cli_profile_when_requested() {
  local repo
  local home_dir
  local bin_dir
  local log_file

  make_temp_dir

  repo="$REPLY"
  home_dir="$repo/home"
  bin_dir="$repo/bin"
  log_file="$repo/main.log"

  mkdir -p "$repo/scripts/lib" "$repo/dotfiles/.agent" "$home_dir" "$bin_dir"
  cp "$MAIN_SCRIPT" "$repo/main.sh"
  copy_script_libs "$repo"
  cat > "$repo/scripts/install_homebrew.sh" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
print -r -- "install_homebrew:\$*" >> "$log_file"
EOF

  cat > "$bin_dir/uname" <<'EOF'
#!/usr/bin/env zsh
set -euo pipefail
if [[ "${1:-}" == "-s" ]]; then
  print -r -- "Linux"
elif [[ "${1:-}" == "-m" ]]; then
  print -r -- "x86_64"
else
  print -r -- "Linux"
fi
EOF
  cat > "$bin_dir/curl" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
print -r -- "curl:\$*" >> "$log_file"
exit 99
EOF
  cat > "$repo/scripts/nix_install.sh" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
print -r -- "nix_install:\$*" >> "$log_file"
EOF
  cat > "$repo/scripts/chezmoi_apply.sh" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
print -r -- "chezmoi_apply:\$*" >> "$log_file"
EOF
  cat > "$repo/scripts/setup_git_hooks.sh" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
print -r -- "setup_git_hooks:\$*" >> "$log_file"
EOF
  cat > "$repo/scripts/setup_agent_files.sh" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
print -r -- "sync_agent" >> "$log_file"
EOF
  cat > "$repo/scripts/install_mas_apps.sh" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
print -r -- "install_mas_apps:\$*" >> "$log_file"
EOF
  cat > "$bin_dir/mise" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
print -r -- "mise:\$*" >> "$log_file"
EOF
  cat > "$bin_dir/nix" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
print -r -- "nix:\$*" >> "$log_file"
EOF

  chmod +x \
    "$repo/scripts/install_homebrew.sh" \
    "$repo/scripts/nix_install.sh" \
    "$repo/scripts/chezmoi_apply.sh" \
    "$repo/scripts/setup_agent_files.sh" \
    "$repo/scripts/install_mas_apps.sh" \
    "$repo/scripts/setup_git_hooks.sh" \
    "$bin_dir/uname" \
    "$bin_dir/curl" \
    "$bin_dir/nix" \
    "$bin_dir/mise"

  HOME="$home_dir" USER=dotfiles-test PATH="$bin_dir:/bin:/usr/bin:/usr/sbin:/sbin" \
    DOTFILES_SKIP_SUDO_KEEPALIVE=1 \
    "$TEST_ZSH_BIN" "$repo/main.sh" --cli-only > "$repo/output.log"

  assert_output_contains "$repo/output.log" "Profile: cli"
  assert_output_contains "$repo/output.log" "Setup completed successfully!"
  assert_contains "$log_file" 'sync_agent'
  assert_contains "$log_file" 'install_homebrew:--profile cli'
  assert_contains "$log_file" 'nix_install:--profile cli'
  assert_contains "$log_file" 'chezmoi_apply:--profile cli --mark-default'
  assert_contains "$log_file" 'setup_git_hooks:--profile cli'
  assert_contains "$log_file" 'mise:install python uv'
  assert_contains "$log_file" 'mise:which python3'
  assert_contains "$log_file" 'mise:which uv'
  assert_contains "$log_file" 'mise:install'
  assert_not_contains "$log_file" 'curl:'

  rm -rf "$repo"
}

test_main_script_bootstraps_nix_on_macos_when_missing() {
  skip_unless_macos "$funcstack[1]" || return 0

  local repo
  local home_dir
  local bin_dir
  local log_file

  make_temp_dir

  repo="$REPLY"
  home_dir="$repo/home"
  bin_dir="$repo/bin"
  log_file="$repo/main.log"

  mkdir -p "$repo/scripts/lib" "$repo/dotfiles/.agent" "$home_dir" "$bin_dir"
  cp "$MAIN_SCRIPT" "$repo/main.sh"
  copy_script_libs "$repo"

  cat > "$repo/scripts/install_homebrew.sh" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
print -r -- "install_homebrew:\$*" >> "$log_file"
EOF
  cat > "$repo/scripts/nix_install.sh" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
print -r -- "nix_install:\$*" >> "$log_file"
EOF
  cat > "$repo/scripts/chezmoi_apply.sh" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
print -r -- "chezmoi_apply:\$*" >> "$log_file"
EOF
  cat > "$repo/scripts/setup_git_hooks.sh" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
print -r -- "setup_git_hooks:\$*" >> "$log_file"
EOF
  cat > "$repo/scripts/setup_agent_files.sh" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
print -r -- "sync_agent" >> "$log_file"
EOF
  cat > "$repo/scripts/install_mas_apps.sh" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
print -r -- "install_mas_apps:\$*" >> "$log_file"
EOF
  cat > "$bin_dir/mise" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
print -r -- "mise:\$*" >> "$log_file"
EOF
  cat > "$bin_dir/curl" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
print -r -- "curl:\$*" >> "$log_file"
local output=""
while (( \$# )); do
  if [[ "\$1" == "-o" ]]; then
    shift
    output="\$1"
  fi
  shift || true
done
[[ -n "\$output" ]] || exit 1
print -r -- "# fake nix installer" > "\$output"
EOF
  cat > "$bin_dir/sh" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
print -r -- "sh:\$*" >> "$log_file"
mkdir -p "\$HOME/.nix-profile/bin"
cat > "\$HOME/.nix-profile/bin/nix" <<'INNER'
#!/usr/bin/env zsh
set -euo pipefail
exit 0
INNER
chmod +x "\$HOME/.nix-profile/bin/nix"
EOF

  chmod +x \
    "$repo/scripts/install_homebrew.sh" \
    "$repo/scripts/nix_install.sh" \
    "$repo/scripts/chezmoi_apply.sh" \
    "$repo/scripts/setup_agent_files.sh" \
    "$repo/scripts/install_mas_apps.sh" \
    "$repo/scripts/setup_git_hooks.sh" \
    "$bin_dir/curl" \
    "$bin_dir/mise" \
    "$bin_dir/sh"

  HOME="$home_dir" USER=dotfiles-test PATH="$bin_dir:/bin:/usr/bin:/usr/sbin:/sbin" \
    DOTFILES_SKIP_SUDO_KEEPALIVE=1 \
    DOTFILES_SKIP_ROSETTA_INSTALL=1 \
    DOTFILES_NIX_PROFILE_PATHS="$home_dir/.nix-profile/bin" \
    DOTFILES_NIX_INSTALL_SHELL="$bin_dir/sh" \
    "$TEST_ZSH_BIN" "$repo/main.sh" --profile full > "$repo/output.log"

  assert_output_contains "$repo/output.log" "Installing Nix daemon"
  assert_output_contains "$repo/output.log" "Nix installed"
  assert_output_contains "$repo/output.log" "Setup completed successfully!"
  assert_contains "$log_file" 'install_homebrew:--profile full'
  assert_contains "$log_file" "curl:--fail --proto =https --tlsv1.2 -L https://nixos.org/nix/install -o "
  assert_contains "$log_file" 'sh:'
  assert_contains "$log_file" '--daemon'
  assert_contains "$log_file" '--yes'
  assert_contains "$log_file" 'nix_install:--profile full'
  assert_contains "$log_file" 'mise:install'

  rm -rf "$repo"
}

test_main_script_keeps_sudo_authentication_alive_on_macos() {
  skip_unless_macos "$funcstack[1]" || return 0

  local repo
  local home_dir
  local bin_dir
  local log_file

  make_temp_dir

  repo="$REPLY"
  home_dir="$repo/home"
  bin_dir="$repo/bin"
  log_file="$repo/main.log"

  mkdir -p "$repo/scripts/lib" "$repo/dotfiles/.agent" "$home_dir" "$bin_dir"
  cp "$MAIN_SCRIPT" "$repo/main.sh"
  copy_script_libs "$repo"

  cat > "$repo/scripts/install_homebrew.sh" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
print -r -- "install_homebrew:\$*" >> "$log_file"
EOF
  cat > "$repo/scripts/nix_install.sh" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
print -r -- "nix_install:\$*" >> "$log_file"
EOF
  cat > "$repo/scripts/chezmoi_apply.sh" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
print -r -- "chezmoi_apply:\$*" >> "$log_file"
EOF
  cat > "$repo/scripts/setup_git_hooks.sh" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
print -r -- "setup_git_hooks:\$*" >> "$log_file"
EOF
  cat > "$repo/scripts/setup_agent_files.sh" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
print -r -- "sync_agent" >> "$log_file"
EOF
  cat > "$repo/scripts/install_mas_apps.sh" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
print -r -- "install_mas_apps:\$*" >> "$log_file"
EOF
  cat > "$bin_dir/mise" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
print -r -- "mise:\$*" >> "$log_file"
EOF
  cat > "$bin_dir/nix" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
print -r -- "nix:\$*" >> "$log_file"
EOF
  cat > "$bin_dir/sudo" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
print -r -- "sudo:\$*" >> "$log_file"
EOF

  chmod +x \
    "$repo/scripts/install_homebrew.sh" \
    "$repo/scripts/nix_install.sh" \
    "$repo/scripts/chezmoi_apply.sh" \
    "$repo/scripts/setup_agent_files.sh" \
    "$repo/scripts/install_mas_apps.sh" \
    "$repo/scripts/setup_git_hooks.sh" \
    "$bin_dir/nix" \
    "$bin_dir/sudo" \
    "$bin_dir/mise"

  HOME="$home_dir" USER=dotfiles-test PATH="$bin_dir:/bin:/usr/bin:/usr/sbin:/sbin" \
    DOTFILES_SKIP_ROSETTA_INSTALL=1 \
    "$TEST_ZSH_BIN" "$repo/main.sh" --profile full > "$repo/output.log"

  assert_output_contains "$repo/output.log" "Preparing sudo authentication for this setup run"
  assert_output_contains "$repo/output.log" "Sudo authentication cached"
  assert_output_contains "$repo/output.log" "Setup completed successfully!"
  assert_contains "$log_file" 'sudo:-v'
  assert_contains "$log_file" 'sudo:-n true'
  assert_contains "$log_file" 'nix_install:--profile full'

  rm -rf "$repo"
}

test_main_script_installs_rosetta_on_apple_silicon_full_profile() {
  skip_unless_macos "$funcstack[1]" || return 0

  local repo
  local home_dir
  local bin_dir
  local log_file

  make_temp_dir

  repo="$REPLY"
  home_dir="$repo/home"
  bin_dir="$repo/bin"
  log_file="$repo/main.log"

  mkdir -p "$repo/scripts/lib" "$repo/dotfiles/.agent" "$home_dir" "$bin_dir"
  cp "$MAIN_SCRIPT" "$repo/main.sh"
  copy_script_libs "$repo"

  cat > "$repo/scripts/install_homebrew.sh" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
print -r -- "install_homebrew:\$*" >> "$log_file"
EOF
  cat > "$repo/scripts/nix_install.sh" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
print -r -- "nix_install:\$*" >> "$log_file"
EOF
  cat > "$repo/scripts/chezmoi_apply.sh" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
print -r -- "chezmoi_apply:\$*" >> "$log_file"
EOF
  cat > "$repo/scripts/setup_git_hooks.sh" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
print -r -- "setup_git_hooks:\$*" >> "$log_file"
EOF
  cat > "$repo/scripts/setup_agent_files.sh" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
print -r -- "sync_agent" >> "$log_file"
EOF
  cat > "$repo/scripts/install_mas_apps.sh" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
print -r -- "install_mas_apps:\$*" >> "$log_file"
EOF
  cat > "$bin_dir/uname" <<'EOF'
#!/usr/bin/env zsh
set -euo pipefail
case "${1:-}" in
  -s) print -r -- "Darwin" ;;
  -m) print -r -- "arm64" ;;
  *) print -r -- "Darwin" ;;
esac
EOF
  cat > "$bin_dir/pkgutil" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
print -r -- "pkgutil:\$*" >> "$log_file"
exit 1
EOF
  cat > "$bin_dir/softwareupdate" <<'EOF'
#!/usr/bin/env zsh
set -euo pipefail
exit 0
EOF
  cat > "$bin_dir/sudo" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
print -r -- "sudo:\$*" >> "$log_file"
EOF
  cat > "$bin_dir/mise" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
print -r -- "mise:\$*" >> "$log_file"
EOF
  cat > "$bin_dir/nix" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
print -r -- "nix:\$*" >> "$log_file"
EOF

  chmod +x \
    "$repo/scripts/install_homebrew.sh" \
    "$repo/scripts/nix_install.sh" \
    "$repo/scripts/chezmoi_apply.sh" \
    "$repo/scripts/setup_agent_files.sh" \
    "$repo/scripts/install_mas_apps.sh" \
    "$repo/scripts/setup_git_hooks.sh" \
    "$bin_dir/uname" \
    "$bin_dir/pkgutil" \
    "$bin_dir/softwareupdate" \
    "$bin_dir/sudo" \
    "$bin_dir/nix" \
    "$bin_dir/mise"

  HOME="$home_dir" USER=dotfiles-test PATH="$bin_dir:/bin:/usr/bin:/usr/sbin:/sbin" \
    DOTFILES_SKIP_SUDO_KEEPALIVE=1 \
    "$TEST_ZSH_BIN" "$repo/main.sh" --profile full > "$repo/output.log"

  assert_output_contains "$repo/output.log" "Installing Rosetta 2 for Intel-only macOS installers"
  assert_output_contains "$repo/output.log" "Rosetta 2 installed"
  assert_output_contains "$repo/output.log" "Setup completed successfully!"
  assert_contains "$log_file" 'pkgutil:--pkg-info com.apple.pkg.RosettaUpdateAuto'
  assert_contains "$log_file" 'sudo:softwareupdate --install-rosetta --agree-to-license'
  assert_contains "$log_file" 'nix_install:--profile full'

  rm -rf "$repo"
}

test_install_mas_apps_script_continues_after_individual_failures() {
  skip_unless_macos "$funcstack[1]" || return 0

  local repo
  local home_dir
  local bin_dir
  local log_file
  local output_file

  make_temp_dir

  repo="$REPLY"
  home_dir="$repo/home"
  bin_dir="$repo/bin"
  log_file="$repo/mas.log"
  output_file="$repo/output.log"

  mkdir -p "$repo/scripts/lib" "$repo/config/nix" "$home_dir" "$bin_dir"
  cp "$INSTALL_MAS_APPS_SCRIPT" "$repo/scripts/install_mas_apps.sh"
  cp "$REPO_ROOT/scripts/lib/setup_profile.sh" "$repo/scripts/lib/setup_profile.sh"

  cat > "$repo/config/nix/mas-apps.nix" <<'EOF'
{
  "AlreadyInstalled" = 111;
  "BrokenDownload" = 333;
  "NewApp" = 222;
}
EOF
  cat > "$bin_dir/uname" <<'EOF'
#!/usr/bin/env zsh
set -euo pipefail
case "${1:-}" in
  -s) print -r -- "Darwin" ;;
  -m) print -r -- "arm64" ;;
  *) print -r -- "Darwin" ;;
esac
EOF
  cat > "$bin_dir/nix" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
print -r -- "nix:\$*" >> "$log_file"
if [[ "\$1" == "eval" ]]; then
  print -r -- \$'AlreadyInstalled\t111\nBrokenDownload\t333\nNewApp\t222'
fi
EOF
  cat > "$bin_dir/mas" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
print -r -- "mas:\$*" >> "$log_file"
case "\${1:-}" in
  list)
    print -r -- "111 AlreadyInstalled (1.0)"
    ;;
  install)
    if [[ "\${2:-}" == "333" ]]; then
      print -r -- "Error: No downloads initiated for ADAM ID 333" >&2
      exit 2
    fi
    ;;
esac
EOF

  chmod +x "$repo/scripts/install_mas_apps.sh" "$bin_dir/uname" "$bin_dir/nix" "$bin_dir/mas"

  HOME="$home_dir" USER=dotfiles-test PATH="$bin_dir:/bin:/usr/bin:/usr/sbin:/sbin" \
    "$TEST_ZSH_BIN" "$repo/scripts/install_mas_apps.sh" --profile full > "$output_file" 2>&1

  assert_output_contains "$output_file" "Installing Mac App Store apps best-effort (3 apps)"
  assert_output_contains "$output_file" "[1/3] Using AlreadyInstalled (111)"
  assert_output_contains "$output_file" "[2/3] Installing BrokenDownload (333)"
  assert_output_contains "$output_file" "[2/3] Installing BrokenDownload (333) failed; continuing."
  assert_output_contains "$output_file" "[3/3] Installing NewApp (222)"
  assert_output_contains "$output_file" "[3/3] Installed NewApp"
  assert_output_contains "$output_file" "Mac App Store app step complete: used=1 installed=1 failed=1"
  assert_contains "$log_file" 'mas:list'
  assert_contains "$log_file" 'mas:install 333'
  assert_contains "$log_file" 'mas:install 222'

  rm -rf "$repo"
}

test_main_script_reports_nix_portable_when_nix_missing_on_linux() {
  if is_test_macos; then
    echo "SKIP: $funcstack[1] requires Linux"
    return 0
  fi

  local repo
  local home_dir
  local bin_dir
  local log_file

  make_temp_dir

  repo="$REPLY"
  home_dir="$repo/home"
  bin_dir="$repo/bin"
  log_file="$repo/main.log"

  mkdir -p "$repo/scripts/lib" "$repo/dotfiles/.agent" "$home_dir" "$bin_dir"
  cp "$MAIN_SCRIPT" "$repo/main.sh"
  copy_script_libs "$repo"

  cat > "$repo/scripts/install_homebrew.sh" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
print -r -- "install_homebrew:\$*" >> "$log_file"
EOF
  cat > "$repo/scripts/nix_install.sh" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
print -r -- "nix_install:\$*" >> "$log_file"
EOF
  cat > "$repo/scripts/chezmoi_apply.sh" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
print -r -- "chezmoi_apply:\$*" >> "$log_file"
EOF
  cat > "$repo/scripts/setup_git_hooks.sh" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
print -r -- "setup_git_hooks:\$*" >> "$log_file"
EOF
  cat > "$repo/scripts/setup_agent_files.sh" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
print -r -- "sync_agent" >> "$log_file"
EOF
  cat > "$repo/scripts/install_mas_apps.sh" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
print -r -- "install_mas_apps:\$*" >> "$log_file"
EOF
  ln -s "$TEST_ZSH_BIN" "$bin_dir/zsh"
  ln -s "$(command -v dirname)" "$bin_dir/dirname"

  chmod +x \
    "$repo/scripts/install_homebrew.sh" \
    "$repo/scripts/nix_install.sh" \
    "$repo/scripts/chezmoi_apply.sh" \
    "$repo/scripts/setup_agent_files.sh" \
    "$repo/scripts/install_mas_apps.sh" \
    "$repo/scripts/setup_git_hooks.sh"

  if HOME="$home_dir" USER=dotfiles-test PATH="$bin_dir" \
    DOTFILES_NIX_PROFILE_PATHS="" \
    "$TEST_ZSH_BIN" "$repo/main.sh" --cli-only > "$repo/output.log" 2>&1; then
    fail "expected main.sh to fail when nix is missing on Linux"
  fi

  assert_output_contains "$repo/output.log" "nix is not installed or not found in PATH"
  assert_output_contains "$repo/output.log" "zsh scripts/nix_portable_install.sh"
  assert_contains "$log_file" 'install_homebrew:--profile cli'
  assert_not_contains "$log_file" 'nix_install:'

  rm -rf "$repo"
}

test_main_script_applies_chezmoi_instead_of_copying_legacy_dotfiles() {
  local repo
  local home_dir
  local bin_dir
  local log_file

  make_temp_dir

  repo="$REPLY"
  home_dir="$repo/home"
  bin_dir="$repo/bin"
  log_file="$repo/main.log"

  mkdir -p "$repo/scripts/lib" "$repo/dotfiles/.agent" "$home_dir" "$bin_dir"
  cp "$MAIN_SCRIPT" "$repo/main.sh"
  copy_script_libs "$repo"

  cat > "$repo/scripts/install_homebrew.sh" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
print -r -- "install_homebrew:\$*" >> "$log_file"
EOF
  cat > "$repo/scripts/nix_install.sh" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
print -r -- "nix_install:\$*" >> "$log_file"
EOF
  cat > "$repo/scripts/chezmoi_apply.sh" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
print -r -- "chezmoi_apply:\$*" >> "$log_file"
EOF
  cat > "$repo/scripts/setup_git_hooks.sh" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
print -r -- "setup_git_hooks:\$*" >> "$log_file"
EOF
  cat > "$repo/scripts/setup_agent_files.sh" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
print -r -- "sync_agent" >> "$log_file"
EOF
  cat > "$repo/scripts/install_mas_apps.sh" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
print -r -- "install_mas_apps:\$*" >> "$log_file"
EOF
  cat > "$bin_dir/mise" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
print -r -- "mise:\$*" >> "$log_file"
EOF
  cat > "$bin_dir/nix" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
print -r -- "nix:\$*" >> "$log_file"
EOF

  chmod +x \
    "$repo/scripts/install_homebrew.sh" \
    "$repo/scripts/nix_install.sh" \
    "$repo/scripts/chezmoi_apply.sh" \
    "$repo/scripts/setup_agent_files.sh" \
    "$repo/scripts/install_mas_apps.sh" \
    "$repo/scripts/setup_git_hooks.sh" \
    "$bin_dir/nix" \
    "$bin_dir/mise"

  HOME="$home_dir" USER=dotfiles-test PATH="$bin_dir:/bin:/usr/bin:/usr/sbin:/sbin" \
    DOTFILES_SKIP_SUDO_KEEPALIVE=1 \
    "$TEST_ZSH_BIN" "$repo/main.sh" --cli-only > "$repo/output.log"

  assert_output_contains "$repo/output.log" "Setup completed successfully!"
  assert_contains "$log_file" 'chezmoi_apply:--profile cli --mark-default'
  assert_not_contains "$log_file" 'setup_config'

  rm -rf "$repo"
}

test_apply_updates_applies_chezmoi_and_refreshes_agent_and_hooks() {
  local repo
  local home_dir
  local log_file

  make_temp_dir

  repo="$REPLY"
  home_dir="$repo/home"
  log_file="$repo/apply.log"

  mkdir -p "$repo/scripts/lib" "$repo/dotfiles/.agent" "$home_dir"
  cp "$APPLY_UPDATES_SCRIPT" "$repo/scripts/apply_updates.sh"
  cp "$REPO_ROOT/scripts/lib/setup_profile.sh" "$repo/scripts/lib/setup_profile.sh"

  cat > "$repo/scripts/chezmoi_apply.sh" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
print -r -- "chezmoi_apply:\$*" >> "$log_file"
EOF
  cat > "$repo/scripts/setup_git_hooks.sh" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
print -r -- "setup_git_hooks:\$*" >> "$log_file"
EOF
  cat > "$repo/scripts/setup_agent_files.sh" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
print -r -- "sync_agent" >> "$log_file"
EOF
  cat > "$repo/scripts/install_mas_apps.sh" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
print -r -- "install_mas_apps:\$*" >> "$log_file"
EOF

  chmod +x \
    "$repo/scripts/chezmoi_apply.sh" \
    "$repo/scripts/setup_agent_files.sh" \
    "$repo/scripts/install_mas_apps.sh" \
    "$repo/scripts/setup_git_hooks.sh"

  HOME="$home_dir" USER=dotfiles-test PATH="/bin:/usr/bin:/usr/sbin:/sbin" \
    "$TEST_ZSH_BIN" "$repo/scripts/apply_updates.sh" --cli-only > "$repo/output.log"

  assert_output_contains "$repo/output.log" "Dotfiles update complete"
  assert_contains "$log_file" 'chezmoi_apply:--profile cli'
  assert_contains "$log_file" 'sync_agent'
  assert_contains "$log_file" 'setup_git_hooks:--profile cli'

  rm -rf "$repo"
}

test_setup_git_hooks_generates_executable_hooks_with_valid_zsh_shebang() {
  local repo
  local home_dir
  local hook_file
  local xdg_config_home
  make_temp_dir
  repo="$REPLY"
  home_dir="$repo/home"
  hook_file="$repo/.git/hooks/post-checkout"
  xdg_config_home="$repo/xdg"

  mkdir -p "$repo/scripts/lib" "$xdg_config_home" "$home_dir"
  cp "$SETUP_GIT_HOOKS_SCRIPT" "$repo/scripts/setup_git_hooks.sh"
  cp "$REPO_ROOT/scripts/lib/setup_profile.sh" "$repo/scripts/lib/setup_profile.sh"

  HOME="$home_dir" GIT_CONFIG_GLOBAL=/dev/null git -C "$repo" init >/dev/null
  HOME="$home_dir" XDG_CONFIG_HOME="$xdg_config_home" GIT_CONFIG_GLOBAL=/dev/null \
    "$TEST_ZSH_BIN" "$repo/scripts/setup_git_hooks.sh" --cli-only >/dev/null

  assert_executable "$hook_file"
  assert_contains "$hook_file" '#!/bin/zsh'
  assert_not_contains "$hook_file" '#!/usr/bin/zsh'

  rm -rf "$repo"
}

test_ai_cli_tools_are_managed_by_mise() {
  assert_contains "$MISE_CONFIG" 'codex = "latest"'
  assert_contains "$MISE_CONFIG" 'claude-code = "latest"'
  assert_contains "$MISE_CONFIG" '"github:ogulcancelik/herdr" = "latest"'
  assert_not_contains "$MISE_CONFIG" 'gemini-cli = "latest"'
  assert_not_contains "$NIX_PACKAGE_NAMES_FILE" '"codex"'
  assert_not_contains "$NIX_PACKAGE_NAMES_FILE" '"gemini-cli"'
  assert_not_contains "$NIX_GUI_COMMON_PACKAGE_NAMES_FILE" '"claude-code"'
  assert_not_contains "$HOMEBREW_FALLBACK_FILE" '"claude-code@latest"'
  assert_not_contains "$HOMEBREW_FALLBACK_FILE" '"codex"'
  assert_not_contains "$HOMEBREW_FALLBACK_FILE" '"gemini-cli"'
  assert_not_contains "$FLAKE_FILE" 'packageVersionOverrides'
  assert_not_contains "$FLAKE_FILE" 'codex = prev.codex.overrideAttrs'
  assert_not_contains "$FLAKE_FILE" 'codexOverlay'
  assert_not_contains "$NIX_GUI_COMMON_PACKAGE_NAMES_FILE" '"codex"'
  assert_contains "$HOMEBREW_FALLBACK_FILE" '"microsoft-office"'
  assert_not_contains "$HOMEBREW_FALLBACK_FILE" '"onedrive"'
  assert_contains "$INSTALL_SCRIPT" 'Homebrew is required for this Nix profile'
  assert_contains "$INSTALL_SCRIPT" 'Use --cli-only'
  assert_contains "$UPDATE_MANAGED_VERSIONS_SCRIPT" 'resolve_nix_apply_profile'
  assert_contains "$UPDATE_MANAGED_VERSIONS_SCRIPT" 'falling back to the CLI Nix profile'
  assert_contains "$UPDATE_MANAGED_VERSIONS_SCRIPT" 'warn "Homebrew is not installed; falling back to the CLI Nix profile'
  assert_contains "$UPDATE_MANAGED_VERSIONS_SCRIPT" 'dotfiles_homebrew_fallback_has_cli_entries'
}

test_managed_update_script_skips_gui_profile_on_macos_unless_requested() {
  skip_unless_macos "$funcstack[1]" || return 0

  local repo
  local home_dir
  local bin_dir
  local log_file

  make_temp_dir

  repo="$REPLY"
  home_dir="$repo/home"
  bin_dir="$repo/bin"
  log_file="$repo/update.log"

  mkdir -p "$repo/scripts/lib" "$repo/config/nix" "$home_dir" "$bin_dir"
  cp "$UPDATE_MANAGED_VERSIONS_SCRIPT" "$repo/scripts/update_managed_versions.sh"
  copy_script_libs "$repo"

  cat > "$repo/config/nix/homebrew-fallback.nix" <<'EOF'
{
  taps = [
  ];

  brews = [
  ];

  casks = [
    "anki"
  ];

  vscode = [
  ];

  unsupportedUvPackages = [
  ];
}
EOF
  cat > "$repo/config/nix/mas-apps.nix" <<'EOF'
{ }
EOF
  cat > "$repo/scripts/nix_install.sh" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
print -r -- "nix_install:\$*" >> "$log_file"
EOF
  cat > "$bin_dir/uname" <<'EOF'
#!/usr/bin/env zsh
set -euo pipefail
case "${1:-}" in
  -s) print -r -- "Darwin" ;;
  -m) print -r -- "arm64" ;;
  *) print -r -- "Darwin" ;;
esac
EOF
  cat > "$bin_dir/nix" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
print -r -- "nix:\$*" >> "$log_file"
EOF
  cat > "$bin_dir/brew" <<'EOF'
#!/usr/bin/env zsh
set -euo pipefail
exit 0
EOF

  chmod +x \
    "$repo/scripts/update_managed_versions.sh" \
    "$repo/scripts/nix_install.sh" \
    "$bin_dir/uname" \
    "$bin_dir/nix" \
    "$bin_dir/brew"

  HOME="$home_dir" USER=dotfiles-test PATH="$bin_dir:/bin:/usr/bin:/usr/sbin:/sbin" \
    "$TEST_ZSH_BIN" "$repo/scripts/update_managed_versions.sh" --only nix > "$repo/output.log" 2>&1

  assert_contains "$log_file" 'nix:flake update'
  assert_contains "$log_file" 'nix_install:--profile cli'
  assert_not_contains "$log_file" '--with-gui-apps'
  assert_output_contains "$repo/output.log" 'Managed update defaults to the CLI Nix profile on macOS'

  rm -rf "$repo"
}

test_managed_update_script_includes_gui_profile_when_requested() {
  local repo
  local home_dir
  local bin_dir
  local log_file
  local expected_nix_install_args

  make_temp_dir

  repo="$REPLY"
  home_dir="$repo/home"
  bin_dir="$repo/bin"
  log_file="$repo/update.log"

  mkdir -p "$repo/scripts/lib" "$repo/config/nix" "$home_dir" "$bin_dir"
  cp "$UPDATE_MANAGED_VERSIONS_SCRIPT" "$repo/scripts/update_managed_versions.sh"
  copy_script_libs "$repo"

  cat > "$repo/config/nix/homebrew-fallback.nix" <<'EOF'
{
  taps = [
  ];

  brews = [
  ];

  casks = [
    "anki"
  ];

  vscode = [
  ];

  unsupportedUvPackages = [
  ];
}
EOF
  cat > "$repo/config/nix/mas-apps.nix" <<'EOF'
{ }
EOF
  cat > "$repo/scripts/nix_install.sh" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
print -r -- "nix_install:\$*" >> "$log_file"
EOF
  cat > "$bin_dir/uname" <<'EOF'
#!/usr/bin/env zsh
set -euo pipefail
case "${1:-}" in
  -s) print -r -- "Darwin" ;;
  -m) print -r -- "arm64" ;;
  *) print -r -- "Darwin" ;;
esac
EOF
  cat > "$bin_dir/nix" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
print -r -- "nix:\$*" >> "$log_file"
EOF
  cat > "$bin_dir/brew" <<'EOF'
#!/usr/bin/env zsh
set -euo pipefail
if [[ "${1:-}" == "info" ]]; then
  cat <<'JSON'
{"casks":[{"token":"anki","full_token":"anki","auto_updates":false}]}
JSON
fi
EOF

  chmod +x \
    "$repo/scripts/update_managed_versions.sh" \
    "$repo/scripts/nix_install.sh" \
    "$bin_dir/uname" \
    "$bin_dir/nix" \
    "$bin_dir/brew"

  if [[ "$OSTYPE" == darwin* ]]; then
    expected_nix_install_args='nix_install:--profile full --with-gui-apps'
  else
    expected_nix_install_args='nix_install:--profile cli --with-gui-apps'
  fi

  DISPLAY="${DISPLAY:-:99}" HOME="$home_dir" HOMEBREW_PREFIX= USER=dotfiles-test PATH="$bin_dir:/bin:/usr/bin:/usr/sbin:/sbin" \
    "$TEST_ZSH_BIN" "$repo/scripts/update_managed_versions.sh" --only nix --with-gui-apps > "$repo/output.log"

  assert_contains "$log_file" 'nix:flake update'
  assert_contains "$log_file" "$expected_nix_install_args"

  rm -rf "$repo"
}

test_managed_update_script_upgrades_declared_homebrew_fallbacks_when_gui_requested() {
  skip_unless_macos "$funcstack[1]" || return 0

  local repo
  local home_dir
  local bin_dir
  local log_file

  make_temp_dir

  repo="$REPLY"
  home_dir="$repo/home"
  bin_dir="$repo/bin"
  log_file="$repo/update.log"

  mkdir -p "$repo/scripts/lib" "$repo/config/nix" "$home_dir" "$bin_dir"
  cp "$UPDATE_MANAGED_VERSIONS_SCRIPT" "$repo/scripts/update_managed_versions.sh"
  copy_script_libs "$repo"

  cat > "$repo/config/nix/homebrew-fallback.nix" <<'EOF'
{
  taps = [
    "lyraphase/pcloud"
  ];

  brews = [
    "example-tool"
  ];

  casks = [
    "anki"
    "lyraphase/pcloud/pcloud-drive"
  ];

  vscode = [
  ];

  unsupportedUvPackages = [
  ];
}
EOF
  cat > "$repo/config/nix/mas-apps.nix" <<'EOF'
{ }
EOF
  cat > "$repo/scripts/nix_install.sh" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
print -r -- "nix_install:\$*" >> "$log_file"
EOF
  cat > "$bin_dir/nix" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
print -r -- "nix:\$*" >> "$log_file"
EOF
  cat > "$bin_dir/brew" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
print -r -- "brew:\$*" >> "$log_file"
if [[ "\${1:-}" == "info" ]]; then
  cat <<'JSON'
{"casks":[{"token":"anki","full_token":"anki","auto_updates":false},{"token":"pcloud-drive","full_token":"lyraphase/pcloud/pcloud-drive","auto_updates":false}]}
JSON
fi
EOF

  chmod +x \
    "$repo/scripts/update_managed_versions.sh" \
    "$repo/scripts/nix_install.sh" \
    "$bin_dir/nix" \
    "$bin_dir/brew"

  HOME="$home_dir" HOMEBREW_PREFIX= USER=dotfiles-test PATH="$bin_dir:/bin:/usr/bin:/usr/sbin:/sbin" \
    "$TEST_ZSH_BIN" "$repo/scripts/update_managed_versions.sh" --only nix --with-gui-apps > "$repo/output.log"

  assert_contains "$log_file" 'nix_install:--profile full --with-gui-apps'
  assert_contains "$log_file" 'brew:update'
  assert_contains "$log_file" 'brew:upgrade example-tool'
  assert_contains "$log_file" 'brew:upgrade --cask anki lyraphase/pcloud/pcloud-drive'

  rm -rf "$repo"
}

test_managed_update_script_skips_auto_updating_homebrew_casks() {
  skip_unless_macos "$funcstack[1]" || return 0

  local repo
  local home_dir
  local bin_dir
  local log_file

  make_temp_dir

  repo="$REPLY"
  home_dir="$repo/home"
  bin_dir="$repo/bin"
  log_file="$repo/update.log"

  mkdir -p "$repo/scripts/lib" "$repo/config/nix" "$home_dir" "$bin_dir"
  cp "$UPDATE_MANAGED_VERSIONS_SCRIPT" "$repo/scripts/update_managed_versions.sh"
  copy_script_libs "$repo"

  cat > "$repo/config/nix/homebrew-fallback.nix" <<'EOF'
{
  taps = [
  ];

  brews = [
  ];

  casks = [
    "codex-app"
  ];

  vscode = [
  ];

  unsupportedUvPackages = [
  ];
}
EOF
  cat > "$repo/config/nix/mas-apps.nix" <<'EOF'
{ }
EOF
  cat > "$repo/scripts/nix_install.sh" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
print -r -- "nix_install:\$*" >> "$log_file"
EOF
  cat > "$bin_dir/nix" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
print -r -- "nix:\$*" >> "$log_file"
EOF
  cat > "$bin_dir/brew" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
print -r -- "brew:\$*" >> "$log_file"
if [[ "\${1:-}" == "info" ]]; then
  cat <<'JSON'
{"casks":[{"token":"codex-app","full_token":"codex-app","auto_updates":true}]}
JSON
fi
EOF

  chmod +x \
    "$repo/scripts/update_managed_versions.sh" \
    "$repo/scripts/nix_install.sh" \
    "$bin_dir/nix" \
    "$bin_dir/brew"

  HOME="$home_dir" HOMEBREW_PREFIX= USER=dotfiles-test PATH="$bin_dir:/bin:/usr/bin:/usr/sbin:/sbin" \
    "$TEST_ZSH_BIN" "$repo/scripts/update_managed_versions.sh" --only nix --with-gui-apps > "$repo/output.log" 2>&1

  assert_contains "$log_file" 'brew:update'
  assert_contains "$log_file" 'brew:info --cask --json=v2 codex-app'
  assert_not_contains "$log_file" 'brew:upgrade --cask'
  assert_output_contains "$repo/output.log" 'Skipping Homebrew auto-updating casks during managed upgrade: codex-app'

  rm -rf "$repo"
}

test_managed_update_script_upgrades_only_non_auto_updating_homebrew_casks() {
  skip_unless_macos "$funcstack[1]" || return 0

  local repo
  local home_dir
  local bin_dir
  local log_file

  make_temp_dir

  repo="$REPLY"
  home_dir="$repo/home"
  bin_dir="$repo/bin"
  log_file="$repo/update.log"

  mkdir -p "$repo/scripts/lib" "$repo/config/nix" "$home_dir" "$bin_dir"
  cp "$UPDATE_MANAGED_VERSIONS_SCRIPT" "$repo/scripts/update_managed_versions.sh"
  copy_script_libs "$repo"

  cat > "$repo/config/nix/homebrew-fallback.nix" <<'EOF'
{
  taps = [
  ];

  brews = [
  ];

  casks = [
    "anki"
    "codex-app"
  ];

  vscode = [
  ];

  unsupportedUvPackages = [
  ];
}
EOF
  cat > "$repo/config/nix/mas-apps.nix" <<'EOF'
{ }
EOF
  cat > "$repo/scripts/nix_install.sh" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
print -r -- "nix_install:\$*" >> "$log_file"
EOF
  cat > "$bin_dir/nix" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
print -r -- "nix:\$*" >> "$log_file"
EOF
  cat > "$bin_dir/brew" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
print -r -- "brew:\$*" >> "$log_file"
if [[ "\${1:-}" == "info" ]]; then
  cat <<'JSON'
{"casks":[{"token":"anki","full_token":"anki","auto_updates":false},{"token":"codex-app","full_token":"codex-app","auto_updates":true}]}
JSON
fi
EOF

  chmod +x \
    "$repo/scripts/update_managed_versions.sh" \
    "$repo/scripts/nix_install.sh" \
    "$bin_dir/nix" \
    "$bin_dir/brew"

  HOME="$home_dir" HOMEBREW_PREFIX= USER=dotfiles-test PATH="$bin_dir:/bin:/usr/bin:/usr/sbin:/sbin" \
    "$TEST_ZSH_BIN" "$repo/scripts/update_managed_versions.sh" --only nix --with-gui-apps > "$repo/output.log" 2>&1

  assert_contains "$log_file" 'brew:update'
  assert_contains "$log_file" 'brew:info --cask --json=v2 anki codex-app'
  assert_contains "$log_file" 'brew:upgrade --cask anki'
  assert_not_contains "$log_file" 'brew:upgrade --cask anki codex-app'
  assert_output_contains "$repo/output.log" 'Skipping Homebrew auto-updating casks during managed upgrade: codex-app'

  rm -rf "$repo"
}

test_bash_templates_support_dynamic_shell_setup() {
  assert_contains "$BASHRC_TEMPLATE_FILE" 'dotfiles-shell-common.sh'
  assert_contains "$BASH_PROFILE_TEMPLATE_FILE" '. "$HOME/.bashrc"'
  assert_contains "$SHELL_COMMON_TEMPLATE_FILE" '__DOTFILES_REPO_ROOT__'
  assert_contains "$SHELL_COMMON_TEMPLATE_FILE" '$HOME/.nix-profile/bin'
  assert_contains "$SHELL_COMMON_TEMPLATE_FILE" '[ "$dotfiles_shell_name" = "bash" ]'
  assert_contains "$SHELL_COMMON_TEMPLATE_FILE" 'mise activate "$dotfiles_shell_name"'
  assert_contains "$SHELL_COMMON_TEMPLATE_FILE" 'hm-session-vars.sh'
  assert_contains "$SHELL_COMMON_TEMPLATE_FILE" 'shell/secrets.env'
  assert_contains "$SHELL_COMMON_TEMPLATE_FILE" 'fgcc()'
  assert_contains "$SHELL_COMMON_TEMPLATE_FILE" 'fgcc_rinit()'
  assert_contains "$SHELL_COMMON_TEMPLATE_FILE" 'fgcc_p()'
  assert_contains "$SHELL_COMMON_TEMPLATE_FILE" 'gstop_instance()'
  assert_contains "$REPO_ROOT/home/dot_bashrc.tmpl" '.chezmoitemplates/bashrc'
  assert_contains "$REPO_ROOT/home/dot_bash_profile.tmpl" '.chezmoitemplates/bash_profile'
  assert_contains "$REPO_ROOT/home/private_dot_config/shell/dotfiles-shell-common.sh.tmpl" '.chezmoitemplates/dotfiles-shell-common.sh'
}

test_managed_update_script_updates_mise_and_nix() {
  local output
  make_temp_file
  output="$REPLY"

  assert_contains "$MISE_CONFIG" '[tasks.package-update]'
  assert_contains "$MISE_CONFIG" 'alias = "nix-mise-upgrade"'
  assert_contains "$MISE_CONFIG" 'run = "zsh scripts/update_managed_versions.sh"'
  assert_contains "$MISE_CONFIG" '[tasks.lock-update]'
  assert_contains "$MISE_CONFIG" 'alias = "nix-lock-update"'
  assert_contains "$MISE_CONFIG" 'run = "zsh scripts/update_managed_versions.sh --only lock"'
  assert_contains "$MISE_CONFIG" '[tasks.lock-update-nixpkgs]'
  assert_contains "$MISE_CONFIG" 'alias = "nixpkgs-lock-update"'
  assert_contains "$MISE_CONFIG" 'run = "zsh scripts/update_managed_versions.sh --only lock --nix-input nixpkgs"'
  assert_contains "$MISE_CONFIG" '[tasks.lock-update-home-manager]'
  assert_contains "$MISE_CONFIG" 'alias = "home-manager-lock-update"'
  assert_contains "$MISE_CONFIG" 'run = "zsh scripts/update_managed_versions.sh --only lock --nix-input home-manager"'
  assert_contains "$MISE_CONFIG" '[tasks.lock-update-nix-darwin]'
  assert_contains "$MISE_CONFIG" 'alias = "nix-darwin-lock-update"'
  assert_contains "$MISE_CONFIG" 'run = "zsh scripts/update_managed_versions.sh --only lock --nix-input nix-darwin"'
  assert_contains "$MISE_CONFIG" '[tasks.nix-update]'
  assert_contains "$MISE_CONFIG" 'alias = "nix-upgrade"'
  assert_contains "$MISE_CONFIG" 'run = "zsh scripts/update_managed_versions.sh --only nix"'
  assert_contains "$MISE_CONFIG" '[tasks.nixpkgs-update]'
  assert_contains "$MISE_CONFIG" 'alias = "nixpkgs-upgrade"'
  assert_contains "$MISE_CONFIG" 'run = "zsh scripts/update_managed_versions.sh --only nix --nix-input nixpkgs"'
  assert_contains "$MISE_CONFIG" '[tasks.home-manager-update]'
  assert_contains "$MISE_CONFIG" 'alias = "home-manager-upgrade"'
  assert_contains "$MISE_CONFIG" 'run = "zsh scripts/update_managed_versions.sh --only nix --nix-input home-manager"'
  assert_contains "$MISE_CONFIG" '[tasks.nix-darwin-update]'
  assert_contains "$MISE_CONFIG" 'alias = "nix-darwin-upgrade"'
  assert_contains "$MISE_CONFIG" 'run = "zsh scripts/update_managed_versions.sh --only nix --nix-input nix-darwin"'
  assert_contains "$MISE_CONFIG" '[tasks.mise-update]'
  assert_contains "$MISE_CONFIG" 'alias = "mise-upgrade"'
  assert_contains "$MISE_CONFIG" 'run = "zsh scripts/update_managed_versions.sh --only mise"'
  assert_not_contains "$MISE_CONFIG" 'git-head-commit-rest'
  assert_contains "$MISE_CONFIG" 'node = "22"'
  assert_contains "$MISE_CONFIG" 'go = "1.25"'
  assert_contains "$MISE_CONFIG" 'java = "zulu-21"'
  assert_contains "$MISE_CONFIG" 'python = "3.13"'
  assert_not_contains "$MISE_CONFIG" 'install_before'
  assert_contains "$MISE_CONFIG" '[tools."http:devin"]'
  assert_contains "$MISE_CONFIG" 'version_list_url = "https://static.devin.ai/cli/current/manifest.json"'
  assert_contains "$MISE_CONFIG" '[tools."http:cursor-agent"]'
  assert_contains "$MISE_CONFIG" 'https://downloads.cursor.com/lab/{{ version }}/{{ os(macos="darwin", linux="linux") }}/{{ arch(x64="x64", arm64="arm64") }}/agent-cli-package.tar.gz'
  assert_contains "$MISE_CONFIG" 'opencode = "latest"'
  assert_contains "$MISE_CONFIG" '"github:ogulcancelik/herdr" = "latest"'
  assert_contains "$MISE_CONFIG" '"pipx:markitdown" = "latest"'
  assert_contains "$MISE_CONFIG" '"pipx:google-colab-cli" = "latest"'
  assert_not_contains "$MISE_CONFIG" 'pipx:git+https://github.com/NousResearch/hermes-agent.git'
  assert_contains "$MISE_CONFIG" '[tasks.hermes-setup]'
  assert_contains "$MISE_CONFIG" 'run = "zsh scripts/setup_hermes_agent.sh"'
  assert_contains "$MISE_CONFIG" '[tasks.hermes-update]'
  assert_contains "$MISE_CONFIG" 'alias = "hermes-upgrade"'
  assert_contains "$MISE_CONFIG" 'run = "zsh scripts/update_managed_versions.sh --only hermes"'
  assert_contains "$MISE_CONFIG" '"npm:@github/copilot" = "latest"'
  assert_contains "$MISE_CONFIG" '"npm:openclaw" = "latest"'
  assert_not_contains "$MISE_CONFIG" '"npm:@desplega.ai/agent-swarm" = "latest"'
  assert_contains "$MISE_CONFIG" 'mysql = "8.0.34"'
  assert_contains "$MISE_CONFIG" 'sqlite = "3.51"'
  assert_contains "$MISE_CONFIG" 'redis = "8.2"'
  assert_contains "$NIX_PACKAGE_NAMES_FILE" '"pkg-config"'
  assert_contains "$NIX_PACKAGE_NAMES_FILE" '"icu"'
  assert_contains "$NIX_PACKAGE_NAMES_FILE" '"icu.dev"'
  assert_contains "$NIX_PACKAGE_NAMES_FILE" '"openssl.out"'
  assert_contains "$NIX_PACKAGE_NAMES_FILE" '"openssl.dev"'
  assert_not_contains "$NIX_PACKAGE_NAMES_FILE" '"pkgconf"'
  assert_contains "$UPDATE_MANAGED_VERSIONS_SCRIPT" 'MISE_GLOBAL_CONFIG_FILE'
  assert_contains "$UPDATE_MANAGED_VERSIONS_SCRIPT" 'source "$LIB_DIR/homebrew_fallback.sh"'
  assert_contains "$UPDATE_MANAGED_VERSIONS_SCRIPT" 'source "$LIB_DIR/runtime.sh"'
  assert_contains "$UPDATE_MANAGED_VERSIONS_SCRIPT" '"$MISE_BIN" upgrade --exclude java'
  assert_not_contains "$UPDATE_MANAGED_VERSIONS_SCRIPT" 'mise upgrade --bump'
  assert_contains "$UPDATE_MANAGED_VERSIONS_SCRIPT" 'nix flake lock --update-input'
  assert_contains "$UPDATE_MANAGED_VERSIONS_SCRIPT" 'nixpkgs|home-manager|nix-darwin'
  assert_contains "$UPDATE_MANAGED_VERSIONS_SCRIPT" 'render_progress_bar'
  assert_contains "$UPDATE_MANAGED_VERSIONS_SCRIPT" 'SHOW_PROGRESS'
  assert_contains "$UPDATE_MANAGED_VERSIONS_SCRIPT" 'DOTFILES_SHOW_PROGRESS'
  assert_not_contains "$UPDATE_MANAGED_VERSIONS_SCRIPT" '[[ -t 1 ]]'
  assert_contains "$UPDATE_MANAGED_VERSIONS_SCRIPT" 'config/mise/config.toml'
  assert_contains "$UPDATE_MANAGED_VERSIONS_SCRIPT" 'home/.chezmoitemplates/mise-config.toml'
  assert_contains "$UPDATE_MANAGED_VERSIONS_SCRIPT" 'XDG_CONFIG_HOME'
  assert_contains "$UPDATE_MANAGED_VERSIONS_SCRIPT" '__DOTFILES_REPO_ROOT__'
  assert_contains "$UPDATE_MANAGED_VERSIONS_SCRIPT" 'nix flake update'
  assert_contains "$UPDATE_MANAGED_VERSIONS_SCRIPT" 'nix_install.sh'
  assert_contains "$UPDATE_MANAGED_VERSIONS_SCRIPT" 'activate_nix_environment'
  assert_contains "$UPDATE_MANAGED_VERSIONS_SCRIPT" 'cleanup_stale_java_install_state'
  assert_contains "$UPDATE_MANAGED_VERSIONS_SCRIPT" '${contents_path:h:t}'
  assert_contains "$UPDATE_MANAGED_VERSIONS_SCRIPT" 'dotfiles_create_unique_temp_directory'
  assert_contains "$UPDATE_MANAGED_VERSIONS_SCRIPT" 'dotfiles_create_unique_temp_file'
  assert_contains "$UPDATE_MANAGED_VERSIONS_SCRIPT" 'dotfiles_resolve_command_from_path'
  assert_contains "$UPDATE_MANAGED_VERSIONS_SCRIPT" 'resolve_mise_command'
  assert_contains "$UPDATE_MANAGED_VERSIONS_SCRIPT" 'resolve_nix_command'
  assert_contains "$UPDATE_MANAGED_VERSIONS_SCRIPT" 'prepend_paths_from_repo_package_envs'
  assert_contains "$UPDATE_MANAGED_VERSIONS_SCRIPT" 'export_homebrew_prefix_if_available'
  assert_contains "$UPDATE_MANAGED_VERSIONS_SCRIPT" 'HOMEBREW_PREFIX="${brew_path%/bin/brew}"'
  assert_contains "$UPDATE_MANAGED_VERSIONS_SCRIPT" 'configure_macos_build_toolchain'
  assert_contains "$UPDATE_MANAGED_VERSIONS_SCRIPT" 'resolve_macos_sdk_root'
  assert_contains "$UPDATE_MANAGED_VERSIONS_SCRIPT" 'export CC="/usr/bin/clang"'
  assert_contains "$UPDATE_MANAGED_VERSIONS_SCRIPT" 'export SDKROOT="$sdk_root"'
  assert_contains "$UPDATE_MANAGED_VERSIONS_SCRIPT" 'prepend_paths_from_repo_package_attr "dotfiles-cli-packages"'
  assert_contains "$UPDATE_MANAGED_VERSIONS_SCRIPT" 'prepend_paths_from_repo_package_attr "dotfiles-full-packages"'
  assert_contains "$UPDATE_MANAGED_VERSIONS_SCRIPT" 'PKG_CONFIG_PATH'
  assert_contains "$UPDATE_MANAGED_VERSIONS_SCRIPT" 'pkg-config'
  assert_not_contains "$UPDATE_MANAGED_VERSIONS_SCRIPT" '< <('
  assert_not_contains "$UPDATE_MANAGED_VERSIONS_SCRIPT" 'mktemp -d'
  assert_not_contains "$UPDATE_MANAGED_VERSIONS_SCRIPT" 'tmp="$(mktemp)"'
  assert_not_contains "$UPDATE_MANAGED_VERSIONS_SCRIPT" '$commands[mise]'
  assert_not_contains "$UPDATE_MANAGED_VERSIONS_SCRIPT" '$commands[nix]'
  assert_not_contains "$UPDATE_MANAGED_VERSIONS_SCRIPT" 'for candidate_dir in $PATH'
  assert_not_contains "$UPDATE_MANAGED_VERSIONS_SCRIPT" '$(basename "$(dirname "$contents_path")")'
  assert_not_contains "$UPDATE_MANAGED_VERSIONS_SCRIPT" '$(describe_nix_input)'
  assert_not_contains "$UPDATE_MANAGED_VERSIONS_SCRIPT" '$(mise_command)'
  assert_not_contains "$UPDATE_MANAGED_VERSIONS_SCRIPT" '$(nix_command)'

  "$TEST_ZSH_BIN" "$UPDATE_MANAGED_VERSIONS_SCRIPT" --help > "$output"
  assert_output_contains "$output" '--shell zsh|bash'
  assert_output_contains "$output" '--cli-only'
  assert_output_contains "$output" '--only all|lock|nix|mise'
  assert_output_contains "$output" '--nix-input all|nixpkgs|home-manager|nix-darwin'
  assert_output_contains "$output" '--with-gui-apps'

  rm -f "$output"
}

test_managed_update_all_bash_runs_real_helpers_in_order() {
  local target_bash="${1:-/bin/bash}"
  local expected_major="${2:-}"
  local repo
  local repo_real
  local home_dir
  local bin_dir
  local event_log
  local output_file
  local events
  local lock_line
  local mise_line
  local hermes_line
  local apply_line
  local expected_profile
  local bash_major
  local exit_status=0

  make_temp_dir
  repo="$REPLY"
  repo_real="$(cd "$repo" && pwd -P)"
  home_dir="$repo/home"
  bin_dir="$repo/bin"
  event_log="$repo/events.log"
  output_file="$repo/output.log"
  expected_profile="cli"
  if is_test_macos; then
    expected_profile="full"
  fi

  mkdir -p "$repo/scripts/lib" "$repo/config/nix" "$repo/config/mise" \
    "$repo/home/.chezmoitemplates" "$home_dir" "$home_dir/.nix-profile/bin" "$bin_dir"
  cp "$UPDATE_MANAGED_VERSIONS_SCRIPT" "$repo/scripts/update_managed_versions.sh"
  cp "$INSTALL_SCRIPT" "$repo/scripts/nix_install.sh"
  cp "$REPO_ROOT/scripts/setup_hermes_agent.sh" "$repo/scripts/setup_hermes_agent.sh"
  copy_script_libs "$repo"
  : > "$repo/config/nix/homebrew-fallback.nix"
  : > "$repo/config/nix/mas-apps.nix"
  cat > "$repo/config/mise/config.toml" <<'EOF'
[tools]
EOF
  : > "$repo/home/.chezmoitemplates/mise-config.toml"

  write_strict_fake_bash "$bin_dir"
  cat > "$bin_dir/nix" <<'EOF'
#!/usr/bin/env zsh
set -euo pipefail
print -r -- "nix:$*" >> "$DOTFILES_TEST_EVENT_LOG"
if [[ "$1" == flake && "$2" == update ]]; then
  print -r -- "lock-updated" > "$DOTFILES_TEST_LOCK_FILE"
fi
exit 0
EOF
  cat > "$bin_dir/home-manager" <<'EOF'
#!/usr/bin/env zsh
set -euo pipefail
print -r -- "home-manager:$*" >> "$DOTFILES_TEST_EVENT_LOG"
EOF
  cat > "$bin_dir/mise" <<'EOF'
#!/usr/bin/env zsh
set -euo pipefail
print -r -- "mise:$*" >> "$DOTFILES_TEST_EVENT_LOG"
EOF
  cat > "$bin_dir/hermes" <<'EOF'
#!/usr/bin/env zsh
set -euo pipefail
print -r -- "hermes:$*" >> "$DOTFILES_TEST_EVENT_LOG"
EOF
  cat > "$bin_dir/darwin-rebuild" <<'EOF'
#!/usr/bin/env zsh
set -euo pipefail
print -r -- "darwin-rebuild:$*" >> "$DOTFILES_TEST_EVENT_LOG"
EOF
  write_strict_fake_sudo "$bin_dir"
  chmod +x "$bin_dir"/*
  cp "$bin_dir/bash" "$home_dir/.nix-profile/bin/bash"

  HOME="$home_dir" USER=dotfiles-test \
    PATH="$bin_dir:/bin:/usr/bin:/usr/sbin:/sbin" \
    DOTFILES_SHOW_PROGRESS=0 \
    DOTFILES_TEST_EVENT_LOG="$event_log" \
    DOTFILES_TEST_LOCK_FILE="$repo/flake.lock" \
    DOTFILES_TEST_TARGET_BASH="$target_bash" \
    NIX_TEST_FIXTURE_ROOT="$repo" \
    NIX_TEST_ALLOWED_BASH_SCRIPT="$repo_real/scripts/nix_install.sh" \
    DOTFILES_DARWIN_SUDO_LOCAL_PATH="$repo/etc/pam.d/sudo_local" \
    DOTFILES_DARWIN_ETC_SHELL_RC_PATHS="$repo/etc/bashrc:$repo/etc/zshrc" \
    "$TEST_ZSH_BIN" "$repo/scripts/update_managed_versions.sh" \
    --only all --shell bash > "$output_file" 2>&1 || exit_status=$?

  (( exit_status == 0 )) || {
    echo "--- all bash output ---" >&2
    sed -n '1,200p' "$output_file" >&2
    echo "--- all bash events ---" >&2
    if [[ -f "$event_log" ]]; then
      sed -n '1,200p' "$event_log" >&2
    fi
    fail "expected all --shell bash to complete"
  }

  assert_file "$repo/flake.lock"
  events="$(cat "$event_log")"
  lock_line="$(grep -n '^nix:flake update$' "$event_log" | cut -d: -f1)"
  if is_test_macos; then
    apply_line="$(grep -n '^darwin-rebuild:switch ' "$event_log" | cut -d: -f1)"
  else
    apply_line="$(grep -n '^home-manager:' "$event_log" | cut -d: -f1)"
  fi
  mise_line="$(grep -n '^mise:upgrade --exclude java$' "$event_log" | cut -d: -f1)"
  hermes_line="$(grep -n '^hermes:update --yes$' "$event_log" | cut -d: -f1)"
  [[ -n "$lock_line" && -n "$apply_line" && -n "$mise_line" && -n "$hermes_line" ]] \
    || fail "expected all helper stages in event log: $events"
  (( lock_line < apply_line && apply_line < mise_line && mise_line < hermes_line )) \
    || fail "expected lock, Nix, mise, Hermes order: $events"
  assert_contains "$event_log" "bash:$repo_real/scripts/nix_install.sh --profile $expected_profile"
  bash_major="$(bash_major_version "$target_bash")"
  case "$bash_major" in
    3|5) ;;
    *) fail "managed all fixture must run Bash 3.x or 5.x" ;;
  esac
  [[ -z "$expected_major" || "$bash_major" == "$expected_major" ]] \
    || fail "managed all fixture ran Bash $bash_major, expected Bash $expected_major"
  emit_matrix_result "MATRIX_RESULT|os=$(matrix_os_name)|shell=bash${bash_major}|target=managed-default|status=PASS|requirement=required|reason=$target_bash"

  : > "$event_log"
  exit_status=0
  HOME="$home_dir" USER=dotfiles-test \
    PATH="$bin_dir:/bin:/usr/bin:/usr/sbin:/sbin" \
    DOTFILES_SHOW_PROGRESS=0 \
    DOTFILES_TEST_EVENT_LOG="$event_log" \
    DOTFILES_TEST_LOCK_FILE="$repo/flake.lock" \
    DOTFILES_TEST_TARGET_BASH="$target_bash" \
    NIX_TEST_FIXTURE_ROOT="$repo" \
    NIX_TEST_ALLOWED_BASH_SCRIPT="$repo_real/scripts/nix_install.sh" \
    DOTFILES_DARWIN_SUDO_LOCAL_PATH="$repo/etc/pam.d/sudo_local" \
    DOTFILES_DARWIN_ETC_SHELL_RC_PATHS="$repo/etc/bashrc:$repo/etc/zshrc" \
    "$TEST_ZSH_BIN" "$repo/scripts/update_managed_versions.sh" \
    --only all --shell bash --cli-only > "$output_file" 2>&1 || exit_status=$?
  (( exit_status == 0 )) || fail "explicit CLI all flow should succeed on every OS"
  assert_contains "$event_log" "bash:$repo_real/scripts/nix_install.sh --profile cli"
  assert_not_contains "$event_log" 'brew:'
  emit_matrix_result "MATRIX_RESULT|os=$(matrix_os_name)|shell=bash${bash_major}|target=managed-cli-only|status=PASS|requirement=required|reason=$target_bash"

  rm -rf "$repo"
}

test_managed_update_all_bash_honors_cli_and_full_gui_profiles() {
  local target_bash="${1:-/bin/bash}"
  local expected_major="${2:-}"
  local repo
  local repo_real
  local home_dir
  local bin_dir
  local event_log
  local output_file
  local exit_status
  local apply_line
  local brew_line
  local mise_line
  local hermes_line
  local bash_major

  if ! is_test_macos; then
    emit_not_applicable_skip 'managed-macos-fallback-profiles'
    return 0
  fi
  make_temp_dir
  repo="$REPLY"
  repo_real="$(cd "$repo" && pwd -P)"
  home_dir="$repo/home"
  bin_dir="$repo/bin"
  event_log="$repo/events.log"
  output_file="$repo/output.log"
  bash_major="$(bash_major_version "$target_bash")"
  [[ -z "$expected_major" || "$bash_major" == "$expected_major" ]] \
    || fail "managed profile fixture ran Bash $bash_major, expected Bash $expected_major"
  mkdir -p "$repo/scripts/lib" "$repo/config/nix" "$repo/config/mise" "$home_dir/.chezmoitemplates" "$bin_dir"
  cp "$UPDATE_MANAGED_VERSIONS_SCRIPT" "$repo/scripts/update_managed_versions.sh"
  cp "$INSTALL_SCRIPT" "$repo/scripts/nix_install.sh"
  cp "$REPO_ROOT/scripts/setup_hermes_agent.sh" "$repo/scripts/setup_hermes_agent.sh"
  copy_script_libs "$repo"
  cat > "$repo/config/nix/homebrew-fallback.nix" <<'EOF'
{
  taps = [
  ];
  brews = [
    "example-tool"
  ];
  casks = [
  ];
  vscode = [
  ];
}
EOF
  : > "$repo/config/nix/mas-apps.nix"
  cat > "$repo/config/mise/config.toml" <<'EOF'
[tools]
EOF
  : > "$repo/home/.chezmoitemplates/mise-config.toml"

  write_strict_fake_bash "$bin_dir"
  cat > "$bin_dir/nix" <<'EOF'
#!/bin/zsh
set -euo pipefail
print -r -- "nix:$*" >> "$DOTFILES_TEST_EVENT_LOG"
if [[ "$1" == flake && "$2" == update ]]; then
  print -r -- "lock-updated" > "$DOTFILES_TEST_LOCK_FILE"
fi
EOF
  cat > "$bin_dir/darwin-rebuild" <<'EOF'
#!/bin/zsh
set -euo pipefail
print -r -- "darwin-rebuild:$*" >> "$DOTFILES_TEST_EVENT_LOG"
EOF
  write_strict_fake_sudo "$bin_dir"
  cat > "$bin_dir/brew" <<'EOF'
#!/bin/zsh
set -euo pipefail
print -r -- "brew:$*" >> "$DOTFILES_TEST_EVENT_LOG"
EOF
  cat > "$bin_dir/mise" <<'EOF'
#!/bin/zsh
set -euo pipefail
print -r -- "mise:$*" >> "$DOTFILES_TEST_EVENT_LOG"
EOF
  cat > "$bin_dir/hermes" <<'EOF'
#!/bin/zsh
set -euo pipefail
print -r -- "hermes:$*" >> "$DOTFILES_TEST_EVENT_LOG"
EOF
  chmod +x "$bin_dir"/*

  if is_test_macos; then
    : > "$event_log"
    cat > "$repo/config/nix/homebrew-fallback.nix" <<'EOF'
{
  taps = [
  ];
  brews = [
  ];
  casks = [
  ];
  vscode = [
  ];
}
EOF
    exit_status=0
    HOME="$home_dir" USER=dotfiles-test \
      PATH="$bin_dir:/bin:/usr/bin:/usr/sbin:/sbin" \
      DOTFILES_SHOW_PROGRESS=0 \
      DOTFILES_TEST_EVENT_LOG="$event_log" \
      DOTFILES_TEST_LOCK_FILE="$repo/flake.lock" \
      DOTFILES_TEST_TARGET_BASH="$target_bash" \
      NIX_TEST_FIXTURE_ROOT="$repo" \
      NIX_TEST_ALLOWED_BASH_SCRIPT="$repo_real/scripts/nix_install.sh" \
      DOTFILES_DARWIN_SUDO_LOCAL_PATH="$repo/etc/pam.d/sudo_local" \
      DOTFILES_DARWIN_ETC_SHELL_RC_PATHS="$repo/etc/bashrc:$repo/etc/zshrc" \
      "$TEST_ZSH_BIN" "$repo/scripts/update_managed_versions.sh" \
      --only all --shell bash > "$output_file" 2>&1 || exit_status=$?
    (( exit_status == 0 )) || fail "macOS default full profile without fallback should succeed"
    assert_contains "$event_log" "bash:$repo_real/scripts/nix_install.sh --profile full"
    assert_not_contains "$event_log" 'brew:update'
    emit_matrix_result "MATRIX_RESULT|os=macos|shell=bash${bash_major}|target=managed-default-full|status=PASS|requirement=required|reason=fallback-free"

    : > "$event_log"
    cat > "$repo/config/nix/homebrew-fallback.nix" <<'EOF'
{
  taps = [
  ];
  brews = [
  ];
  casks = [
    "example-gui"
  ];
  vscode = [
  ];
}
EOF
    exit_status=0
    HOME="$home_dir" USER=dotfiles-test \
      PATH="$bin_dir:/bin:/usr/bin:/usr/sbin:/sbin" \
      DOTFILES_SHOW_PROGRESS=0 \
      DOTFILES_TEST_EVENT_LOG="$event_log" \
      DOTFILES_TEST_LOCK_FILE="$repo/flake.lock" \
      DOTFILES_TEST_TARGET_BASH="$target_bash" \
      NIX_TEST_FIXTURE_ROOT="$repo" \
      NIX_TEST_ALLOWED_BASH_SCRIPT="$repo_real/scripts/nix_install.sh" \
      DOTFILES_DARWIN_SUDO_LOCAL_PATH="$repo/etc/pam.d/sudo_local" \
      DOTFILES_DARWIN_ETC_SHELL_RC_PATHS="$repo/etc/bashrc:$repo/etc/zshrc" \
      "$TEST_ZSH_BIN" "$repo/scripts/update_managed_versions.sh" \
      --only all --shell bash > "$output_file" 2>&1 || exit_status=$?
    (( exit_status == 0 )) || fail "macOS GUI fallback default downgrade should succeed"
    assert_contains "$event_log" "bash:$repo_real/scripts/nix_install.sh --profile cli"
    assert_not_contains "$event_log" 'bash:'"$repo_real"'/scripts/nix_install.sh --profile full'
    assert_not_contains "$event_log" 'brew:update'
    assert_output_contains "$output_file" 'Managed update defaults to the CLI Nix profile on macOS'
    emit_matrix_result "MATRIX_RESULT|os=macos|shell=bash${bash_major}|target=managed-default-gui-fallback|status=PASS|requirement=required|reason=cli-downgrade"

    cat > "$repo/config/nix/homebrew-fallback.nix" <<'EOF'
{
  taps = [
  ];
  brews = [
    "example-tool"
  ];
  casks = [
  ];
  vscode = [
  ];
}
EOF
  fi

  : > "$event_log"
  exit_status=0
  HOME="$home_dir" USER=dotfiles-test \
    PATH="$bin_dir:/bin:/usr/bin:/usr/sbin:/sbin" \
    DOTFILES_SHOW_PROGRESS=0 \
    DOTFILES_TEST_EVENT_LOG="$event_log" \
    DOTFILES_TEST_LOCK_FILE="$repo/flake.lock" \
    DOTFILES_TEST_TARGET_BASH="$target_bash" \
    NIX_TEST_FIXTURE_ROOT="$repo" \
    NIX_TEST_ALLOWED_BASH_SCRIPT="$repo_real/scripts/nix_install.sh" \
    DOTFILES_DARWIN_SUDO_LOCAL_PATH="$repo/etc/pam.d/sudo_local" \
    DOTFILES_DARWIN_ETC_SHELL_RC_PATHS="$repo/etc/bashrc:$repo/etc/zshrc" \
    "$TEST_ZSH_BIN" "$repo/scripts/update_managed_versions.sh" \
    --only all --shell bash --cli-only > "$output_file" 2>&1 || exit_status=$?
  (( exit_status == 0 )) || {
    sed -n '1,180p' "$output_file" >&2
    sed -n '1,180p' "$event_log" >&2
    fail "expected explicit CLI all flow to succeed"
  }
  assert_contains "$event_log" "bash:$repo_real/scripts/nix_install.sh --profile cli"
  assert_contains "$event_log" 'brew:update'
  assert_contains "$event_log" 'brew:upgrade example-tool'
  assert_contains "$event_log" 'mise:upgrade --exclude java'
  assert_contains "$event_log" 'hermes:update --yes'
  apply_line="$(grep -n '^darwin-rebuild:switch ' "$event_log" | cut -d: -f1)"
  brew_line="$(grep -n '^brew:update$' "$event_log" | cut -d: -f1)"
  mise_line="$(grep -n '^mise:upgrade --exclude java$' "$event_log" | cut -d: -f1)"
  hermes_line="$(grep -n '^hermes:update --yes$' "$event_log" | cut -d: -f1)"
  (( apply_line < brew_line && brew_line < mise_line && mise_line < hermes_line )) \
    || fail "CLI all flow order was not preserved"
  emit_matrix_result "MATRIX_RESULT|os=macos|shell=bash${bash_major}|target=managed-cli-fallback|status=PASS|requirement=required|reason=homebrew-fallback"

  : > "$event_log"
  exit_status=0
  HOME="$home_dir" USER=dotfiles-test \
    PATH="$bin_dir:/bin:/usr/bin:/usr/sbin:/sbin" \
    DOTFILES_SHOW_PROGRESS=0 \
    DOTFILES_TEST_EVENT_LOG="$event_log" \
    DOTFILES_TEST_LOCK_FILE="$repo/flake.lock" \
    DOTFILES_TEST_TARGET_BASH="$target_bash" \
    NIX_TEST_FIXTURE_ROOT="$repo" \
    NIX_TEST_ALLOWED_BASH_SCRIPT="$repo_real/scripts/nix_install.sh" \
    DOTFILES_DARWIN_SUDO_LOCAL_PATH="$repo/etc/pam.d/sudo_local" \
    DOTFILES_DARWIN_ETC_SHELL_RC_PATHS="$repo/etc/bashrc:$repo/etc/zshrc" \
    "$TEST_ZSH_BIN" "$repo/scripts/update_managed_versions.sh" \
    --only all --shell bash --profile full --with-gui-apps > "$output_file" 2>&1 || exit_status=$?
  (( exit_status == 0 )) || {
    sed -n '1,180p' "$output_file" >&2
    sed -n '1,180p' "$event_log" >&2
    fail "expected explicit full GUI all flow to succeed"
  }
  assert_contains "$event_log" "bash:$repo_real/scripts/nix_install.sh --profile full --with-gui-apps"
  assert_contains "$event_log" 'brew:update'
  assert_contains "$event_log" 'brew:upgrade example-tool'
  assert_contains "$event_log" 'mise:upgrade --exclude java'
  assert_contains "$event_log" 'hermes:update --yes'
  apply_line="$(grep -n '^darwin-rebuild:switch ' "$event_log" | cut -d: -f1)"
  brew_line="$(grep -n '^brew:update$' "$event_log" | cut -d: -f1)"
  mise_line="$(grep -n '^mise:upgrade --exclude java$' "$event_log" | cut -d: -f1)"
  hermes_line="$(grep -n '^hermes:update --yes$' "$event_log" | cut -d: -f1)"
  (( apply_line < brew_line && brew_line < mise_line && mise_line < hermes_line )) \
    || fail "full GUI all flow order was not preserved"
  emit_matrix_result "MATRIX_RESULT|os=macos|shell=bash${bash_major}|target=managed-full-gui|status=PASS|requirement=required|reason=homebrew-fallback"

  rm -rf "$repo"
}

run_managed_all_bash_matrix() {
  local expected_major
  local target_bash
  local matrix_status=0

  for expected_major in 3 5; do
    if select_bash_for_major "$expected_major"; then
      target_bash="$REPLY"
      test_managed_update_all_bash_runs_real_helpers_in_order "$target_bash" "$expected_major" \
        || matrix_status=1
      if is_test_macos; then
        test_managed_update_all_bash_honors_cli_and_full_gui_profiles "$target_bash" "$expected_major" \
          || matrix_status=1
      fi
    elif [[ "$expected_major" == 3 ]] && ! is_test_macos; then
      emit_not_applicable_skip 'managed-default'
      emit_not_applicable_skip 'managed-cli-only'
    else
      emit_required_bash_skip 'managed-default' "$expected_major"
      emit_required_bash_skip 'managed-cli-only' "$expected_major"
      if is_test_macos; then
        emit_required_bash_skip 'managed-default-full' "$expected_major"
        emit_required_bash_skip 'managed-default-gui-fallback' "$expected_major"
        emit_required_bash_skip 'managed-cli-fallback' "$expected_major"
        emit_required_bash_skip 'managed-full-gui' "$expected_major"
      fi
      matrix_status=1
    fi
  done

  if ! is_test_macos; then
    emit_not_applicable_skip 'managed-macos-fallback-profiles'
  fi
  (( matrix_status == 0 )) || fail "managed all Bash matrix failed"
}

test_managed_update_propagates_nix_activation_failure() {
  local target_bash="${1:-/bin/bash}"
  local repo
  local repo_real
  local home_dir
  local bin_dir
  local event_log
  local output_file
  local backup_path
  local archive_path
  local exit_status=0

  make_temp_dir
  repo="$REPLY"
  repo_real="$(cd "$repo" && pwd -P)"
  home_dir="$repo/home"
  bin_dir="$repo/bin"
  event_log="$repo/events.log"
  output_file="$repo/output.log"
  backup_path="$home_dir/.failure.before-nix-darwin"
  archive_path="$backup_path.stale-1700000000"

  mkdir -p "$repo/scripts/lib" "$repo/config/nix" "$repo/config/mise" \
    "$home_dir" "$bin_dir"
  cp "$UPDATE_MANAGED_VERSIONS_SCRIPT" "$repo/scripts/update_managed_versions.sh"
  cp "$INSTALL_SCRIPT" "$repo/scripts/nix_install.sh"
  cp "$REPO_ROOT/scripts/setup_hermes_agent.sh" "$repo/scripts/setup_hermes_agent.sh"
  copy_script_libs "$repo"
  : > "$repo/config/nix/homebrew-fallback.nix"
  : > "$repo/config/nix/mas-apps.nix"
  cat > "$repo/config/mise/config.toml" <<'EOF'
[tools]
EOF
  mkdir -p "$repo/home/.chezmoitemplates"
  : > "$repo/home/.chezmoitemplates/mise-config.toml"
  print -r -- 'lock-sentinel' > "$repo/flake.lock"
  print -r -- 'backup-sentinel' > "$backup_path"

  write_strict_fake_bash "$bin_dir"
  cat > "$bin_dir/nix" <<'EOF'
#!/bin/zsh
set -euo pipefail
print -r -- "nix:$*" >> "$NIX_TEST_EVENT_LOG"
if [[ "${1:-}" == flake && "${2:-}" == update ]]; then
  print -r -- 'lock-updated' > "$NIX_TEST_LOCK_FILE"
fi
EOF
  cat > "$bin_dir/home-manager" <<'EOF'
#!/bin/zsh
set -euo pipefail
print -r -- "home-manager:$*" >> "$NIX_TEST_EVENT_LOG"
exit 23
EOF
  cat > "$bin_dir/darwin-rebuild" <<'EOF'
#!/bin/zsh
set -euo pipefail
print -r -- "darwin-rebuild:$*" >> "$NIX_TEST_EVENT_LOG"
exit 23
EOF
  write_strict_fake_sudo "$bin_dir"
  cat > "$bin_dir/mise" <<'EOF'
#!/bin/zsh
set -euo pipefail
print -r -- "mise:$*" >> "$NIX_TEST_EVENT_LOG"
EOF
  cat > "$bin_dir/hermes" <<'EOF'
#!/bin/zsh
set -euo pipefail
print -r -- "hermes:$*" >> "$NIX_TEST_EVENT_LOG"
EOF
  cat > "$bin_dir/brew" <<'EOF'
#!/bin/zsh
set -euo pipefail
print -r -- "brew:$*" >> "$NIX_TEST_EVENT_LOG"
EOF
  chmod +x "$bin_dir"/*

  NIX_TEST_EVENT_LOG="$event_log" NIX_TEST_LOCK_FILE="$repo/flake.lock" \
    NIX_TEST_TARGET_BASH="$target_bash" HOME="$home_dir" USER=dotfiles-test \
    NIX_TEST_FIXTURE_ROOT="$repo" \
    NIX_TEST_ALLOWED_BASH_SCRIPT="$repo_real/scripts/nix_install.sh" \
    PATH="$bin_dir:/bin:/usr/bin:/usr/sbin:/sbin" \
    DOTFILES_SHOW_PROGRESS=0 \
    DOTFILES_HOME_MANAGER_BACKUP_ARCHIVE_EPOCH=1700000000 \
    DOTFILES_DARWIN_SUDO_LOCAL_PATH="$repo/etc/pam.d/sudo_local" \
    DOTFILES_DARWIN_ETC_SHELL_RC_PATHS="$repo/etc/bashrc:$repo/etc/zshrc" \
    "$TEST_ZSH_BIN" "$repo/scripts/update_managed_versions.sh" \
    --only all --shell bash --cli-only > "$output_file" 2>&1 || exit_status=$?

  (( exit_status != 0 )) || fail "Nix activation failure must reach the managed-update caller"
  assert_file_content "$repo/flake.lock" 'lock-updated'
  assert_file "$archive_path"
  assert_not_exists "$backup_path"
  if is_test_macos; then
    assert_contains "$event_log" 'darwin-rebuild:switch '
  else
    assert_contains "$event_log" 'home-manager:switch '
  fi
  assert_not_contains "$event_log" 'mise:upgrade --exclude java'
  assert_not_contains "$event_log" 'hermes:update --yes'
  assert_not_contains "$event_log" 'brew:update'
  assert_contains "$event_log" "bash:$repo_real/scripts/nix_install.sh --profile cli"

  rm -rf "$repo"
}

test_nix_install_cycle2_parser_and_library_mode() {
  local bash_bin="${1:-/bin/bash}"
  local run_zsh_source="${2:-1}"
  local repo
  local home_dir
  local bin_dir
  local event_log
  local bash_status=0
  local source_bash_status=0
  local source_zsh_status=0
  local source_output
  local failures=0

  make_temp_dir
  repo="$REPLY"
  home_dir="$repo/home"
  bin_dir="$repo/bin"
  event_log="$repo/events.log"
  mkdir -p "$home_dir" "$bin_dir"

  for command_name in nix brew darwin-rebuild home-manager sudo mv find; do
    cat > "$bin_dir/$command_name" <<EOF
#!/bin/zsh
set -euo pipefail
print -r -- "$command_name:\$*" >> "$event_log"
exit 99
EOF
    chmod +x "$bin_dir/$command_name"
  done

  "$bash_bin" -n "$INSTALL_SCRIPT" > "$repo/bash-n.log" 2>&1 || bash_status=$?
  DOTFILES_NIX_INSTALL_LIBRARY_MODE=1 HOME="$home_dir" \
    DOTFILES_HOME_MANAGER_BACKUP_ARCHIVE_EPOCH=1700000000 \
    NIX_TEST_EVENT_LOG="$event_log" PATH="$bin_dir:/bin:/usr/bin:/usr/sbin:/sbin" \
    "$bash_bin" -c '. "$1"' _ "$INSTALL_SCRIPT" > "$repo/source-bash.log" 2>&1 \
    || source_bash_status=$?
  : > "$repo/source-zsh.log"
  if [[ "$run_zsh_source" == 1 ]]; then
    DOTFILES_NIX_INSTALL_LIBRARY_MODE=1 HOME="$home_dir" \
      DOTFILES_HOME_MANAGER_BACKUP_ARCHIVE_EPOCH=1700000000 \
      NIX_TEST_EVENT_LOG="$event_log" PATH="$bin_dir:/bin:/usr/bin:/usr/sbin:/sbin" \
      "$TEST_ZSH_BIN" -c '. "$1"' _ "$INSTALL_SCRIPT" > "$repo/source-zsh.log" 2>&1 \
      || source_zsh_status=$?
  fi
  source_output="$(cat "$repo/source-bash.log" "$repo/source-zsh.log")"

  print -r -- "cycle2 parser rc=$bash_status source-bash rc=$source_bash_status source-zsh rc=$source_zsh_status"
  if (( bash_status != 0 )); then
    sed -n '1,80p' "$repo/bash-n.log" >&2
    print -u2 -- "FAIL: nix_install.sh must parse under Bash"
    failures=1
  fi
  if (( source_bash_status != 0 )); then
    sed -n '1,120p' "$repo/source-bash.log" >&2
    print -u2 -- "FAIL: nix_install.sh library mode must source under Bash: $source_output"
    failures=1
  fi
  if (( source_zsh_status != 0 )); then
    sed -n '1,120p' "$repo/source-zsh.log" >&2
    print -u2 -- "FAIL: nix_install.sh library mode must source under zsh"
    failures=1
  fi
  assert_not_exists "$event_log"

  rm -rf "$repo"
  return "$failures"
}

test_nix_install_cycle2_archives_backups_without_losing_pathnames() {
  local runner_bin="${1:-$TEST_ZSH_BIN}"
  local repo
  local home_dir
  local config_dir
  local bin_dir
  local event_log
  local spaced_home
  local newline_home
  local linked_home
  local directory_home
  local spaced_xdg
  local newline_xdg
  local collision_archive
  local deep_backup
  local output_file
  local exit_status=0

  skip_unless_macos "$funcstack[1]" || return 0
  make_temp_dir
  repo="$REPLY"
  home_dir="$repo/home"
  config_dir="$repo/config-home"
  bin_dir="$repo/bin"
  event_log="$repo/events.log"
  output_file="$repo/output.log"

  spaced_home="$home_dir/.space name.before-nix-darwin"
  newline_home="$home_dir/.line
name.before-nix-darwin"
  linked_home="$home_dir/.linked.before-nix-darwin"
  directory_home="$home_dir/.directory.before-nix-darwin"
  spaced_xdg="$config_dir/mise/file name.before-nix-darwin"
  newline_xdg="$config_dir/mise/line
name.before-nix-darwin"
  collision_archive="${spaced_home}.stale-1700000000"
  deep_backup="$home_dir/nested/.deep.before-nix-darwin"

  mkdir -p "$repo/scripts/lib" "$home_dir" "$config_dir/mise" "$home_dir/nested" "$directory_home" "$bin_dir"
  cp "$INSTALL_SCRIPT" "$repo/scripts/nix_install.sh"
  copy_script_libs "$repo"
  print -r -- "space" > "$spaced_home"
  print -r -- "newline" > "$newline_home"
  print -r -- "target" > "$home_dir/linked-target"
  ln -s "$home_dir/linked-target" "$linked_home"
  print -r -- "directory" > "$directory_home/content"
  print -r -- "xdg-space" > "$spaced_xdg"
  print -r -- "xdg-newline" > "$newline_xdg"
  print -r -- "deep" > "$deep_backup"
  print -r -- 'collision-sentinel' > "$collision_archive"

  cat > "$bin_dir/nix" <<'EOF'
#!/bin/zsh
set -euo pipefail
print -r -- "nix:$*" >> "$NIX_TEST_EVENT_LOG"
EOF
  cat > "$bin_dir/darwin-rebuild" <<'EOF'
#!/bin/zsh
set -euo pipefail
print -r -- "darwin-rebuild:$*" >> "$NIX_TEST_EVENT_LOG"
EOF
  write_strict_fake_sudo "$bin_dir"
  chmod +x "$bin_dir/nix" "$bin_dir/darwin-rebuild" "$bin_dir/sudo"

  NIX_TEST_EVENT_LOG="$event_log" HOME="$home_dir" XDG_CONFIG_HOME="$config_dir" \
    NIX_TEST_FIXTURE_ROOT="$repo" \
    PATH="$bin_dir:/bin:/usr/bin:/usr/sbin:/sbin" \
    DOTFILES_HOME_MANAGER_BACKUP_ARCHIVE_EPOCH=1700000000 \
    DOTFILES_DARWIN_SUDO_LOCAL_PATH="$repo/etc/pam.d/sudo_local" \
    DOTFILES_DARWIN_ETC_SHELL_RC_PATHS="$repo/etc/bashrc:$repo/etc/zshrc" \
    "$runner_bin" "$repo/scripts/nix_install.sh" --profile cli \
    > "$output_file" 2>&1 || exit_status=$?

  if (( exit_status != 0 )); then
    sed -n '1,160p' "$output_file" >&2
    print -u2 -- "FAIL: expected Bash Nix backup fixture to succeed"
    rm -rf "$repo"
    return 1
  fi
  assert_not_exists "$spaced_home"
  assert_not_exists "$newline_home"
  assert_not_exists "$linked_home"
  assert_not_exists "$directory_home"
  assert_not_exists "$spaced_xdg"
  assert_not_exists "$newline_xdg"
  assert_file "${spaced_home}.stale-1700000000-1"
  assert_file_content "${spaced_home}.stale-1700000000-1" 'space'
  assert_file "${newline_home}.stale-1700000000"
  assert_file_content "${newline_home}.stale-1700000000" 'newline'
  assert_file "${linked_home}.stale-1700000000"
  [[ -L "${linked_home}.stale-1700000000" ]] || fail "expected archived Home Manager symlink to remain a symlink"
  [[ "$(readlink "${linked_home}.stale-1700000000")" == "$home_dir/linked-target" ]] \
    || fail "expected archived Home Manager symlink target to be preserved"
  [[ -d "${directory_home}.stale-1700000000" ]] || fail "expected archived directory: ${directory_home}.stale-1700000000"
  assert_file_content "${directory_home}.stale-1700000000/content" 'directory'
  assert_file "${spaced_xdg}.stale-1700000000"
  assert_file_content "${spaced_xdg}.stale-1700000000" 'xdg-space'
  assert_file "${newline_xdg}.stale-1700000000"
  assert_file_content "${newline_xdg}.stale-1700000000" 'xdg-newline'
  assert_file "$deep_backup"
  assert_file_content "$collision_archive" 'collision-sentinel'
  assert_contains "$event_log" 'darwin-rebuild:switch --impure --flake'

  rm -rf "$repo"
}

test_nix_install_cycle2_uses_private_temp_lists_and_cleans_up() {
  local runner_bin="${1:-$TEST_ZSH_BIN}"
  local repo
  local home_dir
  local bin_dir
  local event_log
  local output_file
  local temp_dir
  local real_mkdir
  local real_mktemp
  local real_chmod
  local allowed_temp_root
  local guard_status
  local exit_status=0

  make_temp_dir
  repo="$REPLY"
  home_dir="$repo/home"
  bin_dir="$repo/bin"
  event_log="$repo/events.log"
  output_file="$repo/output.log"
  real_mkdir="/bin/mkdir"
  [[ -x "$real_mkdir" ]] || real_mkdir="/usr/bin/mkdir"
  real_mktemp="/usr/bin/mktemp"
  [[ -x "$real_mktemp" ]] || real_mktemp="/bin/mktemp"
  real_chmod="/bin/chmod"
  [[ -x "$real_chmod" ]] || real_chmod="/usr/bin/chmod"
  allowed_temp_root="/private/tmp"
  is_test_macos || allowed_temp_root="${TMPDIR:-/tmp}"

  mkdir -p "$repo/scripts/lib" "$home_dir" "$bin_dir" "$repo/config/nix"
  cp "$INSTALL_SCRIPT" "$repo/scripts/nix_install.sh"
  copy_script_libs "$repo"
  print -r -- "private" > "$home_dir/.private.before-nix-darwin"
  : > "$repo/config/nix/homebrew-fallback.nix"
  : > "$repo/config/nix/mas-apps.nix"

  cat > "$bin_dir/mkdir" <<EOF
#!/bin/zsh
set -euo pipefail
if (( \$# != 1 )); then
  print -u2 -- "rejected mkdir argv: \$*"
  exit 126
fi
temp_path="\$1"
temp_name="\${temp_path##*/}"
[[ "\$temp_path" == "$allowed_temp_root"/dotfiles-nix-backups."\$PPID".0 ]] || {
  print -u2 -- "rejected mkdir path: \$temp_path"
  exit 126
}
"$real_mkdir" "\$temp_path"
if [[ -d "\$temp_path" ]]; then
  mode=""
  if [[ "\$OSTYPE" == darwin* ]]; then
    mode="\$(stat -f '%Lp' "\$temp_path")"
  else
    mode="\$(stat -c '%a' "\$temp_path")"
  fi
  print -r -- "temp-dir=\$temp_path" >> "\$NIX_TEST_EVENT_LOG"
  print -r -- "temp-dir-created-mode=\$mode" >> "\$NIX_TEST_EVENT_LOG"
fi
EOF
  cat > "$bin_dir/chmod" <<EOF
#!/bin/zsh
set -euo pipefail
if (( \$# == 2 )) && [[ "\$1" == 700 && "\$2" == "$allowed_temp_root"/dotfiles-nix-backups."\$PPID".0 ]]; then
  "$real_chmod" 700 "\$2"
  mode=""
  if [[ "\$OSTYPE" == darwin* ]]; then
    mode="\$(stat -f '%Lp' "\$2")"
  else
    mode="\$(stat -c '%a' "\$2")"
  fi
  print -r -- "temp-dir-mode=\$mode" >> "\$NIX_TEST_EVENT_LOG"
elif (( \$# == 3 )) && [[ "\$1" == 600 ]]; then
  parent_one="\${2%/*}"
  parent_two="\${3%/*}"
  [[ "\$parent_one" == "$allowed_temp_root"/dotfiles-nix-backups."\$PPID".0 && "\$parent_two" == "\$parent_one" ]] || exit 126
  case "\${2##*/}" in
    dotfiles-home-backups.*|dotfiles-config-backups.*) ;;
    *) exit 126 ;;
  esac
  case "\${3##*/}" in
    dotfiles-home-backups.*|dotfiles-config-backups.*) ;;
    *) exit 126 ;;
  esac
  "$real_chmod" 600 "\$2" "\$3"
else
  print -u2 -- "rejected chmod argv: \$*"
  exit 126
fi
EOF
  cat > "$bin_dir/mktemp" <<EOF
#!/bin/zsh
set -euo pipefail
if (( \$# != 1 )); then
  print -u2 -- "rejected mktemp argv: \$*"
  exit 126
fi
template="\$1"
parent="\${template%/*}"
[[ "\$parent" == "$allowed_temp_root"/dotfiles-nix-backups."\$PPID".0 ]] || {
  print -u2 -- "rejected mktemp parent: \$parent"
  exit 126
}
case "\${template##*/}" in
  dotfiles-home-backups.XXXXXX|dotfiles-config-backups.XXXXXX) ;;
  *) print -u2 -- "rejected mktemp template: \$template"; exit 126 ;;
esac
mode=""
if [[ "\$OSTYPE" == darwin* ]]; then
  mode="\$(stat -f '%Lp' "\$parent")"
else
  mode="\$(stat -c '%a' "\$parent")"
fi
print -r -- "temp-list-parent=\$parent" >> "\$NIX_TEST_EVENT_LOG"
print -r -- "temp-list-parent-mode=\$mode" >> "\$NIX_TEST_EVENT_LOG"
exec "$real_mktemp" "\$template"
EOF
  cat > "$bin_dir/nix" <<'EOF'
#!/bin/zsh
set -euo pipefail
print -r -- "nix:$*" >> "$NIX_TEST_EVENT_LOG"
EOF
  cat > "$bin_dir/home-manager" <<'EOF'
#!/bin/zsh
set -euo pipefail
print -r -- "home-manager:$*" >> "$NIX_TEST_EVENT_LOG"
EOF
  cat > "$bin_dir/darwin-rebuild" <<'EOF'
#!/bin/zsh
set -euo pipefail
print -r -- "darwin-rebuild:$*" >> "$NIX_TEST_EVENT_LOG"
EOF
  write_strict_fake_sudo "$bin_dir"
  chmod +x "$bin_dir"/*

  NIX_TEST_EVENT_LOG="$event_log" HOME="$home_dir" \
    NIX_TEST_FIXTURE_ROOT="$repo" \
    NIX_TEST_ALLOWED_TEMP_ROOT="$allowed_temp_root" \
    PATH="$bin_dir:/bin:/usr/bin:/usr/sbin:/sbin" \
    DOTFILES_HOME_MANAGER_BACKUP_ARCHIVE_EPOCH=1700000000 \
    DOTFILES_DARWIN_SUDO_LOCAL_PATH="$repo/etc/pam.d/sudo_local" \
    DOTFILES_DARWIN_ETC_SHELL_RC_PATHS="$repo/etc/bashrc:$repo/etc/zshrc" \
    "$runner_bin" "$repo/scripts/nix_install.sh" --profile cli \
    > "$output_file" 2>&1 || exit_status=$?

  (( exit_status == 0 )) || {
    sed -n '1,160p' "$output_file" >&2
    sed -n '1,160p' "$event_log" >&2
    fail "expected private Nix temporary list fixture to succeed"
  }
  temp_dir="$(grep '^temp-dir=' "$event_log" 2>/dev/null | head -1 | sed 's/^temp-dir=//' || true)"
  [[ -n "$temp_dir" ]] || fail "expected a dedicated Nix temporary directory"
  assert_contains "$event_log" 'temp-dir-created-mode=700'
  assert_contains "$event_log" 'temp-dir-mode=700'
  assert_contains "$event_log" 'temp-list-parent-mode=700'
  assert_not_exists "$temp_dir"

  guard_status=0
  "$bin_dir/mkdir" "$repo/escaped" > "$repo/mkdir-guard.log" 2>&1 || guard_status=$?
  (( guard_status != 0 )) || fail "fake mkdir must reject paths outside its exact allowlist"
  guard_status=0
  "$bin_dir/mktemp" "$repo/escaped.XXXXXX" > "$repo/mktemp-guard.log" 2>&1 || guard_status=$?
  (( guard_status != 0 )) || fail "fake mktemp must reject paths outside its exact allowlist"
  guard_status=0
  NIX_TEST_EVENT_LOG="$event_log" NIX_TEST_FIXTURE_ROOT="$repo" \
    "$bin_dir/sudo" mv /etc/hosts "$repo/escaped" > "$repo/sudo-guard.log" 2>&1 || guard_status=$?
  (( guard_status != 0 )) || fail "fake sudo must reject out-of-fixture mv paths"

  rm -rf "$repo"
}

test_nix_install_cycle2_cleans_private_lists_when_mv_fails() {
  local runner_bin="${1:-$TEST_ZSH_BIN}"
  local repo
  local home_dir
  local bin_dir
  local event_log
  local output_file
  local backup_path
  local real_mkdir
  local allowed_temp_root
  local temp_dir
  local exit_status=0

  make_temp_dir
  repo="$REPLY"
  home_dir="$repo/home"
  bin_dir="$repo/bin"
  event_log="$repo/events.log"
  output_file="$repo/output.log"
  backup_path="$home_dir/.move-failure.before-nix-darwin"
  real_mkdir="/bin/mkdir"
  [[ -x "$real_mkdir" ]] || real_mkdir="/usr/bin/mkdir"
  allowed_temp_root="/private/tmp"
  is_test_macos || allowed_temp_root="${TMPDIR:-/tmp}"
  mkdir -p "$repo/scripts/lib" "$home_dir" "$bin_dir" "$repo/config/nix"
  cp "$INSTALL_SCRIPT" "$repo/scripts/nix_install.sh"
  copy_script_libs "$repo"
  print -r -- 'must-remain' > "$backup_path"
  : > "$repo/config/nix/homebrew-fallback.nix"
  : > "$repo/config/nix/mas-apps.nix"

  cat > "$bin_dir/mkdir" <<EOF
#!/bin/zsh
set -euo pipefail
if (( \$# != 1 )); then
  print -u2 -- "rejected mkdir argv: \$*"
  exit 126
fi
temp_path="\$1"
[[ "\$temp_path" == "$allowed_temp_root"/dotfiles-nix-backups."\$PPID".0 ]] || {
  print -u2 -- "rejected mkdir path: \$temp_path"
  exit 126
}
"$real_mkdir" "\$temp_path"
print -r -- "temp-dir=\$temp_path" >> "\$NIX_TEST_EVENT_LOG"
EOF
  cat > "$bin_dir/mv" <<'EOF'
#!/bin/zsh
set -euo pipefail
print -r -- "mv:$*" >> "$NIX_TEST_EVENT_LOG"
exit 88
EOF
  for command_name in nix home-manager darwin-rebuild sudo; do
    cat > "$bin_dir/$command_name" <<EOF
#!/bin/zsh
set -euo pipefail
print -r -- "$command_name:\$*" >> "$event_log"
exit 99
EOF
  done
  chmod +x "$bin_dir"/*

  NIX_TEST_EVENT_LOG="$event_log" HOME="$home_dir" \
    NIX_TEST_FIXTURE_ROOT="$repo" \
    PATH="$bin_dir:/bin:/usr/bin:/usr/sbin:/sbin" \
    DOTFILES_HOME_MANAGER_BACKUP_ARCHIVE_EPOCH=1700000000 \
    DOTFILES_DARWIN_SUDO_LOCAL_PATH="$repo/etc/pam.d/sudo_local" \
    DOTFILES_DARWIN_ETC_SHELL_RC_PATHS="$repo/etc/bashrc:$repo/etc/zshrc" \
    "$runner_bin" "$repo/scripts/nix_install.sh" --profile cli \
    > "$output_file" 2>&1 || exit_status=$?

  (( exit_status != 0 )) || fail "Nix archive mv failure must stop activation"
  assert_file_content "$backup_path" 'must-remain'
  assert_contains "$event_log" 'mv:'
  assert_not_contains "$event_log" 'darwin-rebuild:'
  assert_not_contains "$event_log" 'home-manager:'
  temp_dir="$(grep '^temp-dir=' "$event_log" 2>/dev/null | head -1 | sed 's/^temp-dir=//' || true)"
  [[ -n "$temp_dir" ]] || fail "expected private temp directory in mv failure fixture"
  assert_not_exists "$temp_dir"

  rm -rf "$repo"
}

test_nix_install_cycle2_dry_run_does_not_move_backups_or_use_sudo() {
  local runner_bin="${1:-$TEST_ZSH_BIN}"
  local repo
  local home_dir
  local bin_dir
  local event_log
  local backup_path
  local output_file
  local exit_status=0
  local expected_stage

  make_temp_dir
  repo="$REPLY"
  home_dir="$repo/home"
  bin_dir="$repo/bin"
  event_log="$repo/events.log"
  output_file="$repo/output.log"
  backup_path="$home_dir/.existing.before-nix-darwin"
  mkdir -p "$repo/scripts/lib" "$home_dir" "$bin_dir" "$repo/config/nix"
  cp "$INSTALL_SCRIPT" "$repo/scripts/nix_install.sh"
  copy_script_libs "$repo"
  print -r -- "legacy" > "$backup_path"
  : > "$repo/config/nix/homebrew-fallback.nix"
  : > "$repo/config/nix/mas-apps.nix"

  cat > "$bin_dir/nix" <<'EOF'
#!/bin/zsh
set -euo pipefail
print -r -- "nix:$*" >> "$NIX_TEST_EVENT_LOG"
EOF
  cat > "$bin_dir/home-manager" <<'EOF'
#!/bin/zsh
set -euo pipefail
print -r -- "home-manager:$*" >> "$NIX_TEST_EVENT_LOG"
EOF
  cat > "$bin_dir/darwin-rebuild" <<'EOF'
#!/bin/zsh
set -euo pipefail
print -r -- "darwin-rebuild:$*" >> "$NIX_TEST_EVENT_LOG"
EOF
  write_strict_fake_sudo "$bin_dir"
  chmod +x "$bin_dir"/*

  NIX_TEST_EVENT_LOG="$event_log" HOME="$home_dir" \
    NIX_TEST_FIXTURE_ROOT="$repo" \
    PATH="$bin_dir:/bin:/usr/bin:/usr/sbin:/sbin" \
    DOTFILES_HOME_MANAGER_BACKUP_ARCHIVE_EPOCH=1700000000 \
    DOTFILES_DARWIN_SUDO_LOCAL_PATH="$repo/etc/pam.d/sudo_local" \
    DOTFILES_DARWIN_ETC_SHELL_RC_PATHS="$repo/etc/bashrc:$repo/etc/zshrc" \
    "$runner_bin" "$repo/scripts/nix_install.sh" --cli-only --dry-run \
    > "$output_file" 2>&1 || exit_status=$?

  (( exit_status == 0 )) || {
    sed -n '1,160p' "$output_file" >&2
    fail "expected Nix dry-run to succeed"
  }
  assert_file "$backup_path"
  assert_not_contains "$event_log" 'sudo:'
  if is_test_macos; then
    expected_stage='darwin-rebuild:build --impure --flake'
  else
    expected_stage='home-manager:build --impure --flake'
  fi
  assert_contains "$event_log" "$expected_stage"

  rm -rf "$repo"
}

test_nix_install_cycle2_uses_date_for_default_archive_epoch() {
  local runner_bin="${1:-$TEST_ZSH_BIN}"
  local repo
  local home_dir
  local bin_dir
  local event_log
  local backup_path
  local output_file
  local exit_status=0

  make_temp_dir
  repo="$REPLY"
  home_dir="$repo/home"
  bin_dir="$repo/bin"
  event_log="$repo/events.log"
  output_file="$repo/output.log"
  backup_path="$home_dir/.default.before-nix-darwin"
  mkdir -p "$repo/scripts/lib" "$home_dir" "$bin_dir" "$repo/config/nix"
  cp "$INSTALL_SCRIPT" "$repo/scripts/nix_install.sh"
  copy_script_libs "$repo"
  print -r -- "legacy" > "$backup_path"
  : > "$repo/config/nix/homebrew-fallback.nix"
  : > "$repo/config/nix/mas-apps.nix"

  cat > "$bin_dir/date" <<'EOF'
#!/bin/zsh
set -euo pipefail
[[ "$1" == +%s ]] || exit 1
print -r -- "1700000000"
EOF
  cat > "$bin_dir/nix" <<'EOF'
#!/bin/zsh
set -euo pipefail
print -r -- "nix:$*" >> "$NIX_TEST_EVENT_LOG"
EOF
  cat > "$bin_dir/home-manager" <<'EOF'
#!/bin/zsh
set -euo pipefail
print -r -- "home-manager:$*" >> "$NIX_TEST_EVENT_LOG"
EOF
  cat > "$bin_dir/darwin-rebuild" <<'EOF'
#!/bin/zsh
set -euo pipefail
print -r -- "darwin-rebuild:$*" >> "$NIX_TEST_EVENT_LOG"
EOF
  write_strict_fake_sudo "$bin_dir"
  chmod +x "$bin_dir"/*

  env -u DOTFILES_HOME_MANAGER_BACKUP_ARCHIVE_EPOCH \
    NIX_TEST_EVENT_LOG="$event_log" HOME="$home_dir" \
    NIX_TEST_FIXTURE_ROOT="$repo" \
    PATH="$bin_dir:/bin:/usr/bin:/usr/sbin:/sbin" \
    DOTFILES_DARWIN_SUDO_LOCAL_PATH="$repo/etc/pam.d/sudo_local" \
    DOTFILES_DARWIN_ETC_SHELL_RC_PATHS="$repo/etc/bashrc:$repo/etc/zshrc" \
    "$runner_bin" "$repo/scripts/nix_install.sh" --profile cli \
    > "$output_file" 2>&1 || exit_status=$?

  (( exit_status == 0 )) || {
    sed -n '1,160p' "$output_file" >&2
    fail "expected default archive epoch to succeed"
  }
  assert_file "$backup_path.stale-1700000000"
  assert_not_exists "$backup_path"

  rm -rf "$repo"
}

test_nix_install_rejects_invalid_epoch_and_date_output() {
  local runner_bin="${1:-$TEST_ZSH_BIN}"
  local repo
  local home_dir
  local bin_dir
  local event_log
  local output_file
  local backup_path
  local invalid_epoch
  local date_mode
  local exit_status

  make_temp_dir
  repo="$REPLY"
  home_dir="$repo/home"
  bin_dir="$repo/bin"
  event_log="$repo/events.log"
  output_file="$repo/output.log"
  backup_path="$home_dir/.invalid.before-nix-darwin"
  mkdir -p "$repo/scripts/lib" "$home_dir" "$bin_dir" "$repo/config/nix"
  cp "$INSTALL_SCRIPT" "$repo/scripts/nix_install.sh"
  copy_script_libs "$repo"
  print -r -- "must-remain" > "$backup_path"
  : > "$repo/config/nix/homebrew-fallback.nix"
  : > "$repo/config/nix/mas-apps.nix"

  for command_name in nix home-manager darwin-rebuild sudo find mv; do
    cat > "$bin_dir/$command_name" <<EOF
#!/bin/zsh
set -euo pipefail
print -r -- "$command_name:\$*" >> "$event_log"
exit 99
EOF
    chmod +x "$bin_dir/$command_name"
  done
  cat > "$bin_dir/date" <<EOF
#!/bin/zsh
set -euo pipefail
print -r -- "date:\$*" >> "$event_log"
case "\${NIX_TEST_DATE_MODE:-failure}" in
  failure)
    exit 71
    ;;
  empty)
    ;;
  nonnumeric)
    print -r -- "not-a-number"
    ;;
esac
EOF
  chmod +x "$bin_dir/date"

  for invalid_epoch in "not-a-number" "1700000000/../escape"; do
    : > "$event_log"
    exit_status=0
    NIX_TEST_EVENT_LOG="$event_log" HOME="$home_dir" \
      PATH="$bin_dir:/bin:/usr/bin:/usr/sbin:/sbin" \
      DOTFILES_HOME_MANAGER_BACKUP_ARCHIVE_EPOCH="$invalid_epoch" \
      "$runner_bin" "$repo/scripts/nix_install.sh" --profile cli \
      > "$output_file" 2>&1 || exit_status=$?
    (( exit_status != 0 )) || fail "invalid epoch must fail before Nix activation: $invalid_epoch"
    assert_output_contains "$output_file" 'Home Manager backup archive epoch must be a non-negative integer.'
    assert_file_content "$backup_path" 'must-remain'
    assert_not_contains "$event_log" 'date:'
    assert_not_contains "$event_log" 'nix:'
    assert_not_contains "$event_log" 'find:'
    assert_not_contains "$event_log" 'mv:'
  done

  for date_mode in failure empty nonnumeric; do
    : > "$event_log"
    exit_status=0
    env -u DOTFILES_HOME_MANAGER_BACKUP_ARCHIVE_EPOCH \
      NIX_TEST_DATE_MODE="$date_mode" NIX_TEST_EVENT_LOG="$event_log" HOME="$home_dir" \
      PATH="$bin_dir:/bin:/usr/bin:/usr/sbin:/sbin" \
      "$runner_bin" "$repo/scripts/nix_install.sh" --profile cli \
      > "$output_file" 2>&1 || exit_status=$?
    (( exit_status != 0 )) || fail "date output must fail closed: $date_mode"
    assert_file_content "$backup_path" 'must-remain'
    assert_contains "$event_log" 'date:+%s'
    assert_not_contains "$event_log" 'nix:'
    assert_not_contains "$event_log" 'find:'
    assert_not_contains "$event_log" 'mv:'
  done

  rm -rf "$repo"
}

test_nix_install_path_resolution_ignores_spoofed_bash_version() {
  local output
  local exit_status=0

  make_temp_file
  output="$REPLY"
  BASH_VERSION=spoofed DOTFILES_HOME_MANAGER_BACKUP_ARCHIVE_EPOCH=1700000000 \
    "$TEST_ZSH_BIN" "$INSTALL_SCRIPT" --help > "$output" 2>&1 || exit_status=$?
  (( exit_status == 0 )) || {
    sed -n '1,120p' "$output" >&2
    fail "zsh must not enter the Bash path only because BASH_VERSION is exported"
  }
  assert_output_contains "$output" '--uninstall-homebrew'

  rm -f "$output"
}

test_nix_install_rejects_spoofed_bash_source_and_ostype() {
  local bash_bin="${1:-/bin/bash}"
  local repo
  local spoof_root
  local event_log
  local output_file
  local bin_dir
  local spoof_setup_profile
  local spoofed_ostype
  local actual_machine
  local spoofed_machine
  local expected_system
  local spoofed_system
  local expected_os_suffix
  local exit_status=0

  make_temp_dir
  repo="$REPLY"
  spoof_root="$repo/spoof"
  event_log="$repo/events.log"
  output_file="$repo/output.log"
  bin_dir="$repo/bin"
  spoof_setup_profile="$spoof_root/scripts/lib/setup_profile.sh"
  mkdir -p "$spoof_root/scripts/lib" "$bin_dir"
  cp "$REPO_ROOT/scripts/lib/homebrew.sh" "$spoof_root/scripts/lib/homebrew.sh"
  cp "$REPO_ROOT/scripts/lib/homebrew_fallback.sh" "$spoof_root/scripts/lib/homebrew_fallback.sh"
  cp "$REPO_ROOT/scripts/lib/runtime.sh" "$spoof_root/scripts/lib/runtime.sh"
  cat > "$spoof_setup_profile" <<'EOF'
#!/bin/zsh
set -euo pipefail
print -r -- 'spoofed-setup-profile' >> "$NIX_TEST_EVENT_LOG"
. "$NIX_TEST_REAL_SETUP_PROFILE"
EOF

  BASH_SOURCE="$spoof_root/scripts/nix_install.sh" \
    NIX_TEST_EVENT_LOG="$event_log" NIX_TEST_REAL_SETUP_PROFILE="$REPO_ROOT/scripts/lib/setup_profile.sh" \
    DOTFILES_HOME_MANAGER_BACKUP_ARCHIVE_EPOCH=1700000000 \
    "$bash_bin" "$INSTALL_SCRIPT" --help > "$output_file" 2>&1 || exit_status=$?
  (( exit_status == 0 )) || {
    sed -n '1,120p' "$output_file" >&2
    fail "Bash direct execution must ignore an imported scalar BASH_SOURCE"
  }
  assert_output_contains "$output_file" '--uninstall-homebrew'
  assert_not_exists "$event_log"

  cat > "$bin_dir/nix" <<'EOF'
#!/bin/zsh
set -euo pipefail
print -r -- "nix:$*" >> "$NIX_TEST_EVENT_LOG"
EOF
  cat > "$bin_dir/home-manager" <<'EOF'
#!/bin/zsh
set -euo pipefail
print -r -- "home-manager:$*" >> "$NIX_TEST_EVENT_LOG"
EOF
  cat > "$bin_dir/darwin-rebuild" <<'EOF'
#!/bin/zsh
set -euo pipefail
print -r -- "darwin-rebuild:$*" >> "$NIX_TEST_EVENT_LOG"
EOF
  write_strict_fake_sudo "$bin_dir"
  chmod +x "$bin_dir"/*
  mkdir -p "$repo/config/nix"
  : > "$repo/config/nix/homebrew-fallback.nix"
  : > "$repo/config/nix/mas-apps.nix"
  spoofed_ostype="darwin-spoof"
  is_test_macos && spoofed_ostype="linux-spoof"
  : > "$event_log"
  exit_status=0
  OSTYPE="$spoofed_ostype" NIX_TEST_EVENT_LOG="$event_log" NIX_TEST_FIXTURE_ROOT="$repo" \
    HOME="$repo/home" PATH="$bin_dir:/bin:/usr/bin:/usr/sbin:/sbin" \
    DOTFILES_HOME_MANAGER_BACKUP_ARCHIVE_EPOCH=1700000000 \
    "$bash_bin" "$INSTALL_SCRIPT" --cli-only --dry-run > "$repo/os-output.log" 2>&1 || exit_status=$?
  (( exit_status == 0 )) || {
    sed -n '1,160p' "$repo/os-output.log" >&2
    fail "Bash direct execution must use the host OS instead of OSTYPE"
  }
  if is_test_macos; then
    assert_contains "$event_log" 'darwin-rebuild:build --impure --flake'
    assert_not_contains "$event_log" 'home-manager:build'
  else
    assert_contains "$event_log" 'home-manager:build --impure --flake'
    assert_not_contains "$event_log" 'darwin-rebuild:build'
  fi

  actual_machine="$(/usr/bin/uname -m)"
  case "$actual_machine" in
    arm64|aarch64)
      spoofed_machine="x86_64"
      expected_system="aarch64"
      spoofed_system="x86_64"
      ;;
    x86_64|amd64)
      spoofed_machine="arm64"
      expected_system="x86_64"
      spoofed_system="aarch64"
      ;;
    *)
      fail "unsupported test machine: $actual_machine"
      ;;
  esac
  expected_os_suffix="linux"
  is_test_macos && expected_os_suffix="darwin"
  : > "$event_log"
  exit_status=0
  CPUTYPE="$spoofed_machine" MACHTYPE="${spoofed_machine}-unknown" OSTYPE="$spoofed_ostype" \
    NIX_TEST_EVENT_LOG="$event_log" NIX_TEST_FIXTURE_ROOT="$repo" \
    HOME="$repo/home" PATH="$bin_dir:/bin:/usr/bin:/usr/sbin:/sbin" \
    DOTFILES_HOME_MANAGER_BACKUP_ARCHIVE_EPOCH=1700000000 \
    "$bash_bin" "$INSTALL_SCRIPT" --cli-only --dry-run > "$repo/arch-output.log" 2>&1 || exit_status=$?
  (( exit_status == 0 )) || {
    sed -n '1,160p' "$repo/arch-output.log" >&2
    fail "Bash direct execution must use uname machine instead of CPUTYPE/MACHTYPE"
  }
  assert_contains "$event_log" "${expected_system}-${expected_os_suffix}-cli"
  assert_not_contains "$event_log" "${spoofed_system}-${expected_os_suffix}-cli"

  rm -rf "$repo"
}

test_nix_install_cycle2_fails_closed_when_find_producer_fails() {
  local runner_bin="${1:-$TEST_ZSH_BIN}"
  local repo
  local home_dir
  local config_dir
  local bin_dir
  local event_log
  local backup_path
  local xdg_backup_path
  local output_file
  local real_mkdir
  local allowed_temp_root
  local temp_dir
  local guard_status
  local exit_status=0

  make_temp_dir
  repo="$REPLY"
  home_dir="$repo/home"
  config_dir="$repo/config-home"
  bin_dir="$repo/bin"
  event_log="$repo/events.log"
  output_file="$repo/output.log"
  backup_path="$home_dir/.home.before-nix-darwin"
  xdg_backup_path="$config_dir/mise/xdg.before-nix-darwin"
  real_mkdir="/bin/mkdir"
  [[ -x "$real_mkdir" ]] || real_mkdir="/usr/bin/mkdir"
  allowed_temp_root="/private/tmp"
  is_test_macos || allowed_temp_root="${TMPDIR:-/tmp}"
  mkdir -p "$repo/scripts/lib" "$home_dir" "$config_dir/mise" "$bin_dir" "$repo/config/nix"
  cp "$INSTALL_SCRIPT" "$repo/scripts/nix_install.sh"
  copy_script_libs "$repo"
  print -r -- "home" > "$backup_path"
  print -r -- "xdg" > "$xdg_backup_path"
  : > "$repo/config/nix/homebrew-fallback.nix"
  : > "$repo/config/nix/mas-apps.nix"

  cat > "$bin_dir/mkdir" <<EOF
#!/bin/zsh
set -euo pipefail
if (( \$# != 1 )); then
  print -u2 -- "rejected mkdir argv: \$*"
  exit 126
fi
temp_path="\$1"
[[ "\$temp_path" == "$allowed_temp_root"/dotfiles-nix-backups."\$PPID".0 ]] || {
  print -u2 -- "rejected mkdir path: \$temp_path"
  exit 126
}
"$real_mkdir" "\$temp_path"
print -r -- "temp-dir=\$temp_path" >> "\$NIX_TEST_EVENT_LOG"
EOF

  cat > "$bin_dir/find" <<'EOF'
#!/bin/zsh
set -euo pipefail
fixture_root="${NIX_TEST_FIXTURE_ROOT:?}"
fixture_root="$(cd -P "$fixture_root" && pwd -P)"
fail_root="${NIX_TEST_FAIL_ROOT:?}"
fail_root="$(cd -P "$fail_root" && pwd -P)"
find_root="$(cd -P "$1" && pwd -P)"
if [[ "$fail_root" == "$fixture_root/home" ]]; then
  (( $# == 8 )) && [[ "$find_root" == "$fixture_root/home" && "$2" == -mindepth && "$3" == 1 \
    && "$4" == -maxdepth && "$5" == 1 && "$6" == -name && "$7" == '.*.before-nix-darwin' \
    && "$8" == -print0 ]] || {
    print -u2 -- "rejected find argv: $*"
    exit 126
  }
  print -r -- "find-failed:$1" >> "$NIX_TEST_EVENT_LOG"
  exit 77
fi
if [[ "$fail_root" == "$fixture_root/config-home" ]]; then
  if (( $# == 8 )) && [[ "$find_root" == "$fixture_root/home" && "$2" == -mindepth && "$3" == 1 \
    && "$4" == -maxdepth && "$5" == 1 && "$6" == -name && "$7" == '.*.before-nix-darwin' \
    && "$8" == -print0 ]]; then
    exec /usr/bin/find "$fixture_root/home" -mindepth 1 -maxdepth 1 -name '.*.before-nix-darwin' -print0
  fi
  (( $# == 6 )) && [[ "$find_root" == "$fixture_root/config-home" && "$2" == -mindepth && "$3" == 1 \
    && "$4" == -name && "$5" == '*.before-nix-darwin' && "$6" == -print0 ]] || {
    print -u2 -- "rejected find argv: $*"
    exit 126
  }
  print -r -- "find-failed:$1" >> "$NIX_TEST_EVENT_LOG"
  exit 77
fi
print -u2 -- "rejected find root: $fail_root"
exit 126
EOF
  cat > "$bin_dir/nix" <<'EOF'
#!/bin/zsh
set -euo pipefail
print -r -- "nix:$*" >> "$NIX_TEST_EVENT_LOG"
EOF
  cat > "$bin_dir/home-manager" <<'EOF'
#!/bin/zsh
set -euo pipefail
print -r -- "home-manager:$*" >> "$NIX_TEST_EVENT_LOG"
EOF
  cat > "$bin_dir/darwin-rebuild" <<'EOF'
#!/bin/zsh
set -euo pipefail
print -r -- "darwin-rebuild:$*" >> "$NIX_TEST_EVENT_LOG"
EOF
  write_strict_fake_sudo "$bin_dir"
  chmod +x "$bin_dir"/*

  NIX_TEST_FAIL_ROOT="$home_dir" NIX_TEST_EVENT_LOG="$event_log" \
    NIX_TEST_FIXTURE_ROOT="$repo" \
    NIX_TEST_ALLOWED_TEMP_ROOT="$allowed_temp_root" \
    HOME="$home_dir" XDG_CONFIG_HOME="$config_dir" \
    PATH="$bin_dir:/bin:/usr/bin:/usr/sbin:/sbin" \
    DOTFILES_HOME_MANAGER_BACKUP_ARCHIVE_EPOCH=1700000000 \
    DOTFILES_DARWIN_SUDO_LOCAL_PATH="$repo/etc/pam.d/sudo_local" \
    DOTFILES_DARWIN_ETC_SHELL_RC_PATHS="$repo/etc/bashrc:$repo/etc/zshrc" \
    "$runner_bin" "$repo/scripts/nix_install.sh" --profile cli \
    > "$output_file" 2>&1 || exit_status=$?

  (( exit_status != 0 )) || fail "expected HOME find producer failure"
  assert_contains "$event_log" "find-failed:$home_dir"
  assert_output_contains "$output_file" "ERROR: failed to discover existing Home Manager backups under $home_dir."
  assert_file "$backup_path"
  assert_file "$xdg_backup_path"
  temp_dir="$(grep '^temp-dir=' "$event_log" 2>/dev/null | head -1 | sed 's/^temp-dir=//' || true)"
  [[ -n "$temp_dir" ]] || fail "expected private temp directory in HOME find failure fixture"
  assert_not_exists "$temp_dir"
  assert_not_contains "$event_log" 'darwin-rebuild:'
  assert_not_contains "$event_log" 'home-manager:'

  guard_status=0
  NIX_TEST_EVENT_LOG="$event_log" NIX_TEST_FIXTURE_ROOT="$repo" NIX_TEST_FAIL_ROOT="$home_dir" \
    "$bin_dir/find" "$repo" -exec /bin/sh -c 'exit 0' \; > "$repo/find-guard.log" 2>&1 || guard_status=$?
  (( guard_status != 0 )) || fail "fake find must reject arbitrary roots and -exec arguments"

  rm -rf "$repo"
}

test_nix_install_cycle2_fails_closed_when_xdg_find_fails() {
  local runner_bin="${1:-$TEST_ZSH_BIN}"
  local repo
  local home_dir
  local config_dir
  local bin_dir
  local event_log
  local backup_path
  local xdg_backup_path
  local output_file
  local real_mkdir
  local allowed_temp_root
  local temp_dir
  local exit_status=0

  make_temp_dir
  repo="$REPLY"
  home_dir="$repo/home"
  config_dir="$repo/config-home"
  bin_dir="$repo/bin"
  event_log="$repo/events.log"
  output_file="$repo/output.log"
  backup_path="$home_dir/.home.before-nix-darwin"
  xdg_backup_path="$config_dir/mise/xdg.before-nix-darwin"
  real_mkdir="/bin/mkdir"
  [[ -x "$real_mkdir" ]] || real_mkdir="/usr/bin/mkdir"
  allowed_temp_root="/private/tmp"
  is_test_macos || allowed_temp_root="${TMPDIR:-/tmp}"
  mkdir -p "$repo/scripts/lib" "$home_dir" "$config_dir/mise" "$bin_dir" "$repo/config/nix"
  cp "$INSTALL_SCRIPT" "$repo/scripts/nix_install.sh"
  copy_script_libs "$repo"
  print -r -- "home" > "$backup_path"
  print -r -- "xdg" > "$xdg_backup_path"
  : > "$repo/config/nix/homebrew-fallback.nix"
  : > "$repo/config/nix/mas-apps.nix"

  cat > "$bin_dir/mkdir" <<EOF
#!/bin/zsh
set -euo pipefail
if (( \$# != 1 )); then
  print -u2 -- "rejected mkdir argv: \$*"
  exit 126
fi
temp_path="\$1"
[[ "\$temp_path" == "$allowed_temp_root"/dotfiles-nix-backups."\$PPID".0 ]] || {
  print -u2 -- "rejected mkdir path: \$temp_path"
  exit 126
}
"$real_mkdir" "\$temp_path"
print -r -- "temp-dir=\$temp_path" >> "\$NIX_TEST_EVENT_LOG"
EOF

  cat > "$bin_dir/find" <<'EOF'
#!/bin/zsh
set -euo pipefail
fixture_root="${NIX_TEST_FIXTURE_ROOT:?}"
fixture_root="$(cd -P "$fixture_root" && pwd -P)"
fail_root="${NIX_TEST_FAIL_ROOT:?}"
fail_root="$(cd -P "$fail_root" && pwd -P)"
find_root="$(cd -P "$1" && pwd -P)"
if [[ "$fail_root" == "$fixture_root/config-home" ]]; then
  if (( $# == 8 )) && [[ "$find_root" == "$fixture_root/home" && "$2" == -mindepth && "$3" == 1 \
    && "$4" == -maxdepth && "$5" == 1 && "$6" == -name && "$7" == '.*.before-nix-darwin' \
    && "$8" == -print0 ]]; then
    exec /usr/bin/find "$fixture_root/home" -mindepth 1 -maxdepth 1 -name '.*.before-nix-darwin' -print0
  fi
  (( $# == 6 )) && [[ "$find_root" == "$fixture_root/config-home" && "$2" == -mindepth && "$3" == 1 \
    && "$4" == -name && "$5" == '*.before-nix-darwin' && "$6" == -print0 ]] || {
    print -u2 -- "rejected find argv: $*"
    exit 126
  }
  print -r -- "find-failed:$1" >> "$NIX_TEST_EVENT_LOG"
  exit 77
fi
print -u2 -- "rejected find root: $fail_root"
exit 126
EOF
  cat > "$bin_dir/nix" <<'EOF'
#!/bin/zsh
set -euo pipefail
print -r -- "nix:$*" >> "$NIX_TEST_EVENT_LOG"
EOF
  cat > "$bin_dir/home-manager" <<'EOF'
#!/bin/zsh
set -euo pipefail
print -r -- "home-manager:$*" >> "$NIX_TEST_EVENT_LOG"
EOF
  cat > "$bin_dir/darwin-rebuild" <<'EOF'
#!/bin/zsh
set -euo pipefail
print -r -- "darwin-rebuild:$*" >> "$NIX_TEST_EVENT_LOG"
EOF
  write_strict_fake_sudo "$bin_dir"
  chmod +x "$bin_dir"/*

  NIX_TEST_FAIL_ROOT="$config_dir" NIX_TEST_EVENT_LOG="$event_log" \
    NIX_TEST_FIXTURE_ROOT="$repo" \
    NIX_TEST_ALLOWED_TEMP_ROOT="$allowed_temp_root" \
    HOME="$home_dir" XDG_CONFIG_HOME="$config_dir" \
    PATH="$bin_dir:/bin:/usr/bin:/usr/sbin:/sbin" \
    DOTFILES_HOME_MANAGER_BACKUP_ARCHIVE_EPOCH=1700000000 \
    DOTFILES_DARWIN_SUDO_LOCAL_PATH="$repo/etc/pam.d/sudo_local" \
    DOTFILES_DARWIN_ETC_SHELL_RC_PATHS="$repo/etc/bashrc:$repo/etc/zshrc" \
    "$runner_bin" "$repo/scripts/nix_install.sh" --profile cli \
    > "$output_file" 2>&1 || exit_status=$?

  (( exit_status != 0 )) || fail "expected XDG find producer failure"
  assert_contains "$event_log" "find-failed:$config_dir"
  assert_output_contains "$output_file" "ERROR: failed to discover existing Home Manager backups under $config_dir."
  assert_file "$backup_path"
  assert_file "$xdg_backup_path"
  temp_dir="$(grep '^temp-dir=' "$event_log" 2>/dev/null | head -1 | sed 's/^temp-dir=//' || true)"
  [[ -n "$temp_dir" ]] || fail "expected private temp directory in XDG find failure fixture"
  assert_not_exists "$temp_dir"
  assert_not_contains "$event_log" 'darwin-rebuild:'
  assert_not_contains "$event_log" 'home-manager:'

  rm -rf "$repo"
}

test_nix_install_cycle2_splits_darwin_paths_without_losing_characters() {
  local runner_bin="${1:-$TEST_ZSH_BIN}"
  local repo
  local home_dir
  local bin_dir
  local event_log
  local output_file
  local first_rc
  local second_rc
  local third_rc
  local exit_status=0

  skip_unless_macos "$funcstack[1]" || return 0
  make_temp_dir
  repo="$REPLY"
  home_dir="$repo/home"
  bin_dir="$repo/bin"
  event_log="$repo/events.log"
  output_file="$repo/output.log"
  mkdir -p "$repo/scripts/lib" "$home_dir" "$bin_dir" "$repo/config/nix" "$repo/etc"
  cp "$INSTALL_SCRIPT" "$repo/scripts/nix_install.sh"
  copy_script_libs "$repo"
  first_rc="$repo/etc/space rc"
  second_rc="$repo/etc/line
rc"
  third_rc="$repo/etc/back\\slash"
  print -r -- "first" > "$first_rc"
  print -r -- "second" > "$second_rc"
  print -r -- "third" > "$third_rc"
  : > "$repo/config/nix/homebrew-fallback.nix"
  : > "$repo/config/nix/mas-apps.nix"

  cat > "$bin_dir/nix" <<'EOF'
#!/bin/zsh
set -euo pipefail
print -r -- "nix:$*" >> "$NIX_TEST_EVENT_LOG"
EOF
  cat > "$bin_dir/darwin-rebuild" <<'EOF'
#!/bin/zsh
set -euo pipefail
print -r -- "darwin-rebuild:$*" >> "$NIX_TEST_EVENT_LOG"
EOF
  write_strict_fake_sudo "$bin_dir"
  chmod +x "$bin_dir"/*

  NIX_TEST_EVENT_LOG="$event_log" HOME="$home_dir" \
    NIX_TEST_FIXTURE_ROOT="$repo" \
    PATH="$bin_dir:/bin:/usr/bin:/usr/sbin:/sbin" \
    DOTFILES_HOME_MANAGER_BACKUP_ARCHIVE_EPOCH=1700000000 \
    DOTFILES_DARWIN_SUDO_LOCAL_PATH="$repo/etc/sudo_local" \
    DOTFILES_DARWIN_ETC_SHELL_RC_PATHS=":$first_rc::$second_rc:$third_rc:" \
    "$runner_bin" "$repo/scripts/nix_install.sh" --profile cli \
    > "$output_file" 2>&1 || exit_status=$?

  (( exit_status == 0 )) || {
    sed -n '1,160p' "$output_file" >&2
    fail "expected Darwin colon-path fixture to succeed"
  }
  assert_file "$first_rc.before-nix-darwin"
  assert_file "$second_rc.before-nix-darwin"
  assert_file "$third_rc.before-nix-darwin"
  assert_not_exists "$first_rc"
  assert_not_exists "$second_rc"
  assert_not_exists "$third_rc"
  assert_contains "$event_log" 'darwin-rebuild:switch --impure --flake'

  rm -rf "$repo"
}

test_nix_install_direct_copy_uses_bash_shebang() {
  local target_bash="${1:-/bin/bash}"
  local repo
  local script_copy
  local symlink_copy
  local bin_dir
  local event_log
  local exit_status=0

  make_temp_dir
  repo="$REPLY"
  script_copy="$repo/scripts/nix_install.sh"
  symlink_copy="$repo/link/nix_install.sh"
  bin_dir="$repo/bin"
  event_log="$repo/events.log"
  mkdir -p "$repo/scripts/lib" "$repo/link" "$bin_dir"
  cp "$INSTALL_SCRIPT" "$script_copy"
  cp "$REPO_ROOT/scripts/lib/setup_profile.sh" "$repo/scripts/lib/setup_profile.sh"
  cp "$REPO_ROOT/scripts/lib/homebrew.sh" "$repo/scripts/lib/homebrew.sh"
  cp "$REPO_ROOT/scripts/lib/homebrew_fallback.sh" "$repo/scripts/lib/homebrew_fallback.sh"
  cp "$REPO_ROOT/scripts/lib/runtime.sh" "$repo/scripts/lib/runtime.sh"
  chmod +x "$script_copy"
  ln -s "$script_copy" "$symlink_copy"

  write_strict_fake_bash "$bin_dir"
  cat > "$bin_dir/zsh" <<EOF
#!/bin/zsh
set -euo pipefail
print -r -- "zsh:\$*" >> "\$NIX_TEST_EVENT_LOG"
print -u2 -- 'rejected fake zsh dispatch'
exit 125
EOF
  chmod +x "$bin_dir/bash" "$bin_dir/zsh"

  NIX_TEST_EVENT_LOG="$event_log" NIX_TEST_TARGET_BASH="$target_bash" \
    NIX_TEST_ALLOWED_BASH_SCRIPT="$script_copy" NIX_TEST_FIXTURE_ROOT="$repo" \
    PATH="$bin_dir:/bin:/usr/bin:/usr/sbin:/sbin" \
    "$script_copy" --help > "$repo/output.log" 2>&1 || exit_status=$?

  (( exit_status == 0 )) || {
    sed -n '1,120p' "$repo/output.log" >&2
    fail "expected direct Nix copy to run"
  }
  assert_contains "$event_log" "bash:$script_copy --help"
  assert_not_contains "$event_log" 'zsh:'
  assert_output_contains "$repo/output.log" '--uninstall-homebrew'

  : > "$event_log"
  exit_status=0
  NIX_TEST_EVENT_LOG="$event_log" NIX_TEST_TARGET_BASH="$target_bash" \
    NIX_TEST_ALLOWED_BASH_SCRIPT="$symlink_copy" NIX_TEST_FIXTURE_ROOT="$repo" \
    PATH="$bin_dir:/bin:/usr/bin:/usr/sbin:/sbin" \
    "$symlink_copy" --help > "$repo/symlink-output.log" 2>&1 || exit_status=$?
  (( exit_status == 0 )) || {
    sed -n '1,120p' "$repo/symlink-output.log" >&2
    fail "expected direct Nix symlink to resolve its target directory"
  }
  assert_contains "$event_log" "bash:$symlink_copy --help"
  assert_not_contains "$event_log" 'zsh:'
  assert_output_contains "$repo/symlink-output.log" '--uninstall-homebrew'

  : > "$event_log"
  exit_status=0
  NIX_TEST_EVENT_LOG="$event_log" NIX_TEST_TARGET_BASH="$target_bash" \
  NIX_TEST_ALLOWED_BASH_SCRIPT="$script_copy" NIX_TEST_FIXTURE_ROOT="$repo" \
    "$bin_dir/bash" "$script_copy" --profile cli --host /etc > "$repo/bash-guard.log" 2>&1 || exit_status=$?
  (( exit_status != 0 )) || fail "fake Bash must reject unlisted script arguments"
  [[ ! -s "$event_log" ]] || fail "fake Bash must not log rejected invocations"

  rm -rf "$repo"
}

test_nix_install_uninstall_homebrew_uses_bash_child() {
  local runner_bin="${1:-$TEST_ZSH_BIN}"
  local child_bash="${2:-/bin/bash}"
  local repo
  local repo_real
  local home_dir
  local bin_dir
  local event_log
  local output_file
  local activation_line
  local child_line
  local exit_status=0

  make_temp_dir
  repo="$REPLY"
  repo_real="$(cd "$repo" && pwd -P)"
  home_dir="$repo/home"
  bin_dir="$repo/bin"
  event_log="$repo/events.log"
  output_file="$repo/output.log"
  mkdir -p "$repo/scripts/lib" "$repo/config/nix" "$home_dir" "$bin_dir"
  cp "$INSTALL_SCRIPT" "$repo/scripts/nix_install.sh"
  copy_script_libs "$repo"
  : > "$repo/config/nix/homebrew-fallback.nix"
  : > "$repo/config/nix/mas-apps.nix"
  cat > "$repo/scripts/remove_homebrew.sh" <<'EOF'
#!/bin/bash
set -euo pipefail
printf 'remove:%s\n' "$*" >> "$NIX_TEST_EVENT_LOG"
EOF
  chmod +x "$repo/scripts/remove_homebrew.sh"

  write_strict_fake_bash "$bin_dir"
  cat > "$bin_dir/zsh" <<'EOF'
#!/bin/zsh
set -euo pipefail
print -r -- "zsh:$*" >> "$NIX_TEST_EVENT_LOG"
print -u2 -- 'rejected fake zsh dispatch'
exit 125
EOF
  cat > "$bin_dir/nix" <<'EOF'
#!/bin/zsh
set -euo pipefail
print -r -- "nix:$*" >> "$NIX_TEST_EVENT_LOG"
EOF
  cat > "$bin_dir/darwin-rebuild" <<'EOF'
#!/bin/zsh
set -euo pipefail
print -r -- "darwin-rebuild:$*" >> "$NIX_TEST_EVENT_LOG"
EOF
  write_strict_fake_sudo "$bin_dir"
  chmod +x "$bin_dir"/*

  NIX_TEST_EVENT_LOG="$event_log" NIX_TEST_TARGET_BASH="$child_bash" \
    NIX_TEST_ALLOWED_BASH_SCRIPT="$repo_real/scripts/remove_homebrew.sh" \
    NIX_TEST_FIXTURE_ROOT="$repo" HOME="$home_dir" \
    PATH="$bin_dir:/bin:/usr/bin:/usr/sbin:/sbin" \
    DOTFILES_HOME_MANAGER_BACKUP_ARCHIVE_EPOCH=1700000000 \
    DOTFILES_DARWIN_SUDO_LOCAL_PATH="$repo/etc/pam.d/sudo_local" \
    DOTFILES_DARWIN_ETC_SHELL_RC_PATHS="$repo/etc/bashrc:$repo/etc/zshrc" \
    "$runner_bin" "$repo/scripts/nix_install.sh" --profile cli --uninstall-homebrew \
    > "$output_file" 2>&1 || exit_status=$?

  (( exit_status == 0 )) || {
    sed -n '1,180p' "$output_file" >&2
    sed -n '1,180p' "$event_log" >&2
    fail "expected Nix Homebrew child delegation to succeed"
  }
  assert_contains "$event_log" "bash:$repo_real/scripts/remove_homebrew.sh --apply --confirm-nix-ready"
  assert_not_contains "$event_log" 'zsh:'
  assert_contains "$event_log" 'remove:--apply --confirm-nix-ready'
  [[ "$(grep -cF "bash:$repo_real/scripts/remove_homebrew.sh --apply --confirm-nix-ready" "$event_log")" == 1 ]] \
    || fail "expected exactly one Homebrew child dispatch"
  activation_line="$(grep -n -E '^(darwin-rebuild:switch |home-manager:switch |nix:.* run )' "$event_log" | head -1 | cut -d: -f1)"
  child_line="$(grep -nF "bash:$repo_real/scripts/remove_homebrew.sh --apply --confirm-nix-ready" "$event_log" | cut -d: -f1)"
  [[ -n "$activation_line" && -n "$child_line" && "$activation_line" -lt "$child_line" ]] \
    || fail "expected Homebrew child dispatch after Nix activation"

  rm -rf "$repo"
}

test_remove_homebrew_dry_run_output_is_shell_neutral() {
  local bash_bin="${1:-/bin/bash}"
  local repo
  local home_dir
  local bin_dir
  local bash_output
  local zsh_output
  local curl_log
  local bash_status=0
  local zsh_status=0
  local apply_status=0
  local expected_command

  make_temp_dir
  repo="$REPLY"
  home_dir="$repo/home"
  bin_dir="$repo/bin"
  bash_output="$repo/bash-output.log"
  zsh_output="$repo/zsh-output.log"
  curl_log="$repo/curl.log"
  mkdir -p "$repo/scripts/lib" "$repo/config/nix" "$home_dir" "$bin_dir"
  cp "$REMOVE_HOMEBREW_SCRIPT" "$repo/scripts/remove_homebrew.sh"
  cp "$COMMAND_LIB" "$repo/scripts/lib/command.sh"
  : > "$repo/config/nix/homebrew-fallback.nix"
  cat > "$bin_dir/curl" <<'EOF'
#!/bin/zsh
set -euo pipefail
print -r -- "curl-called" >> "$REMOVE_TEST_CURL_LOG"
exit 99
EOF
  chmod +x "$bin_dir/curl"

  expected_command='NONINTERACTIVE=1 /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/uninstall.sh)"'
  PATH="$bin_dir:/bin:/usr/bin:/usr/sbin:/sbin" \
    HOME="$home_dir" "$bash_bin" "$repo/scripts/remove_homebrew.sh" --dry-run \
    > "$bash_output" 2>&1 || bash_status=$?
  PATH="$bin_dir:/bin:/usr/bin:/usr/sbin:/sbin" \
    HOME="$home_dir" "$TEST_ZSH_BIN" "$repo/scripts/remove_homebrew.sh" --dry-run \
    > "$zsh_output" 2>&1 || zsh_status=$?

  if (( bash_status != 0 )); then
    sed -n '1,100p' "$bash_output" >&2
  fi
  if (( zsh_status != 0 )); then
    sed -n '1,100p' "$zsh_output" >&2
  fi
  (( bash_status == 0 && zsh_status == 0 )) || fail "dry-run must run in Bash and zsh"
  assert_output_contains "$bash_output" "Homebrew uninstall command:"
  assert_output_contains "$bash_output" "$expected_command"
  assert_output_contains "$bash_output" "DRY-RUN: Homebrew was not removed"
  assert_output_contains "$zsh_output" "Homebrew uninstall command:"
  assert_output_contains "$zsh_output" "$expected_command"
  assert_output_contains "$zsh_output" "DRY-RUN: Homebrew was not removed"
  [[ "$(sed -n '/^Homebrew uninstall command:/{N;p;}' "$bash_output")" == \
    "$(sed -n '/^Homebrew uninstall command:/{N;p;}' "$zsh_output")" ]] \
    || fail "Bash and zsh Homebrew dry-run command blocks differ"
  assert_not_exists "$curl_log"

  PATH="$bin_dir:/bin:/usr/bin:/usr/sbin:/sbin" \
    HOME="$home_dir" REMOVE_TEST_CURL_LOG="$curl_log" \
    "$bash_bin" "$repo/scripts/remove_homebrew.sh" --apply \
    > "$repo/apply-output.log" 2>&1 || apply_status=$?
  (( apply_status != 0 )) || fail "apply without confirmation must fail"
  assert_output_contains "$repo/apply-output.log" "Refusing to remove Homebrew until Nix setup has been confirmed."
  assert_not_exists "$curl_log"

  rm -rf "$repo"
}

test_remove_homebrew_apply_propagates_curl_failure() {
  local bash_bin="${1:-/bin/bash}"
  local repo
  local home_dir
  local bin_dir
  local event_log
  local mode
  local exit_status

  make_temp_dir
  repo="$REPLY"
  home_dir="$repo/home"
  bin_dir="$repo/bin"
  event_log="$repo/events.log"
  mkdir -p "$repo/scripts/lib" "$repo/config/nix" "$home_dir" "$bin_dir"
  cp "$REMOVE_HOMEBREW_SCRIPT" "$repo/scripts/remove_homebrew.sh"
  cp "$COMMAND_LIB" "$repo/scripts/lib/command.sh"
  cat > "$repo/config/nix/homebrew-fallback.nix" <<'EOF'
{
  taps = [
  ];
  brews = [
  ];
  casks = [
  ];
  vscode = [
  ];
}
EOF
  cat > "$bin_dir/curl" <<'EOF'
#!/bin/zsh
set -euo pipefail
print -r -- "curl:$*" >> "$REMOVE_TEST_CURL_LOG"
  case "$REMOVE_TEST_CURL_MODE" in
  failure)
    exit 73
    ;;
  empty)
    exit 0
    ;;
  whitespace)
    printf ' \t\n'
    exit 0
    ;;
  *)
    print -r -- 'unexpected curl test mode' >&2
    exit 74
    ;;
esac
EOF
  chmod +x "$bin_dir/curl"

  for mode in failure empty whitespace; do
    : > "$event_log"
    exit_status=0
    REMOVE_TEST_CURL_LOG="$event_log" REMOVE_TEST_CURL_MODE="$mode" \
      HOME="$home_dir" PATH="$bin_dir:/bin:/usr/bin:/usr/sbin:/sbin" \
      "$bash_bin" "$repo/scripts/remove_homebrew.sh" --apply --confirm-nix-ready \
      > "$repo/$mode-output.log" 2>&1 || exit_status=$?
    (( exit_status != 0 )) || fail "Homebrew apply must fail when curl is $mode"
    if [[ "$mode" == failure ]]; then
      assert_output_contains "$repo/$mode-output.log" 'failed to download Homebrew uninstall script'
    else
      assert_output_contains "$repo/$mode-output.log" 'Homebrew uninstall script download was empty'
    fi
    assert_contains "$event_log" 'curl:-fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/uninstall.sh'
  done

  rm -rf "$repo"
}

test_remove_homebrew_apply_executes_downloaded_body_with_noninteractive() {
  local bash_bin="${1:-/bin/bash}"
  local repo
  local home_dir
  local bin_dir
  local event_log
  local body_log
  local body_sentinel
  local output_file
  local exit_status=0

  make_temp_dir
  repo="$REPLY"
  home_dir="$repo/home"
  bin_dir="$repo/bin"
  event_log="$repo/events.log"
  body_log="$repo/body.log"
  body_sentinel="$repo/body-complete"
  output_file="$repo/output.log"
  mkdir -p "$repo/scripts/lib" "$repo/config/nix" "$home_dir" "$bin_dir"
  cp "$REMOVE_HOMEBREW_SCRIPT" "$repo/scripts/remove_homebrew.sh"
  cp "$COMMAND_LIB" "$repo/scripts/lib/command.sh"
  cat > "$repo/config/nix/homebrew-fallback.nix" <<'EOF'
{
  taps = [ ];
  brews = [ ];
  casks = [ ];
  vscode = [ ];
}
EOF
  cat > "$bin_dir/curl" <<'EOF'
#!/bin/zsh
set -euo pipefail
(( $# == 2 )) && [[ "$1" == -fsSL && "$2" == https://raw.githubusercontent.com/Homebrew/install/HEAD/uninstall.sh ]] || {
  print -u2 -- "rejected curl argv: $*"
  exit 126
}
print -r -- "curl:$*" >> "$REMOVE_TEST_EVENT_LOG"
cat <<'PAYLOAD'
set -euo pipefail
case "${BASH_VERSINFO[0]}" in
  3|5) ;;
  *) exit 81 ;;
esac
[[ "${NONINTERACTIVE:-}" == 1 ]] || exit 82
[[ "$#" == 0 ]] || exit 83
printf '%s\n' 'payload-start' >> "$REMOVE_TEST_BODY_LOG"
printf 'NONINTERACTIVE=%s\n' "$NONINTERACTIVE" >> "$REMOVE_TEST_BODY_LOG"
: > "$REMOVE_TEST_BODY_SENTINEL"
printf '%s\n' 'payload-complete' >> "$REMOVE_TEST_BODY_LOG"
PAYLOAD
EOF
  chmod +x "$bin_dir/curl"

  REMOVE_TEST_EVENT_LOG="$event_log" REMOVE_TEST_BODY_LOG="$body_log" \
    REMOVE_TEST_BODY_SENTINEL="$body_sentinel" HOME="$home_dir" \
    PATH="$bin_dir:/bin:/usr/bin:/usr/sbin:/sbin" \
    "$bash_bin" "$repo/scripts/remove_homebrew.sh" --apply --confirm-nix-ready \
    > "$output_file" 2>&1 || exit_status=$?

  (( exit_status == 0 )) || {
    sed -n '1,160p' "$output_file" >&2
    fail "Homebrew apply should execute a successful downloaded body"
  }
  assert_contains "$event_log" 'curl:-fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/uninstall.sh'
  [[ "$(grep -c '^curl:' "$event_log")" == 1 ]] || fail "expected exactly one curl download"
  assert_file "$body_sentinel"
  assert_contains "$body_log" 'NONINTERACTIVE=1'
  assert_contains "$body_log" 'payload-complete'
  [[ "$(grep -c '^payload-complete$' "$body_log")" == 1 ]] || fail "expected downloaded body to finish once"

  rm -rf "$repo"
}

test_remove_homebrew_direct_copy_uses_bash_shebang() {
  local target_bash="${1:-/bin/bash}"
  local repo
  local script_copy
  local symlink_copy
  local bin_dir
  local event_log
  local exit_status=0

  make_temp_dir
  repo="$REPLY"
  script_copy="$repo/scripts/remove_homebrew.sh"
  symlink_copy="$repo/link/remove_homebrew.sh"
  bin_dir="$repo/bin"
  event_log="$repo/events.log"
  mkdir -p "$repo/scripts/lib" "$repo/link" "$repo/config/nix" "$bin_dir"
  cp "$REMOVE_HOMEBREW_SCRIPT" "$script_copy"
  cp "$COMMAND_LIB" "$repo/scripts/lib/command.sh"
  : > "$repo/config/nix/homebrew-fallback.nix"
  chmod +x "$script_copy"
  ln -s "$script_copy" "$symlink_copy"
  cat > "$bin_dir/bash" <<EOF
#!/bin/zsh
set -euo pipefail
event_log="\${REMOVE_TEST_EVENT_LOG:?}"
allowed_script="\${REMOVE_TEST_ALLOWED_BASH_SCRIPT:?}"
fixture_root="\$(cd -P "$repo" && pwd -P)"
if (( \$# != 2 )) || [[ "\$2" != --dry-run ]]; then
  print -u2 -- 'rejected fake Bash arguments'
  exit 126
fi
[[ "\$1" == "\$allowed_script" ]] || {
  print -u2 -- "rejected fake Bash script: \$1"
  exit 126
}
[[ "\$1" == /* && "\$1" != *'/../'* && "\$1" != */.. \
  && "\$1" != *'/./'* && "\$1" != */. ]] || exit 126
script_parent="\$(cd -P "\${1:h}" 2>/dev/null && pwd -P)" || exit 126
[[ "\$script_parent" == "\$fixture_root" || "\$script_parent" == "\$fixture_root"/* ]] || exit 126
resolve_existing_path() {
  local candidate="\$1"
  local candidate_dir
  local link_target

  while [[ -L "\$candidate" ]]; do
    candidate_dir="\$(cd -P "\${candidate:h}" 2>/dev/null && pwd -P)" || return 1
    link_target="\$(readlink "\$candidate")" || return 1
    if [[ "\$link_target" == /* ]]; then
      candidate="\$link_target"
    else
      candidate="\$candidate_dir/\$link_target"
    fi
  done
  candidate_dir="\$(cd -P "\${candidate:h}" 2>/dev/null && pwd -P)" || return 1
  print -r -- "\$candidate_dir/\${candidate:t}"
}
resolved_script="\$(resolve_existing_path "\$1")" || exit 126
[[ "\$resolved_script" == "\$fixture_root"/* && -f "\$resolved_script" ]] || exit 126
target_bash="$target_bash"
[[ "\$target_bash" == /* && "\${target_bash:t}" == bash && -x "\$target_bash" && ! -d "\$target_bash" ]] || exit 126
print -r -- "bash:\$1 --dry-run" >> "\$event_log"
exec "\$target_bash" "\$1" --dry-run
EOF
  cat > "$bin_dir/zsh" <<EOF
#!/bin/zsh
set -euo pipefail
print -r -- "zsh:\$*" >> "\$REMOVE_TEST_EVENT_LOG"
print -u2 -- 'rejected fake zsh dispatch'
exit 125
EOF
  chmod +x "$bin_dir/bash" "$bin_dir/zsh"

  REMOVE_TEST_ALLOWED_BASH_SCRIPT="$script_copy" REMOVE_TEST_EVENT_LOG="$event_log" \
    PATH="$bin_dir:/bin:/usr/bin:/usr/sbin:/sbin" \
    "$script_copy" --dry-run > "$repo/output.log" 2>&1 || exit_status=$?

  (( exit_status == 0 )) || {
    sed -n '1,120p' "$repo/output.log" >&2
    fail "expected direct Homebrew helper copy to run"
  }
  assert_contains "$event_log" "bash:$script_copy --dry-run"
  assert_not_contains "$event_log" 'zsh:'
  assert_output_contains "$repo/output.log" "DRY-RUN: Homebrew was not removed"

  : > "$event_log"
  exit_status=0
  REMOVE_TEST_ALLOWED_BASH_SCRIPT="$symlink_copy" REMOVE_TEST_EVENT_LOG="$event_log" \
    PATH="$bin_dir:/bin:/usr/bin:/usr/sbin:/sbin" \
    "$symlink_copy" --dry-run > "$repo/symlink-output.log" 2>&1 || exit_status=$?
  (( exit_status == 0 )) || {
    sed -n '1,120p' "$repo/symlink-output.log" >&2
    fail "expected direct Homebrew symlink to resolve its target directory"
  }
  assert_contains "$event_log" "bash:$symlink_copy --dry-run"
  assert_not_contains "$event_log" 'zsh:'
  assert_output_contains "$repo/symlink-output.log" "DRY-RUN: Homebrew was not removed"

  rm -rf "$repo"
}

run_nix_direct_shebang_matrix() {
  local expected_major
  local bash_bin
  local matrix_status=0

  for expected_major in 3 5; do
    if select_bash_for_major "$expected_major"; then
      bash_bin="$REPLY"
      test_nix_install_direct_copy_uses_bash_shebang "$bash_bin" || matrix_status=1
      (( matrix_status == 0 )) && emit_matrix_result "MATRIX_RESULT|os=$(matrix_os_name)|shell=bash${expected_major}|target=nix-direct-shebang|status=PASS|requirement=required|reason=$bash_bin"
    elif [[ "$expected_major" == 3 ]] && ! is_test_macos; then
      emit_not_applicable_skip 'nix-direct-shebang-bash3'
    else
      emit_required_bash_skip 'nix-direct-shebang' "$expected_major"
      matrix_status=1
    fi
  done
  (( matrix_status == 0 )) || fail "Nix direct shebang matrix failed"
}

run_managed_activation_failure_matrix() {
  local expected_major
  local target_bash
  local matrix_status=0

  for expected_major in 3 5; do
    if select_bash_for_major "$expected_major"; then
      target_bash="$REPLY"
      test_managed_update_propagates_nix_activation_failure "$target_bash" || matrix_status=1
      (( matrix_status == 0 )) && emit_matrix_result "MATRIX_RESULT|os=$(matrix_os_name)|shell=bash${expected_major}|target=managed-activation-failure|status=PASS|requirement=required|reason=$target_bash"
    elif [[ "$expected_major" == 3 ]] && ! is_test_macos; then
      emit_not_applicable_skip 'managed-activation-failure-bash3'
    else
      emit_required_bash_skip 'managed-activation-failure' "$expected_major"
      matrix_status=1
    fi
  done
  (( matrix_status == 0 )) || fail "managed activation failure matrix failed"
}

run_remove_shebang_matrix() {
  local expected_major
  local bash_bin
  local matrix_status=0

  for expected_major in 3 5; do
    if select_bash_for_major "$expected_major"; then
      bash_bin="$REPLY"
      test_remove_homebrew_direct_copy_uses_bash_shebang "$bash_bin" || matrix_status=1
      (( matrix_status == 0 )) && emit_matrix_result "MATRIX_RESULT|os=$(matrix_os_name)|shell=bash${expected_major}|target=remove-direct-shebang|status=PASS|requirement=required|reason=$bash_bin"
    elif [[ "$expected_major" == 3 ]] && ! is_test_macos; then
      emit_not_applicable_skip 'remove-direct-shebang-bash3'
    else
      emit_required_bash_skip 'remove-direct-shebang' "$expected_major"
      matrix_status=1
    fi
  done
  (( matrix_status == 0 )) || fail "Homebrew direct shebang matrix failed"
}

run_remove_output_matrix() {
  local expected_major
  local bash_bin
  local matrix_status=0

  for expected_major in 3 5; do
    if select_bash_for_major "$expected_major"; then
      bash_bin="$REPLY"
      test_remove_homebrew_dry_run_output_is_shell_neutral "$bash_bin" || matrix_status=1
      (( matrix_status == 0 )) && emit_matrix_result "MATRIX_RESULT|os=$(matrix_os_name)|shell=bash${expected_major}|target=remove-output|status=PASS|requirement=required|reason=$bash_bin"
    elif [[ "$expected_major" == 3 ]] && ! is_test_macos; then
      emit_not_applicable_skip 'remove-output-bash3'
    else
      emit_required_bash_skip 'remove-output' "$expected_major"
      matrix_status=1
    fi
  done
  (( matrix_status == 0 )) || fail "Homebrew output matrix failed"
}

run_nix_cycle2_zsh_cell() {
  local parser_bash="/bin/bash"
  local child_bash="/bin/bash"
  local cell_status=0

  if select_bash_for_major 3; then
    parser_bash="$REPLY"
  elif select_bash_for_major 5; then
    parser_bash="$REPLY"
  fi
  if select_bash_for_major 3; then
    child_bash="$REPLY"
  elif select_bash_for_major 5; then
    child_bash="$REPLY"
  fi
  test_nix_install_cycle2_parser_and_library_mode "$parser_bash" || cell_status=1
  if is_test_macos; then
    test_nix_install_cycle2_archives_backups_without_losing_pathnames "$TEST_ZSH_BIN" || cell_status=1
    test_nix_install_cycle2_splits_darwin_paths_without_losing_characters "$TEST_ZSH_BIN" || cell_status=1
  else
    emit_not_applicable_skip 'nix-cycle2-darwin-backup'
    emit_not_applicable_skip 'nix-cycle2-darwin-colon'
  fi
  test_nix_install_cycle2_uses_private_temp_lists_and_cleans_up "$TEST_ZSH_BIN" || cell_status=1
  test_nix_install_cycle2_cleans_private_lists_when_mv_fails "$TEST_ZSH_BIN" || cell_status=1
  test_nix_install_cycle2_dry_run_does_not_move_backups_or_use_sudo "$TEST_ZSH_BIN" || cell_status=1
  test_nix_install_cycle2_uses_date_for_default_archive_epoch "$TEST_ZSH_BIN" || cell_status=1
  test_nix_install_rejects_invalid_epoch_and_date_output "$TEST_ZSH_BIN" || cell_status=1
  test_nix_install_cycle2_fails_closed_when_find_producer_fails "$TEST_ZSH_BIN" || cell_status=1
  test_nix_install_cycle2_fails_closed_when_xdg_find_fails "$TEST_ZSH_BIN" || cell_status=1
  test_nix_install_uninstall_homebrew_uses_bash_child "$TEST_ZSH_BIN" "$child_bash" || cell_status=1
  (( cell_status == 0 )) && emit_matrix_result "MATRIX_RESULT|os=$(matrix_os_name)|shell=zsh|target=nix-cycle2|status=PASS|requirement=required|reason=$TEST_ZSH_BIN"
  return "$cell_status"
}

run_nix_cycle2_bash_cell() {
  local expected_major="$1"
  local bash_bin
  local cell_status=0

  if ! select_bash_for_major "$expected_major"; then
    if [[ "$expected_major" == "3" ]] && ! is_test_macos; then
      emit_not_applicable_skip 'nix-cycle2-bash3'
      return 0
    fi
    emit_required_bash_skip 'nix-cycle2' "$expected_major"
    return 1
  fi
  bash_bin="$REPLY"

  test_nix_install_cycle2_parser_and_library_mode "$bash_bin" 0 || cell_status=1
  test_nix_install_cycle2_uses_private_temp_lists_and_cleans_up "$bash_bin" || cell_status=1
  test_nix_install_cycle2_cleans_private_lists_when_mv_fails "$bash_bin" || cell_status=1
  test_nix_install_cycle2_dry_run_does_not_move_backups_or_use_sudo "$bash_bin" || cell_status=1
  test_nix_install_cycle2_uses_date_for_default_archive_epoch "$bash_bin" || cell_status=1
  test_nix_install_rejects_invalid_epoch_and_date_output "$bash_bin" || cell_status=1
  test_nix_install_cycle2_fails_closed_when_find_producer_fails "$bash_bin" || cell_status=1
  test_nix_install_cycle2_fails_closed_when_xdg_find_fails "$bash_bin" || cell_status=1
  test_nix_install_uninstall_homebrew_uses_bash_child "$bash_bin" "$bash_bin" || cell_status=1
  if is_test_macos; then
    test_nix_install_cycle2_archives_backups_without_losing_pathnames "$bash_bin" || cell_status=1
    test_nix_install_cycle2_splits_darwin_paths_without_losing_characters "$bash_bin" || cell_status=1
  else
    emit_not_applicable_skip "nix-cycle2-bash${expected_major}-darwin"
  fi
  (( cell_status == 0 )) && emit_matrix_result "MATRIX_RESULT|os=$(matrix_os_name)|shell=bash${expected_major}|target=nix-cycle2|status=PASS|requirement=required|reason=$bash_bin"
  return "$cell_status"
}

run_nix_cycle2_matrix() {
  local matrix_status=0

  run_nix_cycle2_zsh_cell || matrix_status=1
  run_nix_cycle2_bash_cell 3 || matrix_status=1
  run_nix_cycle2_bash_cell 5 || matrix_status=1
  (( matrix_status == 0 )) || fail "nix-cycle2 matrix failed"
}

collect_matrix_results() {
  local log_dir="${MATRIX_RESULT_LOG_DIR:-}"
  local manifest="${MATRIX_EXPECTED_FILE:-}"
  local -a log_files

  if [[ -z "$log_dir" || ! -d "$log_dir" ]]; then
    print -u2 -- 'FAIL: matrix collector requires MATRIX_RESULT_LOG_DIR'
    return 1
  fi
  if [[ -z "$manifest" || ! -f "$manifest" ]]; then
    print -u2 -- 'FAIL: matrix collector requires MATRIX_EXPECTED_FILE'
    return 1
  fi

  setopt local_options null_glob
  if [[ -f "$log_dir/matrix-results.log" ]]; then
    log_files=("$log_dir/matrix-results.log")
  else
    log_files=("$log_dir"/*.log(N))
  fi
  if (( ${#log_files[@]} == 0 )); then
    print -u2 -- "FAIL: matrix collector found no .log files in $log_dir"
    return 1
  fi

  awk -F '|' '
    function fail(message) {
      print "FAIL: matrix collector " message > "/dev/stderr"
      invalid = 1
    }
    FILENAME == ARGV[1] {
      if ($0 == "" || $0 ~ /^#/) next
      if (NF != 4 || $1 == "" || $2 == "" || $3 == "" || $4 !~ /^(required|not-applicable)$/) {
        fail("rejected manifest row: " $0)
        next
      }
      key = $1 "|" $2 "|" $3
      if (key in expected) fail("found duplicate manifest key: " key)
      expected[key] = $4
      expected_count++
      next
    }
    $0 !~ /^MATRIX_RESULT\|/ { next }
    {
      if (NF != 7 || $1 != "MATRIX_RESULT" || $2 !~ /^os=/ || $3 !~ /^shell=/ || \
        $4 !~ /^target=/ || $5 !~ /^status=/ || $6 !~ /^requirement=/ || $7 !~ /^reason=/) {
        fail("rejected result row: " $0)
        next
      }
      os = substr($2, 4)
      shell = substr($3, 7)
      target = substr($4, 8)
      status = substr($5, 8)
      requirement = substr($6, 13)
      reason = substr($7, 8)
      key = os "|" shell "|" target
      if (os == "" || shell == "" || target == "" || reason == "") fail("rejected empty result field: " $0)
      else if (!(key in expected)) fail("found unexpected result key: " key)
      else if (expected[key] != requirement) fail("found requirement mismatch for " key)
      else if (key in seen) fail("found duplicate result key: " key)
      else if (requirement == "required" && status != "PASS") fail("required cell is not PASS: " key)
      else if (requirement == "not-applicable" && status != "SKIP") fail("not-applicable cell is not SKIP: " key)
      else if (requirement !~ /^(required|not-applicable)$/) fail("found unknown requirement: " requirement)
      else seen[key] = 1
      next
    }
    END {
      if (expected_count == 0) fail("requires at least one manifest row")
      for (key in expected) if (!(key in seen)) fail("missing result key: " key)
      exit invalid
    }
  ' "$manifest" "${log_files[@]}"
}

run_matrix_collector() {
  local repo="$1"
  local log_dir="$2"
  local manifest="$3"
  local label="$4"
  local expected_status="$5"
  local collector_status=0

  MATRIX_RESULT_LOG_DIR="$log_dir" MATRIX_EXPECTED_FILE="$manifest" \
    collect_matrix_results > "$repo/$label.log" 2>&1 || collector_status=$?
  (( collector_status == expected_status )) || {
    sed -n '1,120p' "$repo/$label.log" >&2
    fail "matrix collector returned $collector_status for $label, expected $expected_status"
  }
}

test_matrix_collector_validates_required_and_not_applicable_cells() {
  local repo
  local log_dir
  local manifest
  local empty_manifest
  local log_file

  make_temp_dir
  repo="$REPLY"
  log_dir="$repo/results"
  manifest="$repo/expected.matrix"
  empty_manifest="$repo/empty.matrix"
  log_file="$log_dir/matrix-results.log"
  mkdir -p "$log_dir"
  : > "$empty_manifest"
  : > "$log_file"
  run_matrix_collector "$repo" "$log_dir" "$empty_manifest" empty-manifest 1

  print -r -- 'linux|bash5|collector-pass|required' > "$manifest"
  print -r -- 'linux|bash3|collector-skip|not-applicable' >> "$manifest"
  print -r -- 'MATRIX_RESULT|os=linux|shell=bash5|target=collector-pass|status=PASS|requirement=required|reason=fixture' > "$log_file"
  print -r -- 'MATRIX_RESULT|os=linux|shell=bash3|target=collector-skip|status=SKIP|requirement=not-applicable|reason=macos-only' >> "$log_file"

  run_matrix_collector "$repo" "$log_dir" "$manifest" valid 0

  print -r -- 'MATRIX_RESULT|os=linux|shell=bash5|target=collector-pass|status=PASS|requirement=required|reason=duplicate' >> "$log_file"
  run_matrix_collector "$repo" "$log_dir" "$manifest" duplicate-result 1

  print -r -- 'MATRIX_RESULT|os=linux|shell=bash5|target=collector-pass|status=SKIP|requirement=required|reason=unavailable' > "$log_file"
  print -r -- 'MATRIX_RESULT|os=linux|shell=bash3|target=collector-skip|status=SKIP|requirement=not-applicable|reason=macos-only' >> "$log_file"
  run_matrix_collector "$repo" "$log_dir" "$manifest" required-skip 1

  print -r -- 'MATRIX_RESULT|os=linux|shell=bash5|target=collector-pass|status=PASS|requirement=required|reason=fixture' > "$log_file"
  print -r -- 'MATRIX_RESULT|os=linux|shell=unknown|target=collector-unknown|status=PASS|requirement=required|reason=fixture' >> "$log_file"
  run_matrix_collector "$repo" "$log_dir" "$manifest" unknown-and-missing 1

  rm -rf "$repo"
}

main() {
  local path_matrix_status=0

  if [[ "${1:-}" == "--focus" ]]; then
    case "${2:-}" in
      all-bash)
        run_managed_all_bash_matrix
        ;;
      all-failure)
        run_managed_activation_failure_matrix
        ;;
      nix-cycle2)
        run_nix_cycle2_matrix
        ;;
      nix-private-temp)
        test_nix_install_cycle2_uses_private_temp_lists_and_cleans_up
        ;;
      nix-invalid-epoch)
        test_nix_install_rejects_invalid_epoch_and_date_output
        ;;
      nix-path-resolution)
        test_nix_install_path_resolution_ignores_spoofed_bash_version
        for expected_major in 3 5; do
          if select_bash_for_major "$expected_major"; then
            test_nix_install_rejects_spoofed_bash_source_and_ostype "$REPLY"
          elif [[ "$expected_major" == 3 ]] && ! is_test_macos; then
            emit_not_applicable_skip "nix-path-resolution-bash${expected_major}"
          else
            emit_required_bash_skip "nix-path-resolution" "$expected_major"
            path_matrix_status=1
          fi
        done
        (( path_matrix_status == 0 )) || fail "Nix path-resolution matrix failed"
        ;;
      direct-shebang)
        run_nix_direct_shebang_matrix
        ;;
      nix-colon)
        test_nix_install_script_backs_up_existing_etc_shell_rc_before_darwin_switch
        ;;
      nix-homebrew-child)
        test_nix_install_uninstall_homebrew_uses_bash_child
        ;;
      sudo-fixture-guards)
        test_nix_install_script_defaults_to_cli_profile_on_macos
        test_nix_install_script_uses_nix_run_impure_after_subcommand_when_darwin_rebuild_is_missing
        test_nix_install_script_backs_up_existing_sudo_local_before_darwin_switch
        test_nix_install_script_backs_up_existing_etc_shell_rc_before_darwin_switch
        test_nix_install_script_archives_existing_home_manager_backups_before_switch
        test_nix_install_script_handles_dirty_worktree_without_hanging
        test_nix_install_script_uses_git_aware_flake_ref_for_tracked_worktree
        ;;
      remove-output)
        run_remove_output_matrix
        ;;
      remove-curl-failure)
        test_remove_homebrew_apply_propagates_curl_failure
        test_remove_homebrew_apply_executes_downloaded_body_with_noninteractive
        ;;
      remove-shebang)
        run_remove_shebang_matrix
        ;;
      matrix-collect)
        if [[ -n "${MATRIX_EXPECTED_FILE:-}" ]]; then
          collect_matrix_results
        else
          test_matrix_collector_validates_required_and_not_applicable_cells
        fi
        ;;
      *)
        fail "unknown focus: ${2:-}"
        ;;
    esac
    return 0
  fi

  test_brewfile_migration_writes_nix_lists_and_unmapped_report
  test_brewfile_migration_dry_run_does_not_write_outputs
  test_repository_migration_moves_available_formulae_and_gui_apps_to_nix
  test_waza_is_integrated_for_agent_skill_evaluations
  test_waza_cli_agent_eval_script_is_guarded_and_can_dry_run
  test_waza_cli_agent_eval_script_preserves_cli_failure_status
  test_waza_cli_agent_eval_script_grades_successful_cli_output
  test_waza_eval_suites_cover_all_regular_agent_skills
  run_managed_all_bash_matrix
  run_managed_activation_failure_matrix
  run_nix_cycle2_matrix
  run_nix_direct_shebang_matrix
  run_remove_output_matrix
  test_remove_homebrew_apply_propagates_curl_failure
  test_remove_homebrew_apply_executes_downloaded_body_with_noninteractive
  run_remove_shebang_matrix
  test_agent_skills_use_supported_discovery_paths
  test_flake_exposes_nix_darwin_and_home_manager_profiles
  test_home_manager_and_darwin_modules_define_profiles_without_homebrew
  test_nix_install_script_switches_nix_darwin_or_home_manager
  test_nix_install_script_defaults_to_cli_profile_on_macos
  test_nix_install_script_uses_nix_run_impure_after_subcommand_when_darwin_rebuild_is_missing
  test_nix_install_script_backs_up_existing_sudo_local_before_darwin_switch
  test_nix_install_script_backs_up_existing_etc_shell_rc_before_darwin_switch
  test_nix_install_script_archives_existing_home_manager_backups_before_switch
  test_nix_install_script_handles_dirty_worktree_without_hanging
  test_nix_install_script_uses_git_aware_flake_ref_for_tracked_worktree
  test_rootless_nix_install_script_supports_no_sudo_linux
  test_nix_portable_install_script_supports_no_sudo_nix_main_path
  test_remove_homebrew_script_is_explicit_and_dry_run_first
  test_cleanup_package_caches_script_supports_safe_nix_and_homebrew_cleanup
  test_install_homebrew_script_supports_required_profiles
  test_main_mise_shell_and_hooks_use_nix_as_the_setup_path
  test_main_script_runs_homebrew_before_nix_setup
  test_main_script_can_skip_mas_apps_for_full_profile
  test_main_script_uses_cli_profile_when_requested
  test_main_script_bootstraps_nix_on_macos_when_missing
  test_main_script_keeps_sudo_authentication_alive_on_macos
  test_main_script_installs_rosetta_on_apple_silicon_full_profile
  test_install_mas_apps_script_continues_after_individual_failures
  test_main_script_reports_nix_portable_when_nix_missing_on_linux
  test_main_script_applies_chezmoi_instead_of_copying_legacy_dotfiles
  test_apply_updates_applies_chezmoi_and_refreshes_agent_and_hooks
  test_setup_git_hooks_generates_executable_hooks_with_valid_zsh_shebang
  test_ai_cli_tools_are_managed_by_mise
  test_managed_update_script_skips_gui_profile_on_macos_unless_requested
  test_managed_update_script_includes_gui_profile_when_requested
  test_managed_update_script_upgrades_declared_homebrew_fallbacks_when_gui_requested
  test_managed_update_script_skips_auto_updating_homebrew_casks
  test_managed_update_script_upgrades_only_non_auto_updating_homebrew_casks
  test_bash_templates_support_dynamic_shell_setup
  test_managed_update_script_updates_mise_and_nix
  echo "nix migration tests passed"
}

main "$@"
