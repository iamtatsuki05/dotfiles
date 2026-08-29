# Fallback Issue and Pull Request Templates

リポジトリに該当テンプレートが無い場合だけ使う。Issue本文は同じリポジトリの最近のIssueに合わせる。以下のPR言語規則はReady/Draft PRだけに適用し、Issue見出しには適用しない。PRの見出し名は下記の英語表記を維持し、日本語見出しを過去PRからコピーしない。PRの説明文・箇条書き・チェック項目だけを、今回の明示指示、同じリポジトリ・同種作業のhuman-authoredな最近のReady/merged PR、デフォルト日本語の順で決める。言語が混在・不明なら日本語を使う。該当しない節は空欄で残さず削除する。Links は、PRのbaseが既定ブランチでmerge時にIssueを完了させる場合だけ `Closes`、それ以外は `Refs` を残す。

## Work Issue

```markdown
## Purpose

<Why this work is needed and what outcome it should produce.>

## Scope

- <Included change>
- <Included change>

## Acceptance criteria

- [ ] <Observable completion condition>
- [ ] <Required verification>

## Dependencies and links

- Parent: <parent issue or "None">
- Depends on: <issue/PR or "None">

## Out of scope

- <Nearby work intentionally excluded>
```

## Bug Issue

```markdown
## Problem

<Observed behavior and impact.>

## Reproduction

1. <Step>
2. <Step>

## Expected behavior

<Expected result.>

## Actual behavior

<Actual result, including the exact error when relevant.>

## Acceptance criteria

- [ ] <The regression is covered by a test or equivalent check.>
- [ ] <The fix is verified in the affected environment.>

## Risk and rollback

<Production risk and rollback condition when applicable.>
```

## Ready Pull Request

Use the WHY / WHAT / VERIFICATION structure while keeping links and risk explicit.

```markdown
## Why

<この変更が必要な理由、ユーザー影響、判断背景。>

## What

- <主な変更>
- <主な変更>

## Verification

- `<実行したコマンドまたは確認>`: <結果>
- <手動、表示、環境固有の確認>: <結果>

## Links

Closes #<既定ブランチへのmergeで完了させるIssue>
Refs #<このPRでは完了させないIssue>
Part of #<親Issue。該当する場合>
Depends on #<依存するPRまたはIssue。該当する場合>

## Risks and remaining work

- <既知のリスク、展開時の注意、後続Issue>
```

## Draft Pull Request

```markdown
## Status

早期レビュー用のDraftです。まだマージ可能ではありません。

## Why

<この作業が必要な理由。>

## Completed

- <実装済みの内容>

## Remaining

- [ ] <残っている実装>
- [ ] <残っている検証>

## Review focus

- <早めに確認してほしい論点>

## Partial verification

- `<コマンドまたは確認>`: <現時点の結果と制約>

## Links

Closes #<既定ブランチへのmergeで完了させるIssue>
Refs #<このPRでは完了させないIssue>
Part of #<親Issue。該当する場合>
Depends on #<依存するPRまたはIssue。該当する場合>
```

Readyにする前にDraft本文を最終PR本文へ置き換え、`Status`だけを削除して済ませない。既存の `Closes` / `Refs` / `Part of` / `Depends on` とURLは失わず、最終baseと完了条件に合わせてclosing keywordだけを更新する。
