# Harness support matrix

[日本語](support-matrix_JA.md) · [README](../README.md) ·
[Background adapters](background-adapters.md) ·
[Machine-readable matrix](harness-safety-matrix.json)

The repository manages these ten harness identities. Recognition is not the
same as execution: `recognized` means the name is known; `available` means the
expected command is found by PATH; `implemented` means at least one exact
role/transport/permission profile is implemented and tested; `runnable` means
that an implemented profile also has its command available. `agent-team
harnesses` performs only static registry and PATH resolution. It never checks
login, downloads packages, starts a process, or writes a workspace.

| Harness | Direct profiles currently runnable | ACP adapter known | Static registry snapshot (not safety status) | Why not broader |
|---|---|---|---|---|
| Claude | Main `orchestrator`; Planner/Reviewer `read-only` | `acpx@0.13.2` + `@agentclientprotocol/claude-agent-acp@0.70.0` | Verified | ACP is limited to read-only background roles. |
| Codex | Main `orchestrator`; Planner/Reviewer `read-only`; Worker `workspace-write` | `codex-acp` | Direct verified; ACP rejected | ACP permission mediation did not stop internal writes in the negative test. |
| GitHub Copilot | Planner/Reviewer `read-only` (direct background, exact `1.0.81`) | Native `copilot --acp`; acpx built-in `copilot` | Verified when exact GitHub CLI is resolved | The profile is intentionally limited to read-only Planner/Reviewer; Workers remain rejected. |
| Cursor | None | Native `cursor-agent acp`; acpx built-in `cursor` | Recognized; direct=`not-run`; acp=`not-run` | A historical auth observation is unverified; current permission phases are `not-run`. |
| Devin | None | Native `devin acp` | Recognized; direct=`blocked`; acp=`blocked` | A no-tool smoke and account/tier limitation do not prove a tool turn. |
| Antigravity | None | None registered | Recognized; raw=`rejected` (historical); snapshot=`blocked` | The raw outside-read is historical; snapshot enforcement is unverified. |
| Hermes Agent | None | Native `hermes acp` | Recognized; direct=`rejected` (historical); acp=`not-run`; external=`blocked` | Historical local writes reject that profile; ACP is not a filesystem sandbox and the external runtime is unavailable. |
| OpenCode | None (adapter implemented, not yet registered) | Native `opencode acp`; acpx built-in `opencode` | Recognized; raw=`blocked`; snapshot=`blocked` | Current raw/snapshot authentication is blocked; the historical raw symlink rejection remains separate. |
| OpenClaw | None | Native `openclaw acp`; acpx built-in `openclaw` | Recognized; direct=`not-run`; Docker=`blocked` | Direct sandbox-off is not a safe profile; Docker image/context/endpoint pins are unavailable. |
| Grok | None | Native `grok agent stdio`; acpx built-in `grok-build` | Recognized; direct=`blocked`; native stdio=`blocked` | Authentication is not established; direct/native-stdio phases are `not-run`. |

An ACP adapter being installed or listed by acpx does not prove that the
adapter is safe for a role. It is shown separately from the verified
agent-team profile. Unknown providers and recognized-but-rejected profiles
fail before an Orca Task, terminal, or ACP process is created. There is no
fallback to another harness.

The Claude ACP profile also requires Node.js `22.13.0` or newer. Before launch,
agent-team resolves only the selected ACP roles' `node`, `acpx`, and
`claude-agent-acp` files, verifies the exact package manifests, and records
absolute paths with SHA-256 fingerprints. Packages must be installed explicitly
outside `agent-team`; runtime operations use the saved files and never invoke
`npm` or `npx`. Direct-only teams do not resolve ACP dependencies. The other
ACP entries remain at the documented status and evidence scope in their rows.

```bash
agent-team harnesses
agent-team harnesses --json
```

The JSON output contains `recognized`, `available`, `command_resolution_status`,
`implemented`, `runnable`, `runnable_profiles`, `acp_adapter`, `acp_status`, and
`rejection_reason` so a
caller can make the distinction without parsing human text.

## Current 17-cell safety matrix

The table below is the current profile-level safety disposition for the seven
providers introduced by Issues #22--#28. It is separate from the ten-harness
inventory above: the inventory keeps the existing Claude, Codex, and Copilot
summary, while this matrix never collapses a provider into one status.

`candidate` is the conceptual status for matching identity, every required
phase, tool-attested positive and negative results, and a clean cleanup
readback. There are currently zero candidate cells (`candidate_count = 0`).
Schema v1 has no live receipt artifact, so it always rejects a candidate row as
`candidate unsupported`. A future candidate requires a schema bump and a
checked-in, verifiable live receipt; static identity, serializer tests, Ruff,
mypy, and CI cannot substitute for it.

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

The machine-readable source for this table is
[`harness-safety-matrix.json`](harness-safety-matrix.json). It uses schema
version `1`, preserves required phases in canonical order, and records
`live_phase`, `cleanup_evidence`, and `approval_gate` for every cell. A zero
cleanup inventory is evidence only after the cleanup phase was attempted;
`no-evidence` and `historical-unavailable` are not clean results.
Every cell uses `safe_reproduction=static-reference`; a live command is not a
machine-readable safety result.
The profile table has a fixed marker set per column: each marker appears once,
unknown markers are rejected, and the source cell contains only the expected PR
and Issue links plus head SHA. The static identity ledger is parsed as a
three-column table and compared with the JSON version, hashes, probe revision,
and provenance payload. Each identity/provenance cell is canonical JSON with
only the expected `PR #` anchor; extra URLs, links, or tokens are rejected.
Public scans use one normalized finite taxonomy for sensitive JSON keys and
assignments, including credentials, authorization, token values, and private or
secret keys; free prose words without `:` or `=` are not treated as payloads.
Sensitive key names or assignments are rejected even when their value is empty.
Every pipe-table block must be a contiguous header, exact separator, and one or
more rows; standalone or broken blocks are invalid. Sensitive scans inspect raw
text and bounded ASCII-escape-normalized text, including escaped key letters
and punctuation. The profile and static sections must consume exactly their
canonical row sets; extra rows inside or outside those blocks are invalid.

Each phase has the exact keys `attempted`, `outcome`, `tool_used`, and
`evidence`. Unrun phases carry an empty evidence list. Structured evidence
uses the canonical `{tool, operation, target, result}` JSON to recompute its
SHA-256 digest. A future schema may add a candidate only with non-empty
structured evidence for every phase, including `cleanup.inspect` with a clean
result; schema v1 rejects candidate rows before they become safety claims.

## Status and evidence rules

- `candidate`: conceptually, all identity fields match; all required phases
  were attempted; positive phases allow, negative phases deny, tool evidence is
  present, and cleanup is independently read back as clean with no residuals.
  Schema v1 rejects this status as `candidate unsupported` because it has no
  verifiable live receipt artifact.
- `rejected`: a boundary violation, identity drift, failed or inconclusive
  phase, timeout, missing evidence, or cleanup residual was observed. A
  historical rejection remains historical and does not prove current cleanup.
- `blocked`: a prerequisite such as authentication, account, Docker, package,
  quota, or platform is unavailable before a provider turn. A blocked cell must
  have no attempted live or required phase.
- `not-run`: a phase or transport has not been attempted, or its available
  metadata is not safety evidence. It is not pass, runnable, or candidate.
- `not-applicable`: the OpenClaw direct/sandbox-off cell has no safe permission
  profile and therefore has no positive phase. It can never become a candidate.

The reason is kept at cell level. `blocked-authentication`,
`blocked-account`, `blocked-docker`, `blocked-image`, and `blocked-platform`
are prerequisite blockers; `boundary-violation` is a failed safety result.
They are not interchangeable. Unknown reasons, malformed phases, duplicate
cells, and source head mismatches fail closed in the focused documentation
test.

Historical boundary failures use a separate `historical_evidence` object with
`status=rejected`, `scope=historical-unverified`, `not_current_evidence=true`,
an observation date, source verification, a source digest where independently
verified, verification status, and structured tool evidence. Antigravity's
historical timestamp and digest are caller-supplied and unverified, so its
`source_verification` is `caller-supplied-unverified` and `source_digest` is
`null`; this is not the verified source digest used by the OpenCode and Hermes
historical records. It never changes the current cell's `live_phase` or
cleanup result.
The old `historical_observation` key is invalid. The focused test also stores
an `EXPECTED_CELL_DIGESTS` table over each cell's canonical permission,
transport, policy, safe reproduction, status, reason, source PR/Issue numbers,
URLs, head SHA, version, hash, probe revision, and provenance subset. If a
sibling PR head changes, update the source readback and the expected digest
before changing this matrix.

## Static identity and provenance

Static identity is useful for selecting the intended source, but it is not a
live permission result. The following values are the redacted, versioned
identities referenced by the seven source PRs:

| Provider | Version and static hashes | Probe revision and provenance |
|---|---|---|
| OpenCode | `{"profiles":{"opencode/raw-workspace-read-only":{"hashes":{"auth_observation_sha256":"8fc9336fb6cac498366d951c3a986c7bdf16efdd72e2beb13f39630c6fbcb225","executable_sha256":"88eed7b0c2431162422cb0625aa68a55239970446951e4c9aad6a4f1fbc232b9","historical_source_sha256":"0238a41c5fd6e2def4a1457d5a21e52d565ada836d1e9e9555098db65aefd4c7"},"version":"1.18.25"},"opencode/snapshot-read-only":{"hashes":{"executable_sha256":"88eed7b0c2431162422cb0625aa68a55239970446951e4c9aad6a4f1fbc232b9"},"version":"1.18.25"}}}` | `{"probe_revision":{"opencode/raw-workspace-read-only":"opencode-static-probe-20260830-v3","opencode/snapshot-read-only":"opencode-static-probe-20260830-v3"},"provenance":{"opencode/raw-workspace-read-only":"PR #41 static identity and redacted historical observation; authentication is not current live evidence","opencode/snapshot-read-only":"PR #41 snapshot policy descriptor and static identity; authentication is not current live evidence"},"source_pr":"PR #41"}` |
| Cursor Agent | `{"profiles":{"cursor/acp":{"hashes":{"bundle_sha256":"cbe95bd372a165cebd83658a293d844cae2dfddc7e1e0aef59e704d16d960257","node_sha256":"336b5b3ebc5deb86df842102b20b6e4761605b7a667823e68dda7761b91a161b","wrapper_sha256":"b7babf47d8b1eee28ac27a74affa02a559bb38103a6e71fbb1f120805d51fedf"},"version":"2026.05.09-0afadcc"},"cursor/direct-plan":{"hashes":{"auth_observation_sha256":"a7310241b8829d8da6ff8dd753acb0841e2967fbafac6e3d8170e100f6ccc105","bundle_sha256":"cbe95bd372a165cebd83658a293d844cae2dfddc7e1e0aef59e704d16d960257","node_sha256":"336b5b3ebc5deb86df842102b20b6e4761605b7a667823e68dda7761b91a161b","wrapper_sha256":"b7babf47d8b1eee28ac27a74affa02a559bb38103a6e71fbb1f120805d51fedf"},"version":"2026.05.09-0afadcc"}}}` | `{"probe_revision":{"cursor/acp":"cursor-static-preflight-20260830","cursor/direct-plan":"cursor-static-preflight-20260830"},"provenance":{"cursor/acp":"PR #40 ACP descriptor is static only; ACP availability is not a filesystem sandbox","cursor/direct-plan":"PR #40 static installation identity; historical authentication observation is unverified"},"source_pr":"PR #40"}` |
| Devin CLI | `{"profiles":{"devin/direct-auto-sandbox-read-only":{"hashes":{"executable_sha256":"82a95b4c7c4bfd73a3ad8ac31207d7ba815ceab2c2fc4a432d002adf7fe43590"},"version":"3000.6.7 build 260a97c8"},"devin/native-acp-review-no-sandbox":{"hashes":{"executable_sha256":"82a95b4c7c4bfd73a3ad8ac31207d7ba815ceab2c2fc4a432d002adf7fe43590"},"version":"3000.6.7 build 260a97c8"}}}` | `{"probe_revision":{"devin/direct-auto-sandbox-read-only":"devin-static-probe-20260830-v2","devin/native-acp-review-no-sandbox":"devin-static-probe-20260830-v2"},"provenance":{"devin/direct-auto-sandbox-read-only":"PR #38 exact executable and signing metadata; current account tool-turn is not established","devin/native-acp-review-no-sandbox":"PR #38 native ACP descriptor; tool turn and sandbox control are not established"},"source_pr":"PR #38"}` |
| Antigravity CLI | `{"profiles":{"antigravity/raw-workspace":{"hashes":{"executable_sha256":"7b1317779085913d338bde0e9b39b72323d9083a879525f944fd469c8ecca906"},"version":"agy 1.1.22"},"antigravity/snapshot":{"hashes":{"executable_sha256":"7b1317779085913d338bde0e9b39b72323d9083a879525f944fd469c8ecca906"},"version":"agy 1.1.22"}}}` | `{"probe_revision":{"antigravity/raw-workspace":"antigravity-static-probe-20260830-v3","antigravity/snapshot":"antigravity-static-probe-20260830-v3"},"provenance":{"antigravity/raw-workspace":"PR #42 historical outside-read observation; signer metadata is not current live evidence","antigravity/snapshot":"PR #42 snapshot and outer-sandbox descriptors are static only"},"source_pr":"PR #42"}` |
| Hermes Agent | `{"profiles":{"hermes/acp":{"hashes":{"target_sha256":"5e2ec9e800822fddae84ef1f337cc3b9ebb5c1ee7fcc8b28df7dfb91c9a8d75c"},"source_commit":"9162ea6db1fe0f57d6fc4de5120fac5c5a1938be","version":"0.20.4 release 2026.8.18"},"hermes/direct-local-oneshot":{"hashes":{"historical_source_artifact_sha256":"0238a41c5fd6e2def4a1457d5a21e52d565ada836d1e9e9555098db65aefd4c7","launcher_sha256":"f2e2083aeab61839230ee3b19932e7302a5302261ec2fb3bcb0c45def48102df","target_sha256":"5e2ec9e800822fddae84ef1f337cc3b9ebb5c1ee7fcc8b28df7dfb91c9a8d75c"},"source_commit":"9162ea6db1fe0f57d6fc4de5120fac5c5a1938be","version":"0.20.4 release 2026.8.18"},"hermes/external-docker":{"hashes":{"target_sha256":"5e2ec9e800822fddae84ef1f337cc3b9ebb5c1ee7fcc8b28df7dfb91c9a8d75c"},"source_commit":"9162ea6db1fe0f57d6fc4de5120fac5c5a1938be","version":"0.20.4 release 2026.8.18"},"hermes/external-openshell":{"hashes":{"target_sha256":"5e2ec9e800822fddae84ef1f337cc3b9ebb5c1ee7fcc8b28df7dfb91c9a8d75c"},"source_commit":"9162ea6db1fe0f57d6fc4de5120fac5c5a1938be","version":"0.20.4 release 2026.8.18"}}}` | `{"probe_revision":{"hermes/acp":"hermes-probe-20260830-v2","hermes/direct-local-oneshot":"hermes-probe-20260830-v2","hermes/external-docker":"hermes-probe-20260830-v2","hermes/external-openshell":"hermes-probe-20260830-v2"},"provenance":{"hermes/acp":"PR #37 ACP availability is not filesystem sandbox evidence","hermes/direct-local-oneshot":"PR #37 historical direct write observations; current rerun was not performed","hermes/external-docker":"PR #37 external Docker policy and image are unverified","hermes/external-openshell":"PR #37 external OpenShell platform and policy are unverified"},"source_pr":"PR #37"}` |
| OpenClaw | `{"profiles":{"openclaw/direct-sandbox-off":{"hashes":{"executable_sha256":"f643b005d6db233a0b45204e8d8e943256874ccc6897b8a6e0cf42a9b376a188"},"version":"2026.7.1 build 2d2ddc4"},"openclaw/docker-read-only":{"hashes":{"executable_sha256":"f643b005d6db233a0b45204e8d8e943256874ccc6897b8a6e0cf42a9b376a188"},"version":"2026.7.1 build 2d2ddc4"},"openclaw/docker-workspace-write":{"hashes":{"executable_sha256":"f643b005d6db233a0b45204e8d8e943256874ccc6897b8a6e0cf42a9b376a188"},"version":"2026.7.1 build 2d2ddc4"}}}` | `{"probe_revision":{"openclaw/direct-sandbox-off":"openclaw-docker-probe-20260830-v1","openclaw/docker-read-only":"openclaw-docker-probe-20260830-v1","openclaw/docker-workspace-write":"openclaw-docker-probe-20260830-v1"},"provenance":{"openclaw/direct-sandbox-off":"PR #39 direct sandbox-off descriptor; no safety profile is claimed","openclaw/docker-read-only":"PR #39 Docker image, context, and endpoint pins are unset","openclaw/docker-workspace-write":"PR #39 Docker workspace-write profile is not enabled without audited image pins"},"source_pr":"PR #39"}` |
| Grok CLI | `{"profiles":{"grok/direct":{"hashes":{"binary_sha256":"8669e0fdadceec25b8c159c355f427ffbd82583525d774b6ab1522197ea83b80","package_sha256":"5c1a4a21cb52065961ba51cc74a4fc45984ccdf72d38863f4c7dca2214b924ca","wrapper_sha256":"13a2405556fe9e86108731a801771db1b9a742ef11e934ab2cb886f1089aeef0"},"version":"1.0.13 alpha commit 5e9a58528b76"},"grok/native-stdio":{"hashes":{"binary_sha256":"8669e0fdadceec25b8c159c355f427ffbd82583525d774b6ab1522197ea83b80","package_sha256":"5c1a4a21cb52065961ba51cc74a4fc45984ccdf72d38863f4c7dca2214b924ca","wrapper_sha256":"13a2405556fe9e86108731a801771db1b9a742ef11e934ab2cb886f1089aeef0"},"version":"1.0.13 alpha commit 5e9a58528b76"}}}` | `{"probe_revision":{"grok/direct":"grok-probe-20260830-v2","grok/native-stdio":"grok-probe-20260830-v2"},"provenance":{"grok/direct":"PR #43 static identity and unverified authentication marker; credential content is not recorded","grok/native-stdio":"PR #43 native stdio descriptor; authentication and permission enforcement are not verified"},"source_pr":"PR #43"}` |

Only redacted release, hash, revision, and provenance metadata is retained.
Prompt text, raw logs, environment values, credentials, cookies, personal
absolute paths, and machine-specific identity values are not part of the
matrix.

## Approval gates and future live work

Login, account or tier changes, Docker daemon/image/container operations,
package installation, quota-consuming turns, and platform changes require
explicit approval before they are attempted. No such operation was performed
for this documentation change. `provider-turn`, `cleanup.inspect`, and every
current live required phase remain unrun for all seven providers.

When a future live probe is approved, it must create a fresh manifest for the
exact identity, use a fixed invocation and isolated workspace, record every
phase, and read back cleanup for child processes, sessions, containers, and
temporary roots. A historical rejected observation cannot satisfy that gate.
No provider registry, Orca lifecycle, fallback transport, or dangerous profile
is enabled by this matrix.
