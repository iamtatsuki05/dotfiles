{{- $dotfilesRepoRoot := .dotfilesRepoRoot -}}
{{- if not (kindIs "map" $dotfilesRepoRoot) }}{{ fail "invalid DOTFILES_REPO_ROOT template context" }}{{ end -}}
{{- if not (kindIs "string" $dotfilesRepoRoot.raw) }}{{ fail "invalid DOTFILES_REPO_ROOT raw template context" }}{{ end -}}
{{- if not (kindIs "string" $dotfilesRepoRoot.prequoted) }}{{ fail "invalid DOTFILES_REPO_ROOT quoted template context" }}{{ end -}}
{{- $shell := .shell -}}
{{- $safeWord := "^[A-Za-z0-9._+%:@/-]+$" -}}
{{- $safeXdgPath := "^\\.[A-Za-z0-9._/-]+$" -}}
{{- $safeRelativePath := "^[A-Za-z0-9._/-]+$" -}}
{{- $safeAbsolutePath := "^/[A-Za-z0-9._/-]+$" -}}
{{- if not (kindIs "map" $shell) }}{{ fail "invalid canonical shell data" }}{{ end -}}
{{- if not (kindIs "string" $shell.editor) }}{{ fail "invalid canonical shell editor" }}{{ end -}}
{{- if not (regexMatch "^[A-Za-z0-9._+-]+$" $shell.editor) }}{{ fail "invalid canonical shell editor" }}{{ end -}}
{{- if not (kindIs "map" $shell.xdg) }}{{ fail "invalid canonical shell XDG data" }}{{ end -}}
{{- range $name := (list "config" "cache" "data" "state") -}}
  {{- $value := index $shell.xdg $name -}}
  {{- if not (kindIs "string" $value) }}{{ fail "invalid canonical shell XDG value" }}{{ end -}}
  {{- if not (regexMatch $safeXdgPath $value) }}{{ fail "invalid canonical shell XDG value" }}{{ end -}}
  {{- if or (contains ".." $value) (contains "//" $value) (contains ":" $value) }}{{ fail "invalid canonical shell XDG value" }}{{ end -}}
{{- end -}}
{{- if not (kindIs "map" $shell.path) }}{{ fail "invalid canonical shell PATH data" }}{{ end -}}
{{- range $name := (list "home_relative" "darwin_homebrew" "linuxbrew_user_relative" "linuxbrew_system" "absolute") -}}
  {{- $values := index $shell.path $name -}}
  {{- if not (kindIs "slice" $values) }}{{ fail "invalid canonical shell PATH list" }}{{ end -}}
  {{- if ne (len $values) 2 }}{{ fail "invalid canonical shell PATH list" }}{{ end -}}
  {{- if ne (len (uniq $values)) 2 }}{{ fail "invalid canonical shell PATH list" }}{{ end -}}
  {{- range $value := $values -}}
    {{- if not (kindIs "string" $value) }}{{ fail "invalid canonical shell PATH value" }}{{ end -}}
    {{- if or (eq $name "home_relative") (eq $name "linuxbrew_user_relative") }}
      {{- if not (regexMatch $safeRelativePath $value) }}{{ fail "invalid canonical shell relative PATH" }}{{ end -}}
      {{- if or (contains ".." $value) (contains "//" $value) (contains ":" $value) (hasPrefix "/" $value) (hasPrefix "~" $value) }}{{ fail "invalid canonical shell relative PATH" }}{{ end -}}
    {{- else }}
      {{- if not (regexMatch $safeAbsolutePath $value) }}{{ fail "invalid canonical shell absolute PATH" }}{{ end -}}
      {{- if or (contains ".." $value) (contains "//" $value) (contains ":" $value) }}{{ fail "invalid canonical shell absolute PATH" }}{{ end -}}
    {{- end -}}
  {{- end -}}
{{- end -}}
{{- $profileRoot := $shell.path.user_profile_root -}}
{{- if not (kindIs "string" $profileRoot) }}{{ fail "invalid canonical shell profile root" }}{{ end -}}
{{- if not (regexMatch $safeAbsolutePath $profileRoot) }}{{ fail "invalid canonical shell profile root" }}{{ end -}}
{{- if or (contains ".." $profileRoot) (contains "//" $profileRoot) (contains ":" $profileRoot) }}{{ fail "invalid canonical shell profile root" }}{{ end -}}
{{- $profileSuffix := $shell.path.user_profile_suffix -}}
{{- if not (kindIs "string" $profileSuffix) }}{{ fail "invalid canonical shell profile suffix" }}{{ end -}}
{{- if not (regexMatch $safeRelativePath $profileSuffix) }}{{ fail "invalid canonical shell profile suffix" }}{{ end -}}
{{- if or (contains ".." $profileSuffix) (contains "//" $profileSuffix) (contains ":" $profileSuffix) (hasPrefix "/" $profileSuffix) (hasPrefix "~" $profileSuffix) }}{{ fail "invalid canonical shell profile suffix" }}{{ end -}}
{{- $stateRelative := $shell.path.state_relative -}}
{{- if not (kindIs "string" $stateRelative) }}{{ fail "invalid canonical shell state PATH" }}{{ end -}}
{{- if not (regexMatch $safeRelativePath $stateRelative) }}{{ fail "invalid canonical shell state PATH" }}{{ end -}}
{{- if or (contains ".." $stateRelative) (contains "//" $stateRelative) (contains ":" $stateRelative) (hasPrefix "/" $stateRelative) (hasPrefix "~" $stateRelative) }}{{ fail "invalid canonical shell state PATH" }}{{ end -}}
{{- if not (kindIs "map" $shell.aliases) }}{{ fail "invalid canonical shell aliases" }}{{ end -}}
{{- range $name := (list "ginit" "gauth" "gls") -}}
  {{- if not (hasKey $shell.aliases $name) }}{{ fail "invalid canonical shell alias set" }}{{ end -}}
{{- end -}}
{{- range $name, $argv := $shell.aliases -}}
  {{- if not (regexMatch "^[A-Za-z][A-Za-z0-9_-]*$" $name) }}{{ fail "invalid canonical shell alias name" }}{{ end -}}
  {{- $expectedLength := index (dict "ginit" 2 "gauth" 3 "gls" 4) $name -}}
  {{- if not $expectedLength }}{{ fail "invalid canonical shell alias name" }}{{ end -}}
  {{- if not (kindIs "slice" $argv) }}{{ fail "invalid canonical shell alias argv" }}{{ end -}}
  {{- if ne (len $argv) $expectedLength }}{{ fail "invalid canonical shell alias argv" }}{{ end -}}
  {{- range $value := $argv -}}
    {{- if not (kindIs "string" $value) }}{{ fail "invalid canonical shell alias argv" }}{{ end -}}
    {{- if not (regexMatch $safeWord $value) }}{{ fail "invalid canonical shell alias argv" }}{{ end -}}
  {{- end -}}
{{- end -}}
if [ -z "${DOTFILES_REPO_ROOT:-}" ]; then
  DOTFILES_REPO_ROOT={{ $dotfilesRepoRoot.prequoted }}
fi
export DOTFILES_REPO_ROOT
export EDITOR="${EDITOR:-{{ shellQuote .shell.editor }}}"
export XDG_CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/{{ shellQuote .shell.xdg.config }}}"
export XDG_CACHE_HOME="${XDG_CACHE_HOME:-$HOME/{{ shellQuote .shell.xdg.cache }}}"
export XDG_DATA_HOME="${XDG_DATA_HOME:-$HOME/{{ shellQuote .shell.xdg.data }}}"
export XDG_STATE_HOME="${XDG_STATE_HOME:-$HOME/{{ shellQuote .shell.xdg.state }}}"

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

if [ -d "{{ shellQuote (index .shell.path.darwin_homebrew 1) }}" ]; then
  dotfiles_prepend_path "{{ shellQuote (index .shell.path.darwin_homebrew 0) }}"
  dotfiles_prepend_path "{{ shellQuote (index .shell.path.darwin_homebrew 1) }}"
elif [ -d "$HOME/{{ shellQuote (index .shell.path.linuxbrew_user_relative 1) }}" ]; then
  dotfiles_prepend_path "$HOME/{{ shellQuote (index .shell.path.linuxbrew_user_relative 0) }}"
  dotfiles_prepend_path "$HOME/{{ shellQuote (index .shell.path.linuxbrew_user_relative 1) }}"
elif [ -d "{{ shellQuote (index .shell.path.linuxbrew_system 1) }}" ]; then
  dotfiles_prepend_path "{{ shellQuote (index .shell.path.linuxbrew_system 0) }}"
  dotfiles_prepend_path "{{ shellQuote (index .shell.path.linuxbrew_system 1) }}"
fi

dotfiles_prepend_path "$HOME/{{ shellQuote (index .shell.path.home_relative 0) }}"
{{- if eq (index .shell.path.home_relative 1) ".nix-profile/bin" }}
dotfiles_prepend_path "$HOME/.nix-profile/bin"
{{- else }}
dotfiles_prepend_path "$HOME/{{ shellQuote (index .shell.path.home_relative 1) }}"
{{- end }}
if [ -n "${USER:-}" ]; then
  dotfiles_prepend_path "{{ shellQuote .shell.path.user_profile_root }}/$USER/{{ shellQuote .shell.path.user_profile_suffix }}"
fi
dotfiles_prepend_path "{{ shellQuote (index .shell.path.absolute 0) }}"
dotfiles_prepend_path "{{ shellQuote (index .shell.path.absolute 1) }}"
dotfiles_prepend_path "${XDG_STATE_HOME:-$HOME/{{ shellQuote .shell.xdg.state }}}/{{ shellQuote .shell.path.state_relative }}"
export PATH

for dotfiles_hm_vars in \
  "$HOME/.nix-profile/etc/profile.d/hm-session-vars.sh" \
  "{{ shellQuote .shell.path.user_profile_root }}/$USER/etc/profile.d/hm-session-vars.sh"
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

if [ "$dotfiles_shell_name" = "zsh" ]; then
  _ssh() {
    compadd $(fgrep 'Host ' ~/.ssh/config 2>/dev/null | awk '{print $2}' | sort)
  }
fi

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

if command -v mise >/dev/null 2>&1; then
  if [ "$dotfiles_shell_name" = "bash" ]; then
    eval "$(command mise activate "$dotfiles_shell_name")"
  fi
fi

if [ -r "${XDG_CONFIG_HOME:-$HOME/.config}/shell/secrets.env" ]; then
  . "${XDG_CONFIG_HOME:-$HOME/.config}/shell/secrets.env"
fi
