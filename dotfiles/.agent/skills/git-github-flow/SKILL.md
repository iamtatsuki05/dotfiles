---
name: git-github-flow
description: Use when Git or GitHub work involves auth, remotes, worktrees, branches, commits, Issues, pull requests, PR review, CI checks, releases, or history recovery. DO NOT USE FOR tasks with no Git/GitHub operation, or for editing CI workflow files (use ci-cd).
---

# Git and GitHub Flow

Git/GitHub 作業を「確認 → 依頼された範囲だけ write → readback」の順で進める。GitHub の読み書きは `gh`、local 操作は `git` を使う。

## 最初に確認する

- 対象 repo と login を推測しない。`Could not resolve to a Repository` は owner 推測の誤りなので、次で確定した値を `-R "$base_repo"` と `--assignee "$current_user"` に使う。

  ```bash
  base_repo=$(gh repo view --json nameWithOwner --jq .nameWithOwner)   # checkout の remote から解決
  github_host=$(gh repo view -R "$base_repo" --json url --jq '.url | sub("^https://"; "") | split("/")[0]')
  gh auth status --hostname "$github_host"
  current_user=$(gh api --hostname "$github_host" user --jq .login)
  test -n "$base_repo" && test -n "$current_user"
  ```

  fork checkout では、Issue/PR を置く `base_repo` と head を置く `branch_repo` を remote URL から別々に確定する(手順は [operations.md](references/operations.md))。
- `git status --short --branch`、既存 worktree、repo の `AGENTS.md` / `CONTRIBUTING.md`、重複 Issue/PR、`gh label list` を確認する。dirty な checkout を stash / reset / commit で空けず、並列作業は branch ごとに worktree を分ける。
- 直接の merge 先 `target_base` は、今回の明示指示 → repo 規約 → 同種作業の最近の PR → 既定 branch の順で決め、branch の起点、`gh issue develop --base`、`gh pr create --base` に同じ値を使う。候補が複数あって決められなければ、作成前に止めて確認する。

## write の境界

- read-only の `git` / `gh` は自由。`git add`、commit、push、Issue/PR の作成・編集、merge、close、release、repo 設定変更は、それぞれ依頼された範囲だけ行う。文面作成の依頼は操作の許可ではない。
- `git push --force` / `--force-with-lease` と、内部で force-push し得るコマンド(`gh stack sync` など)は、対象 branch を含む明示許可なしで実行しない。共有済み履歴の取消は revert、squash / rebase は未公開 commit だけに使う。
- `gh pr create` は未公開 branch を暗黙に push し得る。branch 公開が依頼に含まれる場合だけ先に明示的に push する。

## Issue / PR の作成

- 既存 Issue があるときだけ `gh issue develop` で Development link し、PR のために Issue を作らない。`Closes #N` は base が既定 branch で merge が Issue を完了させる場合だけ、それ以外は `Refs #N` を使う。
- assignee は `@me` を使わず `--assignee "$current_user"`。label は `gh label list` にある既存から最小限を選び、推測で新設しない。
- commit subject、PR title、PR 本文の Markdown 見出しは英語にする。本文の言語は、今回の明示指示 → 同種作業の human-authored な最近の PR → 日本語の順で決める。repo に template があればその構造を保ち、無い場合だけ [templates.md](references/templates.md) を使う。
- 新規 PR は local 検証済みでも必ず Draft で作り、title は `[WIP] <concise English title>` にする。

  ```bash
  gh pr create -R "$base_repo" --base "$target_base" --head "$head_arg" \
    --draft --assignee "$current_user" --title "$title" --body-file "$body_file"
  ```

- 作成・編集直後に readback し、`isDraft`、assignees に `$current_user`、labels を確認する。欠けていれば `gh pr edit --add-assignee "$current_user"` / `--add-label` で補正して再確認し、それでも欠ければ完了扱いにせず権限・assignability の問題として報告する。

  ```bash
  gh pr view "$pr_url" -R "$base_repo" \
    --json url,title,isDraft,baseRefName,baseRefOid,headRefName,headRefOid,assignees,labels,closingIssuesReferences
  gh pr checks "$pr_url" -R "$base_repo"
  ```

## Ready gate

- Ready にできるのは、期待する check 名が全部登録され、同じ head SHA で全 check 成功と metadata を確認した後だけ。未登録・pending・failure・判定不能の間は Draft を維持し、Ready にして check を起動する fallback は使わない。

  ```bash
  checked_head=$(gh pr view "$pr_url" -R "$base_repo" --json headRefOid --jq .headRefOid)
  gh pr checks "$pr_url" -R "$base_repo" --watch --interval 10
  test "$checked_head" = "$(gh pr view "$pr_url" -R "$base_repo" --json headRefOid --jq .headRefOid)"
  ```

- gate 通過後に `gh pr edit` で `[WIP]` を外した title と最終本文へ置き換え(既存の `Closes` / `Refs` / `Part of` / `Depends on` と URL を失わない)、`gh pr ready "$pr_url" -R "$base_repo"` を実行して `isDraft=false` を readback する。

## 詳細を読む場面

- Issue から実装して verified PR まで、fork / organization fork からの PR、repo 作成・remote・release・settings: [operations.md](references/operations.md)
- 既存 PR のレビュー依頼、review / comment の投稿: [code-review.md](references/code-review.md)
- commit 整理、merge 済み変更の revert、失敗した rebase / revert の復旧、gh-stack: [history-and-stacks.md](references/history-and-stacks.md)

## 完了報告

URL、base / head SHA、Draft 状態、assignee、labels、Links、CI の状態、remote の状態を readback した値で報告する。「コマンドが成功した」だけで完了にせず、未検証・pending・既存 failure を分けて書く。
