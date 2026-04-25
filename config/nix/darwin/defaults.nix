{
  lib,
  username,
  homeDirectory,
  ...
}:

let
  screenshotsDirectory = "${homeDirectory}/SS";
in
{
  security.pam.services.sudo_local = {
    enable = true;
    touchIdAuth = true;
  };

  system.activationScripts.postActivation.text = lib.mkAfter ''
    sudo --user=${username} -- mkdir -p ${lib.escapeShellArg screenshotsDirectory}
  '';

  system.defaults.NSGlobalDomain = {
    InitialKeyRepeat = 12;
    KeyRepeat = 1;
  };

  system.defaults.screencapture = {
    location = screenshotsDirectory;
  };
}
