# Direct background adapter implementation

[日本語](background-adapters_JA.md) · [Support matrix](support-matrix.md)

The provider-side Copilot and OpenCode adapters are direct one-shot processes.
They do not use ACP. Copilot's read-only Planner/Reviewer profiles use the
common state-v3 background lifecycle. OpenCode remains rejected until that
same lifecycle and its snapshot contract are separately verified. The
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

`judge_profile(manifest, receipt)` is deterministic and never starts a
provider. It returns `candidate` only when every required phase was attempted
with matching structured evidence, all identities match, and every cleanup
inventory is empty. An unattempted phase is `not-run`; authentication,
account, Docker, package, quota, or platform prerequisites are `blocked`; tool
failures, timeouts, evidence failures, identity drift, and cleanup residuals
are `rejected`. Fixture files can be judged through the module's pure Python
API without starting a provider.

## Why Workers remain rejected

Read-only evidence does not prove a safe workspace-write contract. Workers
would need a separate positive/negative matrix for `.git`, state, secrets,
symlinks, network, process creation, and cleanup. Until that evidence exists,
Copilot and OpenCode Workers fail before an Orca Task is created. The other six
recognized harnesses remain rejected for the same reason.

## Recovery

If preflight or identity validation fails, no provider process is started. If a
provider or runner fails after snapshot creation, the exact snapshot root is
cleaned and the outer lifecycle reports a failed `worker_done`. A version drift
or executable replacement is not repaired automatically; restore the exact
managed installation and start a new turn.
