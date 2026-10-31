# Operations Reference

Git/GitHub 作業を、レビューしやすい単位と追跡可能なリンクで整理する。GitHub の読み書きには `gh` を使い、ローカル Git 操作には `git` を使う。

## 最初に確認する

リポジトリの `AGENTS.md`、`CONTRIBUTING.md`、既定ブランチ、現在の dirty state、既存 worktree、重複 Issue/PR、利用可能なラベルを確認する。既存変更を stash、reset、commit して作業場所を空けない。

`git` / `gh` のread-only操作は自由に行ってよいが、`git add`、commit、push、Issue/PR作成・編集、merge、close、release、repository設定変更は、それぞれ依頼された範囲だけ実行する。stageを依頼された場合も今回の変更ファイルだけを対象にし、commitやpushへ許可を広げない。文面作成の依頼は、対応するGit/GitHub操作の許可ではない。

GitHub操作前に対象repositoryのhost/accountを確定し、hostを省略せず認証とloginを確認する。login取得失敗または空値ならwriteへ進まない。

```bash
github_host=$(gh repo view "$(git remote get-url "$base_remote")" --json url \
  --jq '.url | sub("^https://"; "") | split("/")[0]')
test -n "$github_host"
gh auth status --hostname "$github_host"
current_user=$(gh api --hostname "$github_host" user --jq .login)
test -n "$current_user"
```

未認証なら `gh auth login` の実行方針を確認し、tokenを出力・credential fileから抽出・remote URLへ埋め込み・平文保存しない。repository作成/fork、branch protection、secrets、releaseなど影響の大きい操作は、対象・影響・取消方法を示して明示許可を得る。

既定ブランチをそのまま作業baseと仮定せず、branch/PRごとの直接のmerge先を先に確定する。根拠の優先順は、今回の明示指示、リポジトリの `AGENTS.md` / `CONTRIBUTING.md` / branch設定、同じ作業種別の一貫した最近のPR履歴、既定ブランチの順とする。最高順位の利用可能な根拠が一意なら、下位の履歴はsanity checkにだけ使い、決定を覆さない。最高順位の根拠が不足・内部矛盾して複数の長期branchが候補になる場合は、作成前に停止して確認する。中間branchの証拠がなく既定branchだけが候補の場合に限り、既定branchへfallbackする。branchの存在だけではbaseを決めない。

例えば `main <- develop <- feature/*` なら、通常作業は `develop` から分岐してPRも `develop` 向けにし、`develop -> main` を最終統合とする。hotfixが `main` へ直接入る運用ならhotfixだけ `main` を使う。`main <- develop <- work/topic-integration <- child/*` では、子PRの直接baseは `work/topic-integration`、umbrella PRの直接baseは `develop`、最終統合PRのbaseは `main` と、PRごとに別の値を持つ。

```bash
base_repo=OWNER/REPO  # PRを置く対象。fork checkoutでは明示する
gh repo view -R "$base_repo" --json defaultBranchRef
gh pr list -R "$base_repo" --state all --limit 50 \
  --json number,title,baseRefName,headRefName,mergedAt,isDraft
git branch --all --no-color
```

IssueまたはPRを作るときは、対象に対応するテンプレートを `.github`、リポジトリ直下、`docs/` から探す。テンプレートがあれば、その構造と必須項目を保持して埋める。無い場合だけ [templates.md](templates.md) を読む。

## 作業単位を決める

- IssueはPRの前提ではない。ユーザーが既存Issueへの対応を指定した場合は、そのIssueをbranch/PRへ紐づける。Issueが指定されず、repositoryにも作成必須の規約がなければ、PRのためだけに新しいIssueを作らない。
- 新しいIssueは、ユーザーが作成を依頼した場合、repository規約で必須の場合、または別途追跡すべき親tracker・後続作業があり作成の許可を得た場合だけ作る。
- PRは、目的・スコープ・完了条件を短時間で理解でき、独立してレビュー・検証・mergeできる一つの成果にする。Issueがある場合は原則として 1 Issue = 1 branch = 1 PR とする。
- 分割しすぎて全体像が失われ、Issue作成も許可されている場合は、親tracker Issueを作り、子Issue/PRをチェックリストと `Part of` / `Depends on` リンクでネストする。
- 子変更を個別に上位branchへ入れられない場合だけ integration branch と umbrella PR を使う。既存の長期 `develop` はそのまま使い、新規に作る一時 integration branch は通常の命名規約に従って、例えば `work/<topic>-integration` とする。各 PR の base、依存順、親 Issue、umbrella PR を相互リンクする。子PRと非既定branch向けumbrella PRは `Refs`、既定ブランチへ入る最終統合PRだけが完了させるIssueへの `Closes` を持つ。

## Branch と worktree

命名とアプリ既定との競合は [SKILL.md のブランチ命名](../SKILL.md#ブランチの命名)に従う。branch名と直接のmerge先を確定してから作成する。

独立した並列作業は、作業ごとに branch と worktree を分ける。現在の checkout が dirty な場合も worktree を優先する。Issue から開始するときは branch を Development に先に結び付ける。branchごとに確定した直接のmerge先を `target_base` とし、そのbranchの起点、`gh issue develop --base`、`gh pr create --base` の3か所で同じ値を使う。

Issueを置くリポジトリを `issue_repo`、PRを置くリポジトリを `base_repo`、それを指すGit remoteを `base_remote`、head branchを置くGitHubリポジトリを `branch_repo`、それを指すGit remoteを `head_remote` として対応付ける。同一リポジトリでは通常どちらのremoteも `origin` になる。forkでは `gh issue develop --branch-repo` と、各リポジトリを指すremoteを明示し、`origin` と決め打ちしない。

```bash
base_repo=OWNER/REPO
issue_repo=OWNER/ISSUE_REPO  # 対象IssueのURLから確定する。同一repoならbase_repoと同じ値
branch_repo=OWNER/REPO
base_remote=origin
head_remote=origin
target_base=develop  # このbranchの直接のmerge先
branch=bugfix/login-redirect-loop
test "$(gh repo view "$(git remote get-url "$base_remote")" --json nameWithOwner --jq .nameWithOwner)" = "$base_repo"
test "$(gh repo view "$(git remote get-url "$head_remote")" --json nameWithOwner --jq .nameWithOwner)" = "$branch_repo"
git fetch "$base_remote" "$target_base"
base_oid=$(git rev-parse "$base_remote/$target_base")
gh issue develop 42 -R "$issue_repo" --branch-repo "$branch_repo" \
  --name "$branch" --base "$target_base"
git fetch "$head_remote" "$branch"
git worktree add ../repo-login-redirect-loop --track \
  -b "$branch" "$head_remote/$branch"
git merge-base --is-ancestor "$base_oid" "$head_remote/$branch"
```

既存 branch/path と衝突したら上書きや削除をせず、既存用途を確認する。同じファイルや共有状態を同時に変更する作業は並列化しない。

## 履歴変更とstacked PR

force-push境界、commit整理、merge済み変更のrevert、失敗した履歴操作の復旧、gh-stackの判断と操作は [history-and-stacks.md](history-and-stacks.md) に従う。2件以上の変更が直線状に依存し、各layerを独立してレビューできる場合だけgh-stackを候補にする。

## GitHub metadata と Development

- 実際に作成・編集するIssue/PRには、`@me`ではなく認証済みの明示login `$current_user` を `--assignee` / `--add-assignee` で設定する。片方を操作するために、もう片方を新規作成しない。
- 作成・編集直後にassigneesをreadbackし、`$current_user` が無ければ `gh issue edit` / `gh pr edit` の `--add-assignee "$current_user"` で補正して再確認する。再確認後も無ければ完了扱いせず、権限・assignabilityの問題として報告する。
- `gh label list` で確認した既存ラベルから、種類・領域・優先度など判断に役立つ最小限を選び、実際に作成・編集するIssue/PRへ `--label` または `--add-label` で付ける。ラベルを推測で新設しない。適切な既存ラベルがなければ明示し、作成は別途許可を得る。
- GitHubのauto-closeを使う場合、PRのbaseが既定ブランチ以外なら、論理的に完了していても `Refs #123` を使う。PRのbaseが既定ブランチで、そのmergeがIssueを完了させる場合だけ `Closes #123` を使う。親 tracker は全体完了まで閉じず、`Part of #100` と子 Issue/PR のリンクを併記する。
- 対応対象の既存Issueがある場合、IssueとbranchのDevelopment linkは `gh issue develop`、作成後の確認は `gh issue develop --list 123` のように対象Issueを指定する。IssueがないPRではDevelopment linkや`Closes` / `Refs`を捏造しない。

### PR–IssueのDevelopmentリンク

本文の `Refs #N` / `Part of #N`、timelineの相互参照、Issue–branchのリンクは、PR–IssueのDevelopmentリンクの証拠ではない。PR作成・編集時は、対応する既存Issueについて本文参照とDevelopment欄の関係を両方確認する。正しい既存リンクは保持し、不足分だけ追加する。

未完了を維持すべきIssueにPR本文・commit messageのclosing keywordや手動closingリンクがあれば、Ready・mergeへ進まず誤closeの影響を報告する。本文補正や手動リンクの削除は許可された範囲で行い、未修正なら紐づけ作業を完了扱いしない。手動リンクの削除は `removeCloseIssueReferences` に同じ `issueId` / `pullRequestIds` を渡して行うが、本文keywordは別途補正して両側を読み戻す。commit messageのkeywordはDevelopment欄に出ないためcommit履歴も確認し、履歴変更やforce-pushの許可がなければ未完了として報告する。

1. 対象Issueのrepo/番号、PRのrepo/番号/base、Issueをいつ完了させるかを確認する。本文参照は同一repoなら `#N`、別repoなら `OWNER/REPO#N` とし、keywordにも完全な参照を付ける。`Closes #N` は既定branch向けPR本文で解釈され、Developmentにも反映される。既に反映済みなら手動リンクを重複追加しない。非既定branch向けでは本文keywordだけではリンクされないため、手動リンクを確認する。
2. Developmentの手動PRリンクもclosing関係で、既定branchへのmerge時にIssueを自動closeし得る。`Refs` へ変えるだけでは手動リンクのclose効果を無効にできない。未完了を維持する親Issueや部分実装のIssueにはclosingリンクを作らず、本文参照を残してDevelopment未設定の理由を報告する。必要なら完了単位の子Issueを使うが、未承認で新設しない。base変更時も再確認する。
3. PRの `closingIssuesReferences` とIssueの `closedByPullRequestsReferences` を読み、双方に対象があるか確認する。branchからPRへの自動リンクも推測せず読み戻す。下記queryの各connectionを独立にページングする。`hasNextPage` がtrueの側の `endCursor` を、次回は `-f issueAfter=<cursor>` / `-f prAfter=<cursor>` として渡し、取得済みページと合わせて全対象を確認する。初回は両引数を省略する。途中のページだけで未リンクと判断しない。
4. 不足していれば、許可された対象に限って `addCloseIssueReferences` で追加する。PR本文の正しい既存参照は維持し、本文とDevelopmentが同じrepoの同じIssueを指すことも確認する。API・権限の不足やreadback不能は未完了として報告し、branchリンクや本文参照を代替の成功証拠にしない。

`gh issue view <number> -R <issue-repo> --json id --jq .id` と `gh pr view <number> -R <pr-repo> --json id --jq .id` で取得した空でないnode IDを、対象URL・解決済みの `$github_host` と照合して `issue_id` / `pr_id` に使う。読み戻しは次のqueryで行う。

```bash
gh api graphql --hostname "$github_host" -f issueId="$issue_id" -f prId="$pr_id" -f query='
query($issueId: ID!, $prId: ID!, $issueAfter: String, $prAfter: String) {
  issue: node(id: $issueId) { ... on Issue {
    url closedByPullRequestsReferences(first: 100, after: $issueAfter, includeClosedPrs: true) {
      nodes { id url } pageInfo { hasNextPage endCursor }
    }
  } }
  pr: node(id: $prId) { ... on PullRequest {
    url closingIssuesReferences(first: 100, after: $prAfter) {
      nodes { id url } pageInfo { hasNextPage endCursor }
    }
  } }
}'
```

追加が必要で自動closeの条件にも問題がない場合だけ、次を実行して同じqueryで再確認する。終了コードやmutationの応答だけでリンク済みと報告しない。

```bash
gh api graphql --hostname "$github_host" -f issueId="$issue_id" -f prId="$pr_id" -f query='
mutation($issueId: ID!, $prId: ID!) {
  addCloseIssueReferences(input: {issueId: $issueId, pullRequestIds: [$prId]}) {
    issue { id url }
  }
}'
```

仕様は [GitHubのIssueとPRのリンク](https://docs.github.com/en/issues/tracking-your-work-with-issues/using-issues/linking-a-pull-request-to-an-issue) と [GraphQLのaddCloseIssueReferences](https://docs.github.com/en/graphql/reference/issues#addcloseissuereferences) を参照する。紐づける既存IssueがないPRでは「対象Issueなし」とし、この操作を行わない。

GitHub 上の作成・編集・状態確認は `gh issue ...`、`gh pr ...`、`gh label ...`、必要な場合の `gh api ...` で行う。ユーザーが依頼していない push、merge、close、branch/worktree 削除まで権限を広げない。当該PRのマージ報告を受けた場合の限定的な整理は、[マージ後の後片付け](post-merge-cleanup.md)に従う。`gh pr create` は未公開branchを暗黙にpushし得るため、PR作成の依頼にbranch公開が含まれる場合だけ明示的にpushし、`git ls-remote --exit-code --heads "$head_remote" "$branch"` でheadの存在を確認する。

PR作成では対象repo・直接base・headをすべて明示する。同一リポジトリなら `head_arg=$branch`、user-owned forkなら `head_arg=$head_owner:$branch` とする。forkでは、明示的なopt-inがなければ `--no-maintainer-edit` を追加する。

```bash
maintainer_edit_args=()
if [ "$base_repo" != "$branch_repo" ]; then
  maintainer_edit_args=(--no-maintainer-edit)
fi
gh pr create -R "$base_repo" --base "$target_base" --head "$head_arg" \
  --draft --assignee "$current_user" --title "$title" --body-file "$body_file" \
  "${maintainer_edit_args[@]}"
```

`gh pr create` が `OWNER:branch` を受け付けないorganization-owned forkでは、黙って別headへ切り替えない。ユーザーがPR作成を依頼している場合だけ、次のように全payloadを明示する。同一organization内でAPIが要求する場合は `head_repo=$branch_repo` も渡す。

```bash
pr_url=$(gh api "repos/$base_repo/pulls" --method POST \
  -f head="$head_owner:$branch" -f base="$target_base" \
  -f head_repo="$branch_repo" \
  -f title="$title" -f body="$(<"$body_file")" \
  -F draft=true -F maintainer_can_modify=false --jq .html_url)
gh pr edit "$pr_url" -R "$base_repo" --add-assignee "$current_user" \
  --add-label "$existing_label"
```

## タイトル、commit、Draft

PR title と commit subject は、短く具体的な命令形の英語にする。リポジトリが Conventional Commits を使う場合だけ `feat(scope): ...` など既存形式に合わせる。Issue title と本文の言語は、テンプレートと同じリポジトリの最近の履歴に合わせる。

commit message、PR title/bodyを作る前に、同じrepositoryの `git log` / `git show` / `gh pr list` / `gh pr view` をread-onlyで確認し、既存の形式・粒度へ合わせる。

PR本文のMarkdown見出し名は、ユーザー指示・既存テンプレート・過去PRの言語にかかわらず英語にする。最近のPRから言語を合わせる対象は見出し配下だけで、日本語見出しはコピーしない。既存PRテンプレートは、見出しレベル・順序・意味・必須項目を保持し、Markdown見出しの表示名だけを対応する英語名へ置き換える。HTMLコメント、表、checkbox、機械処理用marker、見出し以外の固定labelは変更しない。custom見出しの英語対応が一意でなければ推測で書き換えず、PR作成前に確認する。

見出し配下の説明文・箇条書き・チェック項目の言語は、今回の明示指示、同じリポジトリ・同種作業の代表的な最近のPR、デフォルト日本語の順で決める。履歴はhuman-authoredのReady/merged PRを優先し、bot・自動更新・未完成Draftは根拠にしない。言語が混在して一貫しない、または参考PRがない場合は日本語を使う。このPR言語規則はIssue見出しには適用しない。

PR本文を保存・送信する前に、`#` から始まる全Markdown見出し行を監査し、見出し名がすべて英語であることを確認する。fallback Ready PRは `Why` / `What` / `Verification` / `Risks and remaining work`、fallback Draft PRは `Status` / `Why` / `Completed` / `Remaining` / `Review focus` / `Partial verification` を使い、関連先がある場合だけ `Links` を加える。

新規PRは実装やlocal検証が完了済みでも、必ず `gh pr create --draft` で作り、タイトルを `[WIP] <concise English title>` とする。本文には完了済み、残作業またはCI待ち、今回見てほしい点、部分検証、関連Issueがあればそのリンクを明記する。作成直後に `isDraft=true` とassigneeをreadbackする。初回からReadyで作らず、Ready作成を求められていても次のgateまではDraftを維持する。

Readyへ変更できるのは、実装とlocal検証が完了し、期待checkの全名称が登録され、全check成功とmetadataを同じhead SHAで確認できた後だけとする。PR作成前にworkflow、branch protection/ruleset、同じbaseの最近のPRから期待check名を列挙する。一つでも未登録・欠落・判定不能ならActions登録待ちとしてDraftを維持する。repositoryにCIが無い場合は、同じ根拠からcheckが無いことを確認し、その根拠とlocal検証を記録してから進める。

```bash
checked_head=$(gh pr view "$pr_url" -R "$base_repo" --json headRefOid --jq .headRefOid)
gh pr checks "$pr_url" -R "$base_repo" --watch --interval 10
current_head=$(gh pr view "$pr_url" -R "$base_repo" --json headRefOid --jq .headRefOid)
test "$checked_head" = "$current_head"
```

pending、failure、cancel中はDraftを維持する。check成功後にhead SHAが変わっていれば、新しいheadのlocal検証、期待check登録、全成功の確認をやり直す。

期待されるcheckが `ready_for_review` でしか起動しない構成では、このgateと両立しない。Readyにして起動するfallbackは使わず、Draftを維持したままworkflowまたは運用の変更が必要なblockerとして報告する。

gate通過後に `gh pr edit` で `[WIP]` を外した最終titleと、最終的な変更・検証・リスクを反映したbodyに置き換え、`gh pr ready "$pr_url" -R "$base_repo"` を実行する。本文置換時も既存の `Closes` / `Refs` / `Part of` / `Depends on` とURLを失わず、最終baseと完了条件に応じて `Closes` / `Refs` だけを更新する。最後に `isDraft=false` とmetadataを再確認する。gate未達のReady PRは `gh pr ready --undo` でDraftに戻す。

## 作成後の検証

作成・編集後は readback し、作成・編集したIssue/PRのURL、base/head、Draft状態、title、body、`$current_user`、labelsを確認する。assigneesに `$current_user` が無ければ前述の補正と再確認を行う。対応する既存Issueがある場合は上記のPR–IssueのDevelopmentリンクを双方から確認し、branchリンクの確認だけで済ませない。branch作成直後は、作成直前に記録した `base_oid` がhead branchの祖先であることも確認する。PR時点の `baseRefOid` は作成後に進み得るため、`headRefOid` の祖先であることを要求せず、両OIDは現在のserver stateとして記録する。

Issue側の `issue_repo` は実際のIssueのrepository、PR側の `base_repo` はPRを置くrepositoryとする。同一repoの場合だけ両者が同じ値になる。

```bash
gh issue view 42 -R "$issue_repo" --json url,title,body,assignees,labels
gh issue develop --list 42 -R "$issue_repo"
gh pr view 57 -R "$base_repo" \
  --json url,title,body,isDraft,baseRefName,baseRefOid,headRefName,headRefOid,headRepository,headRepositoryOwner,assignees,labels,closingIssuesReferences
gh pr checks 57 -R "$base_repo"
```

「コマンドが成功した」だけで完了にせず、readback と必要な CI/検証結果を報告する。

## Repository Management

GitHub repositoryの作成、clone、fork、remote、settings、secrets、releaseを依頼された場合だけ読む。

- cloneは対象owner/repoと保存先を確認し、既存directoryへ上書きしない。
- repository作成ではowner、name、visibility、template、default branchを明示し、private/publicを推測しない。
- forkではbase repository、fork先owner、clone有無を確認する。`base_repo` / `base_remote` と `branch_repo` / `head_remote` を分け、remote URLをreadbackする。
- remote追加・変更では既存fetch/push URL、tracking branch、`remote.pushDefault`を確認する。credentialをURLへ埋め込まない。
- branch protection、Actions secrets、repository variables、visibility、archival、transfer、deleteは高影響操作。対象、影響、取消可否を示し、明示許可なしに変更しない。secret値は出力・readbackしない。
- releaseはtag、target SHA、title/body、draft/prerelease、assetをpreviewし、明示許可後に作成する。作成後はrelease URL、tag、target SHA、asset一覧をreadbackする。

GitHub操作には `gh repo` / `gh api` / `gh release` を使う。flagやpayloadが不明な場合は現在の `gh ... --help` とGitHub API schemaを確認し、curl/token fallbackを作らない。
