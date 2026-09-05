# Harness対応matrix

[English](support-matrix.md) · [README](../README_JA.md) ·
[Background adapter](background-adapters_JA.md) ·
[機械可読matrix](harness-safety-matrix.json)

このrepositoryが管理するharness identityは10種類です。認識していることと、実行できる
ことは別です。`recognized`は名前をregistryが知っていること、`available`は期待するcommand
がPATH上にあること、`implemented`は少なくとも1つのrole・transport・permissionの組み合わせが
実装・検証済みであること、`runnable`は実装済みprofileのcommandも利用できることを示します。
`agent-team harnesses`はstatic registryとPATH解決だけを行い、login確認、
package download、process起動、workspace書き込みは行いません。

| Harness | 現在実行できるdirect profile | 既知のACP adapter | Static registry snapshot (not safety status) | 広く対応していない理由 |
|---|---|---|---|---|
| Claude | Main `orchestrator`、Planner/Reviewer `read-only` | `acpx@0.13.2` + `@agentclientprotocol/claude-agent-acp@0.70.0` | 検証済み | ACPはread-only background roleに限定。 |
| Codex | Main `orchestrator`、Planner/Reviewer `read-only`、Worker `workspace-write` | `codex-acp` | direct検証済み、ACP拒否 | ACPのpermission制御がinternal writeを止めないnegative test結果。 |
| GitHub Copilot | Planner/Reviewer `read-only`（direct background、厳密な`1.0.81`） | native `copilot --acp`、acpx built-in `copilot` | 厳密なGitHub CLIを解決できた場合は検証済み | read-onlyのPlanner/Reviewerに限定。Workerは引き続き拒否。 |
| Cursor | なし | native `cursor-agent acp`、acpx built-in `cursor` | 認識済み; direct=`not-run`; acp=`not-run` | historicalなauth観測は未検証。現在のpermission phaseは`not-run`。 |
| Devin | なし | native `devin acp` | 認識済み; direct=`blocked`; acp=`blocked` | no-tool smokeとaccount/tierの制限だけではtool turnを証明できない。 |
| Antigravity | なし | 登録なし | 認識済み; raw=`rejected`（historical）; snapshot=`blocked` | rawのoutside-readはhistorical。snapshot enforcementは未検証。 |
| Hermes Agent | なし | native `hermes acp` | 認識済み; direct=`rejected`（historical）; acp=`not-run`; external=`blocked` | historicalなlocal writeでdirectを拒否。ACPはfilesystem sandboxではなく、外部runtimeも利用できない。 |
| OpenCode | なし（adapterは実装済みだが未登録） | native `opencode acp`、acpx built-in `opencode` | 認識済み; raw=`blocked`; snapshot=`blocked` | 現在のraw/snapshotはauthenticationでblocked。historicalなraw symlink rejectionは別記録。 |
| OpenClaw | なし | native `openclaw acp`、acpx built-in `openclaw` | 認識済み; direct=`not-run`; Docker=`blocked` | direct sandbox-offはsafe profileではない。Dockerのimage/context/endpoint pinも利用できない。 |
| Grok | なし | native `grok agent stdio`、acpx built-in `grok-build` | 認識済み; direct=`blocked`; native stdio=`blocked` | authenticationが成立しておらず、direct/native-stdio phaseは`not-run`。 |

ACP adapterがインストールされていることやacpxが表示することだけでは、安全なrole用adapterで
あることは証明できません。adapterの存在とagent-teamの検証済みprofileは別々に表示します。
unknown providerと認識済みだが拒否されたprofileは、Orca Task、terminal、ACP processを作る前に
失敗します。別harnessへのfallbackはありません。

Claude ACP profileにはNode.js `22.13.0`以降も必要です。起動前に、agent-teamは選択したACP roleの
`node`、`acpx`、`claude-agent-acp`だけを解決し、exact package manifestを確認したうえで、absoluteな
pathとSHA-256 fingerprintを保存します。packageは`agent-team`の外で明示的に導入してください。実行時は
保存したfileを使い、`npm`や`npx`を呼び出しません。directだけのteamではACP依存関係を解決しません。
このmatrixの他のACP entryも、各rowに記載したstatusとevidence scopeのままです。

```bash
agent-team harnesses
agent-team harnesses --json
```

JSONには`recognized`、`available`、`command_resolution_status`、`implemented`、`runnable`、
`runnable_profiles`、`acp_adapter`、`acp_status`、`rejection_reason`が含まれます。人間向け表示を
parseせずに状態を区別できます。

## 現在の17-cell safety matrix

次の表は、Issue #22--#28で追加された7 providerのprofile単位の安全判定です。上の10種類の
harness一覧とは役割が異なります。上の一覧には既存のClaude、Codex、Copilotの要約を残し、
このmatrixではprovider全体を1つのstatusに丸めません。

`candidate`は、identityの一致、全必須phase、toolがattestしたpositive/negative結果、
cleanupの再確認を満たす場合の概念上のstatusです。現在のcandidate cellは0件です。
schema v1にはlive receipt artifactがないため、candidate rowは常に`candidate unsupported`として
拒否します。将来candidateを追加するにはschema bumpと、checked-inで検証可能なlive receiptが
必要です。static identity、serializer test、Ruff、mypy、CIでは代替できません。

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

この表の機械可読なsourceは
[`harness-safety-matrix.json`](harness-safety-matrix.json)です。schema version `1`を使い、
必須phaseをcanonical orderで保存します。全cellに`live_phase`、`cleanup_evidence`、
`approval_gate`を持たせています。cleanup phaseを試行して初めてzero inventoryを証拠と
みなせます。`no-evidence`と`historical-unavailable`はcleanではありません。
全cellの`safe_reproduction`は`static-reference`です。live commandを安全性の結果として
扱いません。
profile tableの各columnにはmarkerを固定し、各markerは1回だけ許可します。未知または重複した
marker、余分なsource linkやSHAはfail closedにします。static identity ledgerは3列のtableとしてparseし、
JSONのversion、hash、probe revision、provenance payloadとcolumn単位で照合します。各cellは期待する
`PR #` anchorだけを含むcanonical JSONとし、余分なURL、link、tokenはfail closedで拒否します。public scanは
credential、authorization、token value、private/secret keyを含む正規化済みfinite taxonomyを使い、値が
空でもsensitiveなkey名やassignmentを拒否します。`:`や`=`のない自由文の単語はpayloadとみなしません。
すべてのpipe tableは、header、正確なseparator、1行以上のrowが連続するblockでなければなりません。
単独のrow・separatorや壊れたblockは拒否します。sensitive scanはraw textと、key文字や句読点のescapeを
boundedに正規化したtextの両方を調べます。profile sectionとstatic sectionはcanonicalなrow集合を
すべて消費し、block内外の余分なrowを拒否します。

各phaseのkeyは`attempted`、`outcome`、`tool_used`、`evidence`に固定します。未実行phaseの
evidence listは空です。structured evidenceのdigestはcanonicalな`{tool, operation, target, result}`
JSONから再計算します。将来のschemaでcandidateを追加する場合も、全phaseにnon-emptyなstructured
evidenceを持たせ、`cleanup.inspect`でclean resultを返す必要があります。schema v1ではcandidate
rowを安全性の主張に昇格させません。

## statusと証拠のルール

- `candidate`: 概念上は、identityが一致し、全必須phaseを試行し、positiveはallow、negativeはdeny、
  tool evidenceがあり、cleanupを独立に再確認して残留がない状態です。ただしschema v1では
  検証可能なlive receipt artifactがないため、`candidate unsupported`として拒否します。
- `rejected`: 境界違反、identity drift、phase失敗・inconclusive、timeout、evidence欠落、
  cleanup残留を観測した状態です。historical rejectionはhistoricalなままで、現在のcleanupを
  証明しません。
- `blocked`: authentication、account、Docker、package、quota、platformなどの前提が
  provider turnより前に不足した状態です。blocked cellにliveまたは必須phaseの試行はありません。
- `not-run`: phaseまたはtransportを試していない、または利用可能なmetadataが安全性の証拠に
  なっていない状態です。pass、runnable、candidateとは別です。
- `not-applicable`: OpenClaw direct/sandbox-offにはsafe permission profileがなく、positive phaseも
  ありません。candidateへ昇格することはありません。

reasonはcell単位で保持します。`blocked-authentication`、`blocked-account`、
`blocked-docker`、`blocked-image`、`blocked-platform`は前提条件のblockerであり、
`boundary-violation`は安全性の失敗です。両者を混同しません。未知のreason、壊れたphase、
重複cell、source headの不一致はfocused documentation testでfail closedにします。

過去の境界違反は、`status=rejected`、`scope=historical-unverified`、
`not_current_evidence=true`、観測日、source verification、独立検証済みの場合のsource digest、
verification status、structured tool evidenceを持つ別の`historical_evidence` objectに保存します。
Antigravityのhistorical timestampとdigestはcaller-suppliedで未検証のため、
`source_verification=caller-supplied-unverified`、`source_digest=null`です。OpenCodeとHermesの
verified source digestとは別扱いです。これは現在cellの`live_phase`やcleanup結果を変更しません。
旧`historical_observation` keyは無効です。focused testには、cellの
permission、transport、policy、safe reproduction、status、reason、source PR/Issue number、URL、head SHA、
version、hash、probe revision、provenanceのcanonical subsetから計算した`EXPECTED_CELL_DIGESTS`を固定しています。
sibling PRのheadが変わったら、source readbackと期待digestを更新してからmatrixを変更します。

## static identityとprovenance

static identityは意図したsourceを選ぶための情報であり、live permissionの結果ではありません。
7つのsource PRが参照するredactedなversioned identityは次のとおりです。

| Provider | Versionとstatic hash | Probe revisionとprovenance |
|---|---|---|
| OpenCode | `{"profiles":{"opencode/raw-workspace-read-only":{"hashes":{"auth_observation_sha256":"8fc9336fb6cac498366d951c3a986c7bdf16efdd72e2beb13f39630c6fbcb225","executable_sha256":"88eed7b0c2431162422cb0625aa68a55239970446951e4c9aad6a4f1fbc232b9","historical_source_sha256":"0238a41c5fd6e2def4a1457d5a21e52d565ada836d1e9e9555098db65aefd4c7"},"version":"1.18.25"},"opencode/snapshot-read-only":{"hashes":{"executable_sha256":"88eed7b0c2431162422cb0625aa68a55239970446951e4c9aad6a4f1fbc232b9"},"version":"1.18.25"}}}` | `{"probe_revision":{"opencode/raw-workspace-read-only":"opencode-static-probe-20260830-v3","opencode/snapshot-read-only":"opencode-static-probe-20260830-v3"},"provenance":{"opencode/raw-workspace-read-only":"PR #41 static identity and redacted historical observation; authentication is not current live evidence","opencode/snapshot-read-only":"PR #41 snapshot policy descriptor and static identity; authentication is not current live evidence"},"source_pr":"PR #41"}` |
| Cursor Agent | `{"profiles":{"cursor/acp":{"hashes":{"bundle_sha256":"cbe95bd372a165cebd83658a293d844cae2dfddc7e1e0aef59e704d16d960257","node_sha256":"336b5b3ebc5deb86df842102b20b6e4761605b7a667823e68dda7761b91a161b","wrapper_sha256":"b7babf47d8b1eee28ac27a74affa02a559bb38103a6e71fbb1f120805d51fedf"},"version":"2026.05.09-0afadcc"},"cursor/direct-plan":{"hashes":{"auth_observation_sha256":"a7310241b8829d8da6ff8dd753acb0841e2967fbafac6e3d8170e100f6ccc105","bundle_sha256":"cbe95bd372a165cebd83658a293d844cae2dfddc7e1e0aef59e704d16d960257","node_sha256":"336b5b3ebc5deb86df842102b20b6e4761605b7a667823e68dda7761b91a161b","wrapper_sha256":"b7babf47d8b1eee28ac27a74affa02a559bb38103a6e71fbb1f120805d51fedf"},"version":"2026.05.09-0afadcc"}}}` | `{"probe_revision":{"cursor/acp":"cursor-static-preflight-20260830","cursor/direct-plan":"cursor-static-preflight-20260830"},"provenance":{"cursor/acp":"PR #40 ACP descriptor is static only; ACP availability is not a filesystem sandbox","cursor/direct-plan":"PR #40 static installation identity; historical authentication observation is unverified"},"source_pr":"PR #40"}` |
| Devin CLI | `{"profiles":{"devin/direct-auto-sandbox-read-only":{"hashes":{"executable_sha256":"82a95b4c7c4bfd73a3ad8ac31207d7ba815ceab2c2fc4a432d002adf7fe43590"},"version":"3000.6.7 build 260a97c8"},"devin/native-acp-review-no-sandbox":{"hashes":{"executable_sha256":"82a95b4c7c4bfd73a3ad8ac31207d7ba815ceab2c2fc4a432d002adf7fe43590"},"version":"3000.6.7 build 260a97c8"}}}` | `{"probe_revision":{"devin/direct-auto-sandbox-read-only":"devin-static-probe-20260830-v2","devin/native-acp-review-no-sandbox":"devin-static-probe-20260830-v2"},"provenance":{"devin/direct-auto-sandbox-read-only":"PR #38 exact executable and signing metadata; current account tool-turn is not established","devin/native-acp-review-no-sandbox":"PR #38 native ACP descriptor; tool turn and sandbox control are not established"},"source_pr":"PR #38"}` |
| Antigravity CLI | `{"profiles":{"antigravity/raw-workspace":{"hashes":{"executable_sha256":"7b1317779085913d338bde0e9b39b72323d9083a879525f944fd469c8ecca906"},"version":"agy 1.1.22"},"antigravity/snapshot":{"hashes":{"executable_sha256":"7b1317779085913d338bde0e9b39b72323d9083a879525f944fd469c8ecca906"},"version":"agy 1.1.22"}}}` | `{"probe_revision":{"antigravity/raw-workspace":"antigravity-static-probe-20260830-v3","antigravity/snapshot":"antigravity-static-probe-20260830-v3"},"provenance":{"antigravity/raw-workspace":"PR #42 historical outside-read observation; signer metadata is not current live evidence","antigravity/snapshot":"PR #42 snapshot and outer-sandbox descriptors are static only"},"source_pr":"PR #42"}` |
| Hermes Agent | `{"profiles":{"hermes/acp":{"hashes":{"target_sha256":"5e2ec9e800822fddae84ef1f337cc3b9ebb5c1ee7fcc8b28df7dfb91c9a8d75c"},"source_commit":"9162ea6db1fe0f57d6fc4de5120fac5c5a1938be","version":"0.20.4 release 2026.8.18"},"hermes/direct-local-oneshot":{"hashes":{"historical_source_artifact_sha256":"0238a41c5fd6e2def4a1457d5a21e52d565ada836d1e9e9555098db65aefd4c7","launcher_sha256":"f2e2083aeab61839230ee3b19932e7302a5302261ec2fb3bcb0c45def48102df","target_sha256":"5e2ec9e800822fddae84ef1f337cc3b9ebb5c1ee7fcc8b28df7dfb91c9a8d75c"},"source_commit":"9162ea6db1fe0f57d6fc4de5120fac5c5a1938be","version":"0.20.4 release 2026.8.18"},"hermes/external-docker":{"hashes":{"target_sha256":"5e2ec9e800822fddae84ef1f337cc3b9ebb5c1ee7fcc8b28df7dfb91c9a8d75c"},"source_commit":"9162ea6db1fe0f57d6fc4de5120fac5c5a1938be","version":"0.20.4 release 2026.8.18"},"hermes/external-openshell":{"hashes":{"target_sha256":"5e2ec9e800822fddae84ef1f337cc3b9ebb5c1ee7fcc8b28df7dfb91c9a8d75c"},"source_commit":"9162ea6db1fe0f57d6fc4de5120fac5c5a1938be","version":"0.20.4 release 2026.8.18"}}}` | `{"probe_revision":{"hermes/acp":"hermes-probe-20260830-v2","hermes/direct-local-oneshot":"hermes-probe-20260830-v2","hermes/external-docker":"hermes-probe-20260830-v2","hermes/external-openshell":"hermes-probe-20260830-v2"},"provenance":{"hermes/acp":"PR #37 ACP availability is not filesystem sandbox evidence","hermes/direct-local-oneshot":"PR #37 historical direct write observations; current rerun was not performed","hermes/external-docker":"PR #37 external Docker policy and image are unverified","hermes/external-openshell":"PR #37 external OpenShell platform and policy are unverified"},"source_pr":"PR #37"}` |
| OpenClaw | `{"profiles":{"openclaw/direct-sandbox-off":{"hashes":{"executable_sha256":"f643b005d6db233a0b45204e8d8e943256874ccc6897b8a6e0cf42a9b376a188"},"version":"2026.7.1 build 2d2ddc4"},"openclaw/docker-read-only":{"hashes":{"executable_sha256":"f643b005d6db233a0b45204e8d8e943256874ccc6897b8a6e0cf42a9b376a188"},"version":"2026.7.1 build 2d2ddc4"},"openclaw/docker-workspace-write":{"hashes":{"executable_sha256":"f643b005d6db233a0b45204e8d8e943256874ccc6897b8a6e0cf42a9b376a188"},"version":"2026.7.1 build 2d2ddc4"}}}` | `{"probe_revision":{"openclaw/direct-sandbox-off":"openclaw-docker-probe-20260830-v1","openclaw/docker-read-only":"openclaw-docker-probe-20260830-v1","openclaw/docker-workspace-write":"openclaw-docker-probe-20260830-v1"},"provenance":{"openclaw/direct-sandbox-off":"PR #39 direct sandbox-off descriptor; no safety profile is claimed","openclaw/docker-read-only":"PR #39 Docker image, context, and endpoint pins are unset","openclaw/docker-workspace-write":"PR #39 Docker workspace-write profile is not enabled without audited image pins"},"source_pr":"PR #39"}` |
| Grok CLI | `{"profiles":{"grok/direct":{"hashes":{"binary_sha256":"8669e0fdadceec25b8c159c355f427ffbd82583525d774b6ab1522197ea83b80","package_sha256":"5c1a4a21cb52065961ba51cc74a4fc45984ccdf72d38863f4c7dca2214b924ca","wrapper_sha256":"13a2405556fe9e86108731a801771db1b9a742ef11e934ab2cb886f1089aeef0"},"version":"1.0.13 alpha commit 5e9a58528b76"},"grok/native-stdio":{"hashes":{"binary_sha256":"8669e0fdadceec25b8c159c355f427ffbd82583525d774b6ab1522197ea83b80","package_sha256":"5c1a4a21cb52065961ba51cc74a4fc45984ccdf72d38863f4c7dca2214b924ca","wrapper_sha256":"13a2405556fe9e86108731a801771db1b9a742ef11e934ab2cb886f1089aeef0"},"version":"1.0.13 alpha commit 5e9a58528b76"}}}` | `{"probe_revision":{"grok/direct":"grok-probe-20260830-v2","grok/native-stdio":"grok-probe-20260830-v2"},"provenance":{"grok/direct":"PR #43 static identity and unverified authentication marker; credential content is not recorded","grok/native-stdio":"PR #43 native stdio descriptor; authentication and permission enforcement are not verified"},"source_pr":"PR #43"}` |

保存するのはredactedなrelease、hash、revision、provenance metadataだけです。prompt本文、
raw log、environment value、credential、cookie、個人固有のabsolute path、machine-specific
identity valueはmatrixに含めません。

## approval gateと今後のlive作業

login、accountまたはtier変更、Docker daemon/image/container操作、package install、quotaを
消費するturn、platform変更は、実行前に明示承認が必要です。このdocumentation変更では、
これらを実行していません。7 providerすべてで`provider-turn`、`cleanup.inspect`、現在のliveで
必要な全phaseは未実行です。

将来live probeを承認する場合は、exact identity用のfresh manifest、固定invocation、隔離workspaceを
用意し、全phaseを記録して、child process、session、container、temporary rootのcleanupを
再確認します。historical rejected observationだけではgateを満たしません。このmatrixはprovider
registry、Orca lifecycle、fallback transport、dangerous profileを有効にしません。
