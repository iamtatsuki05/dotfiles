#!/usr/bin/env zsh

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

assert_file() {
  local file_path="$1"
  [[ -f "$file_path" ]] || fail "expected file: $file_path"
}

assert_executable() {
  local file_path="$1"
  [[ -x "$file_path" ]] || fail "expected executable file: $file_path"
}

assert_not_exists() {
  local target_path="$1"
  [[ ! -e "$target_path" ]] || fail "expected path not to exist: $target_path"
}

assert_same_file() {
  local expected="$1"
  local actual="$2"

  assert_file "$expected"
  assert_file "$actual"
  cmp "$expected" "$actual" >/dev/null || fail "expected $actual to match $expected"
}

assert_contains() {
  local file_path="$1"
  local expected="$2"

  assert_file "$file_path"
  grep -Fq -- "$expected" "$file_path" || fail "expected $file_path to contain: $expected"
}

assert_not_contains() {
  local file_path="$1"
  local unexpected="$2"

  assert_file "$file_path"
  ! grep -Fq -- "$unexpected" "$file_path" || fail "expected $file_path not to contain: $unexpected"
}

assert_file_content() {
  local file_path="$1"
  local expected="$2"

  assert_file "$file_path"
  [[ "$(cat "$file_path")" == "$expected" ]] || fail "expected $file_path to be: $expected"
}

assert_output_contains() {
  local output_file="$1"
  local expected="$2"

  if grep -Fq -- "$expected" "$output_file"; then
    return 0
  fi

  echo "FAIL: expected output to contain: $expected" >&2
  echo "--- output: $output_file ---" >&2
  sed -n '1,160p' "$output_file" >&2
  echo "--- end output ---" >&2
  exit 1
}

assert_contains_text() {
  local text="$1"
  local expected="$2"

  [[ "$text" == *"$expected"* ]] || fail "expected output to contain: $expected"
}

assert_not_contains_text() {
  local text="$1"
  local unexpected="$2"

  [[ "$text" != *"$unexpected"* ]] || fail "expected output not to contain: $unexpected"
}

make_temp_dir() {
  local prefix="${1:-dotfiles-test}"
  local candidate
  local attempts=0

  while (( attempts < 10 )); do
    candidate="${TMPDIR:-/tmp}/$prefix-$$-$RANDOM-$RANDOM"
    if mkdir "$candidate" 2>/dev/null; then
      REPLY="$candidate"
      return 0
    fi
    attempts=$((attempts + 1))
  done

  fail "failed to create temporary directory"
}

make_temp_file() {
  make_temp_dir "${1:-dotfiles-test}"
  rmdir "$REPLY"
  : > "$REPLY"
}
