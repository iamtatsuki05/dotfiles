# Agent Team

[日本語](README_JA.md)

`agent-team` starts a project-scoped Planner → Worker → Reviewer workflow on
Orca without changing ordinary `claude` or `codex` sessions. Orca owns task,
message, terminal, and lifecycle coordination. Each role uses either its normal
CLI (`direct`) or an Agent Client Protocol client (`acp`).

Read the install, prerequisite, and start sections to launch a team. Use the
linked reference documents when changing the implementation or configuration.

## Read this first

- [Quick start](#start-a-team) explains the normal workflow.
- [Architecture](docs/architecture.md) explains the runtime and safety
  boundaries.
- [Configuration](docs/configuration.md) lists the version-3 schema and the
  supported provider/transport combinations. [Version-4 configuration](docs/configuration-v4.md)
  describes explicit team selection and pure topology inspection.
- [Harness support matrix](docs/support-matrix.md) separates recognized,
  available, runnable, and rejected harnesses.
- [ACP boundary](docs/acp.md) explains adapter pins, authentication, and why
  ACP is not a sandbox.
- [Direct background adapters](docs/background-adapters.md) documents the
  Copilot/OpenCode read-only adapter implementation, snapshot boundary, and recovery.
- [Coordination store, recovery, backup, and restore](docs/coordination-store.md)
  documents the SQLite schema boundary, stable writer marker, WAL sidecar
  controller, backup artifact, and candidate-first restore protocol. The
  historical Issue #72 section retains the v3 workflow checkpoint/CAS contract
  and keeps provider effects outside the Store. The current [Issue #80 schema-4
  foundation](https://github.com/iamtatsuki05/dotfiles/issues/80) fixes the
  twelve-table object set, read-only image classifier, and pure codecs; its
  empty-ledger and non-empty fail-closed boundary is explicit there.
- [Task policy schema v4](docs/task-policy-v4.md) defines the immutable
  `TaskSpec`, dependency order, and state observation contract without storage
  or workflow execution.
- [Serial review policy](docs/review-policy.md) defines the typed serial gate
  shared by normal tasks and Issue #50-admitted express tasks, without backend wiring.
- [Path/resource policy](docs/path-resource-policy.md) defines canonical path
  admission, explicit resource modes, reservation-port handoff, and the
  normal/express/research lane matrix without filesystem or provider effects.
- [Fixed-argv verification gate](docs/verification-gate.md) defines the typed
  approval, pinned verification request, before/after snapshot binding, and
  normalized receipt required before a write task can be completed.
- [Policy/verification handoff](docs/policy-verification-handoff.md) defines
  the #49 review ref, #50 completion ref, approved-only composition, exact
  Store readback, and the boundary around the schema-4 work split across
  [Issue #80](https://github.com/iamtatsuki05/dotfiles/issues/80),
  [#81](https://github.com/iamtatsuki05/dotfiles/issues/81),
  [#82](https://github.com/iamtatsuki05/dotfiles/issues/82), and
  [#83](https://github.com/iamtatsuki05/dotfiles/issues/83).

The current configuration uses this team:

| Role | Provider / transport | Model / effort | Permission |
|---|---|---|---|
| Main | Claude / `direct` | `fable` / `high` | `orchestrator` |
| Planner | Claude / `acp` | `fable` / `high` | `read-only` |
| Worker | Codex / `direct` | `gpt-5.6-sol` / `medium` | `workspace-write` |
| Reviewer | Codex / `direct` | `gpt-5.6-sol` / `high` | `read-only` |

Only Main starts immediately. Planner, Worker, and Reviewer start on demand,
and only one background role may be active at a time.

## Run from a checkout or install the project

The project has no third-party Python dependency. Python 3.11 or newer is
required. From a checkout, the launcher is directly executable:

The Orca lifecycle backend and bounded provider runner are POSIX-only; they
fail fast on Windows because the current runtime metadata contract requires a
Unix socket and process-group semantics.
The CLI name is selected deterministically by platform: `orca` on macOS and
`orca-ide` on Linux, with no PATH fallback or environment override.

```bash
scripts/agent-team/agent-team harnesses
scripts/agent-team/agent-team start --dry-run
```

For an isolated installation, build/install the project with your chosen
Python environment. The console script and `python -m agent_team` use the same
package and bundled defaults. Team startup resolves the console script from
that Python environment; it does not fall back to another installation:

```bash
python3.13 -m venv /tmp/agent-team-venv
/tmp/agent-team-venv/bin/python -m pip install scripts/agent-team
/tmp/agent-team-venv/bin/agent-team harnesses --json
```

## Install the managed command

From this dotfiles repository, run the normal agent-file sync:

```bash
zsh dotfiles/.agent/sync.sh
command -v agent-team
```

The sync creates a managed link for the project launcher at
`~/.local/bin/agent-team` and links the dotfiles config/prompts to
`$XDG_CONFIG_HOME/agent-team`. It does not start a team or install Python
packages. If the config directory is a non-empty existing directory, sync
leaves it untouched and the bundled defaults remain available.

## Meet the prerequisites

The current implementation has been smoke-tested against a live Orca runtime on
macOS. The Linux executable mapping is implemented as `orca-ide`, but this
checkout has not had a live Linux Orca smoke test. Windows is unsupported and
fails fast. Orca executable selection is exact by platform, with no PATH
fallback or environment override:

- macOS: `orca`
- Linux: `orca-ide`

Before starting a team:

1. Make sure the platform-specific Orca executable above, `claude`, `codex`,
   Node.js, and `npx` are available.
2. Open Orca and confirm that the platform-specific `status --json` command
   reports a ready runtime and graph.
3. Log in to Claude and Codex with the accounts you intend to use.
4. Register the target repository with Orca once.

```bash
claude auth status
codex login status
# macOS
orca status --json
orca repo add --path "$PWD"
# Linux
orca-ide status --json
orca-ide repo add --path "$PWD"
```

The ACP Planner uses the exact packages `acpx@0.13.2` and
`@agentclientprotocol/claude-agent-acp@0.70.0`. The first ACP run can download
them through `npx`; it does not install them globally.

If a team created by config version 2 is still running, stop it with the old
code before switching to version 3. There is no legacy fallback.

## Start a team

Inspect the derived role metadata and direct-agent arguments without operating
Orca or starting an agent:

```bash
agent-team start --dry-run
```

The dry run does not render the task-specific ACP command. That command is
created only when an ACP role is dispatched.

Start Main and focus its Orca terminal:

```bash
agent-team start
```

Use `--no-attach` when the terminal should remain in the background:

```bash
agent-team start --no-attach
```

Ask Main for the development task. Main decides whether to run Planner first,
then dispatches Worker and Reviewer through the `agent_team` MCP server. Main
is the only role that talks to the user.

## Operate and stop a team

```bash
# Inspect the Run, Main terminal, and worker accounting.
agent-team status

# Focus Main.
agent-team attach main

# Focus a background role only after Main has dispatched it.
agent-team attach worker

# Stop this team's owned terminals and remove its runtime state.
agent-team stop
```

`stop` keeps the Orca Run as an audit record. It does not commit, push,
publish, or delete project files.

All management commands must use the same `--config` and `--cwd` values used by
`start`:

```bash
agent-team start \
  --config /absolute/path/to/config.toml \
  --cwd /absolute/path/to/project

agent-team status \
  --config /absolute/path/to/config.toml \
  --cwd /absolute/path/to/project
```

## Know the safety boundary

- Unsupported provider, transport, permission, config version, or state format
  fails before launch. The launcher never silently switches transports.
- ACP is enabled only for read-only Claude background roles. Main ACP, Codex
  ACP, and workspace-write ACP are rejected.
- ACP permission mediation is not an operating-system sandbox. Write access
  remains on direct Codex with its isolated permission profile.
- Agent output is untrusted data. Matching Task, Dispatch, terminal, sender,
  and Delivery identities decide lifecycle state.
- Claude ACP reuses the ambient `claude.ai` login and receives no API-key
  environment variables. The subscription billing ledger itself has not been
  verified.
- The current Store requires `STORE_SCHEMA=4` and SQLite `user_version=4`.
  Provider events remain `EVENT_SCHEMA_VERSION=2`; workflow events use the
  separate `WORKFLOW_EVENT_SCHEMA_VERSION=1`. Its schema-4 image is the
  existing nine tables plus `task_policy_states`, `verification_operations`,
  and `verification_receipts`, for exactly twelve tables.
- Exact schema-2 and schema-3 Stores are each reported as
  `StoreMigrationRequiredError` with their source schema and target `4`; the
  read-only Doctor reports `MIGRATION_REQUIRED`. Malformed, mixed, missing,
  extra, or future images are different fail-closed schema/integrity errors.
  Issue #48 owns the explicit migration path; this Store does not silently
  migrate, fill defaults, or fall back to another backend.
- Backup destinations are exact single basenames. A backup is successful only
  after its database/manifest pair passes final identity and content readback;
  partial or mixed pairs are rejected. The restore-candidate namespace
  `.coordination.sqlite3.restore-` is reserved and rejected as a destination.
- Version-1 backup/inspect keeps its exact ten-field, two-file manifest shape.
  The schema-4 foundation records `store_schema=4`,
  `event_schema_version=2`, and `sqlite_user_version=4` (`4/2/4`). Its
  production path writes no row to the three new tables: only an image with
  those tables empty is a structural baseline. A non-empty new table fails
  closed; #80 does not claim non-empty verification image inspection or
  backup/restore success.
- The established image is classified before root mutation through a
  read-only, WAL/SHM-aware pre-gate. Structural WAL is copied with the image;
  SQLite reconstructs the ephemeral SHM cache only in a private temporary
  copy. The source, gate, marker, fileset, and DB/WAL/SHM bytes are unchanged;
  the classifier does not checkpoint, truncate, delete, or create source
  sidecars.
- The #80 codecs are pure version-1 codecs for the 15-field `TaskPolicyStateV4`,
  approval-binding snapshot, body-free verification request, and normalized
  receipt. They exclude raw argv/environment values and raw bodies, and check
  value consistency only; they do not capture owner authority or hydrate a
  Gate value. Live capture/context, Store adapter, lifecycle transactions,
  logical record digest, non-empty image semantics, and verification-aware
  Doctor/restore remain downstream work.
- Provider-only restore remains candidate-first and provider-free under its
  historical contract. The #80 foundation proves only the empty-new-ledger
  schema-4 backup/restore round trip; it does not silently repair logs,
  retry an external effect, or authorize a non-empty verification image.
- The P0 Store does not wire the WorkflowEngine reducer or external effect
  adapters, and it makes no external-effect exactly-once claim.

Issue #73 adds a private `workflow_effect_adapter.py` seam between that Store
and an injected durable effect backend. It preserves the public `TeamRuntime`
and `BackendPort` `start`/`request`/`stop` methods, existing request/result
types, and CLI/MCP envelopes. The current public `BackendPort` and Orca backend
fail fast with `DurabilityUnsupported` before any effect: they do not provide
the required role-effect metadata, generation, exact Delivery/read lookup, or
provider proof, and current Orca STOP has no composite-stop proof. The adapter
is not wired into the CLI or MCP path. Durable `StartSpec.attach=True` is also
rejected because its focus stage has no composite proof.

The private path is `load → authority → begin → backend once → validate the
post-effect authority and observation → Store receipt → projector → commit`.
Common capability requires effect-key idempotency or pure lookup, attempt/fence
enforcement, and consumer generation. WAIT additionally requires exact
Delivery lookup, READ exact read lookup, and STOP an ordered composite proof
and pure lookup. START/PROMPT bind effect-allocated post-effect identities,
including generation; receipts and observations retain an immutable field
snapshot. Lookup returns only committed, digest-verified evidence: a
`DurableDeliveryLookup` is the WAIT origin and does not reconstruct ACK/reply
lifecycle, while `DurableReadLookup` obtains output through the backend's pure
lookup. A committed effect replays with zero backend execute and projector
calls; WAIT/READ/RELEASE/STOP may perform one digest-bound pure lookup.
`INTENT`, `UNKNOWN_EFFECT`, response loss, and restart ambiguity remain
`RecoveryRequired` for explicit #32 recovery. Raw bodies are bounded to 1 MiB
of UTF-8 and contribute only digests; this prevents raw persistence but does
not hide equality for low-entropy input. The deterministic fake authority,
backend, projector, and real Store prove this adapter contract only. They do
not prove provider-side exactly-once or a #31 cross-store atomic join. Workflow
reducer wiring remains #33, and policy/verification handoff remains #74.
The schema-4 foundation is [Issue #80](https://github.com/iamtatsuki05/dotfiles/issues/80);
task/review production transitions are [#81](https://github.com/iamtatsuki05/dotfiles/issues/81),
verification transactions and adapter wiring are [#82](https://github.com/iamtatsuki05/dotfiles/issues/82),
and image evidence, backup/restore, and Doctor work are [#83](https://github.com/iamtatsuki05/dotfiles/issues/83).

Issue #74's handoff takes the actual #49 `ReviewPolicyUpdate` plus policy and
the actual #50 `route_task()` plus matching reservation result. Each owner ref
is issued after its owner validation and `save_*`/exact `read_*` readback. The
composer creates an `ApprovalRef` only for canonical `REVIEW_DECISION +
APPROVED` review authority. It compares only overlap fields; #49-only
`Run`/`Dispatch`/`Attempt`, terminal,
review-round, target, and `claim_ref` remain #49 provenance and are not claimed
as #50 comparisons. The Gate keeps `start(ApprovalRef)` and
`resume(VerificationHandle)` and its six state-port operations. Handoff tests
use a deterministic fake, which is not evidence of SQLite, restart, or
provider exactly-once behavior. [Issue #78](https://github.com/iamtatsuki05/dotfiles/issues/78)
is split into the [Issue #80 schema-4 foundation](https://github.com/iamtatsuki05/dotfiles/issues/80)
and the downstream [#81](https://github.com/iamtatsuki05/dotfiles/issues/81),
[#82](https://github.com/iamtatsuki05/dotfiles/issues/82), and
[#83](https://github.com/iamtatsuki05/dotfiles/issues/83) work. The full ledger,
restart/replay, `mark_unknown`, and non-empty image claims are outside #80.
There is no raw-body/action alias/payload path or retry/fallback.

See [Architecture](docs/architecture.md) for the complete boundary and failure
flow.

## Troubleshoot common failures

| Symptom | What to check |
|---|---|
| `workspace is not managed by Orca` | Run `orca repo add --path "$PWD"` on macOS, or `orca-ide repo add --path "$PWD"` on Linux. |
| `agent-team state already exists` | Use `agent-team status`, `attach`, or `stop`; do not start a second owner. |
| `role has no active Orca Dispatch` | Main has not started that background role, or it has already been released. |
| Authentication is required | Run `claude auth status` or `codex login status` outside agent-team. |
| ACP startup fails on the first run | Check network access for the exact `npx` packages, then retry explicitly. |
| A role reports `escalation` | Inspect the retained terminal and Run; do not treat escalation as completion. |

## Prepare a useful failure report

When this guide does not resolve a failure, report it to the repository
maintainer with the command, config path, workspace, Orca version, Run/Task/
Dispatch IDs, and the smallest relevant error. Do not include authentication
tokens, prompt contents, or unrelated terminal output.

Useful terms:

- **Run**: the Orca namespace and coordinator inbox for one team execution.
- **Task**: one bounded Planner, Worker, or Reviewer assignment.
- **Dispatch**: one attempt that binds a Task to a terminal.
- **Delivery**: a message batch that Main must process and acknowledge.
- **direct**: the provider's normal interactive CLI.
- **ACP**: Agent Client Protocol, used through the pinned acpx client.

## Develop and verify changes

```bash
python3.13 -m unittest tests.test_agent_team tests.test_agent_team_mcp
uvx ruff check \
  scripts/agent-team/agent_team \
  tests/test_agent_team.py \
  tests/test_agent_team_mcp.py
uvx mypy --strict scripts/agent-team/agent_team
python3.13 -m build --wheel scripts/agent-team
zsh tests/test_agent_sync.sh
```

When changing Orca or ACP integration, repeat a real bounded smoke test and
confirm that terminals, state, prompt files, sessions, and adapter processes
are gone after `stop`.
