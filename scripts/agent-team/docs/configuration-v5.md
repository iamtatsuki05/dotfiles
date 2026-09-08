# Version 5 configuration reference

[日本語](configuration-v5_JA.md) · [Configuration](configuration.md) · [Architecture](architecture.md)

Version 5 gives each node an ID, role kind, and its own settings, and binds each
TaskSpec stage to exact writer/reviewer nodes. Native `agent`/`serial`,
`program`/`serial`, and `program`/`parallel` teams can run. A program team
has no Main role: the selected native terminal runs the coordinator under the
existing `native_main` supervisor with the fixed `_program-run` argv. It does
not create a separate Main role spec, model, or CLI. Native
`program`/`parallel` uses state version 5 and per-assignment delivery records;
Main-agent parallel and named Orca execution remain rejected. The
implementation has focused contract coverage and bounded real-terminal/
fake-provider coverage. Real-model parallel acceptance is pending.

The bundled version-3 defaults remain Main/Planner on Claude `fable` and
Worker/Reviewer on direct Codex `gpt-6-astra`. The example here explicitly uses
Claude for every node. See [Architecture](architecture.md) for the
bounded real-terminal/fake-provider coverage, separate real-model evidence,
and the limits of each.

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
serial dispatch requires `max_active = 1`, while parallel program dispatch uses
the positive `max_active` cap. Boolean values are not integers.
Unknown fields are rejected. Prompt paths resolve relative to the config
directory and must name existing files inside it.

An agent graph has exactly one Main, which is its only entry. A program graph
has no Main and declares its entries explicitly. Every node must be reachable.
Delegation and review edges must have no directed cycle. Review edges connect
a Planner or Worker to a Reviewer; delegation cannot target Main. Consultation
adds one-hop reachability but does not follow the target's outgoing edges.

For a native program run, coordinator identity is separate from Main identity.
The saved state uses `coordinator_terminal`, `coordinator_argv`,
`coordinator_process`, and `coordinator_pid`; these fields are not aliases for
`main_terminal`, `main_argv`, or `main_process`. Only the recorded coordinator
may advance a program wave. `status`, `stop`, and user answers remain available
as explicit external operations.

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

## Run a Mainless serial program

To derive a Mainless serial variant from the complete example:

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

This variant passes graph validation and can start on a native terminal after
the stated prerequisites are met. The coordinator follows declared TaskSpec
order and dependencies, forms serial integration waves, and advances only
after every writer in the wave has finished, every same-revision Reviewer has
approved, and every declared fixed-argv verification has passed. A successor
wave starts only after that barrier. To run the parallel program variant, use
the same graph with the coordination table below.

## Run a Mainless parallel program

Adapt the serial example above by keeping both Worker-to-Reviewer edges,
TaskSpecs, and routes, then use this coordination table:

```toml
[teams.all-claude.coordination]
mode = "program"
entry_nodes = ["worker-a", "worker-b"]
dispatch_mode = "parallel"
max_active = 2
```

The native coordinator admits independent assignments up to `max_active`. A
candidate is rejected while its node is busy, the cap is full, or its Worker
`allowed_paths` overlap an active Worker scope. A pending user question blocks
its own assignment; an independent candidate may continue when admission still
allows it. `task_verify` remains run-global blocked until every active assignment
and Delivery is drained. State version 5 keeps each assignment's result, question, and
Delivery stage separately. The canonical wave still seals after all writers
finish, reviews the same integrated revision, and runs the declared fixed-argv
verification before the next wave. Focused contract checks and bounded
real-terminal/fake-provider cases cover this path; real-model parallel
acceptance has not been run.

The coordinator uses the existing native supervisor; it does not add a Main
model. Attach to that terminal with `--coordinator` when inspection is needed:

```bash
agent-team attach --config /path/to/config-v5.toml \
  --cwd /workspace/example --team all-claude --coordinator
agent-team status --state /path/to/state.json
```

Native ACP questions use the existing `message_id` path. Answer every message
in the batch before the coordinator acknowledges the Delivery:

```bash
agent-team answer --state /path/to/state.json \
  --message-id ID --body "answer"
```

Reviewer consultation is a separate named-native operation for `agent` and
`program` teams. `status` exposes the opaque consultation ID, task/stage, review
findings, and whether an answer is saved. The ID is bound to the run, TaskSpec digest,
review stage, and exact review Dispatch. The body is limited to 16,000
characters; replaying the same ID and body is idempotent, while a different
body or an old ID is rejected. An answer does not approve the review directly:
the original writer is dispatched again, a new bounded review is required, and
the existing review-round limit remains in force. At the limit, the answer may
be recorded but cannot authorize another dispatch.

```bash
agent-team answer --state /path/to/state.json \
  --consultation-id ID --body "human decision"
```

Plan-only routes, which declare only `plan_writer` and `plan_reviewer`, keep the
plan body digest in `record.revision` and the code snapshot reviewed by the
plan reviewer in `workspace_revision`. These are different values. In a mixed
wave, a plan review that unblocks an implementation writer occurs in the writer
phase; the final plan-only review waits until all writers finish and uses the
same sealed workspace revision as the implementation review. Fixed-argv
verification uses that exact revision. A plan change returns to the exact
Planner and preserves its round limit; it never synthesizes an implementation
writer. The focused contract is tested, but a real-model plan-only run has not
been completed.

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
For the native agent/serial, program/serial, or program/parallel example,
remove `--dry-run` to start after meeting the prerequisites. Agent/parallel
and named Orca starts fail before dependency probes or resource creation.
Inspection does not start providers.

## Use saved identity for management

`NodeRef(node_id, kind)` separates node identity from role kind. Two Workers
can have different model, effort, or prompt settings. No node is selected by
its kind's name or its position in the config.

The runtime team name is
`<team-id>-<workspace-slug>-<sha256(absolute-workspace)[:8]>`.
State is stored at `$XDG_STATE_HOME/agent-team/<derived-team-id>/state.json`,
defaulting to `~/.local/state/agent-team/`. Use `status`, `attach`, or `stop`
with `--state` to manage that saved run without rereading its config.

The config version is 5; named native serial state uses version 4, while native
program/parallel state uses version 5. `role_specs` holds all nodes, while
`roles` holds active assignments and their per-node delivery state. Older
fixed-role state is not converted. See [Architecture](architecture.md) for
identity checks and the question, completion, review, verification, and cleanup
contracts.
