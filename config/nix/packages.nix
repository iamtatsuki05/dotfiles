{ pkgs }:

import ./package-list.nix {
  inherit pkgs;
  packageNames = import ./package-names.nix;
}
