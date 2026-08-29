---
name: git-github-flow
description: Use when organizing Git and GitHub work involving worktrees, branches, issues, pull requests, drafts, labels, assignees, or linked development work.
---

# Git and GitHub Flow

Git/GitHub 作業を、レビューしやすい単位と追跡可能なリンクで整理する。GitHub の読み書きには `gh` を使い、ローカル Git 操作には `git` を使う。

## 最初に確認する

リポジトリの `AGENTS.md`、`CONTRIBUTING.md`、既定ブランチ、現在の dirty state、既存 worktree、重複 Issue/PR、利用可能なラベルを確認する。既存変更を stash、reset、commit して作業場所を空けない。

既定ブランチをそのまま作業baseと仮定せず、branch/PRごとの直接のmerge先を先に確定する。根拠の優先順は、今回の明示指示、リポジトリの `AGENTS.md` / `CONTRIBUTING.md` / branch設定、同じ作業種別の一貫した最近のPR履歴、既定ブランチの順とする。最高順位の利用可能な根拠が一意なら、下位の履歴はsanity checkにだけ使い、決定を覆さない。最高順位の根拠が不足・内部矛盾して複数の長期branchが候補になる場合は、作成前に停止して確認する。中間branchの証拠がなく既定branchだけが候補の場合に限り、既定branchへfallbackする。branchの存在だけではbaseを決めない。

例えば `main <- develop <- feature/*` なら、通常作業は `develop` から分岐してPRも `develop` 向けにし、`develop -> main` を最終統合とする。hotfixが `main` へ直接入る運用ならhotfixだけ `main` を使う。`main <- develop <- work/topic-integration <- child/*` では、子PRの直接baseは `work/topic-integration`、umbrella PRの直接baseは `develop`、最終統合PRのbaseは `main` と、PRごとに別の値を持つ。

```bash
base_repo=OWNER/REPO  # IssueとPRを置く対象。fork checkoutでは明示する
gh repo view -R "$base_repo" --json defaultBranchRef
gh pr list -R "$base_repo" --state all --limit 50 \
  --json number,title,baseRefName,headRefName,mergedAt,isDraft
git branch --all --no-color
```

Issue/PR を作る前に `.github`、リポジトリ直下、`docs/` の Issue/PR テンプレートを探す。テンプレートがあれば、その構造と必須項目を保持して埋める。無い場合だけ [references/templates.md](references/templates.md) を読む。

## 作業単位を決める

- 1 Issue は、目的・スコープ・完了条件を短時間で理解できる一つの成果にする。
- 原則として 1 Issue = 1 branch = 1 PR。PR は独立してレビュー・検証・merge できる大きさにする。
- 分割しすぎて全体像が失われる場合は、親 tracker Issue を作り、子 Issue/PR をチェックリストと `Part of` / `Depends on` リンクでネストする。
- 子変更を個別に上位branchへ入れられない場合だけ integration branch と umbrella PR を使う。既存の長期 `develop` はそのまま使い、新規に作る一時 integration branch は通常の命名規約に従って、例えば `work/<topic>-integration` とする。各 PR の base、依存順、親 Issue、umbrella PR を相互リンクする。子PRと非既定branch向けumbrella PRは `Refs`、既定ブランチへ入る最終統合PRだけが完了させるIssueへの `Closes` を持つ。

## Branch と worktree

リポジトリ固有の規約がなければ、lowercase kebab-case で `feature/`、`work/`、`hotfix/`、`bugfix/` を使う。新機能は `feature/`、通常作業は `work/`、緊急修正は `hotfix/`、通常の不具合修正は `bugfix/` とする。

独立した並列作業は、作業ごとに branch と worktree を分ける。現在の checkout が dirty な場合も worktree を優先する。Issue から開始するときは branch を Development に先に結び付ける。branchごとに確定した直接のmerge先を `target_base` とし、そのbranchの起点、`gh issue develop --base`、`gh pr create --base` の3か所で同じ値を使う。

GitHubの対象リポジトリを `base_repo`、それを指すGit remoteを `base_remote`、head branchを置くGitHubリポジトリを `branch_repo`、それを指すGit remoteを `head_remote` として対応付ける。同一リポジトリでは通常どちらのremoteも `origin` になる。forkでは `gh issue develop --branch-repo` と、各リポジトリを指すremoteを明示し、`origin` と決め打ちしない。

```bash
base_repo=OWNER/REPO
branch_repo=OWNER/REPO
base_remote=origin
head_remote=origin
target_base=develop  # このbranchの直接のmerge先
branch=bugfix/login-redirect-loop
test "$(gh repo view "$(git remote get-url "$base_remote")" --json nameWithOwner --jq .nameWithOwner)" = "$base_repo"
test "$(gh repo view "$(git remote get-url "$head_remote")" --json nameWithOwner --jq .nameWithOwner)" = "$branch_repo"
git fetch "$base_remote" "$target_base"
base_oid=$(git rev-parse "$base_remote/$target_base")
gh issue develop 42 -R "$base_repo" --branch-repo "$branch_repo" \
  --name "$branch" --base "$target_base"
git fetch "$head_remote" "$branch"
git worktree add ../repo-login-redirect-loop --track \
  -b "$branch" "$head_remote/$branch"
git merge-base --is-ancestor "$base_oid" "$head_remote/$branch"
```

既存 branch/path と衝突したら上書きや削除をせず、既存用途を確認する。同じファイルや共有状態を同時に変更する作業は並列化しない。

## 履歴変更とstacked PR

直接の `git push --force` / `--force-with-lease` だけでなく、内部でforce-pushし得るコマンドも、対象branchを含む明示指示がない限り実行しない。merge済み・共有済みの変更を取り消すときは履歴を書き換えずrevert commitとrevert PRを使う。進行中のlocal rebase/revertが失敗しただけなら、共有履歴をrevertせず対応する `--abort` で元へ戻す。

未公開branchでcommitが散らかった場合は、PR作成前に論理単位へsquash/rebaseし、変更後のtreeが同じこととテスト結果を確認する。公開済みbranchのsquash/rebaseはremote更新にforce-pushが必要になるため、明示許可なしでは行わない。

2件以上の変更が直線状に依存し、各layerを独立してレビューできる場合はgh-stackを候補にする。独立作業、branching DAG、1件のPRで十分な変更には使わない。gh-stackを使う、commitを整理する、merge済み変更を戻す、または失敗した履歴操作を復旧するときは [references/history-and-stacks.md](references/history-and-stacks.md) を読む。

## GitHub metadata と Development

- すべての `gh issue create` / `gh pr create` に `--assignee @me` を含める。既存 Issue/PR は `gh issue edit` / `gh pr edit` で `@me` を追加する。
- `gh label list` で確認した既存ラベルから、種類・領域・優先度など判断に役立つ最小限を選び、該当する Issue と PR の両方へ `--label` または `--add-label` で付ける。ラベルを推測で新設しない。適切な既存ラベルがなければ明示し、作成は別途許可を得る。
- GitHubのauto-closeを使う場合、PRのbaseが既定ブランチ以外なら、論理的に完了していても `Refs #123` を使う。PRのbaseが既定ブランチで、そのmergeがIssueを完了させる場合だけ `Closes #123` を使う。親 tracker は全体完了まで閉じず、`Part of #100` と子 Issue/PR のリンクを併記する。
- Issue と branch の Development リンクは `gh issue develop`、作成後の確認は `gh issue develop --list 123` のように対象 Issue を指定する。

GitHub 上の作成・編集・状態確認は `gh issue ...`、`gh pr ...`、`gh label ...`、必要な場合の `gh api ...` で行う。ユーザーが依頼していない push、merge、close、branch/worktree 削除まで権限を広げない。`gh pr create` は未公開branchを暗黙にpushし得るため、PR作成の依頼にbranch公開が含まれる場合だけ明示的にpushし、`git ls-remote --exit-code --heads "$head_remote" "$branch"` でheadの存在を確認する。

PR作成では対象repo・直接base・headをすべて明示する。同一リポジトリなら `head_arg=$branch`、user-owned forkなら `head_arg=$head_owner:$branch` とする。forkでは、明示的なopt-inがなければ `--no-maintainer-edit` を追加する。

```bash
maintainer_edit_args=()
if [ "$base_repo" != "$branch_repo" ]; then
  maintainer_edit_args=(--no-maintainer-edit)
fi
gh pr create -R "$base_repo" --base "$target_base" --head "$head_arg" \
  --assignee @me --title "$title" --body-file "$body_file" \
  "${maintainer_edit_args[@]}"
```

`gh pr create` が `OWNER:branch` を受け付けないorganization-owned forkでは、黙って別headへ切り替えない。ユーザーがPR作成を依頼している場合だけ、次のように全payloadを明示する。同一organization内でAPIが要求する場合は `head_repo=$branch_repo` も渡す。

```bash
pr_url=$(gh api "repos/$base_repo/pulls" --method POST \
  -f head="$head_owner:$branch" -f base="$target_base" \
  -f head_repo="$branch_repo" \
  -f title="$title" -f body="$(<"$body_file")" \
  -F draft=true -F maintainer_can_modify=false --jq .html_url)
gh pr edit "$pr_url" -R "$base_repo" --add-assignee @me \
  --add-label "$existing_label"
```

## タイトル、commit、Draft

PR title と commit subject は、短く具体的な命令形の英語にする。リポジトリが Conventional Commits を使う場合だけ `feat(scope): ...` など既存形式に合わせる。Issue title と本文の言語は、テンプレートと同じリポジトリの最近の履歴に合わせる。

PR本文のMarkdown見出し名は、ユーザー指示・既存テンプレート・過去PRの言語にかかわらず英語にする。最近のPRから言語を合わせる対象は見出し配下だけで、日本語見出しはコピーしない。既存PRテンプレートは、見出しレベル・順序・意味・必須項目を保持し、Markdown見出しの表示名だけを対応する英語名へ置き換える。HTMLコメント、表、checkbox、機械処理用marker、見出し以外の固定labelは変更しない。custom見出しの英語対応が一意でなければ推測で書き換えず、PR作成前に確認する。

見出し配下の説明文・箇条書き・チェック項目の言語は、今回の明示指示、同じリポジトリ・同種作業の代表的な最近のPR、デフォルト日本語の順で決める。履歴はhuman-authoredのReady/merged PRを優先し、bot・自動更新・未完成Draftは根拠にしない。言語が混在して一貫しない、または参考PRがない場合は日本語を使う。このPR言語規則はIssue見出しには適用しない。

PR本文を保存・送信する前に、`#` から始まる全Markdown見出し行を監査し、見出し名がすべて英語であることを確認する。fallback Ready PRは `Why` / `What` / `Verification` / `Links` / `Risks and remaining work`、fallback Draft PRは `Status` / `Why` / `Completed` / `Remaining` / `Review focus` / `Partial verification` / `Links` を使う。

途中で PR を開く場合は Draft にし、タイトルを `[WIP] <concise English title>` とする。本文には完了済み、残作業、今回見てほしい点、部分検証、関連 Issue を明記する。

Ready にする直前に実装と必要な検証を完了し、`gh pr edit` で `[WIP]` を外した最終 title と、最終的な変更・検証・リスクを反映した body に置き換えてから `gh pr ready` を実行する。本文置換時も既存の `Closes` / `Refs` / `Part of` / `Depends on` とURLを失わず、最終baseと完了条件に応じて `Closes` / `Refs` だけを更新する。未完了 PR が Ready なら `gh pr ready --undo` で Draft に戻す。

## 作成後の検証

作成・編集後は readback し、Issue/PR URL、base/head、Draft 状態、title、body、`@me`、labels、Development の closing link が意図どおりか確認する。branch作成直後は、作成直前に記録した `base_oid` がhead branchの祖先であることも確認する。PR時点の `baseRefOid` は作成後に進み得るため、`headRefOid` の祖先であることを要求せず、両OIDは現在のserver stateとして記録する。

```bash
gh issue view 42 -R "$base_repo" --json url,title,body,assignees,labels
gh issue develop --list 42 -R "$base_repo"
gh pr view 57 -R "$base_repo" \
  --json url,title,body,isDraft,baseRefName,baseRefOid,headRefName,headRefOid,headRepository,headRepositoryOwner,assignees,labels,closingIssuesReferences
gh pr checks 57 -R "$base_repo"
```

「コマンドが成功した」だけで完了にせず、readback と必要な CI/検証結果を報告する。
