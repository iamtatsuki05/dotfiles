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
  current Issue #72 head adds the v3 workflow checkpoint/CAS contract and
  keeps provider effects outside the Store.
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
- The current Store requires `STORE_SCHEMA=3` and SQLite `user_version=3`.
  Provider events remain `EVENT_SCHEMA_VERSION=2`; workflow events use the
  separate `WORKFLOW_EVENT_SCHEMA_VERSION=1`.
- A valid v2 Store is reported as `StoreMigrationRequiredError` and Doctor
  `MIGRATION_REQUIRED`. Malformed or future schema is a different error. Only
  Issue #48's explicit migration gate may convert v2 to v3; the Store never
  fills defaults or falls back to another backend.
- Backup destinations are exact single basenames. A backup is successful only
  after its database/manifest pair passes final identity and content readback;
  partial or mixed pairs are rejected. The restore-candidate namespace
  `.coordination.sqlite3.restore-` is reserved and rejected as a destination.
- Version-1 backup/inspect keeps its two-file manifest shape and now records
  version values `3/2/3` (Store/provider events/SQLite user version). It
  preserves and validates workflow rows as part of the image.
- Restore is candidate-first and provider-free. Resume begins with a
  tombstone-then-ledger durability barrier and never silently repairs logs or
  retries an external effect.
- Until a dedicated workflow restore binding exists, source or current images
  containing workflow rows are rejected before promotion/replacement.
- The P0 Store does not wire the WorkflowEngine reducer or external effect
  adapters, and it makes no external-effect exactly-once claim.

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
