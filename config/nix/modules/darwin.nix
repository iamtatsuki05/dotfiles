{
  lib,
  pkgs,
  username,
  homeDirectory,
  enableGuiApps,
  ...
}:

let
  guiPackages = import ../gui-packages.nix { inherit pkgs; };
  homebrewFallback = import ../homebrew-fallback.nix;
  homebrewFallbackHasCliEntries = homebrewFallback.brews != [ ];
  homebrewFallbackHasGuiEntries = homebrewFallback.casks != [ ] || homebrewFallback.vscode != [ ];
  homebrewFallbackEnabled = homebrewFallbackHasCliEntries || (enableGuiApps && homebrewFallbackHasGuiEntries);
in
{
  nixpkgs.config.allowUnfree = true;

  nix.enable = false;

  programs.zsh.enable = true;

  security.pam.services.sudo_local.enable = false;

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
    vscode = lib.optionals enableGuiApps homebrewFallback.vscode;
    enableZshIntegration = false;
    global.autoUpdate = false;
    onActivation = {
      autoUpdate = false;
      upgrade = false;
      cleanup = "none";
    };
  };

  users.users.${username}.home = homeDirectory;

  system.primaryUser = username;
  system.stateVersion = 6;

  system.defaults.NSGlobalDomain = {
    InitialKeyRepeat = 12;
    KeyRepeat = 1;
  };
}
