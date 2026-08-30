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
- [Task policy schema v4](docs/task-policy-v4.md) defines the immutable
  `TaskSpec`, dependency order, and state observation contract without storage
  or workflow execution.
- [Serial review policy](docs/review-policy.md) defines the typed serial gate
  shared by normal tasks and Issue #50-admitted express tasks, without backend wiring.

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

The current implementation is validated on macOS. Before starting a team:

1. Make sure `orca`, `claude`, `codex`, Node.js, and `npx` are available.
2. Open Orca and confirm that `orca status --json` reports a ready runtime and
   graph.
3. Log in to Claude and Codex with the accounts you intend to use.
4. Register the target repository with Orca once.

```bash
claude auth status
codex login status
orca status --json
orca repo add --path "$PWD"
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

See [Architecture](docs/architecture.md) for the complete boundary and failure
flow.

## Troubleshoot common failures

| Symptom | What to check |
|---|---|
| `workspace is not managed by Orca` | Run `orca repo add --path "$PWD"`. |
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
