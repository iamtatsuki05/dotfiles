# Configuration Reference

[日本語](configuration_JA.md) · [README](../README.md) ·
[Architecture](architecture.md)

`agent-team` keeps the existing version-3 fixed-role configuration and also
accepts the explicit version-4 topology configuration. Version-3 `runtime =
"orca"` retains the fixed four-role contract; version-3 `runtime = "tmux"` is
an experimental native subset with Main and optional Claude ACP read-only
background roles. Missing values and unsupported combinations fail before any
role starts. See
[Version-4 configuration](configuration-v4.md) for the separate topology
schema and pure inspection commands.

## Start from the canonical config

```toml
version = 3
runtime = "orca"
team_prefix = "agent-team"
max_review_rounds = 2

[main]
provider = "claude"
transport = "direct"
model = "fable"
effort = "high"
prompt = "prompts/orchestrator.md"
permission = "orchestrator"

[roles.planner]
provider = "claude"
transport = "acp"
model = "fable"
effort = "high"
prompt = "prompts/planner.md"
permission = "read-only"

[roles.worker]
provider = "codex"
transport = "direct"
model = "gpt-6-astra"
effort = "medium"
prompt = "prompts/worker.md"
permission = "workspace-write"

[roles.reviewer]
provider = "codex"
transport = "direct"
model = "gpt-6-astra"
effort = "high"
prompt = "prompts/reviewer.md"
permission = "read-only"
```

The bundled config uses `fable` for Main and Planner and `gpt-6-astra` for
Worker and Reviewer. The canonical Planner is the Claude read-only ACP role;
the canonical Worker and Reviewer remain direct Codex roles.

## Select the experimental native tmux subset explicitly

Use `runtime = "tmux"` only in a custom version-3 config. Main is required and
must be direct Claude with `orchestrator` permission. Planner and Reviewer may
be omitted or may each be verified Claude ACP with `read-only` permission and
the pinned `claude-acp-0.70.0` adapter. Worker and every other native profile
are rejected before state, Task, Dispatch, or process effects are created.
Native startup requires the `tmux` executable and does not require Orca or
Codex. A config that selects an ACP role still requires the pinned Node.js and
ACP dependencies described below.

```toml
version = 3
runtime = "tmux"
team_prefix = "native-experiment"
max_review_rounds = 2

[main]
provider = "claude"
transport = "direct"
model = "<available-claude-model>"
effort = "high"
prompt = "prompts/orchestrator.md"
permission = "orchestrator"
```

Replace `<available-claude-model>` with a model ID exposed by the Claude
account used for this run. It is a user-supplied placeholder and does not
change the bundled `fable`/`gpt-6-astra` defaults. Native `start`, `status`,
`attach`, and `stop` route to `TmuxBackend`; only Main can be attached. Native
ACP completion is published by the runner and is independent of tmux pane
text. The lifecycle still requires `role_read` → `role_release` →
`delivery_ack`; `native.last_ack` is one receipt marker, not Task or goal
completion.

The model-free native tmux CLI start/status/stop path has succeeded with Orca
and Codex absent, a workspace path containing spaces, and a deleted config. A
real Claude 2.1.112 Main launch with `fable`/`high` and a logged-in
`claude.ai` account was rejected by the provider because `fable` was absent or
inaccessible. No alternative model was used, so this does not establish a
native/provider end-to-end run.

## Top-level fields define one team contract

| Field | Contract |
|---|---|
| `version` | Must be integer `3`. No automatic migration is performed. |
| `runtime` | Must be `"orca"` or `"tmux"`. `orca` uses four roles; `tmux` is the experimental native subset. There is no Herdr or Zellij fallback. |
| `team_prefix` | Must match `[a-z][a-z0-9-]{0,23}`. It contributes to the runtime team ID. |
| `max_review_rounds` | Positive integer. Counts the first Reviewer decision and every retry for one stage. |
| `main` | Required Main role table. |
| `roles` | `orca` must contain exactly `planner`, `worker`, and `reviewer`; `tmux` may contain only optional `planner` and `reviewer`. Main is declared separately and is always required. |

The runtime team ID combines `team_prefix` with the workspace name and a hash
of the absolute workspace path. The config path is not part of the ID. Two
configs with the same prefix and workspace therefore refer to the same team
state. Different prefixes create separate states, but agent-team does not
coordinate file edits between those teams.

Changing `team_prefix` changes the derived state location. Stop the existing
team before changing it.

## Every role declares the same fields

| Field | Meaning |
|---|---|
| `provider` | One of the ten recognized harness IDs. Only profiles listed in the [support matrix](support-matrix.md) are runnable. |
| `transport` | `direct` or `acp`; it is always explicit. |
| `model` | Provider model identifier passed to the selected runtime. |
| `effort` | Provider reasoning/effort level. |
| `prompt` | Markdown file relative to the agent-team config directory. |
| `permission` | Role-fixed permission; arbitrary values are rejected. |

Prompt paths must stay inside the config directory and must name existing
files. Absolute escapes and `..` escapes are rejected.

## The Orca capability matrix is intentionally small

| Role | Allowed provider / transport | Required permission |
|---|---|---|
| Main | Claude or Codex / `direct` | `orchestrator` |
| Planner | Claude or Codex / `direct`; Claude / `acp`; Copilot / `direct` | `read-only` |
| Worker | Codex / `direct` | `workspace-write` |
| Reviewer | Claude or Codex / `direct`; Claude / `acp`; Copilot / `direct` | `read-only` |

The current canonical Reviewer is direct Codex. Claude ACP support for
read-only background roles is available, but enabling it is an explicit config
change. Copilot is limited to read-only Planner/Reviewer direct background
profiles with exact CLI `1.0.81`. Main ACP, Codex ACP, Claude workspace-write,
and all workspace-write ACP combinations fail fast.

Adding a new provider or ACP adapter is not a config-only operation. It requires
a code change, capability and permission tests, an exact version policy, and a
real lifecycle/cleanup smoke test.

The native tmux capability matrix is smaller:

| Role | Allowed provider / transport | Required permission |
|---|---|---|
| Main | Claude / `direct` | `orchestrator` |
| Planner | Claude / `acp` (optional) | `read-only` |
| Reviewer | Claude / `acp` (optional) | `read-only` |

Native Worker, direct Reviewer, Codex ACP, Main ACP, workspace-write ACP, and
all other native provider profiles fail before startup effects.

## ACP dependencies are explicit and selected-only

A config that selects Claude `acp` requires Node.js `22.13.0` or newer and the
exact packages `acpx@0.13.2` and
`@agentclientprotocol/claude-agent-acp@0.70.0`. Install them explicitly outside
`agent-team`, for example:

```bash
npm install --prefix /path/to/agent-team-acp acpx@0.13.2 @agentclientprotocol/claude-agent-acp@0.70.0
export PATH="/path/to/agent-team-acp/node_modules/.bin:$PATH"
```

When an ACP role is selected, startup resolves `node`, `acpx`, and
`claude-agent-acp`, checks the exact package manifests, and saves absolute file
paths with SHA-256 fingerprints in the launch snapshot. The runner verifies and
uses that saved binding. Missing or changed files fail closed. Runtime commands
never invoke `npm` or `npx`; a direct-only config does not resolve ACP
dependencies.

## Effort values are provider-specific

| Provider | Accepted values |
|---|---|
| Claude | `low`, `medium`, `high`, `xhigh`, `max` |
| Codex | `none`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max` |
| Copilot | `none` with model `auto`; `low`, `medium`, `high`, `xhigh`, `max` with an explicit model |

The model identifier is not normalized by agent-team. The configured provider
or ACP session must advertise or accept it. A mismatch fails instead of using
another model.

## Permissions are fixed by role

The config cannot promote a role by changing only its permission string:

- Main must use `orchestrator`.
- Planner and Reviewer must use `read-only`.
- Worker must use `workspace-write` and direct Codex.

For direct Codex, agent-team creates an isolated `CODEX_HOME` and derives a
profile from `:read-only` or `:workspace`. For Claude ACP, the client limits
tools to `Read`, `Grep`, and `Glob`, approves reads, and fails when a
non-interactive permission question cannot be resolved. Native tmux has no
direct Worker or Reviewer; its direct Main is supervised by `native_main`, and
its optional ACP roles run as launcher-owned background processes.

## Prompts define role behavior, not process authority

| File | Purpose |
|---|---|
| `prompts/orchestrator.md` | Main routing, handoff, review, and user-gate contract. |
| `prompts/planner.md` | Read-only plan format and scope boundary. |
| `prompts/worker.md` | Minimal implementation, verification, and prohibited operations. |
| `prompts/reviewer.md` | Independent review and `APPROVED` / `CHANGES_REQUESTED` / `ASK_USER`. |

Process authority remains in the launcher, shared MCP allowlist, selected
backend, Dispatch or native assignment, and provider permission profile.
Changing prose cannot grant a role a new tool, transport, or permission.

## Default and custom config precedence

When `--config` is omitted, the launcher uses the first existing source in this
order:

1. `$XDG_CONFIG_HOME/agent-team/config.toml` (or `~/.config/agent-team/config.toml`)
2. the bundled `agent_team/defaults/config.toml` in this project or installed wheel

An existing but invalid user config is an error; it does not silently fall back
to bundled defaults. The dotfiles sync links
`dotfiles/.agent/apps/agent-team/` to the XDG user directory. The dotfiles
config and prompts are user overrides; bundled files are the standalone
distribution default. They are kept byte-identical by the repository test.

## Use a custom config consistently

```bash
agent-team start \
  --config /absolute/path/to/team/config.toml \
  --cwd /absolute/path/to/project
```

Use the same values for `status`, `attach`, and `stop`. The default workspace
is the current directory. All four commands route through the backend named by
the saved `runtime`; `attach` supports Main only for native tmux.

Before making a config active:

```bash
agent-team start \
  --config /absolute/path/to/team/config.toml \
  --cwd /absolute/path/to/project \
  --dry-run
```

The dry run validates config and shows role metadata plus direct-agent
arguments. ACP commands contain task-specific identities and are generated only
at dispatch time.

## Upgrade without a fallback

Config version 2 is rejected by current code. Stop a version-2 team with its old
launcher before installing or switching to version 3. Do not edit a live state
file or copy fields between state versions.
