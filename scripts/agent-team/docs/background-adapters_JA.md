# Direct background adapter実装

[English](background-adapters.md) · [対応matrix](support-matrix_JA.md) ·
[機械可読matrix](harness-safety-matrix.json)

CopilotとOpenCode向けのprovider adapterはdirect one-shot processです。ACPは使いません。Copilotの
read-only Planner/Reviewerは共通のstate v3 background lifecycleで実行できます。OpenCodeは両profile
ともauthenticationでblockedです。historicalなraw-workspace symlink rejectionは別記録として扱い、
現在のlive phaseはすべて`not-run`です。統合後も
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

保存するphase objectのkeyは`attempted`、`outcome`、`tool_used`、`evidence`に固定します。未実行phaseは
`attempted=false`、`outcome=not-run`、`tool_used=false`、`evidence=[]`です。structured evidenceのdigestは
canonicalな`{tool, operation, target, result}` JSONから再計算します。将来のcandidateには、全phaseの
non-empty structured evidenceとdigest、さらに独立にattestされた`cleanup.inspect`結果が必要です。

`judge_profile(manifest, receipt)`はproviderを起動せず、常に同じ結果を返します。概念上は、全必須phaseを試行し、
evidenceと全identityが一致し、cleanup inventoryが空の場合が`candidate`です。ただしschema v1にはlive receipt
artifactがないため、candidate rowは`candidate unsupported`として拒否します。将来追加するにはschema bumpと
検証可能なlive receiptのchecked-in参照が必要です。未試行phaseは`not-run`、authentication・account・Docker・
package・quota・platformの前提不足は`blocked`、tool failure・timeout・evidence不備・identity drift・cleanup残留は
`rejected`です。fixtureはmoduleのpure Python APIからproviderを起動せず判定できます。

## OpenCode probeの判定

今回の`agent_team.opencode_probe`はstatic-onlyです。公開APIは、fresh validation済みのredacted DTOを返す
`build_static_artifact()`と、決定的なredacted JSONを返す`serialize_static_artifact()`だけです。固定したinstallation
identityをreadし、role tokenからredactedなmanifestを内部で作りますが、pinやgenericなmanifest/receipt/judgment objectは返しません。
OpenCodeの起動、provider eventのparse、current candidate receiptのassembly、live runnerも公開しません。認証済みpermission
matrixはIssue #22の後続sliceで扱います。

固定したOpenCode `1.18.25` executableのSHA-256は
`88eed7b0c2431162422cb0625aa68a55239970446951e4c9aad6a4f1fbc232b9`でした。static recordにはdevice、inode、size、mtime、
hashを残しますが、pathは`/probe/opencode`へredactします。rawとsnapshotの固定profileは、sourceをfreshに再検証できない
`auth-list-zero-credentials` observationを`historical-unverified` provenanceとして保持した
`blocked-authentication`で、required phaseはすべて`not-run`です。この構造化 observationには観測日、source digest、固定したexecutable
version/hashを含めます。過去のraw workspace synthetic-marker symlink escapeは、`unverified`なhistorical provenanceを持つ別の
`rejected` observationとして保持し、current runへ昇格しません。どちらもregistryへ登録せず、Orca lifecycleへ接続しません。
adapter側のdynamicなPATH preflightは今回のstatic-only変更の対象外です。static helperはPATH上のbinaryを解決・起動しません。

## Workerを拒否する理由

read-onlyの証拠だけでは安全なworkspace-write contractを証明できません。Workerには`.git`、state、secret、
symlink、network、process作成、cleanupについて別のpositive/negative matrixが必要です。その証拠が揃うまで、
CopilotとOpenCodeのWorkerはOrca Task作成前に失敗します。下の7 provider familyはprofile単位のstatusを
保持し、Workerの一つの判定へまとめません。

## 復旧

preflightまたはidentity検証に失敗した場合、provider processは起動しません。snapshot作成後にproviderまたは
runnerが失敗した場合はexactなsnapshot rootを削除し、外側のlifecycleがfailed `worker_done`を報告します。
version driftや実行ファイル差し替えを自動修復することはありません。exactなmanaged installationを復元して、
新しいturnを開始してください。

## 7 providerのprofile台帳

上のCopilotとOpenCodeのadapter実装概要は既存のまま残しています。次の台帳は、7 provider・17
profile cellの現在の安全判定です。provider単位ではなくprofile単位で保持します。1つのprofileで
historicalな拒否があっても他profileをpassにせず、static adapterがあってもlive turnを`runnable`
とはみなしません。

全cellで`candidate_count = 0`です。`live_phase`は`not-run`、cleanupは`no-evidence`または
`historical-unavailable`です。static serializerのzero inventoryはcleanup evidenceではありません。
ここで保持するsource情報は、PR URL、exact head SHA、static version/hash、probe revision、
provenanceだけです。このbranchへprovider codeはコピーしていません。
全cellの`safe_reproduction`は`static-reference`であり、live commandは表現しません。
profile tableの各columnには必要なmarkerを1つずつ置き、未知・重複markerや余分なsource link・SHAは
fail closedで拒否します。static identity ledgerは3列としてparseし、JSONのversion、hash、probe revision、
provenance payloadとcolumn単位で照合します。各cellは期待する`PR #` anchorだけを含むcanonical JSONとし、
余分なURL、link、tokenはfail closedで拒否します。public scanはcredential、authorization、token value、
private/secret keyを含む正規化済みfinite taxonomyを使い、値が空でもsensitiveなkey名とassignmentを拒否します。
`:`や`=`のない自由文の単語はpayloadとみなしません。
すべてのpipe tableはheader・正確なseparator・1行以上のrowが連続するblockとし、単独row・separatorや壊れた
blockは拒否します。sensitive scanはraw textと、key文字・句読点をboundedにescape正規化したtextの両方を調べます。
profile sectionとstatic sectionはcanonicalなrow集合をすべて消費し、block内外の余分なrowを拒否します。

過去の失敗はstrictな`historical_evidence` objectだけで表し、旧`historical_observation` keyは
受理しません。rejected status、`historical-unverified` scope、観測日、source verification、独立検証済みの場合の
source digest、structured tool evidenceは現在のlive rowから分離します。Antigravityは
`source_verification=caller-supplied-unverified`、`source_digest=null`であり、verified historical sourceでは
ありません。focused testにはcanonicalなcell identityとsource number/URL/head subsetから計算した
`EXPECTED_CELL_DIGESTS`を固定しており、source PRのheadが変わった場合はfresh readbackと期待digestの明示的な
更新が必要です。任意のversion、hash、probe revisionは受理しません。

| Profile cell | Permission; transport; policy | Current status / reason | Live phase; cleanup evidence | Approval gate | Source PR / head; Issue |
|---|---|---|---|---|---|
| `opencode/raw-workspace-read-only` | permission=read-only; transport=direct; policy=opencode-raw-workspace-readonly-static-v3 | status=blocked; reason=blocked-authentication; historical_status=rejected; historical_reason=boundary-violation | live=not-run; cleanup=no-evidence | gate=authentication; approved=false; side_effects=login,provider-turn | [PR #41](https://github.com/iamtatsuki05/dotfiles/pull/41) @ `16ffe9de151371db8105fcab7ded4843759d752a`; [Issue #22](https://github.com/iamtatsuki05/dotfiles/issues/22) |
| `opencode/snapshot-read-only` | permission=read-only; transport=direct; policy=opencode-snapshot-readonly-static-v3 | status=blocked; reason=blocked-authentication | live=not-run; cleanup=no-evidence | gate=authentication; approved=false; side_effects=login,provider-turn | [PR #41](https://github.com/iamtatsuki05/dotfiles/pull/41) @ `16ffe9de151371db8105fcab7ded4843759d752a`; [Issue #22](https://github.com/iamtatsuki05/dotfiles/issues/22) |
| `cursor/direct-plan` | permission=read-only; transport=argv; policy=cursor-advertised-plan-v1 | status=not-run; reason=phase-not-attempted | live=not-run; cleanup=no-evidence | gate=authentication; approved=false; side_effects=login,provider-turn,package-install | [PR #40](https://github.com/iamtatsuki05/dotfiles/pull/40) @ `9b161824ed2ca0d5dbb000fc64187219293fb162`; [Issue #23](https://github.com/iamtatsuki05/dotfiles/issues/23) |
| `cursor/acp` | permission=read-only; transport=stdin; policy=cursor-acp-no-policy-v1 | status=not-run; reason=phase-not-attempted | live=not-run; cleanup=no-evidence | gate=authentication; approved=false; side_effects=login,provider-turn | [PR #40](https://github.com/iamtatsuki05/dotfiles/pull/40) @ `9b161824ed2ca0d5dbb000fc64187219293fb162`; [Issue #23](https://github.com/iamtatsuki05/dotfiles/issues/23) |
| `devin/direct-auto-sandbox-read-only` | permission=read-only; transport=stdin; policy=devin-direct-auto-sandbox-readonly-v2 | status=blocked; reason=blocked-account | live=not-run; cleanup=no-evidence | gate=account; approved=false; side_effects=account-change,provider-turn,tier-change | [PR #38](https://github.com/iamtatsuki05/dotfiles/pull/38) @ `120fa0f1e64b06e00fa0a1dd8e330051eb84f5c9`; [Issue #24](https://github.com/iamtatsuki05/dotfiles/issues/24) |
| `devin/native-acp-review-no-sandbox` | permission=read-only; transport=stdin; policy=devin-native-acp-review-nonsandbox-v2 | status=blocked; reason=blocked-account | live=not-run; cleanup=no-evidence | gate=account; approved=false; side_effects=account-change,provider-turn,tier-change | [PR #38](https://github.com/iamtatsuki05/dotfiles/pull/38) @ `120fa0f1e64b06e00fa0a1dd8e330051eb84f5c9`; [Issue #24](https://github.com/iamtatsuki05/dotfiles/issues/24) |
| `antigravity/raw-workspace` | permission=read-only; transport=print; policy=antigravity-raw-workspace-readonly-v1 | status=rejected; reason=boundary-violation; historical_status=rejected; historical_reason=boundary-violation | live=not-run; cleanup=historical-unavailable | gate=safety-revalidation; approved=false; side_effects=provider-turn,workspace-read | [PR #42](https://github.com/iamtatsuki05/dotfiles/pull/42) @ `aa767e89ad42b4fd1d6dcff9394c1a48d4ef019b`; [Issue #25](https://github.com/iamtatsuki05/dotfiles/issues/25) |
| `antigravity/snapshot` | permission=read-only; transport=print; policy=antigravity-snapshot-seatbelt-readonly-v1 | status=blocked; reason=outer-sandbox-unverified | live=not-run; cleanup=no-evidence | gate=platform; approved=false; side_effects=outer-sandbox-start,provider-turn | [PR #42](https://github.com/iamtatsuki05/dotfiles/pull/42) @ `aa767e89ad42b4fd1d6dcff9394c1a48d4ef019b`; [Issue #25](https://github.com/iamtatsuki05/dotfiles/issues/25) |
| `hermes/direct-local-oneshot` | permission=read-only; transport=argv; policy=hermes-direct-local-oneshot-v1 | status=rejected; reason=boundary-violation; historical_status=rejected; historical_reason=boundary-violation | live=not-run; cleanup=historical-unavailable | gate=safety-revalidation; approved=false; side_effects=provider-turn,workspace-write,outside-write | [PR #37](https://github.com/iamtatsuki05/dotfiles/pull/37) @ `0aff332a5ebdcdd1be874bbc310e4b8c572c85d6`; [Issue #26](https://github.com/iamtatsuki05/dotfiles/issues/26) |
| `hermes/acp` | permission=read-only; transport=stdin; policy=hermes-acp-not-a-filesystem-sandbox-v1 | status=not-run; reason=not-a-filesystem-sandbox | live=not-run; cleanup=no-evidence | gate=filesystem-sandbox; approved=false; side_effects=provider-turn,workspace-read | [PR #37](https://github.com/iamtatsuki05/dotfiles/pull/37) @ `0aff332a5ebdcdd1be874bbc310e4b8c572c85d6`; [Issue #26](https://github.com/iamtatsuki05/dotfiles/issues/26) |
| `hermes/external-docker` | permission=read-only; transport=argv; policy=hermes-external-docker-v1 | status=blocked; reason=blocked-docker | live=not-run; cleanup=no-evidence | gate=docker; approved=false; side_effects=daemon-start,image-pull,container-start | [PR #37](https://github.com/iamtatsuki05/dotfiles/pull/37) @ `0aff332a5ebdcdd1be874bbc310e4b8c572c85d6`; [Issue #26](https://github.com/iamtatsuki05/dotfiles/issues/26) |
| `hermes/external-openshell` | permission=read-only; transport=argv; policy=hermes-external-openshell-v1 | status=blocked; reason=blocked-platform | live=not-run; cleanup=no-evidence | gate=platform; approved=false; side_effects=runtime-start,provider-turn | [PR #37](https://github.com/iamtatsuki05/dotfiles/pull/37) @ `0aff332a5ebdcdd1be874bbc310e4b8c572c85d6`; [Issue #26](https://github.com/iamtatsuki05/dotfiles/issues/26) |
| `openclaw/direct-sandbox-off` | permission=not-applicable; transport=argv; policy=none | status=not-run; reason=sandbox-off-is-not-a-safe-profile | live=not-run; cleanup=no-evidence | gate=safety-profile; approved=false; side_effects=provider-turn,workspace-read | [PR #39](https://github.com/iamtatsuki05/dotfiles/pull/39) @ `aff5423ec5e1dc8066cbc9edb2c1f811bc734474`; [Issue #27](https://github.com/iamtatsuki05/dotfiles/issues/27) |
| `openclaw/docker-read-only` | permission=read-only; transport=argv; policy=openclaw-docker-sandbox-v1-read-only | status=blocked; reason=blocked-image | live=not-run; cleanup=no-evidence | gate=docker; approved=false; side_effects=daemon-start,image-pull,container-start,container-remove | [PR #39](https://github.com/iamtatsuki05/dotfiles/pull/39) @ `aff5423ec5e1dc8066cbc9edb2c1f811bc734474`; [Issue #27](https://github.com/iamtatsuki05/dotfiles/issues/27) |
| `openclaw/docker-workspace-write` | permission=workspace-write; transport=argv; policy=openclaw-docker-sandbox-v1-workspace-write | status=blocked; reason=blocked-image | live=not-run; cleanup=no-evidence | gate=docker; approved=false; side_effects=daemon-start,image-pull,container-start,container-remove | [PR #39](https://github.com/iamtatsuki05/dotfiles/pull/39) @ `aff5423ec5e1dc8066cbc9edb2c1f811bc734474`; [Issue #27](https://github.com/iamtatsuki05/dotfiles/issues/27) |
| `grok/direct` | permission=read-only; transport=file; policy=grok-direct-probe-v2 | status=blocked; reason=blocked-authentication | live=not-run; cleanup=no-evidence | gate=authentication; approved=false; side_effects=login,oauth,api-key-setup,provider-turn | [PR #43](https://github.com/iamtatsuki05/dotfiles/pull/43) @ `bb8cdc7d0a439a316835f618c34672af355d858e`; [Issue #28](https://github.com/iamtatsuki05/dotfiles/issues/28) |
| `grok/native-stdio` | permission=read-only; transport=stdin; policy=grok-native-stdio-unverified-v2 | status=blocked; reason=blocked-authentication | live=not-run; cleanup=no-evidence | gate=authentication; approved=false; side_effects=login,oauth,api-key-setup,provider-turn | [PR #43](https://github.com/iamtatsuki05/dotfiles/pull/43) @ `bb8cdc7d0a439a316835f618c34672af355d858e`; [Issue #28](https://github.com/iamtatsuki05/dotfiles/issues/28) |

## static identity台帳

matrixではstatic identityとlive permission結果を分けます。次はsource materialを特定するための
redactedなpinです。providerのinvocationではなく、login、account、tier、quota、cleanup状態を
証明するものでもありません。

| Provider | Versionとstatic hash | Probe revisionとprovenance |
|---|---|---|
| OpenCode | `{"profiles":{"opencode/raw-workspace-read-only":{"hashes":{"auth_observation_sha256":"8fc9336fb6cac498366d951c3a986c7bdf16efdd72e2beb13f39630c6fbcb225","executable_sha256":"88eed7b0c2431162422cb0625aa68a55239970446951e4c9aad6a4f1fbc232b9","historical_source_sha256":"0238a41c5fd6e2def4a1457d5a21e52d565ada836d1e9e9555098db65aefd4c7"},"version":"1.18.25"},"opencode/snapshot-read-only":{"hashes":{"executable_sha256":"88eed7b0c2431162422cb0625aa68a55239970446951e4c9aad6a4f1fbc232b9"},"version":"1.18.25"}}}` | `{"probe_revision":{"opencode/raw-workspace-read-only":"opencode-static-probe-20260830-v3","opencode/snapshot-read-only":"opencode-static-probe-20260830-v3"},"provenance":{"opencode/raw-workspace-read-only":"PR #41 static identity and redacted historical observation; authentication is not current live evidence","opencode/snapshot-read-only":"PR #41 snapshot policy descriptor and static identity; authentication is not current live evidence"},"source_pr":"PR #41"}` |
| Cursor Agent | `{"profiles":{"cursor/acp":{"hashes":{"bundle_sha256":"cbe95bd372a165cebd83658a293d844cae2dfddc7e1e0aef59e704d16d960257","node_sha256":"336b5b3ebc5deb86df842102b20b6e4761605b7a667823e68dda7761b91a161b","wrapper_sha256":"b7babf47d8b1eee28ac27a74affa02a559bb38103a6e71fbb1f120805d51fedf"},"version":"2026.05.09-0afadcc"},"cursor/direct-plan":{"hashes":{"auth_observation_sha256":"a7310241b8829d8da6ff8dd753acb0841e2967fbafac6e3d8170e100f6ccc105","bundle_sha256":"cbe95bd372a165cebd83658a293d844cae2dfddc7e1e0aef59e704d16d960257","node_sha256":"336b5b3ebc5deb86df842102b20b6e4761605b7a667823e68dda7761b91a161b","wrapper_sha256":"b7babf47d8b1eee28ac27a74affa02a559bb38103a6e71fbb1f120805d51fedf"},"version":"2026.05.09-0afadcc"}}}` | `{"probe_revision":{"cursor/acp":"cursor-static-preflight-20260830","cursor/direct-plan":"cursor-static-preflight-20260830"},"provenance":{"cursor/acp":"PR #40 ACP descriptor is static only; ACP availability is not a filesystem sandbox","cursor/direct-plan":"PR #40 static installation identity; historical authentication observation is unverified"},"source_pr":"PR #40"}` |
| Devin CLI | `{"profiles":{"devin/direct-auto-sandbox-read-only":{"hashes":{"executable_sha256":"82a95b4c7c4bfd73a3ad8ac31207d7ba815ceab2c2fc4a432d002adf7fe43590"},"version":"3000.6.7 build 260a97c8"},"devin/native-acp-review-no-sandbox":{"hashes":{"executable_sha256":"82a95b4c7c4bfd73a3ad8ac31207d7ba815ceab2c2fc4a432d002adf7fe43590"},"version":"3000.6.7 build 260a97c8"}}}` | `{"probe_revision":{"devin/direct-auto-sandbox-read-only":"devin-static-probe-20260830-v2","devin/native-acp-review-no-sandbox":"devin-static-probe-20260830-v2"},"provenance":{"devin/direct-auto-sandbox-read-only":"PR #38 exact executable and signing metadata; current account tool-turn is not established","devin/native-acp-review-no-sandbox":"PR #38 native ACP descriptor; tool turn and sandbox control are not established"},"source_pr":"PR #38"}` |
| Antigravity CLI | `{"profiles":{"antigravity/raw-workspace":{"hashes":{"executable_sha256":"7b1317779085913d338bde0e9b39b72323d9083a879525f944fd469c8ecca906"},"version":"agy 1.1.22"},"antigravity/snapshot":{"hashes":{"executable_sha256":"7b1317779085913d338bde0e9b39b72323d9083a879525f944fd469c8ecca906"},"version":"agy 1.1.22"}}}` | `{"probe_revision":{"antigravity/raw-workspace":"antigravity-static-probe-20260830-v3","antigravity/snapshot":"antigravity-static-probe-20260830-v3"},"provenance":{"antigravity/raw-workspace":"PR #42 historical outside-read observation; signer metadata is not current live evidence","antigravity/snapshot":"PR #42 snapshot and outer-sandbox descriptors are static only"},"source_pr":"PR #42"}` |
| Hermes Agent | `{"profiles":{"hermes/acp":{"hashes":{"target_sha256":"5e2ec9e800822fddae84ef1f337cc3b9ebb5c1ee7fcc8b28df7dfb91c9a8d75c"},"source_commit":"9162ea6db1fe0f57d6fc4de5120fac5c5a1938be","version":"0.20.4 release 2026.8.18"},"hermes/direct-local-oneshot":{"hashes":{"historical_source_artifact_sha256":"0238a41c5fd6e2def4a1457d5a21e52d565ada836d1e9e9555098db65aefd4c7","launcher_sha256":"f2e2083aeab61839230ee3b19932e7302a5302261ec2fb3bcb0c45def48102df","target_sha256":"5e2ec9e800822fddae84ef1f337cc3b9ebb5c1ee7fcc8b28df7dfb91c9a8d75c"},"source_commit":"9162ea6db1fe0f57d6fc4de5120fac5c5a1938be","version":"0.20.4 release 2026.8.18"},"hermes/external-docker":{"hashes":{"target_sha256":"5e2ec9e800822fddae84ef1f337cc3b9ebb5c1ee7fcc8b28df7dfb91c9a8d75c"},"source_commit":"9162ea6db1fe0f57d6fc4de5120fac5c5a1938be","version":"0.20.4 release 2026.8.18"},"hermes/external-openshell":{"hashes":{"target_sha256":"5e2ec9e800822fddae84ef1f337cc3b9ebb5c1ee7fcc8b28df7dfb91c9a8d75c"},"source_commit":"9162ea6db1fe0f57d6fc4de5120fac5c5a1938be","version":"0.20.4 release 2026.8.18"}}}` | `{"probe_revision":{"hermes/acp":"hermes-probe-20260830-v2","hermes/direct-local-oneshot":"hermes-probe-20260830-v2","hermes/external-docker":"hermes-probe-20260830-v2","hermes/external-openshell":"hermes-probe-20260830-v2"},"provenance":{"hermes/acp":"PR #37 ACP availability is not filesystem sandbox evidence","hermes/direct-local-oneshot":"PR #37 historical direct write observations; current rerun was not performed","hermes/external-docker":"PR #37 external Docker policy and image are unverified","hermes/external-openshell":"PR #37 external OpenShell platform and policy are unverified"},"source_pr":"PR #37"}` |
| OpenClaw | `{"profiles":{"openclaw/direct-sandbox-off":{"hashes":{"executable_sha256":"f643b005d6db233a0b45204e8d8e943256874ccc6897b8a6e0cf42a9b376a188"},"version":"2026.7.1 build 2d2ddc4"},"openclaw/docker-read-only":{"hashes":{"executable_sha256":"f643b005d6db233a0b45204e8d8e943256874ccc6897b8a6e0cf42a9b376a188"},"version":"2026.7.1 build 2d2ddc4"},"openclaw/docker-workspace-write":{"hashes":{"executable_sha256":"f643b005d6db233a0b45204e8d8e943256874ccc6897b8a6e0cf42a9b376a188"},"version":"2026.7.1 build 2d2ddc4"}}}` | `{"probe_revision":{"openclaw/direct-sandbox-off":"openclaw-docker-probe-20260830-v1","openclaw/docker-read-only":"openclaw-docker-probe-20260830-v1","openclaw/docker-workspace-write":"openclaw-docker-probe-20260830-v1"},"provenance":{"openclaw/direct-sandbox-off":"PR #39 direct sandbox-off descriptor; no safety profile is claimed","openclaw/docker-read-only":"PR #39 Docker image, context, and endpoint pins are unset","openclaw/docker-workspace-write":"PR #39 Docker workspace-write profile is not enabled without audited image pins"},"source_pr":"PR #39"}` |
| Grok CLI | `{"profiles":{"grok/direct":{"hashes":{"binary_sha256":"8669e0fdadceec25b8c159c355f427ffbd82583525d774b6ab1522197ea83b80","package_sha256":"5c1a4a21cb52065961ba51cc74a4fc45984ccdf72d38863f4c7dca2214b924ca","wrapper_sha256":"13a2405556fe9e86108731a801771db1b9a742ef11e934ab2cb886f1089aeef0"},"version":"1.0.13 alpha commit 5e9a58528b76"},"grok/native-stdio":{"hashes":{"binary_sha256":"8669e0fdadceec25b8c159c355f427ffbd82583525d774b6ab1522197ea83b80","package_sha256":"5c1a4a21cb52065961ba51cc74a4fc45984ccdf72d38863f4c7dca2214b924ca","wrapper_sha256":"13a2405556fe9e86108731a801771db1b9a742ef11e934ab2cb886f1089aeef0"},"version":"1.0.13 alpha commit 5e9a58528b76"}}}` | `{"probe_revision":{"grok/direct":"grok-probe-20260830-v2","grok/native-stdio":"grok-probe-20260830-v2"},"provenance":{"grok/direct":"PR #43 static identity and unverified authentication marker; credential content is not recorded","grok/native-stdio":"PR #43 native stdio descriptor; authentication and permission enforcement are not verified"},"source_pr":"PR #43"}` |

## gateとcleanup contract

gateはprovider側のside effectより前に判定します。loginまたはOAuth、account/tier変更、package
install/update、Docker daemon/image/container操作、quotaを消費するturn、platform変更には、
実行前の明示承認が必要です。このdocumentation変更では、いずれも実行していません。そのため
matrixは該当gateを`approved: false`、現在のlive required phaseを未実行として保持します。

将来のschemaで`candidate`を追加する場合は、exact profileの`positive-read`または`positive-write`、
7つのnegative boundary phase、`cleanup`の試行が必要です。各phaseに一致するtool evidenceを持たせ、
cleanupでchild process、session、container、temporary rootを独立にinspectして残留なしを確認します。
schema v1ではcandidate rowを常に`candidate unsupported`として拒否します。staticな初期値zero、
未試行phase、historical rejectionだけではlive receiptの条件を満たしません。

## safe reproductionの境界

17 cellの機械可読artifactは`harness-safety-matrix.json`だけです。source PR linkはstatic
serializerとstrict testの参照であり、このbranchへprovider moduleをコピー、import、cherry-pick
していません。live commandもありません。将来probeを行う場合は、exact identity、固定invocation、
隔離workspace、bounded timeout、redacted receipt、cleanup readbackを備えた別の承認済み変更にします。

このmatrixからprofileをregistryへ登録せず、Orca lifecycleを起動せず、危険なpermissionを有効に
せず、別providerや別transportへsilent fallbackしません。
