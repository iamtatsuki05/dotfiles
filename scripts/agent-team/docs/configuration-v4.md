# Version-4 Configuration

[日本語](configuration-v4_JA.md) · [Version-3 configuration](configuration.md)

Version 4 describes one or more immutable team topologies. It is an inspection
and selection contract; it does not start a provider, Orca resource, task,
lease, or state store.

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
- team: `name`, `nodes`, `edges`
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

Render one selected topology with the PR #20 renderer:

```bash
agent-team graph \
  --config /absolute/path/to/config-v4.toml \
  --team build \
  --format json
```

`--format` accepts only `json`, `ascii`, or `mermaid`. The renderer output is
topology data only; it does not contain shell commands or Orca payloads.

The v4 dry run passes only a typed selection plan to the future runtime/store
seam:

```bash
agent-team start \
  --config /absolute/path/to/config-v4.toml \
  --cwd /absolute/path/to/project \
  --team build \
  --dry-run
```

Its output has exactly `config_path`, `team_id`, and `workspace`.
The config still validates `runtime = "orca"`, but runtime/backend selection
is not part of this narrow plan.
The workspace is absolute and canonical. It intentionally has no state path,
lease, backend ownership, provider command, or role launch arguments. A v4
`start` without `--dry-run`, and v4 `status`, `attach`, and `stop`, fail
explicitly until the later runtime/store integration is complete.

All three pure commands and v4 dry run perform no external process execution.
An invalid team anywhere in the config is reported by `teams` and prevents a
launch plan or graph from being built.

`--no-attach` is a v3 startup option and is rejected for v4 dry runs rather
than being silently ignored.

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
