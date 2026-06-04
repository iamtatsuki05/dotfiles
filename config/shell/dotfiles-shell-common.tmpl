export DOTFILES_REPO_ROOT="${DOTFILES_REPO_ROOT:-__DOTFILES_REPO_ROOT__}"

dotfiles_prepend_path() {
  local candidate="$1"

  if [ ! -d "$candidate" ]; then
    return 0
  fi

  case ":$PATH:" in
    *":$candidate:"*)
      ;;
    *)
      PATH="$candidate:$PATH"
      ;;
  esac
}

if [ -d "/opt/homebrew/bin" ]; then
  dotfiles_prepend_path "/opt/homebrew/sbin"
  dotfiles_prepend_path "/opt/homebrew/bin"
elif [ -d "$HOME/.linuxbrew/bin" ]; then
  dotfiles_prepend_path "$HOME/.linuxbrew/sbin"
  dotfiles_prepend_path "$HOME/.linuxbrew/bin"
elif [ -d "/home/linuxbrew/.linuxbrew/bin" ]; then
  dotfiles_prepend_path "/home/linuxbrew/.linuxbrew/sbin"
  dotfiles_prepend_path "/home/linuxbrew/.linuxbrew/bin"
fi

dotfiles_prepend_path "$HOME/.local/bin"
dotfiles_prepend_path "$HOME/.nix-profile/bin"
dotfiles_prepend_path "/etc/profiles/per-user/$USER/bin"
dotfiles_prepend_path "/run/current-system/sw/bin"
dotfiles_prepend_path "/nix/var/nix/profiles/default/bin"
dotfiles_prepend_path "${XDG_STATE_HOME:-$HOME/.local/state}/nix/profile/bin"
export PATH

for dotfiles_hm_vars in \
  "$HOME/.nix-profile/etc/profile.d/hm-session-vars.sh" \
  "/etc/profiles/per-user/$USER/etc/profile.d/hm-session-vars.sh"
do
  if [ -r "$dotfiles_hm_vars" ]; then
    . "$dotfiles_hm_vars"
  fi
done
unset dotfiles_hm_vars

if [ -r "$HOME/.nix-profile/etc/profile.d/z.sh" ]; then
  . "$HOME/.nix-profile/etc/profile.d/z.sh"
fi

dotfiles_shell_name=sh
dotfiles_shell_bin=/bin/sh

if [ -n "${ZSH_VERSION:-}" ]; then
  dotfiles_shell_name=zsh
  dotfiles_shell_bin=/bin/zsh
elif [ -n "${BASH_VERSION:-}" ]; then
  dotfiles_shell_name=bash
  dotfiles_shell_bin=/bin/bash
fi

if [ "$(uname -s)" = "Darwin" ]; then
  alias intel="env /usr/bin/arch -x86_64 $dotfiles_shell_bin -l"
  alias arm="env /usr/bin/arch -arm64 $dotfiles_shell_bin -l"
fi

dotfiles_is_in_git_repo() {
  git rev-parse --is-inside-work-tree >/dev/null 2>&1
}

dotfiles_fzf_down() {
  if command -v fzf-down >/dev/null 2>&1; then
    fzf-down "$@"
  else
    fzf "$@"
  fi
}

gt() {
  dotfiles_is_in_git_repo || return
  git tag --sort -version:refname |
    dotfiles_fzf_down --multi --preview-window right:70% \
      --preview 'git show --color=always {} | head -200'
}

gr() {
  dotfiles_is_in_git_repo || return
  git remote -v | awk '{print $1 "\t" $2}' | uniq |
    dotfiles_fzf_down --tac \
      --preview 'git log --oneline --graph --date=short --pretty="format:%C(auto)%cd %h%d %s" {1} | head -200' |
    cut -d'	' -f1
}

gs() {
  dotfiles_is_in_git_repo || return
  git stash list |
    dotfiles_fzf_down --reverse -d: --preview 'git show --color=always {1}' |
    cut -d: -f1
}

if [ "$dotfiles_shell_name" = "zsh" ]; then
  _ssh() {
    compadd $(fgrep 'Host ' ~/.ssh/config 2>/dev/null | awk '{print $2}' | sort)
  }
fi

dotfiles_add_default_ssh_key() {
  local key_path="$HOME/.ssh/id_rsa"

  case "$-" in
    *i*) ;;
    *) return 0 ;;
  esac

  if [ -r "$key_path" ] && [ -S "${SSH_AUTH_SOCK:-}" ]; then
    ssh-add -q "$key_path" 2>/dev/null || true
  fi
}

dotfiles_add_default_ssh_key
unset -f dotfiles_add_default_ssh_key

fgcp() {
  local configuration

  configuration="$(
    gcloud config configurations list |
      awk '{ print $1,$3,$4 }' |
      column -t |
      fzf --header-lines=1 |
      awk '{ print $1 }'
  )"

  if [ -n "$configuration" ]; then
    gcloud config configurations activate "$configuration"
  fi
}

fgcc() {
  local host

  for host in $(
    gcloud compute instances list |
      fzf --header-lines=1 |
      awk '{ print $1"@"$2 }'
  ); do
    gcloud compute ssh \
      --zone "${host##*@}" "${host%%@*}" \
      --tunnel-through-iap \
      --ssh-flag="-A"
  done
}

fgcc_rinit() {
  local host

  for host in $(
    gcloud compute instances list |
      fzf --header-lines=1 |
      awk '{ print $1"@"$2 }'
  ); do
    gcloud compute ssh \
      --zone "${host##*@}" "${host%%@*}" \
      --tunnel-through-iap \
      --dry-run
  done
}

fgcc_p() {
  local port="${1:-}"
  local host

  if [ -z "$port" ]; then
    echo "usage: fgcc_p <local-port>" >&2
    return 2
  fi

  for host in $(
    gcloud compute instances list |
      fzf --header-lines=1 |
      awk '{ print $1"@"$2 }'
  ); do
    gcloud compute ssh \
      --zone "${host##*@}" "${host%%@*}" \
      --tunnel-through-iap \
      --ssh-flag="-A" \
      --ssh-flag="-L ${port}:localhost:${port}"
  done
}

gstop_instance() {
  gcloud compute instances stop "$@"
}

gstart_instance() {
  gcloud compute instances start "$@"
}

gdelete_instance() {
  gcloud compute instances delete "$@"
}

fgrs() {
  local host

  for host in $(
    gcloud compute instances list |
      fzf --header-lines=1 |
      awk '{ print $1"@"$2 }'
  ); do
    gstop_instance --zone "${host##*@}" "${host%%@*}"
    gstart_instance --zone "${host##*@}" "${host%%@*}"
  done
}

alias ginit='gcloud init'
alias gauth='gcloud auth login'
alias gls='gcloud compute instances list'

if command -v claude >/dev/null 2>&1; then
  alias claude-auto='claude --dangerously-skip-permissions'
fi

if command -v mise >/dev/null 2>&1; then
  if [ "$dotfiles_shell_name" = "bash" ]; then
    eval "$(command mise activate "$dotfiles_shell_name")"
  fi
fi

if [ -r "${XDG_CONFIG_HOME:-$HOME/.config}/shell/secrets.env" ]; then
  . "${XDG_CONFIG_HOME:-$HOME/.config}/shell/secrets.env"
fi
