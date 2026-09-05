# Pull Request Review

既存PRのreviewを依頼された場合だけ読む。

## Read-only assessment

`gh pr view`、`gh pr diff`、changed files、base/head SHA、checksを取得し、必要なら専用worktreeでtestを実行する。説明を読んで設計を確認し、主ロジック、test、周辺contractの順に読む。

findingは重大度順に、問題が成立する条件、具体的影響、最小のfile:line、必要な修正/追加testを示す。推測だけの指摘や、好みの整形はfindingにしない。問題がなければ「重大な問題なし」と明記する。

security、correctness、data loss、access control、concurrency、error handling、performance、test、documentationを確認し、review作法は `eng-practices` に従う。

## GitHubへの投稿

comment、inline review、approve、request changesは外部writeなので、明示的に投稿を求められた場合だけ行う。投稿前に対象PR、commit SHA、全comment、review eventを提示し、可能なら1件のatomic reviewとして送る。送信後はGitHubからreview/comment URLとstateをreadbackする。

自分の未commit変更の最終報告前レビューはこのreferenceではなく、AGENTS.md の read-only reviewer 手順に従う。
