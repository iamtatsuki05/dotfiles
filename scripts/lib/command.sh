#!/usr/bin/env zsh

dotfiles_print_command() {
  printf '  '
  printf '%q ' "$@"
  printf '\n'
}

dotfiles_print_raw_command_block() {
  local title="$1"
  local command_line="$2"

  print -r -- "$title"
  print -r -- "  $command_line"
}

dotfiles_run_or_print() {
  local apply="$1"
  shift

  if (( apply )); then
    "$@"
    return 0
  fi

  dotfiles_print_command "$@"
}
