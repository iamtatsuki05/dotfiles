{
  lib,
  username,
  enableGuiApps,
  ...
}:

let
  homebrewFallback = import ../homebrew-fallback.nix;
  homebrewFallbackHasCliEntries = homebrewFallback.brews != [ ];
  homebrewFallbackHasGuiEntries = homebrewFallback.casks != [ ] || homebrewFallback.vscode != [ ];
  homebrewFallbackEnabled = homebrewFallbackHasCliEntries || (enableGuiApps && homebrewFallbackHasGuiEntries);
in
{
  homebrew = lib.mkIf homebrewFallbackEnabled {
    enable = true;
    user = username;
    taps = homebrewFallback.taps;
    brews = homebrewFallback.brews;
    casks = lib.optionals enableGuiApps homebrewFallback.casks;
    masApps = { };
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
