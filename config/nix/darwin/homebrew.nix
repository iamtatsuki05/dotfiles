{
  config,
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
  homebrewTrustedCasks = lib.optionals enableGuiApps (homebrewFallback.trustedCasks or [ ]);
  homebrewTrustCommands = lib.concatMapStringsSep "\n" (
    cask:
    "      PATH=\"${config.homebrew.prefix}/bin:$PATH\" sudo --preserve-env=PATH --user=${lib.escapeShellArg username} --set-home env HOMEBREW_NO_AUTO_UPDATE=1 brew trust --cask ${lib.escapeShellArg cask}"
  ) homebrewTrustedCasks;
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

  system.activationScripts.homebrew.text =
    lib.mkIf (homebrewFallbackEnabled && homebrewTrustedCasks != [ ]) (lib.mkBefore ''
    # Homebrew may refuse third-party casks when tap trust enforcement is enabled.
    if [ -f "${config.homebrew.prefix}/bin/brew" ]; then
      echo >&2 "trusting Homebrew casks..."
${homebrewTrustCommands}
    fi
  '');
}
