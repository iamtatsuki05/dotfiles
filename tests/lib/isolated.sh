# Run side-effect fixtures with a kernel-enforced write and network boundary.
run_isolated() {
  local fixture_root="$1"
  shift

  if [[ "$(uname -s)" != Darwin || ! -x /usr/bin/sandbox-exec ]]; then
    printf 'ERROR: these macOS side-effect fixtures require sandbox-exec\n' >&2
    return 125
  fi
  fixture_root="$(cd "$fixture_root" && pwd -P)" || return 125
  case "$fixture_root" in
    /private/tmp/*|/private/var/folders/*/T/*) ;;
    *)
      printf 'ERROR: sandbox write root must be a dedicated temporary directory\n' >&2
      return 125
      ;;
  esac
  if [[ ( "$1" == env || "$1" == /usr/bin/env ) && "${2:-}" == -i ]]; then
    shift 2
    set -- /usr/bin/env -i "TMPDIR=$fixture_root" "TMPPREFIX=$fixture_root/zsh" "$@"
  else
    set -- /usr/bin/env "TMPDIR=$fixture_root" "TMPPREFIX=$fixture_root/zsh" "$@"
  fi
  /usr/bin/sandbox-exec -D "FIXTURE_ROOT=$fixture_root" -p '
    (version 1)
    (allow default)
    (deny network*)
    (deny file-write*
      (require-all
        (require-not (subpath (param "FIXTURE_ROOT")))
        (require-not (literal "/dev/null"))
        (require-not (literal "/dev/tty"))))
    (deny process-exec
      (literal "/usr/bin/sudo")
      (literal "/usr/bin/osascript")
      (literal "/usr/bin/launchctl")
      (literal "/bin/launchctl")
      (literal "/usr/bin/defaults")
      (literal "/usr/bin/dscl")
      (literal "/usr/bin/security")
      (literal "/usr/bin/open")
      (literal "/usr/bin/killall")
      (literal "/usr/sbin/diskutil")
      (literal "/usr/sbin/softwareupdate")
      (literal "/usr/sbin/networksetup")
      (literal "/usr/bin/tmutil")
      (literal "/usr/bin/mdutil")
      (literal "/opt/homebrew/bin/brew")
      (literal "/usr/local/bin/brew"))
  ' "$@"
}
