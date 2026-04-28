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
    AppleInterfaceStyle = "Dark";
    AppleShowAllExtensions = true;
    InitialKeyRepeat = 12;
    KeyRepeat = 1;
    NSTableViewDefaultSizeMode = 2;
    "com.apple.keyboard.fnState" = true;
    "com.apple.sound.beep.volume" = 0.0;
    "com.apple.trackpad.forceClick" = true;
    "com.apple.trackpad.scaling" = 3.0;
  };

  system.defaults.".GlobalPreferences" = {
    "com.apple.mouse.scaling" = 2.0;
  };

  system.defaults.dock = {
    autohide = true;
    largesize = 128;
    magnification = true;
    mru-spaces = false;
    show-recents = false;
    tilesize = 43;
    wvous-bl-corner = 11;
    wvous-br-corner = 3;
    wvous-tl-corner = 12;
    wvous-tr-corner = 2;
  };

  system.defaults.finder = {
    FXPreferredViewStyle = "Nlsv";
    FXRemoveOldTrashItems = true;
    ShowMountedServersOnDesktop = true;
    ShowPathbar = true;
  };

  system.defaults.screencapture = {
    location = screenshotsDirectory;
  };

  system.defaults.trackpad = {
    Clicking = true;
    FirstClickThreshold = 2;
    SecondClickThreshold = 2;
    TrackpadFourFingerHorizSwipeGesture = 2;
    TrackpadFourFingerPinchGesture = 2;
    TrackpadPinch = true;
    TrackpadRightClick = true;
    TrackpadRotate = true;
    TrackpadThreeFingerTapGesture = 0;
    TrackpadTwoFingerDoubleTapGesture = true;
    TrackpadTwoFingerFromRightEdgeSwipeGesture = 3;
  };

  system.defaults.WindowManager = {
    AppWindowGroupingBehavior = true;
    EnableTiledWindowMargins = false;
    EnableTilingByEdgeDrag = false;
    EnableTopTilingByEdgeDrag = false;
    HideDesktop = true;
    StandardHideWidgets = true;
  };

  system.defaults.CustomUserPreferences.NSGlobalDomain = {
    AppleKeyboardUIMode = 1;
    AppleMenuBarFontSize = "large";
  };
}
