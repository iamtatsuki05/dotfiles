{
  config,
  lib,
  pkgs,
  username,
  homeDirectory,
  profile,
  enableGuiApps,
  ...
}:

let
  features = import ../features.nix;
  isDarwin = pkgs.stdenv.hostPlatform.isDarwin;
  effectiveEnableGuiApps = enableGuiApps && (!isDarwin || features.macos);
in

{
  imports = [
    ./packages.nix
    ./zsh.nix
    ./neovim.nix
    ./auto-update.nix
    ./session.nix
  ];

  options.dotfiles.profile = lib.mkOption {
    type = lib.types.enum [ "cli" "full" ];
    default = profile;
    description = "Dotfiles setup profile.";
  };

  options.dotfiles.enableGuiApps = lib.mkOption {
    type = lib.types.bool;
    default = effectiveEnableGuiApps;
    description = "Install GUI applications from the Nix package set.";
  };

  config = {
    home.username = username;
    home.homeDirectory = homeDirectory;
    home.stateVersion = "25.11";

    programs.home-manager.enable = true;

    targets.darwin.copyApps.enable = pkgs.stdenv.hostPlatform.isDarwin && config.dotfiles.enableGuiApps
      && (!isDarwin || features.macos);
    targets.darwin.linkApps.enable = false;
  };
}
