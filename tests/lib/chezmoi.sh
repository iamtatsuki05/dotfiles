# Shared chezmoi resolution, fixture, and apply helpers for shell tests.

is_valid_executable_file() {
  local candidate="${1:-}"

  [[ "$candidate" == /* ]] || return 1
  [[ "$candidate" != *[[:cntrl:]]* ]] || return 1
  [[ -f "$candidate" && -x "$candidate" ]]
}

resolve_chezmoi() {
  local candidate mise_bin install_dir

  candidate="$(command -v chezmoi 2>/dev/null || true)"
  if is_valid_executable_file "$candidate"; then
    REPLY="$candidate"
    return 0
  fi

  if [[ -n "${HOME:-}" ]]; then
    candidate="$HOME/.local/bin/chezmoi"
    if is_valid_executable_file "$candidate"; then
      REPLY="$candidate"
      return 0
    fi
  fi

  mise_bin="$(command -v mise 2>/dev/null || true)"
  if is_valid_executable_file "$mise_bin"; then
    if install_dir="$("$mise_bin" where chezmoi@latest 2>/dev/null)"; then
      if [[ "$install_dir" == /* && "$install_dir" != *[[:cntrl:]]* ]]; then
        candidate="$install_dir/chezmoi"
        if is_valid_executable_file "$candidate"; then
          REPLY="$candidate"
          return 0
        fi
      fi
    fi
  fi

  return 127
}

file_digest() {
  if command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$1" | awk '{print $1}'
  else
    sha256sum "$1" | awk '{print $1}'
  fi
}

copy_source() {
  local root="$1"

  mkdir -p "$root/home/.chezmoitemplates" "$root/home/private_dot_config/shell" "$root/home/private_dot_config/fish/conf.d"
  cp "$REPO_ROOT/.chezmoiroot" "$root/.chezmoiroot"
  cp "$REPO_ROOT/home/.chezmoidata.toml" "$root/home/.chezmoidata.toml"
  cp "$REPO_ROOT/home/.chezmoitemplates/shell-data-validate" "$root/home/.chezmoitemplates/shell-data-validate"
  cp "$REPO_ROOT/home/.chezmoitemplates/bash_profile" "$root/home/.chezmoitemplates/bash_profile"
  cp "$REPO_ROOT/home/.chezmoitemplates/bashrc" "$root/home/.chezmoitemplates/bashrc"
  cp "$REPO_ROOT/home/.chezmoitemplates/dotfiles-shell-common.sh" "$root/home/.chezmoitemplates/dotfiles-shell-common.sh"
  cp "$REPO_ROOT/home/dot_bash_profile.tmpl" "$root/home/dot_bash_profile.tmpl"
  cp "$REPO_ROOT/home/dot_bashrc.tmpl" "$root/home/dot_bashrc.tmpl"
  cp "$REPO_ROOT/home/private_dot_config/shell/dotfiles-shell-common.sh.tmpl" "$root/home/private_dot_config/shell/"
  cp "$REPO_ROOT/home/private_dot_config/shell/dotfiles-shell-common.csh.tmpl" "$root/home/private_dot_config/shell/"
  cp "$REPO_ROOT/home/private_dot_config/fish/conf.d/zz-dotfiles.fish.tmpl" "$root/home/private_dot_config/fish/conf.d/"
  cp "$REPO_ROOT/home/private_dot_config/shell/create_private_secrets.env" "$root/home/private_dot_config/shell/"
}

write_fakes() {
  FAKE_BIN="$FIXTURE/bin"
  GCLOUD_LOG="$FIXTURE/gcloud.log"
  mkdir -p "$FAKE_BIN"
  : > "$GCLOUD_LOG"
  {
    print -r -- '#!/bin/sh'
    print -r -- 'set -eu'
    print -r -- "printf '%s\\n' \"\$*\" >> \"$GCLOUD_LOG\""
    print -r -- 'case "$*" in "init"|"auth login"|"compute instances list") exit 0 ;; *) exit 97 ;; esac'
  } > "$FAKE_BIN/gcloud"
  {
    print -r -- '#!/bin/sh'
    print -r -- 'set -eu'
    print -r -- 'if [ "${1:-}" != activate ]; then exit 97; fi'
    print -r -- "if [ \"\$2\" = bash ]; then printf '%s\\n' 'activate bash' >> \"$FIXTURE/mise.log\"; case \"\${FAKE_MISE_MODE:-success}\" in success) printf '%s\\n' 'DOTFILES_BASH_ACTIVATED=yes' ;; failure) exit 19 ;; empty) ;; invalid) printf '%s\\n' 'if' ;; esac; exit 0; fi"
    print -r -- "if [ \"\$2\" = zsh ]; then printf '%s\\n' 'activate zsh' >> \"$FIXTURE/mise.log\"; case \"\${FAKE_MISE_MODE:-success}\" in success) printf '%s\\n' 'DOTFILES_ZSH_ACTIVATED=yes' ;; failure) exit 19 ;; empty) ;; invalid) printf '%s\\n' 'if' ;; esac; exit 0; fi"
    print -r -- 'if [ "$2" != fish ]; then exit 97; fi'
    print -r -- "printf '%s\\n' 'activate fish' >> \"$FIXTURE/mise.log\""
    print -r -- 'case "${FAKE_MISE_MODE:-success}" in'
    print -r -- 'success) printf "%s\n" "set -gx DOTFILES_FISH_ACTIVATED yes" ;;'
    print -r -- 'failure) exit 19 ;; empty) ;; invalid) printf "%s\n" "if" ;; *) exit 98 ;; esac'
  } > "$FAKE_BIN/mise"
  chmod +x "$FAKE_BIN/gcloud" "$FAKE_BIN/mise"
  : > "$FIXTURE/mise.log"
}

run_chezmoi_apply() {
  local src="$1" dest="$2" root="$3" cache="$4" config="$5" state="$6" log="$7" profile="${8:-}"
  local tmp_root="${9:-${TMPDIR:-/tmp}}"
  local rc=0
  local -a env_args=(
    "HOME=$dest"
    "XDG_CONFIG_HOME=$dest/.config"
    "XDG_CACHE_HOME=$dest/.cache"
    "XDG_DATA_HOME=$dest/.local/share"
    "XDG_STATE_HOME=$dest/.local/state"
    "MISE_DATA_DIR=$dest/.local/share/mise"
    "TMPDIR=$tmp_root"
    "PATH=/bin:/usr/bin:/usr/sbin:/sbin"
  )

  [[ "$root" == __NO_OVERRIDE__ ]] || env_args+=("DOTFILES_REPO_ROOT=$root")
  [[ -z "$profile" ]] || env_args+=("DOTFILES_PROFILE=$profile")
  mkdir -p "$dest"
  : > "$config"
  env -i "${env_args[@]}" \
    "$CHEZMOI_BIN" -S "$src" -D "$dest" --cache "$cache" --config "$config" \
    --persistent-state "$state" --refresh-externals=never --force --no-tty apply > "$log" 2>&1 || rc=$?
  return "$rc"
}

run_apply() {
  local src="$1" dest="$2" root="$3" rc=0
  local config="$FIXTURE/chezmoi-$(basename "$dest").toml"
  local log="$FIXTURE/chezmoi-$(basename "$dest").log"

  mkdir -p "$FIXTURE/tmp"
  run_chezmoi_apply "$src" "$dest" "$root" "$FIXTURE/cache-$(basename "$dest")" "$config" \
    "$FIXTURE/state-$(basename "$dest").boltdb" "$log" "" "$FIXTURE/tmp" || rc=$?
  (( rc == 0 )) || { sed -n '1,100p' "$log" >&2; return "$rc"; }
}
