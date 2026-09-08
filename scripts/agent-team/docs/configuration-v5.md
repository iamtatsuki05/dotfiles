# Version 5 configuration reference

[日本語](configuration-v5_JA.md) · [Configuration](configuration.md) · [Architecture](architecture.md)

Version 5 gives each node an ID, role kind, and its own settings, and binds each
TaskSpec stage to exact writer/reviewer nodes. Native `agent`/`serial` teams can
run. Program coordination, parallel execution, and named Orca execution remain
unavailable; their graph values can be validated and rendered.

The bundled version-3 defaults remain Main/Planner on Claude `fable` and
Worker/Reviewer on direct Codex `gpt-6-astra`. The example here explicitly uses
Claude for every node. See [Architecture](architecture.md) for the separate
bounded live acceptance and its limits.

## Declare exact node and graph fields

| Location | Required fields and limits |
|---|---|
| Top level | `version = 5`, `runtime`, `teams`; runtime is `orca`, `tmux`, `zellij`, or `herdr`; 1–64 teams. |
| Team | `name`, `max_review_rounds`, `nodes`, `edges`, `coordination`, `tasks`, `routes`; 1–128 nodes and at most 256 edges. |
| Node | `id`, `label`, `kind`, `role_spec`; kind is `main`, `planner`, `worker`, or `reviewer`. |
| Role spec | `provider`, `transport`, `model`, `effort`, `prompt`, `permission`; validated against the selected profile and role kind. |
| Coordination | `mode`, `entry_nodes`, `dispatch_mode`, `max_active`; mode is `agent` or `program`, dispatch is `serial` or `parallel`. |
| Edge | `source`, `target`, `kind`; kind is `delegates-to`, `reviewed-by`, or `consults-to`. |

Team IDs match `[a-z][a-z0-9-]{0,23}`; node IDs match
`[a-z][a-z0-9-]{0,63}`. Names and labels are non-empty printable strings up to
128 characters. `max_review_rounds` and `max_active` are positive integers;
serial dispatch requires `max_active = 1`. Boolean values are not integers.
Unknown fields are rejected. Prompt paths resolve relative to the config
directory and must name existing files inside it.

An agent graph has exactly one Main, which is its only entry. A program graph
has no Main and declares its entries explicitly. Every node must be reachable.
Delegation and review edges must have no directed cycle. Review edges connect
a Planner or Worker to a Reviewer; delegation cannot target Main. Consultation
adds one-hop reachability but does not follow the target's outgoing edges.
Agent-to-agent consultation has no runtime operation yet.

Each task uses the [TaskSpec fields](configuration.md#taskspec-catalog-is-optional-required-for-native-task-dispatch).
Each route requires `task_id` and at least one complete pair:
`plan_writer`/`plan_reviewer` or
`implementation_writer`/`implementation_reviewer`. Each pair needs its matching
review edge. A route with both pairs also needs delegation from plan writer to
implementation writer. Routes and declared task IDs must match exactly.
Omitted pairs become `null` in graph JSON; a declared plan pair must pass plan
review before implementation.

## Define two Workers and two Reviewers

This five-node example omits Planner. Create the referenced prompt files
(or copy the bundled [prompts directory](../agent_team/defaults/prompts)) beside the config before validation.
Before a real run, prepare the source and verifier files in your workspace and
replace `/workspace/example` with that absolute path. Verification runs the
declared argv exactly. The selected native runtime and ACP prerequisites are
listed in [Configuration](configuration.md#select-an-experimental-native-terminal-runtime-explicitly).

```toml
version = 5
runtime = "tmux"
[teams.all-claude]
name = "All Claude Serial"
max_review_rounds = 2
[[teams.all-claude.nodes]]
id = "main"
label = "Main"
kind = "main"
[teams.all-claude.nodes.role_spec]
provider = "claude"
transport = "direct"
model = "fable"
effort = "high"
prompt = "prompts/orchestrator.md"
permission = "orchestrator"
[[teams.all-claude.nodes]]
id = "worker-a"
label = "Worker A"
kind = "worker"
[teams.all-claude.nodes.role_spec]
provider = "claude"
transport = "acp"
model = "fable"
effort = "high"
prompt = "prompts/worker.md"
permission = "workspace-write"
[[teams.all-claude.nodes]]
id = "worker-b"
label = "Worker B"
kind = "worker"
[teams.all-claude.nodes.role_spec]
provider = "claude"
transport = "acp"
model = "fable"
effort = "high"
prompt = "prompts/worker.md"
permission = "workspace-write"
[[teams.all-claude.nodes]]
id = "reviewer-a"
label = "Reviewer A"
kind = "reviewer"
[teams.all-claude.nodes.role_spec]
provider = "claude"
transport = "acp"
model = "fable"
effort = "high"
prompt = "prompts/reviewer.md"
permission = "read-only"
[[teams.all-claude.nodes]]
id = "reviewer-b"
label = "Reviewer B"
kind = "reviewer"
[teams.all-claude.nodes.role_spec]
provider = "claude"
transport = "acp"
model = "fable"
effort = "high"
prompt = "prompts/reviewer.md"
permission = "read-only"
[[teams.all-claude.edges]]
source = "main"
target = "worker-a"
kind = "delegates-to"
[[teams.all-claude.edges]]
source = "main"
target = "worker-b"
kind = "delegates-to"
[[teams.all-claude.edges]]
source = "worker-a"
target = "reviewer-a"
kind = "reviewed-by"
[[teams.all-claude.edges]]
source = "worker-b"
target = "reviewer-b"
kind = "reviewed-by"
[teams.all-claude.coordination]
mode = "agent"
entry_nodes = ["main"]
dispatch_mode = "serial"
max_active = 1
[[teams.all-claude.tasks]]
task_id = "write-sum"
objective = "Implement the sum operation in the user's workspace."
acceptance_criteria = ["The sum verifier passes."]
allowed_paths = ["src/calc_sum.py"]
forbidden_paths = ["src/verify_sum.py"]
dependencies = []
evidence_requirements = ["Changed paths and verifier output."]
consultation_conditions = []
[[teams.all-claude.tasks.verification]]
name = "verify-sum"
argv = ["python", "-B", "/workspace/example/src/verify_sum.py"]
timeout_seconds = 30
[[teams.all-claude.tasks]]
task_id = "write-product"
objective = "Implement the product operation in the user's workspace."
acceptance_criteria = ["The product verifier passes."]
allowed_paths = ["src/calc_product.py"]
forbidden_paths = ["src/verify_product.py"]
dependencies = []
evidence_requirements = ["Changed paths and verifier output."]
consultation_conditions = []
[[teams.all-claude.tasks.verification]]
name = "verify-product"
argv = ["python", "-B", "/workspace/example/src/verify_product.py"]
timeout_seconds = 30
[[teams.all-claude.routes]]
task_id = "write-sum"
implementation_writer = "worker-a"
implementation_reviewer = "reviewer-a"
[[teams.all-claude.routes]]
task_id = "write-product"
implementation_writer = "worker-b"
implementation_reviewer = "reviewer-b"
```

## Inspect a Mainless variant

To derive a graph-only variant from the complete example:

1. Remove the `main` node and its `role_spec`, plus both `main` delegation edges.
2. Replace the coordination table with the snippet below.
3. Keep both Worker-to-Reviewer edges, TaskSpecs, and routes.

```toml
[teams.all-claude.coordination]
mode = "program"
entry_nodes = ["worker-a", "worker-b"]
dispatch_mode = "serial"
max_active = 1
```

This variant passes graph validation; real start rejects `program` mode.
Changing `dispatch_mode` to `parallel` and choosing a positive `max_active`
also produces a graph value, but parallel start is rejected.

## Select a team explicitly

```bash
agent-team teams --config /path/to/config-v5.toml
agent-team validate --config /path/to/config-v5.toml --team all-claude
agent-team graph --config /path/to/config-v5.toml \
  --team all-claude --format json
agent-team start --config /path/to/config-v5.toml \
  --cwd /workspace/example --team all-claude --dry-run
```

`teams` lists every parsed team. `validate` can omit `--team` to check all teams.
`graph` and version-5 `start` require `--team`; selection is exact, without
aliases or case conversion. Graph formats are `json`, `ascii`, and `mermaid`.
For the native agent/serial example, remove `--dry-run` to start after meeting
the prerequisites. Program, parallel, and named Orca starts fail before
dependency probes or resource creation. Inspection does not start providers.

## Use saved identity for management

`NodeRef(node_id, kind)` separates node identity from role kind. Two Workers
can have different model, effort, or prompt settings. No node is selected by
its kind's name or its position in the config.

The runtime team name is
`<team-id>-<workspace-slug>-<sha256(absolute-workspace)[:8]>`.
State is stored at `$XDG_STATE_HOME/agent-team/<derived-team-id>/state.json`,
defaulting to `~/.local/state/agent-team/`. Use `status`, `attach`, or `stop`
with `--state` to manage that saved run without rereading its config.

The config version is 5; named native state uses version 4. `role_specs` holds
all nodes, while `roles` holds only active assignments. Older fixed-role state
is not converted. See [Architecture](architecture.md) for identity checks and
the question, completion, review, verification, and cleanup contracts.
