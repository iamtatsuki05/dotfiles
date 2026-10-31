let
  features = (builtins.fromTOML (builtins.readFile ../../home/.chezmoidata.toml)).features;
in
assert builtins.isAttrs features;
assert builtins.attrNames features == [ "macos" ];
assert builtins.isBool features.macos;
features
