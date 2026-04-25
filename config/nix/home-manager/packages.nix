{ config, lib, pkgs, ... }:

let
  cliPackages = import ../packages.nix { inherit pkgs; };
  guiPackages = import ../gui-packages.nix { inherit pkgs; };
  homeManagerProvidedPackageNames = [
    "neovim"
  ];
  unmanagedCliPackages =
    lib.filter
      (pkg: !(lib.elem (lib.getName pkg) homeManagerProvidedPackageNames))
      cliPackages;
in
{
  home.packages =
    unmanagedCliPackages
    ++ lib.optionals
      (config.dotfiles.enableGuiApps && !pkgs.stdenv.hostPlatform.isDarwin)
      guiPackages;
}
