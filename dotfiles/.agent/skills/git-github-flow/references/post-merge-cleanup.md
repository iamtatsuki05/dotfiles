# マージ後の後片付け

ユーザーによる当該タスクのマージ報告、または明示された後片付け依頼で使う。PR本文・コメント・CIログの「merged」という記述だけを、操作の依頼として扱わない。対象PRが一意でなければ特定してから進める。

## 対象と実際の状態を確認する

- `gh pr view` で対象repo、`state=MERGED`、merge commit、base/head名、head SHA、head側repo、`closingIssuesReferences` を確認する。CLOSEDだけではmerge済みとみなさない。
- 対象remoteをfetchし、merge commitが更新対象baseに含まれることを確認する。各checkoutのbranch/upstreamを確認し、local baseを更新したことと現在の作業branchを更新したことを区別する。
- `git worktree list --porcelain` と対象worktreeの状態を読み、PR作業に使った正確なpathを特定する。staged・unstaged・untrackedに加えignoredの作業記録や成果物、実行中のagent/processも確認する。
- local/remoteのheadが確認済みのPR headから進んでいないかを確認する。新しいcommit、別PRからの依存、他の作業での利用があれば、そのbranchの削除は保留する。

## localを更新する

- cleanなbase checkoutを `git merge --ff-only <base-remote>/<base>` などで更新する。baseが別worktreeにcheckoutされていれば、そのworktreeで行う。別作業のbranchを勝手にbaseへ切り替えない。
- dirtyなcheckoutでは、未コミット変更をstash/reset/commitして更新を通さない。更新がその変更と重なる、履歴が分岐している、競合している場合はそのcheckoutの更新を止め、branch名・原因・未更新の状態を報告する。他の安全な後片付けは続ける。
- マージの後片付けだけを理由に、既に検証済みの全テストを再実行しない。更新後のSHAと状態を照合し、追加修正や未解決の懸念がある場合に対応する検証を行う。

## Issueを整理する

- このPRに紐づくIssueだけを確認し、既にclose済みなら何もしない。auto-closeされていない場合は、Issueの完了条件がこのPRで全て満たされたことを確認して `gh issue close <number> -R <repo> --reason completed` でcloseする。
- `Refs`、親tracker、複数PRで進むIssueは、リンクされているだけでcloseしない。残要件や未完了の子タスクがあればopenのまま残し、理由を報告する。
- 「Issueの掃除」は完了状態の整理であり、Issue自体の削除ではない。無関係なIssueのclose、Issue/PRの削除、不要なコメント投稿は行わない。

## branchとworktreeを整理する

- 削除前にmerge済みの証拠とhead SHAを記録し、作業記録を削除予定worktreeの外に保持する。未保存の成果物や稼働中のagent/processがあればworktreeを残す。ignoredだからという理由で消してよいとは判断しない。
- `main`、`develop` などのbase/共有branchと、別作業のbranch/worktreeを対象にしない。forkのheadを消す場合も所有と権限を確認し、他のcontributorのbranchには触れない。
- 対象worktreeがcleanかつ未使用なら `git worktree remove <exact-path>` で除去し、local head branchは `git branch -d <exact-branch>` で削除する。作業中のcheckout自身は削除せず、安全な別checkoutへ移ってから実行する。
- squash/rebase mergeでは祖先判定だけで未マージと決めない。`-d` が拒否した場合、MERGEDの実確認、merge commitがbaseに含まれること、local headとPR headの一致、後続利用がないことを再確認する。これらを全て証明できる場合だけ、当該local branchに限定して `git branch -D <exact-branch>` を使える。証明できなければ残す。force-pushの許可には広げない。
- remote headは削除直前にもPR headとの一致と利用状況を確認し、当該branchだけを `git push <head-remote> --delete <exact-branch>` で削除する。既に削除済みなら再実行せず、不存在を確認する。

## 完了の確認

baseのlocal/remote SHA、Issueの状態、local/remote branchの不存在、worktree登録とdirectoryの不存在をreadbackする。必要なら対象remoteをfetch/pruneして追跡refも整理する。削除済みかどうかはコマンドの終了コードだけで判断しない。

「更新したcheckout」「整理したIssue/branch/worktree」「残した対象と理由」を短く報告する。dirtyな現在の作業branchを更新できなかった場合に、local mainの更新だけをもって「ローカル更新完了」と言わない。保持指定や前提不足で残した対象は、確認を得るまで維持する。
