{ lib, ... }:

let
  features = import ../features.nix;
in

{
  imports = [ ./base.nix ] ++ lib.optionals features.macos [
    ./defaults.nix
    ./homebrew.nix
    ./auto-update.nix
  ];
}
