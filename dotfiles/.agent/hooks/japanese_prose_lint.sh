#!/usr/bin/env bash

set -euo pipefail

python3 /dev/fd/3 "$@" 3<<'PY'
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path


MARKDOWN_SUFFIXES = {".md", ".markdown"}
SUPPORTED_SUFFIXES = MARKDOWN_SUFFIXES | {".txt"}
MAX_HOOK_FINDINGS = 20
REWRITE_SENTENCE = "問題のある語だけを置換せず、事実関係を保って該当文全体を書き直してください。"


@dataclass(frozen=True)
class Finding:
    rule_id: str
    line: int
    column: int
    message: str
    guidance: str


@dataclass(frozen=True)
class LocalRule:
    rule_id: str
    pattern: re.Pattern[str]
    message: str
    guidance: str
    profiles: frozenset[str] = frozenset({"shared", "longform"})


LOCAL_RULES = (
    LocalRule(
        "JP001",
        re.compile(r"[─—―]{2,}"),
        "全角ダッシュの連続は、AI 生成文に見えやすく表示環境でも崩れます。",
        "読点、句点、コロン、または改行で文の関係を明示してください。",
    ),
    LocalRule(
        "JP002",
        re.compile(r"[🚀🎯✨💡]"),
        "装飾目的の絵文字が本文に含まれています。",
        "意味を担っていない絵文字を削り、必要な情報を本文で直接述べてください。",
    ),
    LocalRule(
        "JP003",
        re.compile(r"(?:シンプル[。.!！]\s*それだけ|それだけ[。.!！]\s*それが本質)"),
        "短い断片を重ねた劇的な言い回しになっています。",
        "何が簡潔なのか、または何を結論とするのかを一文で具体的に述べてください。",
    ),
    LocalRule(
        "JP008",
        re.compile(r"(?:次に|ここから|以下では).{0,40}(?:確認します|見ます|見ていきます|検討します)"),
        "対象の内容ではなく、文書の進行だけを説明しています。",
        "後続の結果を直接書き、まだ結果がなければこの文を削除してください。",
        frozenset({"longform"}),
    ),
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Japanese prose lint for agent-authored documents")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="check files from the command line")
    mode.add_argument(
        "--hook-agent",
        choices=("claude", "codex", "copilot", "cursor", "devin", "antigravity"),
        help="read the named agent's post-tool payload",
    )
    parser.add_argument("--profile", choices=("shared", "longform"), default="shared")
    parser.add_argument("files", nargs="*")
    args = parser.parse_args()
    if args.check and not args.files:
        parser.error("--check requires at least one file")
    if args.hook_agent and args.files:
        parser.error("--hook-agent does not accept file arguments")
    return args


def blank_region(text: str) -> str:
    return "".join("\n" if char == "\n" else " " for char in text)


def mask_inline_code_spans(text: str) -> str:
    masked = list(text)
    consumed_until = 0
    for opening in re.finditer(r"`+", text):
        if opening.start() < consumed_until:
            continue
        tick_count = len(opening.group(0))
        closing_pattern = re.compile(rf"(?<!`)`{{{tick_count}}}(?!`)")
        closing = closing_pattern.search(text, opening.end())
        if not closing:
            continue
        for index in range(opening.start(), closing.end()):
            if masked[index] != "\n":
                masked[index] = " "
        consumed_until = closing.end()
    return "".join(masked)


def mask_markdown(text: str, suffix: str, *, exclude_structural_lines: bool = False) -> str:
    if suffix not in MARKDOWN_SUFFIXES:
        return text

    lines = text.splitlines(keepends=True)
    masked: list[str] = []
    in_frontmatter = bool(lines and lines[0].strip() == "---")
    in_fence = False
    fence_marker = ""
    in_lazy_blockquote = False

    for index, line in enumerate(lines):
        stripped = line.lstrip()
        marker_match = re.match(r"(`{3,}|~{3,})", stripped)

        if in_frontmatter:
            masked.append(blank_region(line))
            if index > 0 and line.strip() == "---":
                in_frontmatter = False
            continue

        if in_fence:
            masked.append(blank_region(line))
            closing_pattern = rf"{re.escape(fence_marker[0])}{{{len(fence_marker)},}}[ \t]*(?:\r?\n)?$"
            if re.fullmatch(closing_pattern, stripped):
                in_fence = False
                fence_marker = ""
            continue

        if marker_match:
            in_fence = True
            fence_marker = marker_match.group(1)
            masked.append(blank_region(line))
            continue

        if re.match(r"\s*>", line):
            in_lazy_blockquote = True
            masked.append(blank_region(line))
            continue

        if in_lazy_blockquote:
            if not line.strip():
                in_lazy_blockquote = False
                masked.append(blank_region(line))
                continue
            if not re.match(r"\s*(?:[-*+] |\d+[.)] |#{1,6} |`{3,}|~{3,})", line):
                masked.append(blank_region(line))
                continue
            in_lazy_blockquote = False

        if line.startswith("    ") or line.startswith("\t"):
            masked.append(blank_region(line))
            continue

        if exclude_structural_lines and re.match(r"\s*(?:[-*+] |\d+[.)] |#{1,6} |\|)", line):
            masked.append(blank_region(line))
            continue

        masked.append(line)

    masked_text = mask_inline_code_spans("".join(masked))
    masked_text = re.sub(
        r"(?<=\]\()[^)\n]+(?=\))|(?<=<)https?://[^>\n]+(?=>)",
        lambda match: " " * len(match.group(0)),
        masked_text,
    )
    return re.sub(
        r"https?://[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]+",
        lambda match: " " * len(match.group(0)),
        masked_text,
    )


def location(text: str, offset: int) -> tuple[int, int]:
    line = text.count("\n", 0, offset) + 1
    previous_newline = text.rfind("\n", 0, offset)
    column = offset - previous_newline
    return line, column


def local_findings(text: str, masked: str, profile: str) -> list[Finding]:
    findings: list[Finding] = []
    for rule in LOCAL_RULES:
        if profile not in rule.profiles:
            continue
        for match in rule.pattern.finditer(masked):
            line, column = location(text, match.start())
            findings.append(Finding(rule.rule_id, line, column, rule.message, rule.guidance))
    return findings


def repeated_term_finding(
    text: str,
    masked: str,
    *,
    rule_id: str,
    pattern: re.Pattern[str],
    minimum: int,
    message: str,
    guidance: str,
) -> Finding | None:
    matches = list(pattern.finditer(masked))
    if len(matches) < minimum:
        return None
    line, column = location(text, matches[0].start())
    return Finding(rule_id, line, column, f"{message}（{len(matches)}回）", guidance)


def sentence_endings(masked: str) -> list[tuple[str, int]]:
    endings: list[tuple[str, int]] = []
    previous_end = 0
    for sentence in re.finditer(r"[^。！？\n]+[。！？]", masked):
        gap = masked[previous_end : sentence.start()]
        if re.search(r"\n[ \t]*\n", gap):
            endings.append(("", sentence.start()))
        value = sentence.group(0).strip()
        match = re.search(r"(ませんでした|ません|ました|でした|ます|です|である|だ)[。！？]$", value)
        if match:
            endings.append((match.group(1), sentence.start() + match.start(1)))
        else:
            endings.append(("", sentence.start()))
        previous_end = sentence.end()
    return endings


def aggregate_findings(text: str, masked: str) -> list[Finding]:
    findings: list[Finding] = []

    contrast = repeated_term_finding(
        text,
        masked,
        rule_id="JP004",
        pattern=re.compile(r"ではなく"),
        minimum=2,
        message="否定から入る『AではなくB』型の対比が繰り返されています。",
        guidance="必要な対比だけを残し、不要な箇所は肯定形で結論を直接述べてください。",
    )
    if contrast:
        findings.append(contrast)

    vague_effect = repeated_term_finding(
        text,
        masked,
        rule_id="JP005",
        pattern=re.compile(r"効く"),
        minimum=2,
        message="効果の対象や変化を示さない『効く』が繰り返されています。",
        guidance="何に対して、どの条件で、どのような変化があるのかを明示してください。",
    )
    if vague_effect:
        findings.append(vague_effect)

    endings = sentence_endings(masked)
    for index in range(len(endings) - 2):
        ending = endings[index][0]
        if ending and endings[index + 1][0] == ending and endings[index + 2][0] == ending:
            line, column = location(text, endings[index][1])
            findings.append(
                Finding(
                    "JP006",
                    line,
                    column,
                    f"同じ文末『{ending}』が3文以上続いています。",
                    "事実関係を変えず、文の接続や文末を見直して単調さを解消してください。",
                )
            )
            break

    polite = sum(1 for ending, _ in endings if ending in {"ませんでした", "ません", "ました", "でした", "ます", "です"})
    plain = sum(1 for ending, _ in endings if ending in {"である", "だ"})
    if polite >= 3 and plain >= 3:
        first_plain = next(offset for ending, offset in endings if ending in {"である", "だ"})
        line, column = location(text, first_plain)
        findings.append(
            Finding(
                "JP007",
                line,
                column,
                "敬体と常体が文書内でまとまって混在しています。",
                "引用などの意図的な箇所を除き、想定読者に合う文体へ統一してください。",
            )
        )

    return findings


def lint_file(path: Path, profile: str) -> list[Finding]:
    text = path.read_text(encoding="utf-8")
    masked = mask_markdown(text, path.suffix.lower())
    aggregate_masked = mask_markdown(text, path.suffix.lower(), exclude_structural_lines=True)
    findings = local_findings(text, masked, profile)
    findings.extend(aggregate_findings(text, aggregate_masked))
    return sorted(findings, key=lambda finding: (finding.line, finding.column, finding.rule_id))


def format_findings(path: Path, findings: list[Finding]) -> list[str]:
    lines: list[str] = []
    for finding in findings:
        lines.append(
            f"{path}:{finding.line}:{finding.column} [{finding.rule_id}] {finding.message} "
            f"修正方針: {finding.guidance} {REWRITE_SENTENCE}"
        )
    return lines


def validate_file(path: Path) -> None:
    if not path.exists():
        raise ValueError(f"file not found: {path}")
    if not path.is_file():
        raise ValueError(f"not a regular file: {path}")


def run_check(files: list[str], profile: str) -> int:
    output: list[str] = []
    for value in files:
        path = Path(value).expanduser()
        try:
            validate_file(path)
            if path.suffix.lower() not in SUPPORTED_SUFFIXES:
                raise ValueError(f"unsupported file type: {path}")
            findings = lint_file(path, profile)
        except (OSError, UnicodeError, ValueError) as error:
            print(f"japanese-prose-lint: {error}", file=sys.stderr)
            return 2
        output.extend(format_findings(path, findings))

    if not output:
        print("日本語 lint: 問題は見つかりませんでした")
        return 0
    print("\n".join(output))
    return 1


def resolve_path(value: object, cwd: Path) -> Path:
    if not isinstance(value, str) or not value:
        raise ValueError("path must be a non-empty string")
    path = Path(value).expanduser()
    return path if path.is_absolute() else cwd / path


def patch_paths(command: object, cwd: Path) -> list[Path]:
    if not isinstance(command, str):
        raise ValueError("patch command must be a string")
    values = re.findall(r"^\*\*\* (?:Add|Update) File: (.+)$|^\*\*\* Move to: (.+)$", command, re.MULTILINE)
    return [resolve_path(left or right, cwd) for left, right in values]


def hook_paths(payload: object, agent: str) -> list[Path]:
    if not isinstance(payload, dict):
        raise ValueError("invalid hook payload: expected an object")
    expected_event = "postToolUse" if agent in {"copilot", "cursor"} else "PostToolUse"
    if payload.get("hook_event_name") not in {expected_event, None}:
        raise ValueError(f"expected {expected_event}")

    cwd = resolve_path(payload.get("cwd", "."), Path.cwd())
    if agent == "copilot":
        tool_name = payload.get("toolName")
        tool_input = payload.get("toolArgs")
        accepted_tools = {"create", "edit", "apply_patch", "Write", "Edit", "MultiEdit"}
    else:
        tool_name = payload.get("tool_name")
        tool_input = payload.get("tool_input")
        accepted_tools = {
            "Edit", "Write", "MultiEdit", "apply_patch",
            "write_to_file", "replace_file_content", "multi_replace_file_content",
        }
    if tool_name not in accepted_tools:
        return []
    if not isinstance(tool_input, dict):
        raise ValueError("invalid hook payload: missing tool_input")

    values: list[object] = []
    for key in ("file_path", "filePath", "path"):
        if key in tool_input:
            values.append(tool_input[key])
    edits = tool_input.get("edits", [])
    if not isinstance(edits, list):
        raise ValueError("invalid hook payload: edits must be a list")
    for edit in edits:
        if isinstance(edit, dict):
            for key in ("file_path", "filePath", "path"):
                if key in edit:
                    values.append(edit[key])
                    break

    paths: list[Path] = []
    for value in values:
        path = resolve_path(value, cwd)
        if path not in paths:
            paths.append(path)
    for key in ("command", "patch", "patchText"):
        if key in tool_input:
            for path in patch_paths(tool_input[key], cwd):
                if path not in paths:
                    paths.append(path)
    return paths


def changed_text_lines(payload: object, agent: str) -> set[str] | None:
    """Return stripped lines introduced by an in-place edit, or None to lint the whole file.

    Only in-place edit tools (Edit / MultiEdit / apply_patch) are scoped; Write and
    create tools return None because their whole content is new.
    """
    if not isinstance(payload, dict):
        return None
    if agent == "copilot":
        tool_name = payload.get("toolName")
        tool_input = payload.get("toolArgs")
    else:
        tool_name = payload.get("tool_name")
        tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return None

    texts: list[str] = []
    saw_edit_content = False
    if tool_name in {"Edit", "MultiEdit", "edit"}:
        candidates: list[object] = [tool_input]
        edits = tool_input.get("edits")
        if isinstance(edits, list):
            candidates.extend(edits)
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            for key in ("new_string", "newString", "new_str"):
                value = candidate.get(key)
                if isinstance(value, str):
                    saw_edit_content = True
                    texts.append(value)
    elif tool_name == "apply_patch":
        for key in ("command", "patch", "patchText"):
            value = tool_input.get(key)
            if not isinstance(value, str):
                continue
            for line in value.splitlines():
                if line.startswith("*** ") or line.startswith("+++") or line.startswith("---"):
                    continue
                if line.startswith("+"):
                    saw_edit_content = True
                    texts.append(line[1:])
                elif line.startswith("-"):
                    saw_edit_content = True
    else:
        return None
    if not saw_edit_content:
        # The payload does not carry the edited text, so lint the whole file.
        return None
    # An in-place edit that introduced no text (a deletion) has nothing to report.
    return {line.strip() for text in texts for line in text.splitlines() if line.strip()}


def restrict_to_changed_lines(path: Path, findings: list[Finding], changed: set[str] | None) -> list[Finding]:
    if changed is None:
        return findings
    file_lines = path.read_text(encoding="utf-8").splitlines()
    kept: list[Finding] = []
    for finding in findings:
        if not 1 <= finding.line <= len(file_lines):
            continue
        line = file_lines[finding.line - 1]
        # Substring match so a partial-line replacement still counts as touching the line.
        if any(fragment in line for fragment in changed):
            kept.append(finding)
    return kept


def run_hook(agent: str, profile: str) -> int:
    try:
        payload = json.load(sys.stdin)
        paths = hook_paths(payload, agent)
    except (json.JSONDecodeError, ValueError) as error:
        print(f"japanese-prose-lint: invalid hook payload: {error}", file=sys.stderr)
        return 2

    # Whole-file findings on untouched lines get re-reported on every edit of a
    # long document, so in-place edits only report lines the edit introduced.
    changed = changed_text_lines(payload, agent)
    output: list[str] = []
    for path in paths:
        if path.suffix.lower() not in SUPPORTED_SUFFIXES:
            continue
        if "/.agent/work/" in path.as_posix():
            continue
        try:
            validate_file(path)
            findings = restrict_to_changed_lines(path, lint_file(path, profile), changed)
        except (OSError, UnicodeError, ValueError) as error:
            print(f"japanese-prose-lint: {error}", file=sys.stderr)
            return 2
        output.extend(format_findings(path, findings))

    if not output:
        return 0

    visible_output = output[:MAX_HOOK_FINDINGS]
    omitted = len(output) - len(visible_output)
    if omitted:
        visible_output.append(f"ほか{omitted}件あります。表示された箇所を修正した後、lint を再実行してください。")
    context = "日本語 lint で修正候補が見つかりました。内容と根拠を変えずに修正し、編集後に再確認してください。\n" + "\n".join(visible_output)
    if agent == "copilot":
        response = {"additionalContext": context}
    elif agent == "cursor":
        response = {"additional_context": context}
    else:
        response = {
                "hookSpecificOutput": {
                    "hookEventName": "PostToolUse",
                    "additionalContext": context,
                }
            }
    print(json.dumps(response, ensure_ascii=False))
    return 0


def main() -> int:
    args = parse_args()
    if args.check:
        return run_check(args.files, args.profile)
    return run_hook(args.hook_agent, args.profile)


if __name__ == "__main__":
    raise SystemExit(main())
PY
