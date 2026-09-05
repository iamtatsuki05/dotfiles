# Version-4 Configuration

[日本語](configuration-v4_JA.md) · [Version-3 configuration](configuration.md)

Version 4 is a catalog of named team topologies. A team can explicitly link a
version-3 launch configuration to use the existing Orca runtime. Teams without
a launch configuration support graph inspection and selection dry runs.

The topology stores provider, transport, and permission. The referenced
version-3 launch config supplies model and effort values and performs ACP
dependency preflight only for the ACP roles it selects.
For Claude ACP, that preflight requires Node.js `22.13.0` or newer and the
exact `acpx@0.13.2` and
`@agentclientprotocol/claude-agent-acp@0.70.0` packages. It records absolute
executable paths with SHA-256 fingerprints and never invokes `npm` or `npx`.

## Minimal schema

The top-level `version` key must be the integer `4`, and `runtime` must be
explicitly set to `"orca"`. The `teams` table must be non-empty. Each team has
a map key as its exact ID, a display name, and node/edge arrays.

```toml
version = 4
runtime = "orca"

[teams.build]
name = "Build Team"

[[teams.build.nodes]]
id = "main"
label = "Main"
main = true
[teams.build.nodes.profile]
provider = "claude"
transport = "direct"
permission = "orchestrator"

[[teams.build.nodes]]
id = "worker"
label = "Worker"
main = false
[teams.build.nodes.profile]
provider = "codex"
transport = "direct"
permission = "workspace-write"

[[teams.build.nodes]]
id = "reviewer"
label = "Reviewer"
main = false
[teams.build.nodes.profile]
provider = "codex"
transport = "direct"
permission = "read-only"

[[teams.build.edges]]
source = "main"
target = "worker"
kind = "delegates-to"

[[teams.build.edges]]
source = "worker"
target = "reviewer"
kind = "reviewed-by"
```

An absent or nested `version` key is not a v4 configuration and is rejected.

For a team with no edges, declare `edges = []`. The accepted fields are
deliberately closed:

- config: `version`, `runtime`, `teams`
- team: `name`, `nodes`, `edges`, optional `launch_config`
- node: `id`, `label`, `main`, `profile`
- profile: `provider`, `transport`, `permission`
- edge: `source`, `target`, `kind`

The parser applies resource and diagnostic bounds before topology validation:

| Item | Maximum |
|---|---:|
| Config file | 1,048,576 bytes |
| Teams | 64 |
| Nodes per team | 128 |
| Edges per team | 256 |
| Team/node/edge/profile identifier | 64 characters |
| Team display name | 128 characters |
| Node label | 128 characters |
| Validation errors per config | 64 |
| One validation error message | 512 characters |
| Combined validation diagnostics | 16,384 characters |

These limits are fail-fast checks. The parser reads at most one byte beyond the
file limit to distinguish an over-limit file, and it does not materialize an
unbounded diagnostic string.

Unknown fields, empty or unsafe text, a non-boolean `main`, and an unsupported
permission fail before any runtime resource is considered. Profiles are
resolved against the verified harness registry. The topology validator then
rejects unknown profiles, duplicate IDs/labels, invalid Main cardinality,
edge errors, relationship cycles, and nodes unreachable from Main.

Team IDs are exact, case-sensitive map keys. Selection never trims, case-folds,
aliases, chooses the first team, or supplies a default. IDs that differ only
by case are rejected as ambiguous.

## Pure inspection commands

List all teams in deterministic ID order. The JSON includes each display name,
whether validation succeeded, and stable validation error records:

```bash
agent-team teams --config /absolute/path/to/config-v4.toml
```

This command always emits JSON; it has no `--json` compatibility switch.

Render one selected topology:

```bash
agent-team graph \
  --config /absolute/path/to/config-v4.toml \
  --team build \
  --format json
```

`--format` accepts only `json`, `ascii`, or `mermaid`. The renderer output is
topology data only; it does not contain shell commands or Orca payloads.

Without `launch_config`, a dry run returns only the selected `config_path`,
`team_id`, and canonical `workspace`:

```bash
agent-team start --config /absolute/path/to/teams.toml --team build --dry-run
```

`teams`, `graph`, and all dry runs launch no external processes. An invalid
topology anywhere in the catalog prevents graph rendering and launch planning.
Inspection-only teams reject real startup and management commands.

## Launch a named team

The bundled [teams.toml](../agent_team/defaults/teams.toml) is a complete runnable
catalog. It refers to the adjacent `config.toml`, which retains the models,
efforts, prompts, permissions, and review-round limit. From this repository:

That bundled launch config uses `fable` for Main and Planner and `gpt-6-astra`
for Worker and Reviewer. A dry run does not resolve ACP dependencies; the
selected launch does so only when its version-3 config contains an ACP role.

```bash
scripts/agent-team/agent-team start \
  --config scripts/agent-team/agent_team/defaults/teams.toml \
  --team agent-team --dry-run
```

After the normal dotfiles sync, the same catalog is available at
`~/.config/agent-team/teams.toml` (or beneath your `XDG_CONFIG_HOME`):

```bash
agent-team start --config ~/.config/agent-team/teams.toml --team agent-team --no-attach
agent-team status --config ~/.config/agent-team/teams.toml --team agent-team
agent-team attach main --config ~/.config/agent-team/teams.toml --team agent-team
agent-team stop --config ~/.config/agent-team/teams.toml --team agent-team
```

To add another runnable team, copy its launch configuration beneath the catalog
directory and give it a distinct `team_prefix`. Use that exact prefix as the
catalog team ID and set `launch_config` to the relative file path. Edit the
models, efforts, and prompts in that launch file. Stop the active team before
switching to another team in the same workspace.

Runnable entries must match the implemented serial workflow:

- Exactly four node IDs: `main`, `planner`, `worker`, `reviewer`; only `main`
  has `main = true`.
- Each node's provider, transport, and permission match its launch role.
- Exactly eight edges: Main delegates to each of the three roles; Planner and
  Worker are reviewed by Reviewer; each of the three roles escalates to Main.
- `launch_config` is a relative path to a regular file within the catalog
  directory, without symlinks in the path.
  The referenced version-3 configuration must pass its normal validation.
- The selected team ID equals the referenced `team_prefix`. This prevents two
  different catalog team IDs from silently selecting the same runtime state.

For these entries, `start --dry-run` shows the actual launch plan, including
role commands and state path; `--no-attach` is accepted. Real `start`, `status`,
`attach`, and `stop` use the same version-3 lifecycle. The plan's `config_path`
is the referenced launch file so child role processes read the same settings.
Unsupported graphs remain available for inspection but fail before runtime
resources are created. Arbitrary graph execution and parallel roles are not
implemented.

## Version boundary

The version-3 loader and state contract remain unchanged. Version-3 files use
the existing fixed-role schema and do not accept v4 team operations. Adding a
top-level v4 `teams` field to a version-3 file is rejected at the CLI version
boundary; fields are never copied between versions and no silent fallback is
performed. Version-3 `start`, `status`, `attach`, and `stop` retain their
historical path and file-size behavior. Supplying `--team` explicitly selects
the bounded v4 loader, which rejects a version-3 file with the version-4 error.
Without `--team`, a version-4 file follows the old version-3 path and fails
with the unchanged `version must be integer 3` message before any resource is
created.
