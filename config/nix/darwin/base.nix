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
in
{
  nixpkgs.config.allowUnfree = true;

  nix.enable = false;

  programs.zsh.enable = true;

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

  users.users.${username}.home = homeDirectory;

  system.primaryUser = username;
  system.stateVersion = 6;
}
