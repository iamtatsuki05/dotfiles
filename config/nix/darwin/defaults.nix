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

  system.startup.chime = false;

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

  system.defaults.spaces = {
    spans-displays = false;
  };

  system.defaults.trackpad = {
    ActuateDetents = true;
    Clicking = true;
    DragLock = false;
    Dragging = false;
    FirstClickThreshold = 2;
    ForceSuppressed = false;
    SecondClickThreshold = 2;
    TrackpadCornerSecondaryClick = 0;
    TrackpadFourFingerHorizSwipeGesture = 2;
    TrackpadFourFingerPinchGesture = 2;
    TrackpadFourFingerVertSwipeGesture = 2;
    TrackpadMomentumScroll = true;
    TrackpadPinch = true;
    TrackpadRightClick = true;
    TrackpadRotate = true;
    TrackpadThreeFingerDrag = false;
    TrackpadThreeFingerHorizSwipeGesture = 2;
    TrackpadThreeFingerTapGesture = 0;
    TrackpadThreeFingerVertSwipeGesture = 2;
    TrackpadTwoFingerDoubleTapGesture = true;
    TrackpadTwoFingerFromRightEdgeSwipeGesture = 3;
  };

  system.defaults.WindowManager = {
    AppWindowGroupingBehavior = true;
    AutoHide = false;
    EnableTiledWindowMargins = false;
    EnableTilingByEdgeDrag = false;
    EnableTopTilingByEdgeDrag = false;
    GloballyEnabled = false;
    HideDesktop = true;
    StandardHideWidgets = true;
    StageManagerHideWidgets = false;
  };

  system.defaults.CustomUserPreferences.NSGlobalDomain = {
    AppleKeyboardUIMode = 1;
    AppleMenuBarFontSize = "large";
  };
}
