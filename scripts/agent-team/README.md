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
  describes named team selection, graph inspection, and launch configuration links.
- [Harness support matrix](docs/support-matrix.md) separates recognized,
  available, runnable, and rejected harnesses.
- [ACP boundary](docs/acp.md) explains adapter pins, authentication, and why
  ACP is not a sandbox.
- [Direct background adapters](docs/background-adapters.md) documents the
  Copilot/OpenCode read-only adapter implementation, snapshot boundary, and recovery.

The current configuration uses this team:

| Role | Provider / transport | Model / effort | Permission |
|---|---|---|---|
| Main | Claude / `direct` | `fable` / `high` | `orchestrator` |
| Planner | Claude / `acp` | `fable` / `high` | `read-only` |
| Worker | Codex / `direct` | `gpt-6-astra` / `medium` | `workspace-write` |
| Reviewer | Codex / `direct` | `gpt-6-astra` / `high` | `read-only` |

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

1. Make sure the selected backend and harness commands are available. The bundled
   team uses the Orca executable above, `claude`, `codex`, and the ACP tools below.
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

The ACP Planner requires Node.js 22.13 or later and the installed commands from
`acpx@0.13.2` and `@agentclientprotocol/claude-agent-acp@0.70.0`. Install the
selected tools explicitly, for example into a directory you choose:

```bash
npm install --prefix /path/to/agent-team-acp acpx@0.13.2 @agentclientprotocol/claude-agent-acp@0.70.0
export PATH="/path/to/agent-team-acp/node_modules/.bin:$PATH"
```

Startup records the resolved program paths and fingerprints. Execution uses
those programs directly and does not run `npm` or `npx`. A missing or changed
dependency is an error. Teams using only direct transport do not require the
ACP tools.

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

For named teams, use the bundled catalog or the synced `teams.toml`:

```bash
agent-team start --config ~/.config/agent-team/teams.toml --team agent-team --dry-run
```

See [Version-4 configuration](docs/configuration-v4.md#launch-a-named-team)
to register more teams, inspect their graphs, and start a selected team.

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

Management commands use the saved launch snapshot, even if the original config
or prompt files have been changed or deleted. Use the same `--cwd` and, when
specified, `--config` and `--team` values as `start` to select that run. `--config`
matches the original input path, including a version-4 catalog; it is not read
again. An omitted selector is allowed only when exactly one saved run matches.

```bash
agent-team start \
  --config /absolute/path/to/config.toml \
  --cwd /absolute/path/to/project

agent-team status \
  --config /absolute/path/to/config.toml \
  --cwd /absolute/path/to/project
```

`status`, `attach`, and `stop` also accept `--state /absolute/path/to/state.json`
to select a saved run independently of the current directory. It cannot be
combined with `--team`; an additional `--config` must match the saved path.

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

On Orca 1.4.190, a terminal created in a hidden/discovered worktree can fail
to close with `runtime_error: tab_not_found`. This was reproduced with a
plain `sleep` process as well as Main. Agent Team reports the stop failure
and retains `state.json` and `.cleanup.json`; a terminal disappearing from
the list is not a verified process-stop receipt. Resolve the Orca lifecycle
failure before reusing that team. Do not delete the state to force a restart.
The tracked limitation is [#11](https://github.com/iamtatsuki05/dotfiles/issues/11).

| Symptom | What to check |
|---|---|
| `workspace is not managed by Orca` | Run `orca repo add --path "$PWD"` on macOS, or `orca-ide repo add --path "$PWD"` on Linux. |
| `agent-team state already exists` | Use `agent-team status`, `attach`, or `stop`; do not start a second owner. |
| `role has no active Orca Dispatch` | Main has not started that background role, or it has already been released. |
| Authentication is required | Run `claude auth status` or `codex login status` outside agent-team. |
| ACP dependency check fails | Install the pinned ACP packages explicitly and include their `node_modules/.bin` directory and Node >=22.13 in `PATH`. |
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

The project tracks its development tools in `pyproject.toml` and their resolved
versions and hashes in `uv.lock`. The lock does not add runtime dependencies.
Run these commands from the repository root:

```bash
uv sync --locked --project scripts/agent-team --python 3.13
uv run --locked --project scripts/agent-team python -m unittest discover -s scripts/agent-team/tests
uv run --locked --project scripts/agent-team ruff check \
  scripts/agent-team/agent_team \
  scripts/agent-team/tests \
  tests/test_agent_team.py \
  tests/test_agent_team_mcp.py
uv run --locked --project scripts/agent-team ruff format --check \
  scripts/agent-team/agent_team \
  scripts/agent-team/tests \
  tests/test_agent_team.py \
  tests/test_agent_team_mcp.py
uv run --locked --project scripts/agent-team mypy --strict --python-version 3.11 scripts/agent-team/agent_team
uv run --locked --project scripts/agent-team python -m build --no-isolation scripts/agent-team
DOTFILES_TEST_PYTHON=python uv run --locked --project scripts/agent-team /bin/zsh tests/run.sh
```

CI runs the same locked environment on Python 3.11 and 3.13. Builds use the
locked setuptools installation without creating a second build environment;
the build-system requirement is also pinned for ordinary isolated installs.
The source distribution includes `uv.lock`.

After intentionally editing development dependencies, run
`uv lock --project scripts/agent-team` and commit both files. `--locked` rejects
an out-of-date lock instead of silently updating it. See the
[uv locking documentation](https://docs.astral.sh/uv/concepts/projects/sync/).

When changing Orca or ACP integration, repeat a real bounded smoke test and
confirm that terminals, state, prompt files, sessions, and adapter processes
are gone after `stop`.

To verify the tmux terminal driver on a machine with tmux installed, explicitly
run this test from the repository root:

```bash
uv run --locked --project scripts/agent-team python scripts/agent-team/tests/live_tmux.py
```

It creates a private tmux server, checks literal arguments and process exit
status, and reclaims its own resources. Missing tmux is an error in this test.
The default suite exercises the driver contract without requiring tmux. This
live test covers terminal operations; it does not prove the full team workflow.
