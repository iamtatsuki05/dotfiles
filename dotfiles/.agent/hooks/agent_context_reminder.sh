#!/usr/bin/env bash

set -euo pipefail

python3 -c '
import json
import os
import sys

try:
    payload = json.load(sys.stdin)
except json.JSONDecodeError:
    sys.exit(0)

if not isinstance(payload, dict):
    sys.exit(0)

event = (
    payload.get("hook_event_name")
    or payload.get("hookEventName")
    or payload.get("event")
    or payload.get("type")
    or payload.get("hook")
)

event_aliases = {
    "sessionStart": "SessionStart",
    "session_start": "SessionStart",
    "beforeSubmitPrompt": "UserPromptSubmit",
    "userPromptSubmitted": "UserPromptSubmit",
    "user_prompt_submitted": "UserPromptSubmit",
    "postToolUse": "PostToolUse",
    "post_tool_use": "PostToolUse",
    "afterFileEdit": "PostToolUse",
    "PreInvocation": "BeforeAgent",
    "stop": "Stop",
    "subagentStop": "SubagentStop",
    "pre_llm_call": "BeforeModel",
    "subagent_stop": "SubagentStop",
    "before_prompt_build": "BeforeAgent",
    "agent_turn_prepare": "BeforeAgent",
}

if isinstance(event, str):
    normalized_event = event_aliases.get(event, event)
else:
    # copilot CLI (0.0.3xx 系) はイベント名フィールドを送らないため、payload の形から推定する。
    # 推定イベントは strict 出力の対象外 (event が str でないため後段で互換キー出力になる)。
    if "initialPrompt" in payload or payload.get("source"):
        event = None
        normalized_event = "SessionStart"
    elif "prompt" in payload:
        event = None
        normalized_event = "UserPromptSubmit"
    else:
        sys.exit(0)

if normalized_event not in {
    "BeforeAgent",
    "BeforeModel",
    "PostToolUse",
    "SessionStart",
    "Stop",
    "SubagentStop",
    "SubagentStart",
    "UserPromptSubmit",
    "UserPromptExpansion",
}:
    sys.exit(0)

workspace_roots = payload.get("workspace_roots")
if isinstance(workspace_roots, list) and workspace_roots:
    workspace_cwd = workspace_roots[0]
else:
    workspace_cwd = None

cwd = payload.get("cwd") or workspace_cwd or os.getcwd()


def find_upwards(start, name):
    current = os.path.abspath(os.path.expanduser(start))
    while True:
        candidate = os.path.join(current, name)
        if os.path.exists(candidate):
            return candidate
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent


def find_agent_dir(start):
    current = os.path.abspath(os.path.expanduser(start))
    while True:
        for relative_path in (".agent", os.path.join("dotfiles", ".agent")):
            candidate = os.path.join(current, relative_path)
            if os.path.isdir(candidate):
                return candidate
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent


git_dir = find_upwards(cwd, ".git")
agent_dir = find_agent_dir(cwd)

# AGENTS.md (各 agent の instructions として同期済み) と同じ規約は繰り返さず、
# workspace 検出に依存する動的情報だけを出す。全 harness・全 subagent の
# 起動時に毎回入るため、固定文を増やすと token 固定費が直接増える。
lines = ["リポジトリ hook リマインダー:"]

if git_dir:
    lines.append(
        "- この git worktree で編集する前に現在の状態を確認し、ユーザーや別作業の差分を保護する。"
    )

if agent_dir:
    sessions_path = os.path.join(agent_dir, "work", "sessions")
    lines.append(
        f"- この workspace には .agent metadata がある。session directory は {sessions_path}/<YYYY-MM-DD-HHMMSS>-<short-slug>-<agent-id>/ に作り、checkpoint.md の運用は AGENTS.md「作業ログ・引き継ぎ」に従う。"
    )

context = "\n".join(lines)

# Claude Code / Codex は正規イベント名を送る。Codex は stdout を厳密な schema で
# パースし、未知のトップレベルキーがあると hook が Failed になるため、正規イベント名の
# ときは hookSpecificOutput だけを出力する (Claude Code も同じ形式を受理する)。
# エイリアスイベント名で呼ぶ他エージェント向けには従来の互換キーを維持する。
strict_events = {
    "SessionStart",
    "UserPromptSubmit",
    "PostToolUse",
    "Stop",
    "SubagentStart",
    "SubagentStop",
}

if event in strict_events:
    output = {
        "hookSpecificOutput": {
            "hookEventName": normalized_event,
            "additionalContext": context,
        }
    }
else:
    output = {
        "context": context,
        "additionalContext": context,
        "additional_context": context,
        "prependContext": context,
        "hookSpecificOutput": {
            "hookEventName": normalized_event,
            "additionalContext": context,
        },
    }

print(json.dumps(output, ensure_ascii=False))
'
