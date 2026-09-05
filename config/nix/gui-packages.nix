{ pkgs }:

let
  inherit (pkgs) lib stdenv;
  features = import ./features.nix;
  commonPackageNames = import ./gui-common-package-names.nix;
  macosPackageNames = import ./gui-macos-package-names.nix;
  linuxPackageNames = import ./gui-linux-package-names.nix;
  macosGuiPackageNames = lib.optionals features.macos (commonPackageNames ++ macosPackageNames);
  packageNames =
    if stdenv.hostPlatform.isDarwin then
      macosGuiPackageNames
    else if stdenv.hostPlatform.isLinux then
      commonPackageNames ++ linuxPackageNames
    else
      commonPackageNames;
in
import ./package-list.nix {
  inherit pkgs packageNames;
}
