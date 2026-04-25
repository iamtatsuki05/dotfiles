{ pkgs, packageNames }:

let
  inherit (pkgs) lib;
  dotfilesPackages = import ./dotfiles-packages.nix { inherit pkgs; };
  packageScope = pkgs // {
    dotfiles = dotfilesPackages;
  };
  packageForName = name: lib.attrsets.getAttrFromPath (lib.splitString "." name) packageScope;
  packages = map packageForName packageNames;
in
lib.filter (pkg: lib.meta.availableOn pkgs.stdenv.hostPlatform pkg) packages
