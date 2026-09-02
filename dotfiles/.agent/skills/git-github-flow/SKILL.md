---
name: git-github-flow
description: Use when Git or GitHub work involves authentication, repositories, remotes, worktrees, branches, issues, pull requests, reviews, CI, releases, recovery, or linked development work.
---

# Git and GitHub Flow

## USE FOR:

- auth、repo、worktree、branch、Issue/PR、CI、履歴復旧

## DO NOT USE FOR:

- Git/GitHub操作なし
- CI workflow編集（`ci-cd`）

## Contract

- 最初にauth、status、repo規約、dirty state、base、template、重複、labels、履歴を確認し、並列作業はworktreeへ分離する。
- write操作は明示範囲だけ。
- Issue/PRのassigneeは`@me`を使わず認証loginを明示する。既存labelsもreadbackし、欠落は補正、確認不能なら未完了。
- 新規PRは必ずDraft。期待CIが未登録・pending・failureの間はReady禁止。全成功後だけReadyにする。
- 既存IssueだけをDevelopment linkし、PRのためのIssueは作らない。commit/PR title・PR見出しは英語、本文は指示→最近のPR→日本語。
- force-pushは直接・間接とも明示許可なしで禁止。共有履歴の取消はrevert、未公開commitの整理はsquash/rebaseを使う。

## Routing

- 通常操作、auth、Issue→PR、repo管理: [operations.md](references/operations.md)
- PR review/投稿: [code-review.md](references/code-review.md)
- 履歴変更/gh-stack: [history-and-stacks.md](references/history-and-stacks.md)
- repo template不在時: [templates.md](references/templates.md)

## Completion

URL、base/head SHA、metadata、既存Links、CI、remote stateをreadbackし、未検証・pending・既存failureを分けて報告する。
