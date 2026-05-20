{
  lib,
  pkgs,
  username,
  homeDirectory,
  enableGuiApps,
  ...
}:

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
    ]);

  users.users.${username}.home = homeDirectory;

  system.primaryUser = username;
  system.stateVersion = 6;
}
