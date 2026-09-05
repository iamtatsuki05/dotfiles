{{- $dotfilesRepoRoot := .dotfilesRepoRoot -}}
{{- if not (kindIs "map" $dotfilesRepoRoot) }}{{ fail "invalid DOTFILES_REPO_ROOT template context" }}{{ end -}}
{{- if not (kindIs "string" $dotfilesRepoRoot.raw) }}{{ fail "invalid DOTFILES_REPO_ROOT raw template context" }}{{ end -}}
{{- if not (kindIs "string" $dotfilesRepoRoot.prequoted) }}{{ fail "invalid DOTFILES_REPO_ROOT quoted template context" }}{{ end -}}
{{- $shell := .shell -}}
{{- includeTemplate ".chezmoitemplates/shell-data-validate" (dict "shell" $shell "features" .features) -}}
if [ -z "${DOTFILES_REPO_ROOT:-}" ]; then
  DOTFILES_REPO_ROOT={{ $dotfilesRepoRoot.prequoted }}
fi
export DOTFILES_REPO_ROOT

dotfiles_saved_editor=${EDITOR:-}
dotfiles_saved_xdg_config_home=${XDG_CONFIG_HOME:-}
dotfiles_saved_xdg_cache_home=${XDG_CACHE_HOME:-}
dotfiles_saved_xdg_data_home=${XDG_DATA_HOME:-}
dotfiles_saved_xdg_state_home=${XDG_STATE_HOME:-}

for dotfiles_hm_vars in \
  "$HOME/.nix-profile/etc/profile.d/hm-session-vars.sh"
do
  if [ -r "$dotfiles_hm_vars" ]; then
    . "$dotfiles_hm_vars"
  fi
done
if [ -n "${USER:-}" ]; then
  dotfiles_hm_vars="{{ shellQuote .shell.path.nix_user_profile_root }}/$USER/etc/profile.d/hm-session-vars.sh"
  if [ -r "$dotfiles_hm_vars" ]; then
    . "$dotfiles_hm_vars"
  fi
fi
unset dotfiles_hm_vars

if [ -n "$dotfiles_saved_editor" ]; then
  EDITOR="$dotfiles_saved_editor"
fi
if [ -n "$dotfiles_saved_xdg_config_home" ]; then
  XDG_CONFIG_HOME="$dotfiles_saved_xdg_config_home"
fi
if [ -n "$dotfiles_saved_xdg_cache_home" ]; then
  XDG_CACHE_HOME="$dotfiles_saved_xdg_cache_home"
fi
if [ -n "$dotfiles_saved_xdg_data_home" ]; then
  XDG_DATA_HOME="$dotfiles_saved_xdg_data_home"
fi
if [ -n "$dotfiles_saved_xdg_state_home" ]; then
  XDG_STATE_HOME="$dotfiles_saved_xdg_state_home"
fi
unset dotfiles_saved_editor dotfiles_saved_xdg_config_home dotfiles_saved_xdg_cache_home \
  dotfiles_saved_xdg_data_home dotfiles_saved_xdg_state_home

export EDITOR="${EDITOR:-{{ shellQuote .shell.editor }}}"
export XDG_CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/{{ shellQuote .shell.xdg.config }}}"
export XDG_CACHE_HOME="${XDG_CACHE_HOME:-$HOME/{{ shellQuote .shell.xdg.cache }}}"
export XDG_DATA_HOME="${XDG_DATA_HOME:-$HOME/{{ shellQuote .shell.xdg.data }}}"
export XDG_STATE_HOME="${XDG_STATE_HOME:-$HOME/{{ shellQuote .shell.xdg.state }}}"
if [ -z "${MISE_GLOBAL_CONFIG_FILE:-}" ] && [ -r "$HOME/.config/mise/config.toml" ]; then
  export MISE_GLOBAL_CONFIG_FILE="$HOME/.config/mise/config.toml"
fi

dotfiles_prepend_path() {
  local candidate="$1"

  if [ ! -d "$candidate" ]; then
    return 0
  fi

  case ":${PATH:-}:" in
    *":$candidate:"*)
      ;;
    *)
      PATH="$candidate${PATH:+:$PATH}"
      ;;
  esac
}

dotfiles_macos_arch=""
if [ "$(uname -s)" = "Darwin" ]; then
{{- if .features.macos }}
  dotfiles_macos_arch="$(uname -m)"
  if [ "$dotfiles_macos_arch" = "arm64" ] && [ -d "{{ shellQuote .shell.path.darwin_arm64_homebrew_bin }}" ]; then
    dotfiles_prepend_path "{{ shellQuote .shell.path.darwin_arm64_homebrew_sbin }}"
    dotfiles_prepend_path "{{ shellQuote .shell.path.darwin_arm64_homebrew_bin }}"
  elif [ "$dotfiles_macos_arch" = "x86_64" ] && [ -d "{{ shellQuote .shell.path.darwin_x86_64_homebrew_bin }}" ]; then
    dotfiles_prepend_path "{{ shellQuote .shell.path.darwin_x86_64_homebrew_sbin }}"
    dotfiles_prepend_path "{{ shellQuote .shell.path.darwin_x86_64_homebrew_bin }}"
  elif [ -d "{{ shellQuote .shell.path.darwin_arm64_homebrew_bin }}" ]; then
    dotfiles_prepend_path "{{ shellQuote .shell.path.darwin_arm64_homebrew_sbin }}"
    dotfiles_prepend_path "{{ shellQuote .shell.path.darwin_arm64_homebrew_bin }}"
  elif [ -d "{{ shellQuote .shell.path.darwin_x86_64_homebrew_bin }}" ]; then
    dotfiles_prepend_path "{{ shellQuote .shell.path.darwin_x86_64_homebrew_sbin }}"
    dotfiles_prepend_path "{{ shellQuote .shell.path.darwin_x86_64_homebrew_bin }}"
  fi
{{- else }}
  :
{{- end }}
else
  if [ -d "$HOME/{{ shellQuote .shell.path.linuxbrew_user_bin }}" ]; then
    dotfiles_prepend_path "$HOME/{{ shellQuote .shell.path.linuxbrew_user_sbin }}"
    dotfiles_prepend_path "$HOME/{{ shellQuote .shell.path.linuxbrew_user_bin }}"
  elif [ -d "{{ shellQuote .shell.path.linuxbrew_system_bin }}" ]; then
    dotfiles_prepend_path "{{ shellQuote .shell.path.linuxbrew_system_sbin }}"
    dotfiles_prepend_path "{{ shellQuote .shell.path.linuxbrew_system_bin }}"
  fi
fi
unset dotfiles_macos_arch

dotfiles_prepend_path "$HOME/{{ shellQuote .shell.path.home_local_bin }}"
dotfiles_prepend_path "$HOME/{{ shellQuote .shell.path.home_nix_profile_bin }}"
if [ -n "${USER:-}" ]; then
  dotfiles_prepend_path "{{ shellQuote .shell.path.nix_user_profile_root }}/$USER/{{ shellQuote .shell.path.nix_user_profile_bin }}"
fi
dotfiles_prepend_path "{{ shellQuote .shell.path.nix_system_bin }}"
dotfiles_prepend_path "{{ shellQuote .shell.path.nix_default_bin }}"
dotfiles_prepend_path "${XDG_STATE_HOME:-$HOME/{{ shellQuote .shell.xdg.state }}}/{{ shellQuote .shell.path.nix_state_relative }}"
export PATH

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

{{- if .features.macos }}
if [ "$(uname -s)" = "Darwin" ]; then
  alias intel="env /usr/bin/arch -x86_64 $dotfiles_shell_bin -l"
  alias arm="env /usr/bin/arch -arm64 $dotfiles_shell_bin -l"
fi
{{- end }}

dotfiles_is_in_git_repo() {
  git rev-parse --is-inside-work-tree >/dev/null 2>&1
}

unalias gt gr gs 2>/dev/null || true

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

dotfiles_add_default_ssh_key() {
  local key_path

  case "$-" in
    *i*) ;;
    *) return 0 ;;
  esac

  [ -S "${SSH_AUTH_SOCK:-}" ] || return 0

  for key_path in "$HOME/.ssh/id_ed25519" "$HOME/.ssh/id_rsa"; do
    if [ -r "$key_path" ]; then
      ssh-add -q "$key_path" 2>/dev/null || true
    fi
  done
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

alias ginit={{ shellQuote (printf "%s %s" (index .shell.aliases.ginit 0) (index .shell.aliases.ginit 1)) }}
alias gauth={{ shellQuote (printf "%s %s %s" (index .shell.aliases.gauth 0) (index .shell.aliases.gauth 1) (index .shell.aliases.gauth 2)) }}
alias gls={{ shellQuote (printf "%s %s %s %s" (index .shell.aliases.gls 0) (index .shell.aliases.gls 1) (index .shell.aliases.gls 2) (index .shell.aliases.gls 3)) }}

if command -v claude >/dev/null 2>&1; then
  alias claude-auto='claude --dangerously-skip-permissions'
fi

claude-account() {
  "$DOTFILES_REPO_ROOT/scripts/claude_account.sh" "$@"
}

if [ -z "${dotfiles_mise_activation_bash+x}" ]; then
  dotfiles_mise_activation_bash=0
fi
if [ -z "${dotfiles_mise_activation_zsh+x}" ]; then
  dotfiles_mise_activation_zsh=0
fi
typeset +x dotfiles_mise_activation_bash dotfiles_mise_activation_zsh 2>/dev/null || true

dotfiles_mise_activate_posix() {
  local dotfiles_mise_output dotfiles_mise_status

  case "$-" in
    *i*) ;;
    *) return 0 ;;
  esac

  case "$dotfiles_shell_name" in
    bash)
      [ "$dotfiles_mise_activation_bash" -eq 0 ] || return 0
      dotfiles_mise_activation_bash=1
      ;;
    zsh)
      [ "$dotfiles_mise_activation_zsh" -eq 0 ] || return 0
      dotfiles_mise_activation_zsh=1
      ;;
    *)
      return 0
      ;;
  esac

  if ! command -v mise >/dev/null 2>&1; then
    return 0
  fi

  dotfiles_mise_output="$(command mise activate "$dotfiles_shell_name")" || {
    dotfiles_mise_status=$?
    echo "dotfiles: mise activate $dotfiles_shell_name failed" >&2
    return "$dotfiles_mise_status"
  }
  if [ -z "$dotfiles_mise_output" ]; then
    echo "dotfiles: mise activate $dotfiles_shell_name returned no hook" >&2
    return 1
  fi
  eval "$dotfiles_mise_output" || {
    dotfiles_mise_status=$?
    echo "dotfiles: mise activate $dotfiles_shell_name hook failed" >&2
    return "$dotfiles_mise_status"
  }
}

dotfiles_mise_activation_status=0
dotfiles_mise_activate_posix || dotfiles_mise_activation_status=$?

dotfiles_secrets_status=0
if [ -r "$HOME/.config/shell/secrets.env" ]; then
  . "$HOME/.config/shell/secrets.env" || dotfiles_secrets_status=$?
fi

if [ "$dotfiles_mise_activation_status" -ne 0 ]; then
  return "$dotfiles_mise_activation_status"
fi
if [ "$dotfiles_secrets_status" -ne 0 ]; then
  return "$dotfiles_secrets_status"
fi
