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

Copilotは、既存のexactなmise installation（`npm:@github/copilot@1.0.81`）に含まれるplatform-native
executableだけを解決します。npm loaderやPATH衝突へfallbackしません。OpenCodeは曖昧でないPATH identity、
またはexactな`opencode@1.18.25` mise installationを使います。install、update、別providerへのfallbackは
行いません。canonical path、device、inode、size、mtime、SHA-256をsnapshotし、実行直前に同じidentityを
再検証します。

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

## version付きprobe manifestとreceipt

安全probe間では、providerを起動しない`agent_team.probe_receipts`の
schema-version付きmanifestとreceiptを受け渡します。manifestにはharness、permission profile、OS/architecture、
probe revision、executableのpath/version/SHA-256、固定argvのSHA-256、prompt transport、snapshot cwd、environment nameのallowlist、
sandbox policy identity、必須matrixを固定します。read-onlyとworkspace-writeはそれぞれpositive phaseに加えて、
`outside-path`、`symlink`、`git`、`secret`、`local-network`、`external-network`、`process`、`cleanup`を
個別phaseとして要求します。

receiptには固定identityと構造化した観測だけを保存します。各phaseは試行有無、tool使用有無、限定された結果
（`passed`、`failed`、`timeout`、`inconclusive`、`not-run`）、exit code/timeout、structured tool evidence、child process・
session・container・temporary rootのcleanup inventoryを記録します。prompt本文、raw log、environment value、token、API key、
cookieを保存するfieldは設けません。argvはdigest、environmentは変数名だけを保存します。未知のkey/schema version、
phaseの重複、矛盾またはphaseと一致しないevidenceはfail closedで拒否します。

`judge_profile(manifest, receipt)`はproviderを起動せず、常に同じ結果を返します。全必須phaseを試行し、evidenceと
全identityが一致し、cleanup inventoryが空の場合だけ`candidate`です。未試行phaseは`not-run`、authentication・
account・Docker・package・quota・platformの前提不足は`blocked`、tool failure・timeout・evidence不備・identity drift・
cleanup残留は`rejected`です。fixtureはmoduleのpure Python APIからproviderを起動せず判定できます。

## OpenCode probeの判定

今回の`agent_team.opencode_probe`はstatic-onlyです。固定したinstallation identityをreadし、role tokenからredactedな
manifestを作るだけで、OpenCodeを起動しません。provider eventのparse、current candidate receiptのassembly、live runnerも
公開しません。認証済みpermission matrixはIssue #22の後続sliceで扱います。

固定したOpenCode `1.18.25` executableのSHA-256は
`88eed7b0c2431162422cb0625aa68a55239970446951e4c9aad6a4f1fbc232b9`でした。static recordにはdevice、inode、size、mtime、
hashを残しますが、pathは`/probe/opencode`へredactします。rawとsnapshotの固定profileは、currentの
`auth-list-zero-credentials` observationに基づく`blocked-authentication`で、required phaseはすべて`not-run`です。
過去のraw workspace synthetic-marker symlink escapeは、`unverified`なhistorical provenanceを持つ別の`rejected`
observationとして保持し、current runへ昇格しません。どちらもregistryへ登録せず、Orca lifecycleへ接続しません。

## Workerを拒否する理由

read-onlyの証拠だけでは安全なworkspace-write contractを証明できません。Workerには`.git`、state、secret、
symlink、network、process作成、cleanupについて別のpositive/negative matrixが必要です。その証拠が揃うまで、
CopilotとOpenCodeのWorkerはOrca Task作成前に失敗します。他の6つの認識済みharnessも同じ理由で拒否しています。

## 復旧

preflightまたはidentity検証に失敗した場合、provider processは起動しません。snapshot作成後にproviderまたは
runnerが失敗した場合はexactなsnapshot rootを削除し、外側のlifecycleがfailed `worker_done`を報告します。
version driftや実行ファイル差し替えを自動修復することはありません。exactなmanaged installationを復元して、
新しいturnを開始してください。
