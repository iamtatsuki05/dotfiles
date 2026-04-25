{ pkgs }:

let
  inherit (pkgs) lib stdenv;
  commonPackageNames = import ./gui-common-package-names.nix;
  macosPackageNames = import ./gui-macos-package-names.nix;
  linuxPackageNames = import ./gui-linux-package-names.nix;
  packageNames =
    commonPackageNames
    ++ lib.optionals stdenv.hostPlatform.isDarwin macosPackageNames
    ++ lib.optionals stdenv.hostPlatform.isLinux linuxPackageNames;
in
import ./package-list.nix {
  inherit pkgs packageNames;
}
