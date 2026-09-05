{ homeDirectory, ... }:

let
  shell = (builtins.fromTOML (builtins.readFile ../../../home/.chezmoidata.toml)).shell;
in

{
  home.sessionVariables = {
    EDITOR = shell.editor;
    XDG_CONFIG_HOME = "${homeDirectory}/${shell.xdg.config}";
    XDG_CACHE_HOME = "${homeDirectory}/${shell.xdg.cache}";
    XDG_DATA_HOME = "${homeDirectory}/${shell.xdg.data}";
    XDG_STATE_HOME = "${homeDirectory}/${shell.xdg.state}";
  };
}
