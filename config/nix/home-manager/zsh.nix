{ ... }:

{
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
}
