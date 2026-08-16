#!/usr/bin/env zsh

set -euo pipefail

readonly TEST_DIR="${0:A:h}"
readonly REPO_ROOT="${TEST_DIR:h}"
readonly LINTER="$REPO_ROOT/dotfiles/.agent/hooks/japanese_prose_lint.sh"
readonly CLAUDE_SETTINGS="$REPO_ROOT/dotfiles/.agent/apps/claude/settings.json"
readonly CODEX_HOOKS="$REPO_ROOT/dotfiles/.agent/apps/codex/hooks.json"
readonly COPILOT_SETTINGS="$REPO_ROOT/dotfiles/.agent/apps/copilot/settings.json"
readonly CURSOR_HOOKS="$REPO_ROOT/dotfiles/.agent/apps/cursor/hooks.json"
readonly DEVIN_CONFIG="$REPO_ROOT/dotfiles/.agent/apps/devin/config.json"
readonly ANTIGRAVITY_HOOKS="$REPO_ROOT/dotfiles/.agent/apps/antigravity-cli/plugins/dotfiles-agent/hooks.json"
readonly HERMES_CONFIG="$REPO_ROOT/dotfiles/.agent/apps/hermes-agent/config.yaml"
readonly OPENCODE_PLUGIN="$REPO_ROOT/dotfiles/.agent/apps/opencode/plugins/japanese-prose-lint.js"
readonly OPENCLAW_CONFIG="$REPO_ROOT/dotfiles/.agent/apps/openclaw/openclaw.json"
readonly OPENCLAW_PLUGIN="$REPO_ROOT/dotfiles/.agent/apps/openclaw/extensions/japanese-prose-lint/index.js"

source "$TEST_DIR/lib/assertions.sh"

make_fixture() {
  make_temp_dir "japanese-prose-lint-test"
  FIXTURE_DIR="${REPLY:A}"
}

run_check() {
  local output_file="$1"
  shift

  set +e
  "$LINTER" --check "$@" > "$output_file" 2>&1
  RUN_STATUS=$?
  set -e
}

test_cli_reports_local_and_document_rules() {
  local document
  local output

  make_fixture
  document="$FIXTURE_DIR/report.md"
  output="$FIXTURE_DIR/output.txt"
  cat > "$document" <<'EOF'
# 結果

これは単なる速度改善ではなく、運用全体の改善です。別の対策ではなく、こちらを採用します。
この設定は検索に効く。キャッシュにも効く。
処理は完了しました。検証も完了しました。記録も完了しました。
シンプル。それだけ。それが本質です。── 完了です。🚀
EOF

  run_check "$output" "$document"

  [[ "$RUN_STATUS" == "1" ]] || fail "expected lint findings to exit 1, got $RUN_STATUS"
  assert_output_contains "$output" "JP001"
  assert_output_contains "$output" "JP002"
  assert_output_contains "$output" "JP003"
  assert_output_contains "$output" "JP004"
  assert_output_contains "$output" "JP005"
  assert_output_contains "$output" "JP006"
  assert_output_contains "$output" "該当文全体を書き直してください"
  assert_output_contains "$output" "$document:3"

  rm -rf "$FIXTURE_DIR"
}

test_cli_ignores_non_prose_markdown_regions() {
  local document
  local output

  make_fixture
  document="$FIXTURE_DIR/reference.md"
  output="$FIXTURE_DIR/output.txt"
  cat > "$document" <<'EOF'
---
description: "AではなくB。── 🚀"
---

通常の説明文です。次の説明も自然です。

> AではなくB。── 🚀

```text
AではなくB。── 🚀
```

`AではなくB。── 🚀` と [参照先](https://example.com/AではなくB/──/🚀) を確認します。
EOF

  run_check "$output" "$document"

  [[ "$RUN_STATUS" == "0" ]] || fail "expected clean document to exit 0, got $RUN_STATUS"
  assert_output_contains "$output" "日本語 lint: 問題は見つかりませんでした"

  rm -rf "$FIXTURE_DIR"
}

test_cli_respects_fence_length_and_closing_syntax() {
  local document
  local output

  make_fixture
  document="$FIXTURE_DIR/fence-length.md"
  output="$FIXTURE_DIR/output.txt"
  cat > "$document" <<'EOF'
````text
```python
シンプル。それだけ。それが本質です。―― 🚀
```
````

通常の本文です。
EOF

  run_check "$output" "$document"

  [[ "$RUN_STATUS" == "0" ]] || fail "expected shorter or annotated fences not to close the block, got $RUN_STATUS"

  rm -rf "$FIXTURE_DIR"
}

test_cli_ignores_multiline_code_spans() {
  local document
  local output

  make_fixture
  document="$FIXTURE_DIR/multiline-code-span.md"
  output="$FIXTURE_DIR/output.txt"
  cat > "$document" <<'EOF'
`AではなくB
シンプル。それだけ。それが本質です。―― 🚀`

通常の本文です。
EOF

  run_check "$output" "$document"

  [[ "$RUN_STATUS" == "0" ]] || fail "expected multiline code spans to be excluded, got $RUN_STATUS"

  rm -rf "$FIXTURE_DIR"
}

test_cli_ignores_lazy_blockquote_continuations() {
  local document
  local output

  make_fixture
  document="$FIXTURE_DIR/lazy-blockquote.md"
  output="$FIXTURE_DIR/output.txt"
  cat > "$document" <<'EOF'
> 引用の開始です。
AではなくB。シンプル。それだけ。それが本質です。―― 🚀

通常の本文です。
EOF

  run_check "$output" "$document"

  [[ "$RUN_STATUS" == "0" ]] || fail "expected lazy blockquote continuation prose to be excluded, got $RUN_STATUS"

  rm -rf "$FIXTURE_DIR"
}

test_cli_detects_repeated_horizontal_bars() {
  local document
  local output

  make_fixture
  document="$FIXTURE_DIR/horizontal-bar.md"
  output="$FIXTURE_DIR/output.txt"
  print -r -- '結論――この設定を採用します。' > "$document"

  run_check "$output" "$document"

  [[ "$RUN_STATUS" == "1" ]] || fail "expected repeated U+2015 bars to be detected, got $RUN_STATUS"
  assert_output_contains "$output" "JP001"

  rm -rf "$FIXTURE_DIR"
}

test_cli_limits_progress_narration_to_longform_profile() {
  local document
  local output

  make_fixture
  document="$FIXTURE_DIR/article.md"
  output="$FIXTURE_DIR/output.txt"
  print -r -- '次にページ種別を確認します。結果は縦書き61%でした。' > "$document"

  run_check "$output" "$document"
  [[ "$RUN_STATUS" == "0" ]] || fail "expected shared profile to ignore narration, got $RUN_STATUS"

  run_check "$output" --profile longform "$document"
  [[ "$RUN_STATUS" == "1" ]] || fail "expected longform finding to exit 1, got $RUN_STATUS"
  assert_output_contains "$output" "JP008"

  rm -rf "$FIXTURE_DIR"
}

test_cli_reports_substantial_style_mixing() {
  local document
  local output

  make_fixture
  document="$FIXTURE_DIR/mixed-style.md"
  output="$FIXTURE_DIR/output.txt"
  cat > "$document" <<'EOF'
入力を確認しました。設定を更新します。結果を記録しました。
別の処理は未実施だ。原因は調査中である。再実行は明日だ。
EOF

  run_check "$output" "$document"

  [[ "$RUN_STATUS" == "1" ]] || fail "expected mixed style to exit 1, got $RUN_STATUS"
  assert_output_contains "$output" "JP007"

  rm -rf "$FIXTURE_DIR"
}

test_cli_does_not_join_endings_across_other_sentences() {
  local document
  local output

  make_fixture
  document="$FIXTURE_DIR/varied-endings.md"
  output="$FIXTURE_DIR/output.txt"
  cat > "$document" <<'EOF'
処理する。保存する。終了する。
入力を確認します。別の処理を実行する。
設定も確認します。結果を保存する。
最後に記録します。
EOF

  run_check "$output" "$document"

  [[ "$RUN_STATUS" == "0" ]] || fail "expected intervening endings to prevent JP006, got $RUN_STATUS"
  assert_not_contains "$output" "JP006"

  rm -rf "$FIXTURE_DIR"
}

test_cli_excludes_markdown_lists_from_document_distribution() {
  local document
  local output

  make_fixture
  document="$FIXTURE_DIR/checklist.md"
  output="$FIXTURE_DIR/output.txt"
  cat > "$document" <<'EOF'
# 確認事項

- 入力を確認します。
- 設定を確認します。
- 結果を記録します。
EOF

  run_check "$output" "$document"

  [[ "$RUN_STATUS" == "0" ]] || fail "expected Markdown lists to be excluded from distribution rules, got $RUN_STATUS"
  assert_not_contains "$output" "JP006"

  rm -rf "$FIXTURE_DIR"
}

test_cli_limits_repeated_endings_to_one_paragraph() {
  local document
  local output

  make_fixture
  document="$FIXTURE_DIR/separate-paragraphs.md"
  output="$FIXTURE_DIR/output.txt"
  cat > "$document" <<'EOF'
入力を確認します。

設定を確認します。

結果を記録します。
EOF

  run_check "$output" "$document"

  [[ "$RUN_STATUS" == "0" ]] || fail "expected paragraph boundaries to break repeated endings, got $RUN_STATUS"
  assert_not_contains "$output" "JP006"

  rm -rf "$FIXTURE_DIR"
}

test_claude_hook_returns_strict_feedback_for_supported_document() {
  local document
  local output
  local payload

  make_fixture
  document="$FIXTURE_DIR/note.md"
  output="$FIXTURE_DIR/output.json"
  print -r -- 'シンプル。それだけ。それが本質です。' > "$document"
  payload='{"hook_event_name":"PostToolUse","tool_name":"Write","tool_input":{"file_path":"'"$document"'"}}'

  print -r -- "$payload" | "$LINTER" --hook-agent claude > "$output"

  python3 - "$output" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    payload = json.load(handle)

assert set(payload) == {"hookSpecificOutput"}
hook_output = payload["hookSpecificOutput"]
assert hook_output["hookEventName"] == "PostToolUse"
context = hook_output["additionalContext"]
assert "JP003" in context
assert "該当文全体を書き直してください" in context
PY

  rm -rf "$FIXTURE_DIR"
}

test_hook_is_silent_for_clean_or_unsupported_files() {
  local document
  local source_file
  local output

  make_fixture
  document="$FIXTURE_DIR/clean.md"
  source_file="$FIXTURE_DIR/example.py"
  output="$FIXTURE_DIR/output.txt"
  print -r -- '検証は完了しています。' > "$document"
  print -r -- 'print("──")' > "$source_file"

  print -r -- '{"hook_event_name":"PostToolUse","tool_name":"Edit","tool_input":{"file_path":"'"$document"'"}}' \
    | "$LINTER" --hook-agent claude > "$output"
  [[ ! -s "$output" ]] || fail "expected clean hook output to be empty"

  print -r -- '{"hook_event_name":"PostToolUse","tool_name":"Edit","tool_input":{"file_path":"'"$source_file"'"}}' \
    | "$LINTER" --hook-agent claude > "$output"
  [[ ! -s "$output" ]] || fail "expected unsupported hook output to be empty"

  rm -rf "$FIXTURE_DIR"
}

test_hook_caps_feedback_volume() {
  local document
  local output

  make_fixture
  document="$FIXTURE_DIR/many-findings.md"
  output="$FIXTURE_DIR/output.json"
  printf '🚀%.0s' {1..25} > "$document"

  print -r -- '{"hook_event_name":"PostToolUse","tool_name":"Write","tool_input":{"file_path":"'"$document"'"}}' \
    | "$LINTER" --hook-agent claude > "$output"

  python3 - "$output" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    payload = json.load(handle)

context = payload["hookSpecificOutput"]["additionalContext"]
assert context.count("[JP002]") == 20
assert "ほか5件" in context
PY

  rm -rf "$FIXTURE_DIR"
}

test_hook_rejects_malformed_payload() {
  local output

  make_fixture
  output="$FIXTURE_DIR/output.txt"

  set +e
  print -r -- 'not-json' | "$LINTER" --hook-agent claude > "$output" 2>&1
  RUN_STATUS=$?
  set -e

  [[ "$RUN_STATUS" == "2" ]] || fail "expected malformed hook payload to exit 2, got $RUN_STATUS"
  assert_output_contains "$output" "invalid hook payload"

  rm -rf "$FIXTURE_DIR"
}

test_cli_reports_missing_files_as_usage_errors() {
  local output

  make_fixture
  output="$FIXTURE_DIR/output.txt"

  run_check "$output" "$FIXTURE_DIR/missing.md"

  [[ "$RUN_STATUS" == "2" ]] || fail "expected missing file to exit 2, got $RUN_STATUS"
  assert_output_contains "$output" "file not found"

  rm -rf "$FIXTURE_DIR"
}

test_cli_supports_markdown_extension() {
  local document
  local output

  make_fixture
  document="$FIXTURE_DIR/article.markdown"
  output="$FIXTURE_DIR/output.txt"
  print -r -- 'シンプル。それだけ。それが本質です。' > "$document"

  run_check "$output" "$document"

  [[ "$RUN_STATUS" == "1" ]] || fail "expected .markdown files to be checked, got $RUN_STATUS"
  assert_output_contains "$output" "JP003"

  rm -rf "$FIXTURE_DIR"
}

test_cli_keeps_japanese_prose_after_url_visible() {
  local document
  local output

  make_fixture
  document="$FIXTURE_DIR/url-adjacent.md"
  output="$FIXTURE_DIR/output.txt"
  print -r -- 'https://example.comを参照した。シンプル。それだけ。それが本質です。' > "$document"

  run_check "$output" "$document"

  [[ "$RUN_STATUS" == "1" ]] || fail "expected prose after a URL to remain lintable, got $RUN_STATUS"
  assert_output_contains "$output" "JP003"

  rm -rf "$FIXTURE_DIR"
}

test_claude_runs_lint_after_document_edits() {
  python3 - "$CLAUDE_SETTINGS" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    settings = json.load(handle)

groups = settings["hooks"]["PostToolUse"]
matching_groups = [group for group in groups if group.get("matcher") == "Edit|Write|MultiEdit"]
commands = {
    hook.get("command")
    for group in matching_groups
    for hook in group.get("hooks", [])
    if hook.get("type") == "command"
}
assert "~/.claude/hooks/japanese_prose_lint.sh --hook-agent claude" in commands
PY
}

test_hook_adapters_follow_each_agent_contract() {
  local document
  local output
  make_fixture
  document="$FIXTURE_DIR/adapter.md"
  output="$FIXTURE_DIR/adapter.json"
  print -r -- 'シンプル。それだけ。それが本質です。' > "$document"

  print -r -- '{"hook_event_name":"PostToolUse","tool_name":"apply_patch","cwd":"'"$FIXTURE_DIR"'","tool_input":{"command":"*** Begin Patch\n*** Update File: adapter.md\n*** End Patch"}}' | "$LINTER" --hook-agent codex > "$output"
  python3 - "$output" <<'PY'
import json, sys
p=json.load(open(sys.argv[1], encoding="utf-8"))
assert "JP003" in p["hookSpecificOutput"]["additionalContext"]
PY

  print -r -- '{"hook_event_name":"postToolUse","toolName":"edit","cwd":"'"$FIXTURE_DIR"'","toolArgs":{"path":"adapter.md"}}' | "$LINTER" --hook-agent copilot > "$output"
  python3 - "$output" <<'PY'
import json, sys
p=json.load(open(sys.argv[1], encoding="utf-8"))
assert set(p) == {"additionalContext"} and "JP003" in p["additionalContext"]
PY

  print -r -- '{"hook_event_name":"postToolUse","tool_name":"Write","cwd":"'"$FIXTURE_DIR"'","tool_input":{"path":"adapter.md"}}' | "$LINTER" --hook-agent cursor > "$output"
  python3 - "$output" <<'PY'
import json, sys
p=json.load(open(sys.argv[1], encoding="utf-8"))
assert set(p) == {"additional_context"} and "JP003" in p["additional_context"]
PY

  rm -rf "$FIXTURE_DIR"
}

test_all_agents_register_a_feedback_capable_path() {
  python3 - "$CLAUDE_SETTINGS" "$CODEX_HOOKS" "$COPILOT_SETTINGS" "$CURSOR_HOOKS" "$DEVIN_CONFIG" "$ANTIGRAVITY_HOOKS" "$OPENCLAW_CONFIG" <<'PY'
import json, sys
docs=[json.load(open(path, encoding="utf-8")) for path in sys.argv[1:]]
serialized=[json.dumps(doc, ensure_ascii=False) for doc in docs]
for agent, text in zip(("claude","codex","copilot","cursor","devin","antigravity"), serialized[:6]):
    assert f"--hook-agent {agent}" in text, agent
codex=docs[1]
assert any(group.get("matcher") == "Edit|Write" and "--hook-agent codex" in json.dumps(group) for group in codex["hooks"]["PostToolUse"])
copilot=docs[2]
assert any(hook.get("matcher") == "create|edit" and "--hook-agent copilot" in hook["bash"] for hook in copilot["hooks"]["postToolUse"])
cursor=docs[3]
assert any("--hook-agent cursor" in hook["command"] for hook in cursor["hooks"]["postToolUse"])
assert all("--hook-agent cursor" not in hook["command"] for hook in cursor["hooks"]["sessionStart"])
devin=docs[4]
assert any(group.get("matcher") == "Edit|Write|MultiEdit" and "--hook-agent devin" in json.dumps(group) for group in devin["hooks"]["PostToolUse"])
openclaw=docs[6]
assert "~/.openclaw/extensions/japanese-prose-lint" in openclaw["plugins"]["load"]["paths"]
assert openclaw["plugins"]["entries"]["japanese-prose-lint"]["enabled"] is True
PY

  grep -q -- 'japanese-prose-lint' "$HERMES_CONFIG" || fail "Hermes plugin is not enabled"
  [[ -f "$OPENCODE_PLUGIN" ]] || fail "OpenCode plugin is missing"
  [[ -f "$OPENCLAW_PLUGIN" ]] || fail "OpenClaw plugin is missing"
  grep -q -- 'transform_tool_result' "$REPO_ROOT/dotfiles/.agent/apps/hermes-agent/plugins/japanese-prose-lint/__init__.py" || fail "Hermes feedback hook is missing"
  grep -q -- 'tool.execute.after' "$OPENCODE_PLUGIN" || fail "OpenCode feedback hook is missing"
  grep -q -- 'before_tool_call' "$OPENCLAW_PLUGIN" || fail "OpenClaw path-capture hook is missing"
  grep -q -- 'tool_result_persist' "$OPENCLAW_PLUGIN" || fail "OpenClaw feedback hook is missing"
}

test_plugin_adapters_append_model_visible_feedback() {
  local document
  local fake_home
  make_fixture
  document="$FIXTURE_DIR/plugin.md"
  fake_home="$FIXTURE_DIR/home"
  print -r -- 'シンプル。それだけ。それが本質です。' > "$document"
  mkdir -p "$fake_home/.config/opencode/hooks" "$fake_home/.openclaw/hooks" "$fake_home/.hermes/agent-hooks"
  ln -s "$LINTER" "$fake_home/.config/opencode/hooks/japanese_prose_lint.sh"
  ln -s "$LINTER" "$fake_home/.openclaw/hooks/japanese_prose_lint.sh"
  ln -s "$LINTER" "$fake_home/.hermes/agent-hooks/japanese_prose_lint.sh"

  HOME="$fake_home" node --input-type=module - "$OPENCODE_PLUGIN" "$document" <<'JS'
const [{ JapaneseProseLint }, document] = [await import(`file://${process.argv[2]}`), process.argv[3]]
const plugin = await JapaneseProseLint()
const output = { output: "written" }
await plugin["tool.execute.after"]({ tool: "write", args: { path: document } }, output)
if (!output.output.includes("JP003")) throw new Error("OpenCode feedback was not appended")
JS

  HOME="$fake_home" node --input-type=module - "$OPENCLAW_PLUGIN" "$document" <<'JS'
const [{ default: plugin }, document] = [await import(`file://${process.argv[2]}`), process.argv[3]]
const { writeFileSync } = await import("node:fs")
const hooks = {}
plugin.register({ on(name, callback) { hooks[name] = callback } })
const created = `${document}.new.md`
hooks.before_tool_call({ toolName: "write", params: { path: created } }, { sessionKey: "session-1", toolCallId: "call-1" })
writeFileSync(created, "シンプル。それだけ。それが本質です。\n")
const original = { role: "toolResult", content: [{ type: "text", text: "written" }] }
const result = hooks.tool_result_persist({ message: original }, { sessionKey: "session-1", toolCallId: "call-1" })
if (!result.message.content.some((item) => item.text?.includes("JP003"))) throw new Error("OpenClaw feedback was not appended")
JS

  HOME="$fake_home" python3 - "$REPO_ROOT/dotfiles/.agent/apps/hermes-agent/plugins/japanese-prose-lint/__init__.py" "$document" <<'PY'
import importlib.util, sys
spec=importlib.util.spec_from_file_location("japanese_prose_lint_plugin", sys.argv[1])
module=importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
result=module._on_transform_tool_result(tool_name="write_file", args={"path": sys.argv[2]}, result="written")
assert "JP003" in result
PY

  rm -rf "$FIXTURE_DIR"
}

main() {
  test_cli_reports_local_and_document_rules
  test_cli_ignores_non_prose_markdown_regions
  test_cli_respects_fence_length_and_closing_syntax
  test_cli_ignores_multiline_code_spans
  test_cli_ignores_lazy_blockquote_continuations
  test_cli_detects_repeated_horizontal_bars
  test_cli_limits_progress_narration_to_longform_profile
  test_cli_reports_substantial_style_mixing
  test_cli_does_not_join_endings_across_other_sentences
  test_cli_excludes_markdown_lists_from_document_distribution
  test_cli_limits_repeated_endings_to_one_paragraph
  test_claude_hook_returns_strict_feedback_for_supported_document
  test_hook_is_silent_for_clean_or_unsupported_files
  test_hook_caps_feedback_volume
  test_hook_rejects_malformed_payload
  test_cli_reports_missing_files_as_usage_errors
  test_cli_supports_markdown_extension
  test_cli_keeps_japanese_prose_after_url_visible
  test_claude_runs_lint_after_document_edits
  test_hook_adapters_follow_each_agent_contract
  test_all_agents_register_a_feedback_capable_path
  test_plugin_adapters_append_model_visible_feedback
  print -r -- "japanese prose lint tests passed"
}

main "$@"
