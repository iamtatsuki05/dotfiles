#!/bin/bash
# PostToolUse / AfterTool フックで .py ファイルの jupytext 同期を行う。
# Claude Code / Gemini CLI / Codex CLI で共通利用。
# .ipynb はbase64画像を含んで肥大化しやすいため、各AIエージェントには .py だけを操作させる構成にしている。

input=$(cat)

file_paths=$(echo "$input" | python3 -c "
import json, sys

try:
    d = json.load(sys.stdin)
except (json.JSONDecodeError, ValueError):
    sys.exit(0)

paths = []

# Claude Code 形式: tool_input.file_path / tool_input.edits[].file_path
ti = d.get('tool_input', {})
if isinstance(ti, dict):
    if 'file_path' in ti:
        paths.append(ti['file_path'])
    for edit in ti.get('edits', []):
        fp = edit.get('file_path', '')
        if fp and fp not in paths:
            paths.append(fp)

# Gemini CLI / Codex 形式: tool_use.input / function_call.arguments など
for key in ('tool_use', 'function_call', 'tool'):
    tc = d.get(key, {})
    if not isinstance(tc, dict):
        continue
    inp = tc.get('input', tc.get('arguments', {}))
    if isinstance(inp, str):
        try:
            inp = json.loads(inp)
        except Exception:
            inp = {}
    if isinstance(inp, dict):
        fp = inp.get('file_path', inp.get('path', ''))
        if fp and fp not in paths:
            paths.append(fp)

print('\n'.join(paths))
" 2>/dev/null)

while IFS= read -r file_path; do
    [[ -z "$file_path" ]] && continue
    [[ "$file_path" != *.py ]] && continue

    ipynb_path="${file_path%.py}.ipynb"
    if [[ -f "$ipynb_path" ]]; then
        jupytext --sync "$file_path" 2>/dev/null
    fi
done <<< "$file_paths"
