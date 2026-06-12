{ lib, ... }:

{
  programs.zsh.enable = true;
  programs.zsh.enableCompletion = true;
  programs.zsh.autosuggestion.enable = true;
  programs.zsh.syntaxHighlighting.enable = true;
  programs.zsh.oh-my-zsh.enable = true;
  programs.zsh.oh-my-zsh.theme = "candy";
  programs.zsh.oh-my-zsh.plugins = [ "git" ];

  # oh-my-zsh は初期化中に自前で compinit を呼ぶため、Home Manager は
  # oh-my-zsh 有効時に completionInit を .zshrc に出力しない。補完パスの
  # 調整は compinit より前(mkOrder 550 = before completion init)で行い、
  # compinit と zcompdump の位置は oh-my-zsh に委ねる。
  programs.zsh.initContent = lib.mkMerge [
    (lib.mkOrder 550 ''
      if [[ -L /opt/homebrew/share/zsh/site-functions/_brew && ! -e /opt/homebrew/share/zsh/site-functions/_brew ]]; then
        fpath=(''${fpath:#/opt/homebrew/share/zsh/site-functions})
      fi
      # Shared Linuxbrew completions are not owned by the current user or root on
      # some NAIST hosts, so zsh compaudit treats them as insecure.
      fpath=(''${fpath:#/home/linuxbrew/.linuxbrew/share/zsh/site-functions})
      fpath=(''${fpath:#/home/linuxbrew/.linuxbrew/share/zsh-completions})

      for dotfiles_completion_dir in \
        "$HOME/.linuxbrew/share/zsh/site-functions" \
        "$HOME/.linuxbrew/share/zsh-completions" \
        "/opt/homebrew/share/zsh/site-functions" \
        "/usr/local/share/zsh/site-functions"
      do
        if [[ -d "$dotfiles_completion_dir" ]]; then
          fpath=("$dotfiles_completion_dir" $fpath)
        fi
      done
      unset dotfiles_completion_dir
    '')

    # oh-my-zsh 読み込み後に適用する(テーマを独自 PROMPT で上書きするため mkAfter)。
    (lib.mkAfter ''
      setopt mark_dirs
      setopt correct
      setopt correct_all
      setopt noautoremoveslash
      setopt list_packed

      zstyle ':completion:*:sudo:*' command-path /usr/local/sbin /usr/local/bin /usr/sbin /usr/bin /sbin /bin /usr/X11R6/bin
      zstyle ':completion:*:processes' command 'ps x -o pid,s,args'
      zstyle ':completion:*' menu select

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

      dotfiles_shell_common="''${XDG_CONFIG_HOME:-$HOME/.config}/shell/dotfiles-shell-common.sh"
      if [ -r "$dotfiles_shell_common" ]; then
        . "$dotfiles_shell_common"
      fi
      unset dotfiles_shell_common

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
    '')
  ];
}
