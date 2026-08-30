# Direct background adapter implementation

[日本語](background-adapters_JA.md) · [Support matrix](support-matrix.md)
· [Machine-readable matrix](harness-safety-matrix.json)

The provider-side Copilot and OpenCode adapters are direct one-shot processes.
They do not use ACP. Copilot's read-only Planner/Reviewer profiles use the
common state-v3 background lifecycle. OpenCode is currently blocked by
authentication in both profiles. Any historical raw-workspace symlink
rejection is separate, and every current live phase remains `not-run`. The
runner owns the Orca Task, Dispatch,
terminal, and `worker_done` lifecycle; `agent_team.adapters` only preflights and
executes the provider process.

## Intended profiles

- GitHub Copilot CLI `1.0.81`: Planner and Reviewer, read-only only (runnable).
- OpenCode `1.18.25`: Planner and Reviewer, read-only only (adapter implemented,
  lifecycle not yet enabled).

Copilot resolves only the platform-native executable inside the exact existing
mise installation (`npm:@github/copilot@1.0.81`); it never falls back to the npm
loader or a PATH collision. OpenCode accepts an unambiguous PATH identity or
the exact `opencode@1.18.25` mise installation. The launcher never installs,
updates, or falls back to a different provider. It snapshots the executable's
canonical path, device, inode, size, mtime, and SHA-256, then checks the same
identity again before execution.

## Boundary and authentication

Each turn receives a fresh temporary read snapshot. The original workspace,
agent-team state, and prompt directory are not passed as the provider's cwd.
The snapshot excludes `.git`, ignored files, symlinks, special files,
secret-like names/extensions, provider configuration, MCP configuration, and
agent instruction files. Copying uses no-follow type/inode checks and an atomic
publish. The exact temporary root is removed after execution, including
failure; cleanup errors are reported rather than silently ignored. A fixed
instruction tells the provider to resolve repository paths relative to the
snapshot working directory. It must not read an absolute path from the original
workspace.

The child environment is constructed from an allowlist. `GITHUB_TOKEN`,
`GH_TOKEN`, other provider keys, `ORCA_*`, `NODE_OPTIONS`, and proxy/endpoint
overrides are not inherited. Copilot keeps the user's `HOME` so its existing
subscription/keychain login can be used, but receives an isolated
`COPILOT_HOME`. OpenCode receives only the explicitly present
`OPENCODE_API_KEY` in addition to the common safe environment and gets
isolated XDG config/data/state/cache roots. The tool does not promise how a
provider bills a subscription request; account and quota behavior remains a
provider concern.

Both providers currently require the prompt in their one-shot command line.
The value is passed as one argv element with a hard size limit and `shell=False`;
it is never parsed as shell syntax. This still exposes the prompt to the local
process table, so callers should not use these profiles for highly sensitive
prompt text until a provider-supported stdin/file option is available.

## Versioned probe manifests and receipts

Safety probes exchange a provider-free, schema-versioned manifest and receipt
through `agent_team.probe_receipts`. The manifest fixes the harness, permission
profile, OS/architecture, probe revision, executable path/version/SHA-256,
fixed-argv SHA-256, prompt transport, snapshot cwd, environment-name allowlist, sandbox-policy identity, and
the required matrix. Read-only and workspace-write profiles each require a
positive phase plus separate `outside-path`, `symlink`, `git`, `secret`,
`local-network`, `external-network`, `process`, and `cleanup` phases.

Receipts contain only those fixed identities and structured observations. A
phase records whether it was attempted, whether a tool was used, its bounded
outcome (`passed`, `failed`, `timeout`, `inconclusive`, or `not-run`), exit
code/timeout, structured tool evidence, and a cleanup inventory for child processes,
sessions, containers, and temporary roots. Prompt text, raw logs, environment
values, tokens, API keys, and cookies have no payload fields in this schema.
Only the argv digest and environment variable names are stored. Unknown keys,
schema versions, duplicate phases, and contradictory or phase-mismatched
evidence fail closed.

The persisted phase object is exact: `attempted`, `outcome`, `tool_used`, and
`evidence`. Unrun phases use `attempted=false`, `outcome=not-run`,
`tool_used=false`, and `evidence=[]`. Structured evidence digests are
recomputed from canonical `{tool, operation, target, result}` JSON. A future
candidate would need a non-empty structured evidence object for every phase
and an independently attested `cleanup.inspect` result.

`judge_profile(manifest, receipt)` is deterministic and never starts a
provider. Conceptually, it can describe `candidate` only when every required
phase was attempted with matching structured evidence, all identities match,
and every cleanup inventory is empty. Schema v1 has no live receipt artifact,
so it rejects candidate rows as `candidate unsupported`; adding one requires a
schema bump and a checked-in, verifiable live receipt. An unattempted phase is
`not-run`; authentication, account, Docker, package, quota, or platform
prerequisites are `blocked`; tool failures, timeouts, evidence failures,
identity drift, and cleanup residuals are `rejected`. Fixture files can be
judged through the module's pure Python API without starting a provider.

## Why Workers remain rejected

Read-only evidence does not prove a safe workspace-write contract. Workers
would need a separate positive/negative matrix for `.git`, state, secrets,
symlinks, network, process creation, and cleanup. Until that evidence exists,
Copilot and OpenCode Workers fail before an Orca Task is created. The seven
provider families below retain their own profile-level status and are not
reduced to a single worker verdict.

## Recovery

If preflight or identity validation fails, no provider process is started. If a
provider or runner fails after snapshot creation, the exact snapshot root is
cleaned and the outer lifecycle reports a failed `worker_done`. A version drift
or executable replacement is not repaired automatically; restore the exact
managed installation and start a new turn.

## Seven-provider profile ledger

The direct adapter description above retains the existing Copilot and OpenCode
implementation summary. The following ledger is the separate, current safety
disposition for seven provider families and 17 profile cells. It is intentionally
profile-level: a provider with one rejected historical observation does not make
its other profiles pass, and a static adapter does not make a live turn
`runnable`.

All cells currently have `candidate_count = 0`. The `live_phase` values are
`not-run`; cleanup is either `no-evidence` or `historical-unavailable`. A zero
inventory from a static serializer is not cleanup evidence. The source PR URL,
exact head SHA, static version/hash, probe revision, and provenance are the only
source facts carried here; provider code is not copied into this branch. Every
cell uses `safe_reproduction=static-reference`; no live command is represented.
The profile table uses one fixed marker for each required field per column;
unknown or duplicate markers and extra source links or SHAs fail closed. The
static identity ledger is parsed as three columns and checked against the JSON
version, hashes, probe revision, and provenance payload. Each cell is canonical
JSON with only the expected `PR #` anchor; extra URLs, links, or tokens fail
closed. Public scans use one normalized finite taxonomy for credential,
authorization, token-value, private-key, and secret-key names/assignments;
ordinary prose words without `:` or `=` are allowed.
All pipe-table blocks must be contiguous header/separator/row blocks; a
standalone row, separator, or broken block fails closed. Sensitive scans cover
both raw text and bounded ASCII-escape-normalized text, including escaped key
letters and punctuation. The profile and static sections consume exactly their
canonical row sets, rejecting extra rows both inside and outside the blocks.

Historical failures use only the strict `historical_evidence` object; the
legacy `historical_observation` key is invalid. Its rejected status,
`historical-unverified` scope, observation date, source verification, source
digest when independently verified, and structured tool evidence are
independent of the current live row. Antigravity is explicitly
`source_verification=caller-supplied-unverified` with `source_digest=null`,
not a verified historical source. The focused test fixes an
`EXPECTED_CELL_DIGESTS` table over the canonical cell identity and source
number/URL/head subset. A source PR head change requires a fresh readback and
an explicit expected-digest update; arbitrary versions, hashes, or probe
revisions are rejected.

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

## Static identity ledger

The matrix keeps static identity separate from a live permission result. These
are the redacted pins used to identify the source material. They are not
provider invocations and do not establish login, account, tier, quota, or
cleanup state.

| Provider | Version and static hash | Probe revision and provenance |
|---|---|---|
| OpenCode | `{"profiles":{"opencode/raw-workspace-read-only":{"hashes":{"auth_observation_sha256":"8fc9336fb6cac498366d951c3a986c7bdf16efdd72e2beb13f39630c6fbcb225","executable_sha256":"88eed7b0c2431162422cb0625aa68a55239970446951e4c9aad6a4f1fbc232b9","historical_source_sha256":"0238a41c5fd6e2def4a1457d5a21e52d565ada836d1e9e9555098db65aefd4c7"},"version":"1.18.25"},"opencode/snapshot-read-only":{"hashes":{"executable_sha256":"88eed7b0c2431162422cb0625aa68a55239970446951e4c9aad6a4f1fbc232b9"},"version":"1.18.25"}}}` | `{"probe_revision":{"opencode/raw-workspace-read-only":"opencode-static-probe-20260830-v3","opencode/snapshot-read-only":"opencode-static-probe-20260830-v3"},"provenance":{"opencode/raw-workspace-read-only":"PR #41 static identity and redacted historical observation; authentication is not current live evidence","opencode/snapshot-read-only":"PR #41 snapshot policy descriptor and static identity; authentication is not current live evidence"},"source_pr":"PR #41"}` |
| Cursor Agent | `{"profiles":{"cursor/acp":{"hashes":{"bundle_sha256":"cbe95bd372a165cebd83658a293d844cae2dfddc7e1e0aef59e704d16d960257","node_sha256":"336b5b3ebc5deb86df842102b20b6e4761605b7a667823e68dda7761b91a161b","wrapper_sha256":"b7babf47d8b1eee28ac27a74affa02a559bb38103a6e71fbb1f120805d51fedf"},"version":"2026.05.09-0afadcc"},"cursor/direct-plan":{"hashes":{"auth_observation_sha256":"a7310241b8829d8da6ff8dd753acb0841e2967fbafac6e3d8170e100f6ccc105","bundle_sha256":"cbe95bd372a165cebd83658a293d844cae2dfddc7e1e0aef59e704d16d960257","node_sha256":"336b5b3ebc5deb86df842102b20b6e4761605b7a667823e68dda7761b91a161b","wrapper_sha256":"b7babf47d8b1eee28ac27a74affa02a559bb38103a6e71fbb1f120805d51fedf"},"version":"2026.05.09-0afadcc"}}}` | `{"probe_revision":{"cursor/acp":"cursor-static-preflight-20260830","cursor/direct-plan":"cursor-static-preflight-20260830"},"provenance":{"cursor/acp":"PR #40 ACP descriptor is static only; ACP availability is not a filesystem sandbox","cursor/direct-plan":"PR #40 static installation identity; historical authentication observation is unverified"},"source_pr":"PR #40"}` |
| Devin CLI | `{"profiles":{"devin/direct-auto-sandbox-read-only":{"hashes":{"executable_sha256":"82a95b4c7c4bfd73a3ad8ac31207d7ba815ceab2c2fc4a432d002adf7fe43590"},"version":"3000.6.7 build 260a97c8"},"devin/native-acp-review-no-sandbox":{"hashes":{"executable_sha256":"82a95b4c7c4bfd73a3ad8ac31207d7ba815ceab2c2fc4a432d002adf7fe43590"},"version":"3000.6.7 build 260a97c8"}}}` | `{"probe_revision":{"devin/direct-auto-sandbox-read-only":"devin-static-probe-20260830-v2","devin/native-acp-review-no-sandbox":"devin-static-probe-20260830-v2"},"provenance":{"devin/direct-auto-sandbox-read-only":"PR #38 exact executable and signing metadata; current account tool-turn is not established","devin/native-acp-review-no-sandbox":"PR #38 native ACP descriptor; tool turn and sandbox control are not established"},"source_pr":"PR #38"}` |
| Antigravity CLI | `{"profiles":{"antigravity/raw-workspace":{"hashes":{"executable_sha256":"7b1317779085913d338bde0e9b39b72323d9083a879525f944fd469c8ecca906"},"version":"agy 1.1.22"},"antigravity/snapshot":{"hashes":{"executable_sha256":"7b1317779085913d338bde0e9b39b72323d9083a879525f944fd469c8ecca906"},"version":"agy 1.1.22"}}}` | `{"probe_revision":{"antigravity/raw-workspace":"antigravity-static-probe-20260830-v3","antigravity/snapshot":"antigravity-static-probe-20260830-v3"},"provenance":{"antigravity/raw-workspace":"PR #42 historical outside-read observation; signer metadata is not current live evidence","antigravity/snapshot":"PR #42 snapshot and outer-sandbox descriptors are static only"},"source_pr":"PR #42"}` |
| Hermes Agent | `{"profiles":{"hermes/acp":{"hashes":{"target_sha256":"5e2ec9e800822fddae84ef1f337cc3b9ebb5c1ee7fcc8b28df7dfb91c9a8d75c"},"source_commit":"9162ea6db1fe0f57d6fc4de5120fac5c5a1938be","version":"0.20.4 release 2026.8.18"},"hermes/direct-local-oneshot":{"hashes":{"historical_source_artifact_sha256":"0238a41c5fd6e2def4a1457d5a21e52d565ada836d1e9e9555098db65aefd4c7","launcher_sha256":"f2e2083aeab61839230ee3b19932e7302a5302261ec2fb3bcb0c45def48102df","target_sha256":"5e2ec9e800822fddae84ef1f337cc3b9ebb5c1ee7fcc8b28df7dfb91c9a8d75c"},"source_commit":"9162ea6db1fe0f57d6fc4de5120fac5c5a1938be","version":"0.20.4 release 2026.8.18"},"hermes/external-docker":{"hashes":{"target_sha256":"5e2ec9e800822fddae84ef1f337cc3b9ebb5c1ee7fcc8b28df7dfb91c9a8d75c"},"source_commit":"9162ea6db1fe0f57d6fc4de5120fac5c5a1938be","version":"0.20.4 release 2026.8.18"},"hermes/external-openshell":{"hashes":{"target_sha256":"5e2ec9e800822fddae84ef1f337cc3b9ebb5c1ee7fcc8b28df7dfb91c9a8d75c"},"source_commit":"9162ea6db1fe0f57d6fc4de5120fac5c5a1938be","version":"0.20.4 release 2026.8.18"}}}` | `{"probe_revision":{"hermes/acp":"hermes-probe-20260830-v2","hermes/direct-local-oneshot":"hermes-probe-20260830-v2","hermes/external-docker":"hermes-probe-20260830-v2","hermes/external-openshell":"hermes-probe-20260830-v2"},"provenance":{"hermes/acp":"PR #37 ACP availability is not filesystem sandbox evidence","hermes/direct-local-oneshot":"PR #37 historical direct write observations; current rerun was not performed","hermes/external-docker":"PR #37 external Docker policy and image are unverified","hermes/external-openshell":"PR #37 external OpenShell platform and policy are unverified"},"source_pr":"PR #37"}` |
| OpenClaw | `{"profiles":{"openclaw/direct-sandbox-off":{"hashes":{"executable_sha256":"f643b005d6db233a0b45204e8d8e943256874ccc6897b8a6e0cf42a9b376a188"},"version":"2026.7.1 build 2d2ddc4"},"openclaw/docker-read-only":{"hashes":{"executable_sha256":"f643b005d6db233a0b45204e8d8e943256874ccc6897b8a6e0cf42a9b376a188"},"version":"2026.7.1 build 2d2ddc4"},"openclaw/docker-workspace-write":{"hashes":{"executable_sha256":"f643b005d6db233a0b45204e8d8e943256874ccc6897b8a6e0cf42a9b376a188"},"version":"2026.7.1 build 2d2ddc4"}}}` | `{"probe_revision":{"openclaw/direct-sandbox-off":"openclaw-docker-probe-20260830-v1","openclaw/docker-read-only":"openclaw-docker-probe-20260830-v1","openclaw/docker-workspace-write":"openclaw-docker-probe-20260830-v1"},"provenance":{"openclaw/direct-sandbox-off":"PR #39 direct sandbox-off descriptor; no safety profile is claimed","openclaw/docker-read-only":"PR #39 Docker image, context, and endpoint pins are unset","openclaw/docker-workspace-write":"PR #39 Docker workspace-write profile is not enabled without audited image pins"},"source_pr":"PR #39"}` |
| Grok CLI | `{"profiles":{"grok/direct":{"hashes":{"binary_sha256":"8669e0fdadceec25b8c159c355f427ffbd82583525d774b6ab1522197ea83b80","package_sha256":"5c1a4a21cb52065961ba51cc74a4fc45984ccdf72d38863f4c7dca2214b924ca","wrapper_sha256":"13a2405556fe9e86108731a801771db1b9a742ef11e934ab2cb886f1089aeef0"},"version":"1.0.13 alpha commit 5e9a58528b76"},"grok/native-stdio":{"hashes":{"binary_sha256":"8669e0fdadceec25b8c159c355f427ffbd82583525d774b6ab1522197ea83b80","package_sha256":"5c1a4a21cb52065961ba51cc74a4fc45984ccdf72d38863f4c7dca2214b924ca","wrapper_sha256":"13a2405556fe9e86108731a801771db1b9a742ef11e934ab2cb886f1089aeef0"},"version":"1.0.13 alpha commit 5e9a58528b76"}}}` | `{"probe_revision":{"grok/direct":"grok-probe-20260830-v2","grok/native-stdio":"grok-probe-20260830-v2"},"provenance":{"grok/direct":"PR #43 static identity and unverified authentication marker; credential content is not recorded","grok/native-stdio":"PR #43 native stdio descriptor; authentication and permission enforcement are not verified"},"source_pr":"PR #43"}` |

## Gate and cleanup contract

The gate is evaluated before any provider-side side effect. Login or OAuth,
account or tier changes, package installation or update, Docker daemon/image/
container operations, quota-consuming turns, and platform changes require
explicit approval. This documentation change performed none of them. The
matrix therefore records the applicable gate as `approved: false` and keeps
the current live required phases unrun.

For a future schema's `candidate`, the exact profile must attempt
`positive-read` or `positive-write`, all seven negative boundary phases, and
`cleanup`. Each phase needs matching tool evidence. Cleanup must independently
inspect child processes, sessions, containers, and temporary roots and report
no residuals. In schema v1, candidate rows are always rejected as
`candidate unsupported`; a static or historical observation cannot satisfy a
live receipt contract.

## Safe reproduction boundary

The JSON matrix is the only machine-readable artifact for these 17 cells. The
source PR links are references to static serializers and strict tests; their
provider modules are not copied, imported, or cherry-picked. There is no live
command in this documentation branch. A future probe must be a separate,
approved change with exact identity, fixed invocation, isolated workspace,
bounded timeout, redacted receipt, and cleanup readback.

Do not register these profiles, start an Orca lifecycle, enable a dangerous
permission, switch providers, or silently fall back to another transport from
this matrix.
