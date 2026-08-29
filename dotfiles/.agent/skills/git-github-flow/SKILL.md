---
name: git-github-flow
description: Use when Git or GitHub work involves authentication, repositories, remotes, worktrees, branches, issues, pull requests, reviews, CI, releases, recovery, or linked development work.
---

# Git and GitHub Flow

Git/GitHub作業の単一entrypoint。GitHubは`gh`、local Gitは`git`を使う。

## USE FOR:

- auth、repo/fork、remote、worktree、branch、Issue、PR、review、CI、merge、release、revert
- commit整理、stacked PR、失敗した履歴操作の復旧

## DO NOT USE FOR:

- Git/GitHub操作を伴わない実装
- CI workflowだけの作業（`ci-cd`を使う）

## Contract

- 最初に `gh auth status` と `git status --short --branch` を確認する。
- read-onlyは自由。stage、commit、push、Issue/PR、merge等は操作ごとに明示された範囲だけ実行する。
- repo規約、dirty state、branch topology、template、重複、labels、過去履歴を先に確認し、独立作業はworktreeへ分離する。
- Issue/PRは`@me`、既存labels、Development linkをreadbackする。commit/PR titleとPR見出しは英語、本文は明示指示→同repoの最近のPR→日本語。
- force-pushは直接・間接とも明示許可なしで禁止。共有履歴の取消はrevert、未公開commitの整理はsquash/rebaseを使う。

## Routing

- 通常操作、auth、Issue→PR、repo管理: [operations.md](references/operations.md)
- PR review/投稿: [code-review.md](references/code-review.md)
- 履歴変更/gh-stack: [history-and-stacks.md](references/history-and-stacks.md)
- repo template不在時: [templates.md](references/templates.md)

## Completion

URL、base/head SHA、metadata、Links、CI、remote stateをreadbackし、未検証・pending・既存failureを分けて報告する。
