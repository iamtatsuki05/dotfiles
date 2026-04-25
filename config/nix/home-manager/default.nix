{
  lib,
  username,
  homeDirectory,
  profile,
  enableGuiApps,
  ...
}:

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
    default = enableGuiApps;
    description = "Install GUI applications from the Nix package set.";
  };

  config = {
    home.username = username;
    home.homeDirectory = homeDirectory;
    home.stateVersion = "25.11";

    programs.home-manager.enable = true;

    targets.darwin.copyApps.enable = false;
    targets.darwin.linkApps.enable = false;
  };
}
