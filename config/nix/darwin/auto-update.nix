{
  lib,
  pkgs,
  username,
  homeDirectory,
  profile,
  ...
}:

let
  dotfilesRepoRoot = "${homeDirectory}/src/dotfiles";
in
{
  launchd.user.agents.dotfiles-auto-update = lib.mkIf (profile == "full") {
    serviceConfig = {
      ProgramArguments = [
        "${pkgs.git}/bin/git"
        "-C"
        dotfilesRepoRoot
        "pull"
        "--ff-only"
      ];
      StartCalendarInterval = [
        {
          Hour = 6;
          Minute = 0;
        }
      ];
      StandardOutPath = "/tmp/dotfiles-git-pull.log";
      StandardErrorPath = "/tmp/dotfiles-git-pull.log";
    };
  };

  system.activationScripts.postActivation.text = lib.mkAfter ''
    if command -v crontab >/dev/null 2>&1; then
      current_cron="$(mktemp)"
      stripped_cron="$(mktemp)"

      if sudo --user=${username} -- crontab -l 2>/dev/null | cat > "$current_cron"; then
        if grep -q '# >>> dotfiles managed cron >>>' "$current_cron"; then
          awk '
            $0 == "# >>> dotfiles managed cron >>>" { skip = 1; next }
            $0 == "# <<< dotfiles managed cron <<<" { skip = 0; next }
            skip != 1 { print }
          ' "$current_cron" > "$stripped_cron"
          sudo --user=${username} -- crontab "$stripped_cron"
          echo "removed legacy dotfiles cron block"
        fi
      fi

      rm -f "$current_cron" "$stripped_cron"
    fi
  '';
}
