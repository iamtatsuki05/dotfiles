{
  config,
  lib,
  pkgs,
  homeDirectory,
  ...
}:

let
  dotfilesRepoRoot = "${homeDirectory}/src/dotfiles";
  dotfilesAutoUpdateScript = pkgs.writeShellScript "dotfiles-auto-update" ''
    set -euo pipefail

    exec >> /tmp/dotfiles-git-pull.log 2>&1
    echo "===> $(${pkgs.coreutils}/bin/date '+%Y-%m-%d %H:%M:%S') dotfiles-auto-update"
    ${pkgs.git}/bin/git -C ${lib.escapeShellArg dotfilesRepoRoot} pull --ff-only
  '';
in
{
  systemd.user.services.dotfiles-auto-update = lib.mkIf
    (config.dotfiles.profile == "full" && !pkgs.stdenv.hostPlatform.isDarwin)
    {
      Unit.Description = "Update dotfiles repository";
      Service = {
        Type = "oneshot";
        ExecStart = "${dotfilesAutoUpdateScript}";
      };
    };

  systemd.user.timers.dotfiles-auto-update = lib.mkIf
    (config.dotfiles.profile == "full" && !pkgs.stdenv.hostPlatform.isDarwin)
    {
      Unit.Description = "Daily dotfiles repository update";
      Timer = {
        OnCalendar = "*-*-* 06:00:00";
        Persistent = true;
      };
      Install.WantedBy = [ "timers.target" ];
    };
}
