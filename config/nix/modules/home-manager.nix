{
  config,
  lib,
  pkgs,
  username,
  homeDirectory,
  profile,
  enableGuiApps,
  ...
}:

let
  cliPackages = import ../packages.nix { inherit pkgs; };
  guiPackages = import ../gui-packages.nix { inherit pkgs; };
  dotfilesRepoRoot = "${homeDirectory}/src/dotfiles";
  dotfilesAutoUpdateScript = pkgs.writeShellScript "dotfiles-auto-update" ''
    set -euo pipefail

    exec >> /tmp/dotfiles-git-pull.log 2>&1
    echo "===> $(${pkgs.coreutils}/bin/date '+%Y-%m-%d %H:%M:%S') dotfiles-auto-update"
    ${pkgs.git}/bin/git -C ${lib.escapeShellArg dotfilesRepoRoot} pull --ff-only
  '';
  homeManagerProvidedPackageNames = [
    "neovim"
  ];
  unmanagedCliPackages =
    lib.filter
      (pkg: !(lib.elem (lib.getName pkg) homeManagerProvidedPackageNames))
      cliPackages;
in
{
  options.dotfiles.profile = lib.mkOption {
    type = lib.types.enum [ "cli" "full" ];
    default = profile;
    description = "Dotfiles setup profile.";
  };

  options.dotfiles.enableGuiApps = lib.mkOption {
    type = lib.types.bool;
    default = enableGuiApps;
    description = "Install GUI applications from the Nix package set.";
  };

  config = {
    home.username = username;
    home.homeDirectory = homeDirectory;
    home.stateVersion = "25.11";

    programs.home-manager.enable = true;

    home.packages =
      unmanagedCliPackages
      ++ lib.optionals
        (config.dotfiles.enableGuiApps && !pkgs.stdenv.hostPlatform.isDarwin)
        guiPackages;

    targets.darwin.copyApps.enable = false;
    targets.darwin.linkApps.enable = false;

    programs.zsh.enable = true;
    programs.zsh.enableCompletion = true;
    programs.zsh.completionInit = ''
      if [[ -L /opt/homebrew/share/zsh/site-functions/_brew && ! -e /opt/homebrew/share/zsh/site-functions/_brew ]]; then
        fpath=(''${fpath:#/opt/homebrew/share/zsh/site-functions})
      fi

      dotfiles_zcompdump_dir="''${XDG_CACHE_HOME:-$HOME/.cache}/zsh"
      mkdir -p "$dotfiles_zcompdump_dir"
      autoload -U compinit && compinit -d "$dotfiles_zcompdump_dir/zcompdump-$ZSH_VERSION"
      unset dotfiles_zcompdump_dir
    '';
    programs.zsh.autosuggestion.enable = true;
    programs.zsh.syntaxHighlighting.enable = true;
    programs.zsh.initContent = ''
      function git-current-branch {
        local branch_name
        branch_name=$(git rev-parse --abbrev-ref HEAD 2>/dev/null)
        if [ -n "$branch_name" ]; then
          echo "%B%F{29}◀%f%K{29}%F{15} $branch_name %f%k%b"
        fi
      }

      setopt prompt_subst
      function prompt-machine-emoji {
        local checksum
        local -a emojis=(🥺 🥹 🥺ྀི 😎 😇 😘 🤗 🤔 🤖 🧠 🚀 🎧 🎮 🎹 🎨)

        checksum=$(printf '%s' "''${HOST:-$(hostname)}" | cksum)
        printf '%s' "$emojis[$(( ''${checksum%% *} % ''${#emojis[@]} + 1 ))]"
      }

      PROMPT_MACHINE_EMOJI="''${PROMPT_MACHINE_EMOJI:-$(prompt-machine-emoji)}"
      PROMPT='%F{33}%~%f `git-current-branch`
       ''${PROMPT_MACHINE_EMOJI}  ▶  '

      for dotfiles_hm_vars in \
        "$HOME/.nix-profile/etc/profile.d/hm-session-vars.sh" \
        "/etc/profiles/per-user/$USER/etc/profile.d/hm-session-vars.sh"
      do
        if [ -r "$dotfiles_hm_vars" ]; then
          . "$dotfiles_hm_vars"
        fi
      done
      unset dotfiles_hm_vars

      if command -v mise >/dev/null 2>&1; then
        eval "$(command mise activate zsh)"
      fi
    '';

    programs.neovim.enable = true;
    programs.neovim.defaultEditor = true;
    programs.neovim.viAlias = true;
    programs.neovim.vimAlias = true;
    programs.neovim.withPython3 = true;
    programs.neovim.withRuby = true;

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

    home.sessionVariables = {
      EDITOR = "nvim";
      XDG_CONFIG_HOME = "${homeDirectory}/.config";
      XDG_CACHE_HOME = "${homeDirectory}/.cache";
      XDG_DATA_HOME = "${homeDirectory}/.local/share";
      XDG_STATE_HOME = "${homeDirectory}/.local/state";
    };
  };
}
