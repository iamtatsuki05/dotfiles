{
  lib,
  pkgs,
  username,
  homeDirectory,
  profile,
  enableGuiApps,
  ...
}:

let
  guiPackages = import ../gui-packages.nix { inherit pkgs; };
  homebrewFallback = import ../homebrew-fallback.nix;
  macAppStoreApps = import ../mas-apps.nix;
  dotfilesRepoRoot = "${homeDirectory}/src/dotfiles";
  screenshotsDirectory = "${homeDirectory}/SS";
  homebrewFallbackHasCliEntries = homebrewFallback.brews != [ ];
  homebrewFallbackHasGuiEntries =
    homebrewFallback.casks != [ ] || homebrewFallback.vscode != [ ] || macAppStoreApps != { };
  homebrewFallbackEnabled = homebrewFallbackHasCliEntries || (enableGuiApps && homebrewFallbackHasGuiEntries);
in
{
  nixpkgs.config.allowUnfree = true;

  nix.enable = false;

  programs.zsh.enable = true;

  security.pam.services.sudo_local = {
    enable = true;
    touchIdAuth = true;
  };

  environment.shells = [
    pkgs.zsh
  ];

  environment.systemPackages =
    (with pkgs; [
      curl
      git
      zsh
    ])
    ++ lib.optionals enableGuiApps guiPackages;

  homebrew = lib.mkIf homebrewFallbackEnabled {
    enable = true;
    user = username;
    taps = homebrewFallback.taps;
    brews = homebrewFallback.brews;
    casks = lib.optionals enableGuiApps homebrewFallback.casks;
    masApps = lib.optionalAttrs enableGuiApps macAppStoreApps;
    vscode = lib.optionals enableGuiApps homebrewFallback.vscode;
    enableZshIntegration = false;
    global.autoUpdate = false;
    onActivation = {
      autoUpdate = false;
      upgrade = false;
      cleanup = "none";
    };
  };

  launchd.user.agents.dotfiles-auto-update = lib.mkIf (profile == "full") {
    serviceConfig = {
      ProgramArguments = [
        "${pkgs.git}/bin/git"
        "-C"
        dotfilesRepoRoot
        "pull"
        "--ff-only"
      ];
      StartCalendarInterval = [
        {
          Hour = 6;
          Minute = 0;
        }
      ];
      StandardOutPath = "/tmp/dotfiles-git-pull.log";
      StandardErrorPath = "/tmp/dotfiles-git-pull.log";
    };
  };

  system.activationScripts.postActivation.text = lib.mkAfter ''
    sudo --user=${username} -- mkdir -p ${lib.escapeShellArg screenshotsDirectory}

    if command -v crontab >/dev/null 2>&1; then
      current_cron="$(mktemp)"
      stripped_cron="$(mktemp)"

      if sudo --user=${username} -- crontab -l 2>/dev/null | cat > "$current_cron"; then
        if grep -q '# >>> dotfiles managed cron >>>' "$current_cron"; then
          awk '
            $0 == "# >>> dotfiles managed cron >>>" { skip = 1; next }
            $0 == "# <<< dotfiles managed cron <<<" { skip = 0; next }
            skip != 1 { print }
          ' "$current_cron" > "$stripped_cron"
          sudo --user=${username} -- crontab "$stripped_cron"
          echo "removed legacy dotfiles cron block"
        fi
      fi

      rm -f "$current_cron" "$stripped_cron"
    fi
  '';

  users.users.${username}.home = homeDirectory;

  system.primaryUser = username;
  system.stateVersion = 6;

  system.defaults.NSGlobalDomain = {
    InitialKeyRepeat = 12;
    KeyRepeat = 1;
  };

  system.defaults.screencapture = {
    location = screenshotsDirectory;
  };
}
