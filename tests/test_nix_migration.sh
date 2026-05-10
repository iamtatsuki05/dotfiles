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
readonly UPDATE_MANAGED_VERSIONS_SCRIPT="$REPO_ROOT/scripts/update_managed_versions.sh"
readonly APPLY_UPDATES_SCRIPT="$REPO_ROOT/scripts/apply_updates.sh"
readonly SETUP_GIT_HOOKS_SCRIPT="$REPO_ROOT/scripts/setup_git_hooks.sh"
readonly MAIN_SCRIPT="$REPO_ROOT/main.sh"
readonly HOMEBREW_LIB="$REPO_ROOT/scripts/lib/homebrew.sh"
readonly HOMEBREW_FALLBACK_LIB="$REPO_ROOT/scripts/lib/homebrew_fallback.sh"
readonly RUNTIME_LIB="$REPO_ROOT/scripts/lib/runtime.sh"
readonly FLAKE_FILE="$REPO_ROOT/flake.nix"
readonly BASHRC_TEMPLATE_FILE="$REPO_ROOT/config/shell/bashrc.tmpl"
readonly BASH_PROFILE_TEMPLATE_FILE="$REPO_ROOT/config/shell/bash_profile.tmpl"
readonly SHELL_COMMON_TEMPLATE_FILE="$REPO_ROOT/config/shell/dotfiles-shell-common.tmpl"
readonly MISE_CONFIG="$REPO_ROOT/config/mise/config.toml"
readonly WAZA_AGENT_EVAL_FILE="$REPO_ROOT/dotfiles/.agent/evals/markdown-docs/eval.yaml"
readonly WAZA_MARKDOWN_DOCS_MODEL_EVAL_FILE="$REPO_ROOT/dotfiles/.agent/evals/markdown-docs/model.yaml"
readonly WAZA_AUTO_DEBUGGER_EVAL_FILE="$REPO_ROOT/dotfiles/.agent/evals/auto-debugger/eval.yaml"
readonly WAZA_AUTO_DEBUGGER_MODEL_EVAL_FILE="$REPO_ROOT/dotfiles/.agent/evals/auto-debugger/model.yaml"
readonly WAZA_PR_CODE_REVIEW_EVAL_FILE="$REPO_ROOT/dotfiles/.agent/evals/pr-code-review/eval.yaml"
readonly WAZA_PR_CODE_REVIEW_MODEL_EVAL_FILE="$REPO_ROOT/dotfiles/.agent/evals/pr-code-review/model.yaml"
readonly WAZA_SECURITY_CHECK_EVAL_FILE="$REPO_ROOT/dotfiles/.agent/evals/security-check/eval.yaml"
readonly WAZA_SECURITY_CHECK_MODEL_EVAL_FILE="$REPO_ROOT/dotfiles/.agent/evals/security-check/model.yaml"
readonly WAZA_ALL_EVAL_SCRIPT="$REPO_ROOT/scripts/waza_eval_all.sh"
readonly WAZA_MODEL_EVAL_SCRIPT="$REPO_ROOT/scripts/waza_eval_model.sh"
readonly WAZA_CLI_AGENT_EVAL_SCRIPT="$REPO_ROOT/scripts/waza_eval_cli_agent.sh"
readonly WAZA_EVAL_ROOT="$REPO_ROOT/dotfiles/.agent/evals"
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

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

make_temp_dir() {
  local candidate
  local attempts=0

  while (( attempts < 10 )); do
    candidate="${TMPDIR:-/tmp}/dotfiles-test-$$-$RANDOM-$RANDOM"
    if mkdir "$candidate" 2>/dev/null; then
      REPLY="$candidate"
      return 0
    fi
    attempts=$((attempts + 1))
  done

  fail "failed to create temporary directory"
}

make_temp_file() {
  make_temp_dir
  rmdir "$REPLY"
  : > "$REPLY"
}

copy_script_libs() {
  local repo="$1"

  mkdir -p "$repo/scripts/lib"
  cp "$REPO_ROOT/scripts/lib/setup_profile.sh" "$repo/scripts/lib/setup_profile.sh"
  cp "$HOMEBREW_LIB" "$repo/scripts/lib/homebrew.sh"
  cp "$HOMEBREW_FALLBACK_LIB" "$repo/scripts/lib/homebrew_fallback.sh"
  cp "$RUNTIME_LIB" "$repo/scripts/lib/runtime.sh"
}

assert_file() {
  local file_path="$1"
  [[ -f "$file_path" ]] || fail "expected file: $file_path"
}

assert_executable() {
  local file_path="$1"
  [[ -x "$file_path" ]] || fail "expected executable file: $file_path"
}

assert_not_exists() {
  local target_path="$1"
  [[ ! -e "$target_path" ]] || fail "expected path not to exist: $target_path"
}

assert_contains() {
  local file_path="$1"
  local expected="$2"

  assert_file "$file_path"
  grep -Fq -- "$expected" "$file_path" || fail "expected $file_path to contain: $expected"
}

assert_not_contains() {
  local file_path="$1"
  local unexpected="$2"

  assert_file "$file_path"
  ! grep -Fq -- "$unexpected" "$file_path" || fail "expected $file_path not to contain: $unexpected"
}

assert_output_contains() {
  local output_file="$1"
  local expected="$2"

  grep -Fq -- "$expected" "$output_file" || fail "expected output to contain: $expected"
}

is_test_macos() {
  [[ "$OSTYPE" == darwin* ]]
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
brew	gemini-cli	gemini-cli
brew	hermes-agent	pipx:git+https://github.com/NousResearch/hermes-agent.git
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
raycast	raycast	macos
EOF

  cat > "$repo/config/nix/mas-to-nix.tsv" <<'EOF'
# mas app name	app store id	nix	nix scope
Bitwarden	1352778147	bitwarden-desktop	common
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
brew "gemini-cli"
brew "hermes-agent"
brew "opencode"
brew "private-tool"
cask "slack"
cask "alacritty"
cask "claude-code@latest"
cask "codex"
cask "cursor-cli"
cask "ghostty"
cask "raycast"
cask "private-app"
mas "Bitwarden", id: 1352778147
mas "Affinity Photo", id: 824183456
mas "Xcode", id: 497799835
vscode "example.extension"
uv "claude-monitor"
EOF
}

test_brewfile_migration_writes_nix_lists_and_unmapped_report() {
  local repo
  make_temp_dir
  repo="$REPLY"
  create_fixture_repo "$repo"

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
  assert_not_contains "$repo/config/nix/package-names.nix" '"hermes-agent"'
  assert_not_contains "$repo/config/nix/package-names.nix" '"opencode"'
  assert_contains "$repo/config/nix/gui-common-package-names.nix" '"slack"'
  assert_contains "$repo/config/nix/gui-common-package-names.nix" '"alacritty"'
  assert_contains "$repo/config/nix/gui-common-package-names.nix" '"bitwarden-desktop"'
  assert_not_contains "$repo/config/nix/gui-common-package-names.nix" '"copilot-cli"'
  assert_not_contains "$repo/config/nix/gui-common-package-names.nix" '"cursor-cli"'
  assert_not_contains "$repo/config/nix/gui-common-package-names.nix" '"claude-code"'
  assert_not_contains "$repo/config/nix/gui-common-package-names.nix" '"codex"'
  assert_contains "$repo/config/nix/gui-linux-package-names.nix" '"ghostty"'
  assert_contains "$repo/config/nix/gui-macos-package-names.nix" '"raycast"'
  assert_contains "$repo/config/nix/migrated-brew-formulae.txt" "gnu-sed"
  assert_not_contains "$repo/config/nix/migrated-brew-formulae.txt" "gemini-cli"
  assert_not_contains "$repo/config/nix/migrated-brew-formulae.txt" "claude-code"
  assert_not_contains "$repo/config/nix/migrated-brew-formulae.txt" "codex"
  assert_contains "$repo/config/nix/migrated-brew-casks.txt" "slack"
  assert_not_contains "$repo/config/nix/migrated-brew-casks.txt" "claude-code@latest"
  assert_not_contains "$repo/config/nix/migrated-brew-casks.txt" "codex"
  assert_not_contains "$repo/config/nix/migrated-brew-casks.txt" "cursor-cli"
  assert_contains "$repo/config/nix/unmapped-homebrew.tsv" $'brew	claude-code	managed-by-mise:claude-code'
  assert_contains "$repo/config/nix/unmapped-homebrew.tsv" $'brew	codex	managed-by-mise:codex'
  assert_contains "$repo/config/nix/unmapped-homebrew.tsv" $'brew	gemini-cli	managed-by-mise:gemini-cli'
  assert_contains "$repo/config/nix/unmapped-homebrew.tsv" $'brew	hermes-agent	managed-by-mise:pipx:git+https://github.com/NousResearch/hermes-agent.git'
  assert_contains "$repo/config/nix/unmapped-homebrew.tsv" $'brew	opencode	managed-by-mise:opencode'
  assert_contains "$repo/config/nix/unmapped-homebrew.tsv" $'cask	claude-code@latest	managed-by-mise:claude-code'
  assert_contains "$repo/config/nix/unmapped-homebrew.tsv" $'cask	codex	managed-by-mise:codex'
  assert_contains "$repo/config/nix/unmapped-homebrew.tsv" $'cask	cursor-cli	managed-by-mise:cursor-agent'
  assert_contains "$repo/config/nix/unmapped-homebrew.tsv" $'brew	private-tool'
  assert_contains "$repo/config/nix/unmapped-homebrew.tsv" $'cask	private-app'
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
  assert_contains "$repo/config/nix/homebrew-fallback.nix" '"affinity-photo"'
  assert_contains "$repo/config/nix/homebrew-fallback.nix" '"example.extension"'
  assert_contains "$repo/config/nix/homebrew-fallback.nix" '"claude-monitor"'
  assert_contains "$repo/config/nix/mas-apps.nix" '"Xcode" = 497799835;'
  assert_not_contains "$repo/config/nix/mas-apps.nix" 'Bitwarden'
  assert_not_contains "$repo/config/nix/mas-apps.nix" 'Affinity Photo'
  assert_contains "$repo/config/nix/migrated-mas-apps.tsv" $'Bitwarden	nix	bitwarden-desktop'
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
    "emacs.pkgs.cask"
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
    "discord"
    "google-chrome"
    "slack"
    "vscode"
    "zed-editor"
  )
  local macos_gui_attrs=(
    "alt-tab-macos"
    "betterdisplay"
    "daisydisk"
    "iterm2"
    "raycast"
    "rectangle-pro"
  )
  local linux_gui_attrs=(
    "android-studio"
    "freefilesync"
    "ghostty"
    "pcloud"
    "vlc"
  )
  local migrated_casks=(
    "1password-cli"
    "alacritty"
    "discord"
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
  assert_contains "$UNMAPPED_HOMEBREW_FILE" $'cask	yoink'
  assert_contains "$UNMAPPED_HOMEBREW_FILE" $'vscode	adpyke.codesnap'
  assert_contains "$MIGRATED_FORMULAE_FILE" "mise"
  assert_not_contains "$MIGRATED_FORMULAE_FILE" "gemini-cli"
  assert_not_contains "$MIGRATED_CASKS_FILE" "claude-code@latest"
  assert_not_contains "$MIGRATED_CASKS_FILE" "codex"
  assert_contains "$UNMAPPED_HOMEBREW_FILE" $'brew	gemini-cli	managed-by-mise:gemini-cli'
  assert_contains "$UNMAPPED_HOMEBREW_FILE" $'cask	claude-code@latest	managed-by-mise:claude-code'
  assert_contains "$UNMAPPED_HOMEBREW_FILE" $'cask	codex	managed-by-mise:codex'
  assert_contains "$UNMAPPED_HOMEBREW_FILE" $'brew	claude-code	managed-by-mise:claude-code'
  assert_contains "$UNMAPPED_HOMEBREW_FILE" $'brew	codex	managed-by-mise:codex'
  assert_contains "$MIGRATED_MAS_APPS_FILE" $'Alfred	nix	alfred'
  assert_contains "$MIGRATED_MAS_APPS_FILE" $'Affinity Photo	brew	affinity-photo'
  assert_contains "$HOMEBREW_FALLBACK_FILE" 'taps = ['
  assert_contains "$HOMEBREW_FALLBACK_FILE" '"cloudflare/cloudflare"'
  assert_contains "$HOMEBREW_FALLBACK_FILE" 'casks = ['
  assert_contains "$HOMEBREW_FALLBACK_FILE" '"affinity"'
  assert_contains "$HOMEBREW_FALLBACK_FILE" '"affinity-photo"'
  assert_contains "$HOMEBREW_FALLBACK_FILE" '"ghostty"'
  assert_contains "$HOMEBREW_FALLBACK_FILE" 'vscode = ['
  assert_contains "$HOMEBREW_FALLBACK_FILE" '"adpyke.codesnap"'
  assert_contains "$HOMEBREW_FALLBACK_FILE" 'unsupportedUvPackages = ['
  assert_contains "$HOMEBREW_FALLBACK_FILE" '"claude-monitor"'
  assert_file "$MAS_APPS_FILE"
  assert_contains "$MAS_APPS_FILE" '"Xcode" = 497799835;'
  assert_not_contains "$MAS_APPS_FILE" '"Alfred"'
  assert_not_contains "$MAS_APPS_FILE" '"Bitwarden"'
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
  assert_contains "$MISE_CONFIG" '[tasks.waza-eval-codex]'
  assert_contains "$MISE_CONFIG" '[tasks.waza-eval-claude]'
  assert_contains "$MISE_CONFIG" '[tasks.waza-eval-gemini]'
  assert_contains "$MISE_CONFIG" '[tasks.waza-eval-copilot]'
  assert_contains "$MISE_CONFIG" '[tasks.waza-eval-devin]'
  assert_contains "$MISE_CONFIG" '[tasks.waza-eval-cursor]'
  assert_contains "$MISE_CONFIG" '[tasks.waza-eval-opencode]'
  assert_contains "$MISE_CONFIG" '[tasks.waza-eval-hermes]'
  assert_contains "$MISE_CONFIG" '[tasks.waza-eval-openclaw]'
  assert_contains "$MISE_CONFIG" '[tasks.waza-eval-cli-agents]'
  assert_contains "$MISE_CONFIG" '[tasks.waza-dashboard]'
  assert_contains "$MISE_CONFIG" 'nix run path:.#waza -- run'
  assert_contains "$WAZA_AGENT_EVAL_FILE" 'markdown-docs-eval'
  assert_contains "$WAZA_AGENT_EVAL_FILE" 'executor: mock'
  assert_contains "$WAZA_AUTO_DEBUGGER_EVAL_FILE" 'auto-debugger-eval'
  assert_contains "$WAZA_MARKDOWN_DOCS_MODEL_EVAL_FILE" 'duplicated_headings_detected'
  assert_contains "$WAZA_AUTO_DEBUGGER_MODEL_EVAL_FILE" 'string_concatenation_detected'
  assert_contains "$WAZA_PR_CODE_REVIEW_EVAL_FILE" 'pr-code-review-eval'
  assert_contains "$WAZA_SECURITY_CHECK_EVAL_FILE" 'security-check-eval'
  assert_contains "$WAZA_PR_CODE_REVIEW_MODEL_EVAL_FILE" 'executor: copilot-sdk'
  assert_contains "$WAZA_PR_CODE_REVIEW_MODEL_EVAL_FILE" 'authorization_bypass_detected'
  assert_contains "$WAZA_SECURITY_CHECK_MODEL_EVAL_FILE" 'executor: copilot-sdk'
  assert_contains "$WAZA_SECURITY_CHECK_MODEL_EVAL_FILE" 'sql_injection_detected'
  assert_contains "$WAZA_MODEL_EVAL_SCRIPT" 'Waza model-backed evals require model credentials'
  assert_contains "$WAZA_ALL_EVAL_SCRIPT" 'if [[ -d "$context_dir" ]]'
  assert_contains "$WAZA_MODEL_EVAL_SCRIPT" 'if [[ -d "$context_dir" ]]'
  assert_executable "$WAZA_CLI_AGENT_EVAL_SCRIPT"
  assert_contains "$WAZA_CLI_AGENT_EVAL_SCRIPT" 'CLI agent evals require explicit --allow'
  assert_contains "$WAZA_CLI_AGENT_EVAL_SCRIPT" 'codex exec -C'
  assert_contains "$WAZA_CLI_AGENT_EVAL_SCRIPT" 'claude -p'
  assert_contains "$WAZA_CLI_AGENT_EVAL_SCRIPT" 'gemini -p'
  assert_contains "$WAZA_CLI_AGENT_EVAL_SCRIPT" 'copilot'
  assert_contains "$WAZA_CLI_AGENT_EVAL_SCRIPT" 'devin'
  assert_contains "$WAZA_CLI_AGENT_EVAL_SCRIPT" 'cursor-agent'
  assert_contains "$WAZA_CLI_AGENT_EVAL_SCRIPT" 'opencode run'
  assert_contains "$WAZA_CLI_AGENT_EVAL_SCRIPT" 'hermes'
  assert_contains "$WAZA_CLI_AGENT_EVAL_SCRIPT" 'openclaw agent'
  assert_contains "$WAZA_CLI_AGENT_EVAL_SCRIPT" 'run_direct_or_mise'
  assert_contains "$WAZA_CLI_AGENT_EVAL_SCRIPT" '.waza-results/cli-agents'
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

  "$TEST_ZSH_BIN" "$WAZA_CLI_AGENT_EVAL_SCRIPT" codex --dry-run >"$output"
  assert_output_contains "$output" "DRY-RUN codex"
  assert_output_contains "$output" "dotfiles/.agent/evals/markdown-docs/model.yaml"
  assert_output_contains "$output" "tasks/restructure-guide.yaml"

  "$TEST_ZSH_BIN" "$WAZA_CLI_AGENT_EVAL_SCRIPT" claude --dry-run --suite dotfiles/.agent/evals/security-check/model.yaml >"$output"
  assert_output_contains "$output" "DRY-RUN claude"
  assert_output_contains "$output" "dotfiles/.agent/evals/security-check/model.yaml"
  assert_output_contains "$output" "tasks/review-flask-handler.yaml"

  "$TEST_ZSH_BIN" "$WAZA_CLI_AGENT_EVAL_SCRIPT" gemini --dry-run --suite dotfiles/.agent/evals/auto-debugger/model.yaml >"$output"
  assert_output_contains "$output" "DRY-RUN gemini"
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
  assert_output_contains "$output" "DRY-RUN copilot"
  assert_output_contains "$output" "DRY-RUN hermes"
  assert_output_contains "$output" "DRY-RUN openclaw"

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
    alphaxiv-paper-lookup
    api-design
    auto-debugger
    ci-cd
    claude-code
    codex
    database-dev
    empirical-prompt-tuning
    gemini
    go-dev
    goal-prompt-builder
    gws
    magika
    markdown-docs
    markitdown
    pr-code-review
    prompt-tuner
    python-dev
    retrospective-codify
    security-check
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
    superpowers-dispatching-parallel-agents "superpowers:dispatching-parallel-agents"
    superpowers-test-driven-development "superpowers:test-driven-development"
    superpowers-writing-skills "superpowers:writing-skills"
  )

  for eval_dir skill in "${(@kv)superpower_eval_dirs}"; do
    eval_file="$WAZA_EVAL_ROOT/$eval_dir/eval.yaml"
    model_file="$WAZA_EVAL_ROOT/$eval_dir/model.yaml"
    assert_file "$REPO_ROOT/dotfiles/.agent/skills/superpowers/${eval_dir#superpowers-}/SKILL.md"
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

test_flake_exposes_nix_darwin_and_home_manager_profiles() {
  assert_contains "$FLAKE_FILE" 'nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable"'
  assert_contains "$FLAKE_FILE" 'url = "github:nix-darwin/nix-darwin"'
  assert_contains "$FLAKE_FILE" 'url = "github:nix-community/home-manager"'
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
  assert_contains "$HOME_MANAGER_MODULE" 'targets.darwin.copyApps.enable = false'
  assert_contains "$HOME_MANAGER_MODULE" 'targets.darwin.linkApps.enable = false'
  assert_contains "$HOME_MANAGER_MODULE" 'programs.home-manager.enable = true'
  assert_contains "$HOME_MANAGER_MODULE" './packages.nix'
  assert_contains "$HOME_MANAGER_MODULE" './zsh.nix'
  assert_contains "$HOME_MANAGER_MODULE" './neovim.nix'
  assert_contains "$HOME_MANAGER_MODULE" './auto-update.nix'
  assert_contains "$HOME_MANAGER_MODULE" './session.nix'

  assert_contains "$HOME_MANAGER_PACKAGES_MODULE" 'home.packages'
  assert_contains "$HOME_MANAGER_PACKAGES_MODULE" '!pkgs.stdenv.hostPlatform.isDarwin'
  assert_contains "$HOME_MANAGER_PACKAGES_MODULE" 'homeManagerProvidedPackageNames'
  assert_contains "$HOME_MANAGER_PACKAGES_MODULE" 'lib.getName pkg'

  assert_contains "$HOME_MANAGER_ZSH_MODULE" 'programs.zsh.enable = true'
  assert_contains "$HOME_MANAGER_ZSH_MODULE" 'programs.zsh.completionInit'
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
  assert_contains "$DARWIN_BASE_MODULE" 'enableGuiApps'
  assert_contains "$DARWIN_BASE_MODULE" 'import ../gui-packages.nix'
  assert_contains "$DARWIN_BASE_MODULE" 'enableGuiApps && !pkgs.stdenv.hostPlatform.isDarwin'
  assert_contains "$DARWIN_BASE_MODULE" 'users.users.${username}.home'
  assert_not_contains "$DARWIN_BASE_MODULE" 'nix.settings'
  assert_not_contains "$DARWIN_BASE_MODULE" 'nix.optimise'

  assert_contains "$DARWIN_DEFAULTS_MODULE" 'security.pam.services.sudo_local = {'
  assert_contains "$DARWIN_DEFAULTS_MODULE" 'touchIdAuth = true'
  assert_contains "$DARWIN_DEFAULTS_MODULE" 'InitialKeyRepeat = 12'
  assert_contains "$DARWIN_DEFAULTS_MODULE" 'KeyRepeat = 1'
  assert_contains "$DARWIN_DEFAULTS_MODULE" 'screenshotsDirectory = "${homeDirectory}/SS"'
  assert_contains "$DARWIN_DEFAULTS_MODULE" 'system.defaults.screencapture = {'
  assert_contains "$DARWIN_DEFAULTS_MODULE" 'location = screenshotsDirectory'

  assert_contains "$DARWIN_HOMEBREW_MODULE" 'import ../homebrew-fallback.nix'
  assert_contains "$DARWIN_HOMEBREW_MODULE" 'import ../mas-apps.nix'
  assert_contains "$DARWIN_HOMEBREW_MODULE" 'homebrewFallbackHasCliEntries = homebrewFallback.brews != [ ]'
  assert_contains "$DARWIN_HOMEBREW_MODULE" 'homebrewFallback.casks != [ ] || homebrewFallback.vscode != [ ] || macAppStoreApps != { }'
  assert_contains "$DARWIN_HOMEBREW_MODULE" 'homebrewFallbackEnabled = homebrewFallbackHasCliEntries || (enableGuiApps && homebrewFallbackHasGuiEntries)'
  assert_contains "$DARWIN_HOMEBREW_MODULE" 'homebrew = lib.mkIf homebrewFallbackEnabled'
  assert_contains "$DARWIN_HOMEBREW_MODULE" 'enable = true'
  assert_contains "$DARWIN_HOMEBREW_MODULE" 'taps = homebrewFallback.taps'
  assert_contains "$DARWIN_HOMEBREW_MODULE" 'brews = homebrewFallback.brews'
  assert_contains "$DARWIN_HOMEBREW_MODULE" 'casks = lib.optionals enableGuiApps homebrewFallback.casks'
  assert_contains "$DARWIN_HOMEBREW_MODULE" 'masApps = lib.optionalAttrs enableGuiApps macAppStoreApps'
  assert_contains "$DARWIN_HOMEBREW_MODULE" 'vscode = lib.optionals enableGuiApps homebrewFallback.vscode'
  assert_contains "$DARWIN_HOMEBREW_MODULE" 'cleanup = "none"'

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
  assert_contains "$INSTALL_SCRIPT" 'switch --flake'
  assert_contains "$INSTALL_SCRIPT" 'build --flake'
  assert_contains "$INSTALL_SCRIPT" 'aarch64-darwin-full'
  assert_contains "$INSTALL_SCRIPT" 'x86_64-linux-cli'
  assert_contains "$INSTALL_SCRIPT" 'NIX_EXPERIMENTAL_ARGS=(--extra-experimental-features "nix-command flakes")'
  assert_contains "$INSTALL_SCRIPT" 'source "$SCRIPT_DIR/lib/runtime.sh"'
  assert_contains "$INSTALL_SCRIPT" 'dotfiles_resolve_command_from_path "nix-rootless"'
  assert_contains "$INSTALL_SCRIPT" 'zmodload zsh/datetime'
  assert_contains "$INSTALL_SCRIPT" 'HOME_MANAGER_BACKUP_EXTENSION="before-nix-darwin"'
  assert_contains "$INSTALL_SCRIPT" 'HOME_MANAGER_BACKUP_ARCHIVE_EPOCH'
  assert_contains "$INSTALL_SCRIPT" 'DOTFILES_DARWIN_SUDO_LOCAL_PATH'
  assert_contains "$INSTALL_SCRIPT" 'DARWIN_SUDO_LOCAL_BACKUP_PATH'
  assert_contains "$INSTALL_SCRIPT" 'archive_existing_home_manager_backups'
  assert_contains "$INSTALL_SCRIPT" 'backup_existing_darwin_sudo_local'
  assert_contains "$INSTALL_SCRIPT" 'sudo mv "$DARWIN_SUDO_LOCAL_PATH" "$DARWIN_SUDO_LOCAL_BACKUP_PATH"'
  assert_contains "$INSTALL_SCRIPT" 'switch -b "$HOME_MANAGER_BACKUP_EXTENSION" --flake'
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
  local repo
  local bin_dir
  local log_file
  local output_file

  make_temp_dir

  repo="$REPLY"
  bin_dir="$repo/bin"
  log_file="$repo/commands.log"
  output_file="$repo/output.log"

  mkdir -p "$repo/scripts/lib" "$repo/config/nix" "$bin_dir"
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
  cat > "$bin_dir/sudo" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
print -r -- "sudo:\$*" >> "$log_file"
"\$@"
EOF

  chmod +x "$bin_dir/uname" "$bin_dir/darwin-rebuild" "$bin_dir/nix" "$bin_dir/sudo"

  PATH="$bin_dir:/bin:/usr/bin:/usr/sbin:/sbin" \
    "$TEST_ZSH_BIN" "$repo/scripts/nix_install.sh" > "$output_file"

  assert_output_contains "$output_file" 'Nix profile: cli'
  assert_output_contains "$output_file" 'Flake output: aarch64-darwin-cli'
  assert_contains "$log_file" 'darwin-rebuild:switch --flake'
  assert_not_contains "$output_file" 'aarch64-darwin-full'

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

  make_temp_dir

  repo="$REPLY"
  bin_dir="$repo/bin"
  etc_dir="$repo/etc/pam.d"
  log_file="$repo/commands.log"
  output_file="$repo/output.log"
  sudo_local="$etc_dir/sudo_local"
  backup_file="${sudo_local}.before-nix-darwin"

  mkdir -p "$repo/scripts/lib" "$bin_dir" "$etc_dir"
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
  cat > "$bin_dir/sudo" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
print -r -- "sudo:\$*" >> "$log_file"
"\$@"
EOF

  chmod +x "$bin_dir/uname" "$bin_dir/nix" "$bin_dir/darwin-rebuild" "$bin_dir/sudo"

  cat > "$sudo_local" <<'EOF'
# sudo_local: local config file which survives system update and is included for sudo
# uncomment following line to enable Touch ID for sudo
auth sufficient pam_tid.so
EOF

  PATH="$bin_dir:/bin:/usr/bin:/usr/sbin:/sbin" \
    DOTFILES_DARWIN_SUDO_LOCAL_PATH="$sudo_local" \
    "$TEST_ZSH_BIN" "$repo/scripts/nix_install.sh" --profile full > "$output_file"

  assert_output_contains "$output_file" "Backing up existing $sudo_local to $backup_file before nix-darwin manages sudo Touch ID."
  assert_contains "$log_file" "sudo:mv $sudo_local $backup_file"
  assert_contains "$log_file" 'sudo:env HOME=/var/root darwin-rebuild switch --flake'
  assert_contains "$log_file" 'darwin-rebuild:switch --flake'
  assert_file "$backup_file"
  assert_not_exists "$sudo_local"

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
  cat > "$bin_dir/sudo" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
print -r -- "sudo:\$*" >> "$log_file"
"\$@"
EOF

  chmod +x "$bin_dir/uname" "$bin_dir/nix" "$bin_dir/darwin-rebuild" "$bin_dir/sudo"

  cat > "$old_zshrc_backup" <<'EOF'
legacy zshrc backup
EOF
  cat > "$old_xdg_backup" <<'EOF'
legacy xdg backup
EOF

  HOME="$home_dir" XDG_CONFIG_HOME="$config_dir" PATH="$bin_dir:/bin:/usr/bin:/usr/sbin:/sbin" \
    DOTFILES_HOME_MANAGER_BACKUP_ARCHIVE_EPOCH="1700000000" \
    "$TEST_ZSH_BIN" "$repo/scripts/nix_install.sh" --profile full > "$output_file"

  assert_output_contains "$output_file" "Archiving existing Home Manager backup $old_zshrc_backup to $archived_zshrc_backup before activation."
  assert_output_contains "$output_file" "Archiving existing Home Manager backup $old_xdg_backup to $archived_xdg_backup before activation."
  assert_contains "$log_file" 'sudo:env HOME=/var/root darwin-rebuild switch --flake'
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

  make_temp_dir

  repo="$REPLY"
  bin_dir="$repo/bin"
  log_file="$repo/commands.log"
  output_file="$repo/output.log"

  mkdir -p "$repo/scripts/lib" "$repo/config/nix" "$bin_dir"
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
  cat > "$bin_dir/sudo" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
print -r -- "sudo:\$*" >> "$log_file"
"\$@"
EOF

  chmod +x "$bin_dir/git" "$bin_dir/uname" "$bin_dir/nix" "$bin_dir/darwin-rebuild" "$bin_dir/sudo"

  PATH="$bin_dir:/bin:/usr/bin:/usr/sbin:/sbin" \
    "$TEST_ZSH_BIN" "$repo/scripts/nix_install.sh" --profile full > "$output_file"

  assert_output_contains "$output_file" 'Flake path: /private/tmp/dotfiles-flake.'
  assert_contains "$log_file" 'sudo:env HOME=/var/root darwin-rebuild switch --flake path:/private/tmp/dotfiles-flake.'
  assert_contains "$log_file" 'darwin-rebuild:switch --flake path:/private/tmp/dotfiles-flake.'

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
  assert_contains "$REMOVE_HOMEBREW_SCRIPT" 'mas-apps.nix'
  assert_contains "$REMOVE_HOMEBREW_SCRIPT" 'taps|brews|casks|vscode'
  assert_contains "$REMOVE_HOMEBREW_SCRIPT" 'mas_apps_has_entries'
  assert_contains "$REMOVE_HOMEBREW_SCRIPT" 'Refusing to remove Homebrew'
  assert_contains "$REMOVE_HOMEBREW_SCRIPT" 'Homebrew uninstall command'
  assert_contains "$REMOVE_HOMEBREW_SCRIPT" 'raw.githubusercontent.com/Homebrew/install/HEAD/uninstall.sh'
  assert_contains "$MAIN_SCRIPT" 'install_homebrew.sh'
}

test_cleanup_package_caches_script_supports_safe_nix_and_homebrew_cleanup() {
  local repo
  local bin_dir
  local log_file
  local output_file

  make_temp_dir

  repo="$REPLY"
  bin_dir="$repo/bin"
  log_file="$repo/cleanup.log"
  output_file="$repo/output.log"

  assert_contains "$MISE_CONFIG" '[tasks.nix-brew-cleanup]'
  assert_contains "$MISE_CONFIG" 'run = "zsh scripts/cleanup_package_caches.sh"'
  assert_contains "$CLEANUP_PACKAGE_CACHES_SCRIPT" '--older-than Nd'
  assert_contains "$CLEANUP_PACKAGE_CACHES_SCRIPT" '--apply'
  assert_contains "$CLEANUP_PACKAGE_CACHES_SCRIPT" 'nix profile wipe-history'
  assert_contains "$CLEANUP_PACKAGE_CACHES_SCRIPT" '--profile "$profile"'
  assert_contains "$CLEANUP_PACKAGE_CACHES_SCRIPT" 'nix store gc'
  assert_contains "$CLEANUP_PACKAGE_CACHES_SCRIPT" 'nix store optimise'
  assert_contains "$CLEANUP_PACKAGE_CACHES_SCRIPT" 'brew cleanup --prune=all --scrub'

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

  chmod +x "$repo/scripts/cleanup_package_caches.sh" "$bin_dir/nix" "$bin_dir/brew"

  HOME="$repo/home" HOMEBREW_PREFIX= PATH="$bin_dir:/bin:/usr/bin:/usr/sbin:/sbin" \
    "$TEST_ZSH_BIN" "$repo/scripts/cleanup_package_caches.sh" > "$output_file"

  assert_output_contains "$output_file" 'DRY-RUN: package caches were not removed'
  assert_output_contains "$output_file" "nix profile wipe-history --profile $repo/home/.local/state/nix/profiles/profile --older-than 30d"
  assert_output_contains "$output_file" "nix profile wipe-history --profile $repo/home/.nix-profile --older-than 30d"
  assert_output_contains "$output_file" 'nix store gc'
  assert_output_contains "$output_file" 'nix store optimise'
  assert_output_contains "$output_file" 'brew cleanup --prune=all --scrub'
  assert_not_exists "$log_file"

  HOME="$repo/home" HOMEBREW_PREFIX= PATH="$bin_dir:/bin:/usr/bin:/usr/sbin:/sbin" \
    "$TEST_ZSH_BIN" "$repo/scripts/cleanup_package_caches.sh" --apply > "$output_file"

  assert_contains "$log_file" "nix:profile wipe-history --profile $repo/home/.local/state/nix/profiles/profile --older-than 30d"
  assert_contains "$log_file" "nix:profile wipe-history --profile $repo/home/.nix-profile --older-than 30d"
  assert_contains "$log_file" 'nix:store gc'
  assert_contains "$log_file" 'nix:store optimise'
  assert_contains "$log_file" 'brew:cleanup --prune=all --scrub'

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
  assert_not_contains "$MAIN_SCRIPT" 'dotfiles/.agent/sync.sh'
  assert_contains "$MAIN_SCRIPT" 'install_homebrew.sh'
  assert_contains "$MAIN_SCRIPT" '--profile "$profile"'
  assert_contains "$MISE_CONFIG" '[tasks.nix-apply]'
  assert_contains "$MISE_CONFIG" 'run = "zsh scripts/nix_install.sh --cli-only"'
  assert_contains "$MISE_CONFIG" '[tasks.nix-apply-cli]'
  assert_contains "$MISE_CONFIG" 'run = "zsh scripts/nix_install.sh --cli-only"'
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
  assert_contains "$HOME_MANAGER_ZSH_MODULE" 'zcompdump-$ZSH_VERSION'
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
  cat > "$bin_dir/mise" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
print -r -- "mise:\$*" >> "$log_file"
EOF

  chmod +x \
    "$repo/scripts/install_homebrew.sh" \
    "$repo/scripts/nix_install.sh" \
    "$repo/scripts/chezmoi_apply.sh" \
    "$repo/scripts/setup_agent_files.sh" \
    "$repo/scripts/setup_git_hooks.sh" \
    "$bin_dir/mise"

  HOME="$home_dir" USER=dotfiles-test PATH="$bin_dir:/bin:/usr/bin:/usr/sbin:/sbin" \
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
  cat > "$bin_dir/mise" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
print -r -- "mise:\$*" >> "$log_file"
EOF

  chmod +x \
    "$repo/scripts/install_homebrew.sh" \
    "$repo/scripts/nix_install.sh" \
    "$repo/scripts/chezmoi_apply.sh" \
    "$repo/scripts/setup_agent_files.sh" \
    "$repo/scripts/setup_git_hooks.sh" \
    "$bin_dir/uname" \
    "$bin_dir/curl" \
    "$bin_dir/mise"

  HOME="$home_dir" USER=dotfiles-test PATH="$bin_dir:/bin:/usr/bin:/usr/sbin:/sbin" \
    "$TEST_ZSH_BIN" "$repo/main.sh" --cli-only > "$repo/output.log"

  assert_output_contains "$repo/output.log" "Profile: cli"
  assert_output_contains "$repo/output.log" "Setup completed successfully!"
  assert_contains "$log_file" 'sync_agent'
  assert_contains "$log_file" 'install_homebrew:--profile cli'
  assert_contains "$log_file" 'nix_install:--profile cli'
  assert_contains "$log_file" 'chezmoi_apply:--profile cli --mark-default'
  assert_contains "$log_file" 'setup_git_hooks:--profile cli'
  assert_contains "$log_file" 'mise:install'
  assert_not_contains "$log_file" 'curl:'

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
  cat > "$bin_dir/mise" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
print -r -- "mise:\$*" >> "$log_file"
EOF

  chmod +x \
    "$repo/scripts/install_homebrew.sh" \
    "$repo/scripts/nix_install.sh" \
    "$repo/scripts/chezmoi_apply.sh" \
    "$repo/scripts/setup_agent_files.sh" \
    "$repo/scripts/setup_git_hooks.sh" \
    "$bin_dir/mise"

  HOME="$home_dir" USER=dotfiles-test PATH="$bin_dir:/bin:/usr/bin:/usr/sbin:/sbin" \
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

  chmod +x \
    "$repo/scripts/chezmoi_apply.sh" \
    "$repo/scripts/setup_agent_files.sh" \
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
  assert_contains "$MISE_CONFIG" 'gemini-cli = "latest"'
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
exit 0
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

  DISPLAY="${DISPLAY:-:99}" HOME="$home_dir" USER=dotfiles-test PATH="$bin_dir:/bin:/usr/bin:/usr/sbin:/sbin" \
    "$TEST_ZSH_BIN" "$repo/scripts/update_managed_versions.sh" --only nix --with-gui-apps > "$repo/output.log"

  assert_contains "$log_file" 'nix:flake update'
  assert_contains "$log_file" "$expected_nix_install_args"

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
  assert_contains "$REPO_ROOT/home/dot_bashrc.tmpl" '.chezmoitemplates/bashrc'
  assert_contains "$REPO_ROOT/home/dot_bash_profile.tmpl" '.chezmoitemplates/bash_profile'
  assert_contains "$REPO_ROOT/home/private_dot_config/shell/dotfiles-shell-common.sh.tmpl" '.chezmoitemplates/dotfiles-shell-common.sh'
}

test_managed_update_script_updates_mise_and_nix() {
  local output
  make_temp_file
  output="$REPLY"

  assert_contains "$MISE_CONFIG" '[tasks.nix-mise-upgrade]'
  assert_contains "$MISE_CONFIG" 'run = "zsh scripts/update_managed_versions.sh"'
  assert_contains "$MISE_CONFIG" '[tasks.nix-lock-update]'
  assert_contains "$MISE_CONFIG" 'run = "zsh scripts/update_managed_versions.sh --only lock"'
  assert_contains "$MISE_CONFIG" '[tasks.nixpkgs-lock-update]'
  assert_contains "$MISE_CONFIG" 'run = "zsh scripts/update_managed_versions.sh --only lock --nix-input nixpkgs"'
  assert_contains "$MISE_CONFIG" '[tasks.home-manager-lock-update]'
  assert_contains "$MISE_CONFIG" 'run = "zsh scripts/update_managed_versions.sh --only lock --nix-input home-manager"'
  assert_contains "$MISE_CONFIG" '[tasks.nix-darwin-lock-update]'
  assert_contains "$MISE_CONFIG" 'run = "zsh scripts/update_managed_versions.sh --only lock --nix-input nix-darwin"'
  assert_contains "$MISE_CONFIG" '[tasks.nix-upgrade]'
  assert_contains "$MISE_CONFIG" 'run = "zsh scripts/update_managed_versions.sh --only nix"'
  assert_contains "$MISE_CONFIG" '[tasks.nixpkgs-upgrade]'
  assert_contains "$MISE_CONFIG" 'run = "zsh scripts/update_managed_versions.sh --only nix --nix-input nixpkgs"'
  assert_contains "$MISE_CONFIG" '[tasks.home-manager-upgrade]'
  assert_contains "$MISE_CONFIG" 'run = "zsh scripts/update_managed_versions.sh --only nix --nix-input home-manager"'
  assert_contains "$MISE_CONFIG" '[tasks.nix-darwin-upgrade]'
  assert_contains "$MISE_CONFIG" 'run = "zsh scripts/update_managed_versions.sh --only nix --nix-input nix-darwin"'
  assert_contains "$MISE_CONFIG" '[tasks.mise-upgrade]'
  assert_contains "$MISE_CONFIG" 'run = "zsh scripts/update_managed_versions.sh --only mise"'
  assert_contains "$MISE_CONFIG" 'node = "22"'
  assert_contains "$MISE_CONFIG" 'go = "1.25"'
  assert_contains "$MISE_CONFIG" 'java = "zulu-21"'
  assert_contains "$MISE_CONFIG" 'python = "3.13"'
  assert_contains "$MISE_CONFIG" '[tools."http:devin"]'
  assert_contains "$MISE_CONFIG" 'version_list_url = "https://static.devin.ai/cli/current/manifest.json"'
  assert_contains "$MISE_CONFIG" '[tools."http:cursor-agent"]'
  assert_contains "$MISE_CONFIG" 'https://downloads.cursor.com/lab/{{ version }}/{{ os(macos="darwin", linux="linux") }}/{{ arch(x64="x64", arm64="arm64") }}/agent-cli-package.tar.gz'
  assert_contains "$MISE_CONFIG" 'opencode = "latest"'
  assert_contains "$MISE_CONFIG" '"pipx:git+https://github.com/NousResearch/hermes-agent.git" = "latest"'
  assert_contains "$MISE_CONFIG" '"npm:@github/copilot" = "latest"'
  assert_contains "$MISE_CONFIG" '"npm:openclaw" = "latest"'
  assert_contains "$MISE_CONFIG" 'mysql = "8.0"'
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

main() {
  test_brewfile_migration_writes_nix_lists_and_unmapped_report
  test_brewfile_migration_dry_run_does_not_write_outputs
  test_repository_migration_moves_available_formulae_and_gui_apps_to_nix
  test_waza_is_integrated_for_agent_skill_evaluations
  test_waza_cli_agent_eval_script_is_guarded_and_can_dry_run
  test_waza_cli_agent_eval_script_preserves_cli_failure_status
  test_waza_cli_agent_eval_script_grades_successful_cli_output
  test_waza_eval_suites_cover_all_regular_agent_skills
  test_flake_exposes_nix_darwin_and_home_manager_profiles
  test_home_manager_and_darwin_modules_define_profiles_without_homebrew
  test_nix_install_script_switches_nix_darwin_or_home_manager
  test_nix_install_script_defaults_to_cli_profile_on_macos
  test_nix_install_script_backs_up_existing_sudo_local_before_darwin_switch
  test_nix_install_script_archives_existing_home_manager_backups_before_switch
  test_nix_install_script_handles_dirty_worktree_without_hanging
  test_rootless_nix_install_script_supports_no_sudo_linux
  test_nix_portable_install_script_supports_no_sudo_nix_main_path
  test_remove_homebrew_script_is_explicit_and_dry_run_first
  test_cleanup_package_caches_script_supports_safe_nix_and_homebrew_cleanup
  test_install_homebrew_script_supports_required_profiles
  test_main_mise_shell_and_hooks_use_nix_as_the_setup_path
  test_main_script_runs_homebrew_before_nix_setup
  test_main_script_uses_cli_profile_when_requested
  test_main_script_applies_chezmoi_instead_of_copying_legacy_dotfiles
  test_apply_updates_applies_chezmoi_and_refreshes_agent_and_hooks
  test_setup_git_hooks_generates_executable_hooks_with_valid_zsh_shebang
  test_ai_cli_tools_are_managed_by_mise
  test_managed_update_script_skips_gui_profile_on_macos_unless_requested
  test_managed_update_script_includes_gui_profile_when_requested
  test_bash_templates_support_dynamic_shell_setup
  test_managed_update_script_updates_mise_and_nix
  echo "nix migration tests passed"
}

main "$@"
