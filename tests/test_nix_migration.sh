#!/usr/bin/env zsh

set -euo pipefail

readonly TEST_DIR="$(cd "$(dirname "$0")" && pwd)"
readonly REPO_ROOT="$(cd "$TEST_DIR/.." && pwd)"
readonly MIGRATION_SCRIPT="$REPO_ROOT/scripts/migrate_brew_to_nix.sh"
readonly INSTALL_SCRIPT="$REPO_ROOT/scripts/nix_install.sh"
readonly REMOVE_HOMEBREW_SCRIPT="$REPO_ROOT/scripts/remove_homebrew.sh"
readonly APPLY_UPDATES_SCRIPT="$REPO_ROOT/scripts/apply_updates.sh"
readonly MAIN_SCRIPT="$REPO_ROOT/main.sh"
readonly FLAKE_FILE="$REPO_ROOT/flake.nix"
readonly ZSHRC_FILE="$REPO_ROOT/dotfiles/.zshrc"
readonly MISE_CONFIG="$REPO_ROOT/config/mise/config.toml"
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

assert_file() {
  local file_path="$1"
  [[ -f "$file_path" ]] || fail "expected file: $file_path"
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

create_fixture_repo() {
  local repo="$1"

  mkdir -p "$repo/config/nix" "$repo/input"

  cat > "$repo/config/nix/brew-to-nix.tsv" <<'EOF'
# brew	nix
git	git
gnu-sed	gnused
mise	dotfiles.mise
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
brew "private-tool"
cask "slack"
cask "alacritty"
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
  repo="$(mktemp -d)"
  create_fixture_repo "$repo"

  "$TEST_ZSH_BIN" "$MIGRATION_SCRIPT" \
    --repo-root "$repo" \
    --brewfile "$repo/input/Brewfile" \
    --apply >/dev/null

  assert_contains "$repo/config/nix/package-names.nix" '"git"'
  assert_contains "$repo/config/nix/package-names.nix" '"gnused"'
  assert_contains "$repo/config/nix/package-names.nix" '"dotfiles.mise"'
  assert_contains "$repo/config/nix/gui-common-package-names.nix" '"slack"'
  assert_contains "$repo/config/nix/gui-common-package-names.nix" '"alacritty"'
  assert_contains "$repo/config/nix/gui-common-package-names.nix" '"bitwarden-desktop"'
  assert_contains "$repo/config/nix/gui-linux-package-names.nix" '"ghostty"'
  assert_contains "$repo/config/nix/gui-macos-package-names.nix" '"raycast"'
  assert_contains "$repo/config/nix/migrated-brew-formulae.txt" "gnu-sed"
  assert_contains "$repo/config/nix/migrated-brew-casks.txt" "slack"
  assert_contains "$repo/config/nix/unmapped-homebrew.tsv" $'brew	private-tool'
  assert_contains "$repo/config/nix/unmapped-homebrew.tsv" $'cask	private-app'
  assert_contains "$repo/config/nix/unmapped-homebrew.tsv" $'vscode	example.extension'
  assert_contains "$repo/config/nix/unmapped-homebrew.tsv" $'uv	claude-monitor'
  assert_contains "$repo/config/nix/homebrew-fallback.nix" '"example/tap"'
  assert_contains "$repo/config/nix/homebrew-fallback.nix" '"private-tool"'
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
  repo="$(mktemp -d)"
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
    "gemini-cli"
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
    "_1password-gui"
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
    "1password"
    "alacritty"
    "discord"
    "google-chrome"
    "slack"
    "visual-studio-code"
  )

  for nix_attr in "${cli_attrs[@]}"; do
    assert_contains "$NIX_PACKAGE_NAMES_FILE" "\"$nix_attr\""
  done

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
  assert_contains "$DARWIN_BASE_MODULE" 'lib.optionals enableGuiApps guiPackages'
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
  assert_contains "$INSTALL_SCRIPT" 'HOME_MANAGER_BACKUP_EXTENSION="before-nix-darwin"'
  assert_contains "$INSTALL_SCRIPT" 'switch -b "$HOME_MANAGER_BACKUP_EXTENSION" --flake'
  assert_contains "$INSTALL_SCRIPT" '"${NIX_EXPERIMENTAL_ARGS[@]}"'
  assert_contains "$INSTALL_SCRIPT" 'sudo env HOME=/var/root'
  assert_contains "$INSTALL_SCRIPT" 'scripts/remove_homebrew.sh'
  assert_contains "$INSTALL_SCRIPT" '$REMOVE_HOMEBREW_SCRIPT" --apply --confirm-nix-ready'
  assert_contains "$INSTALL_SCRIPT" '--exclude result'
  assert_contains "$INSTALL_SCRIPT" '--exclude .agent'
  assert_not_contains "$INSTALL_SCRIPT" '$(nix_args)'
  assert_not_contains "$INSTALL_SCRIPT" 'brew bundle'
  assert_not_contains "$INSTALL_SCRIPT" 'fallback.Brewfile'
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
  assert_not_contains "$MAIN_SCRIPT" "brew_install.sh"
}

test_main_mise_shell_and_hooks_use_nix_as_the_setup_path() {
  assert_contains "$MAIN_SCRIPT" 'nix_install.sh'
  assert_contains "$MAIN_SCRIPT" '--profile "$profile"'
  assert_contains "$MISE_CONFIG" '[tasks.nix-apply]'
  assert_contains "$MISE_CONFIG" 'run = "zsh scripts/nix_install.sh"'
  assert_contains "$MISE_CONFIG" '[tasks.nix-apply-cli]'
  assert_contains "$MISE_CONFIG" 'run = "zsh scripts/nix_install.sh --cli-only"'
  assert_contains "$MISE_CONFIG" '[tasks.nix-apply-with-gui-apps]'
  assert_contains "$MISE_CONFIG" 'run = "zsh scripts/nix_install.sh --with-gui-apps"'
  assert_contains "$MISE_CONFIG" 'run = "zsh scripts/remove_homebrew.sh --apply --confirm-nix-ready"'
  assert_not_contains "$MISE_CONFIG" '[tasks.homebrew-dump]'
  assert_not_contains "$MISE_CONFIG" 'brew_dump.sh'
  assert_contains "$ZSHRC_FILE" '$HOME/.nix-profile/bin'
  assert_contains "$ZSHRC_FILE" 'hm-session-vars.sh'
  assert_contains "$ZSHRC_FILE" 'dotfiles_cleanup_stale_homebrew_completion'
  assert_contains "$ZSHRC_FILE" 'zcompdump-$ZSH_VERSION'
  assert_not_contains "$ZSHRC_FILE" 'HOMEBREW_PREFIX'
  assert_not_contains "$ZSHRC_FILE" 'brew shellenv'
  assert_not_contains "$APPLY_UPDATES_SCRIPT" "sync_nix_profile"
}

main() {
  test_brewfile_migration_writes_nix_lists_and_unmapped_report
  test_brewfile_migration_dry_run_does_not_write_outputs
  test_repository_migration_moves_available_formulae_and_gui_apps_to_nix
  test_flake_exposes_nix_darwin_and_home_manager_profiles
  test_home_manager_and_darwin_modules_define_profiles_without_homebrew
  test_nix_install_script_switches_nix_darwin_or_home_manager
  test_remove_homebrew_script_is_explicit_and_dry_run_first
  test_main_mise_shell_and_hooks_use_nix_as_the_setup_path
  echo "nix migration tests passed"
}

main "$@"
