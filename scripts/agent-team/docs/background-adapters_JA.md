# Direct background adapter実装

[English](background-adapters.md) · [対応matrix](support-matrix_JA.md)

CopilotとOpenCode向けのprovider adapterはdirect one-shot processです。ACPは使いません。Copilotの
read-only Planner/Reviewerは共通のstate v3 background lifecycleで実行できます。OpenCodeは同じ
lifecycleとsnapshot契約を個別に検証するまで拒否状態です。統合後も
runnerがOrcaのTask、Dispatch、terminal、`worker_done` lifecycleを管理し、
`agent_team.adapters`はprovider processのpreflightと実行だけを担当します。

## 想定profile

- GitHub Copilot CLI `1.0.81`: PlannerとReviewerのread-onlyだけ（実行可能）。
- OpenCode `1.18.25`: PlannerとReviewerのread-onlyだけ（adapterは実装済みだが未有効化）。

実行ファイルは、曖昧でないPATH identity、または既存のexactなmise installation
（`npm:@github/copilot@1.0.81` / `opencode@1.18.25`）から解決します。install、update、別providerへの
fallbackは行いません。canonical path、device、inode、size、mtime、SHA-256をsnapshotし、実行直前に
同じidentityを再検証します。

## 境界と認証

各turnで新しいtemporary read snapshotを作ります。元workspace、agent-team state、prompt directoryは
providerのcwdに渡しません。snapshotからは`.git`、gitignore対象、symlink、special file、secret-likeな
名前・拡張子、provider設定、MCP設定、Agent instructionを除外します。copyではnofollowのtype/inode検証と
atomic publishを使います。実行後は成功・失敗にかかわらずexactなtemporary rootを削除し、cleanup失敗も
隠さず報告します。providerにはrepository pathをsnapshotのcwdからの相対pathとして解決する固定指示を
追加します。元workspaceのabsolute pathをそのまま読むことは許可しません。

child environmentはallowlistから構築します。`GITHUB_TOKEN`、`GH_TOKEN`、他providerのkey、`ORCA_*`、
`NODE_OPTIONS`、proxy/endpoint overrideは継承しません。Copilotは既存のsubscription/keychain loginを使える
ようuserの`HOME`を維持しますが、`COPILOT_HOME`は隔離します。OpenCodeには共通の安全な環境に加え、明示的に
存在する`OPENCODE_API_KEY`だけを渡し、XDGのconfig/data/state/cache rootを隔離します。このtoolはsubscription
requestのbillingを保証せず、accountとquotaはproviderの責任です。

現状、両providerのone-shot commandはpromptをcommand lineに必要とします。値は1つのargv elementとして
hard limit付きで渡し、`shell=False`を使うためshell syntaxとして解釈しません。ただしlocal process tableには
見えるため、providerがstdin/file optionを提供するまでは高度に機密なpromptには使わないでください。

## Workerを拒否する理由

read-onlyの証拠だけでは安全なworkspace-write contractを証明できません。Workerには`.git`、state、secret、
symlink、network、process作成、cleanupについて別のpositive/negative matrixが必要です。その証拠が揃うまで、
CopilotとOpenCodeのWorkerはOrca Task作成前に失敗します。他の6つの認識済みharnessも同じ理由で拒否しています。

## 復旧

preflightまたはidentity検証に失敗した場合、provider processは起動しません。snapshot作成後にproviderまたは
runnerが失敗した場合はexactなsnapshot rootを削除し、外側のlifecycleがfailed `worker_done`を報告します。
version driftや実行ファイル差し替えを自動修復することはありません。exactなmanaged installationを復元して、
新しいturnを開始してください。
