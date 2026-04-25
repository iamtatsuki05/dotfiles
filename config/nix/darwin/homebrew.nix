{
  lib,
  username,
  enableGuiApps,
  ...
}:

let
  homebrewFallback = import ../homebrew-fallback.nix;
  macAppStoreApps = import ../mas-apps.nix;
  homebrewFallbackHasCliEntries = homebrewFallback.brews != [ ];
  homebrewFallbackHasGuiEntries =
    homebrewFallback.casks != [ ] || homebrewFallback.vscode != [ ] || macAppStoreApps != { };
  homebrewFallbackEnabled = homebrewFallbackHasCliEntries || (enableGuiApps && homebrewFallbackHasGuiEntries);
in
{
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
}
