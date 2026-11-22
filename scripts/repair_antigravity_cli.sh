#!/usr/bin/env bash
set -euo pipefail

prefix="$(brew --prefix)"
target="$prefix/bin/agy"
caskroom="$(brew --caskroom antigravity-cli)"

has_cask_link() {
  [[ -L "$target" && -x "$target" && -d "$caskroom" && "$(realpath "$target")" == "$(realpath "$caskroom")/"* ]]
}

if has_cask_link; then
  brew list --cask --versions antigravity-cli
  echo "Homebrew already manages $target"
  exit 0
fi

if [[ -e "$target" || -L "$target" ]]; then
  if [[ -L "$target" || ! -f "$target" || ! -x "$target" ]] ||
     ! codesign --verify --strict -R='anchor apple generic and certificate leaf[subject.OU] = "EQHXZ8M8AV" and identifier "cli"' "$target"; then
    echo "Cannot identify $target as the Google-signed legacy agy executable." >&2
    exit 1
  fi

  backup="$(mktemp -d "$prefix/var/agy-repair.XXXXXX")/agy"
  echo "Saving the existing executable to $backup"
  restore_on_failure() {
    if [[ -f "$backup" && ! -e "$target" && ! -L "$target" ]]; then
      mv -n "$backup" "$target"
      [[ -f "$backup" ]] || rmdir "$(dirname "$backup")"
    fi
  }
  trap restore_on_failure EXIT
  mv "$target" "$backup"
fi

brew install --cask antigravity-cli
if ! has_cask_link; then
  echo "Expected $target to link to an executable under $caskroom." >&2
  exit 1
fi
