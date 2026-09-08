# Configuration Reference

[日本語](configuration_JA.md) · [README](../README.md) ·
[Architecture](architecture.md)

`agent-team` keeps the existing version-3 fixed-role configuration and also
accepts the explicit version-4 topology configuration. Version-3 `runtime =
"orca"` retains the fixed four-role contract; version-3 `runtime = "tmux"`,
`"herdr"`, or `"zellij"` selects
an experimental native subset with direct Claude Main and optional Claude ACP
Planner, Worker, and Reviewer roles. Native Worker assignments require an
exact TaskSpec from the config's `[[tasks]]` catalog. Missing values and
unsupported combinations fail before any role starts. See
[Version-4 configuration](configuration-v4.md) for the separate topology
schema and pure inspection commands.
For node-local settings, multiple Workers/Reviewers, and explicit TaskSpec
routes, use [Version-5 configuration](configuration-v5.md). Native
`agent`/`serial` and `program`/`serial` execution are connected in version 5;
parallel and named Orca execution remain rejected. The version-3 reference
below retains its fixed Main role and does not express a Mainless program graph.

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

## Select an experimental native terminal runtime explicitly

This section describes a custom version-3 config with
`runtime = "tmux"`, `"herdr"`, or `"zellij"`. For multiple named nodes, use
[Version 5](configuration-v5.md) and its separate execution conditions. In the
version-3 config below, Main is required and
must be direct Claude with `orchestrator` permission. Planner and Reviewer may
be omitted or may each be verified Claude ACP with `read-only` permission and
the pinned `claude-acp-0.70.0` adapter. Worker may be selected as the scoped
Claude ACP `workspace-write` profile; selecting it requires a Reviewer.
Dispatching its work requires a matching `[[tasks]]` entry. Direct Worker/Reviewer and every other native profile are
rejected before state, Task, Dispatch, or process effects are created.
Native startup requires the selected terminal executable and does not require
Orca or Codex. A config that selects an ACP role still requires the documented Node.js minimum and
ACP dependencies described below.

```toml
version = 3
runtime = "tmux"
team_prefix = "native-experiment"
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
provider = "claude"
transport = "acp"
model = "fable"
effort = "high"
prompt = "prompts/worker.md"
permission = "workspace-write"

[roles.reviewer]
provider = "claude"
transport = "acp"
model = "fable"
effort = "high"
prompt = "prompts/reviewer.md"
permission = "read-only"

[[tasks]]
task_id = "addition-workflow"
objective = "Implement add(a, b) in the allowed source file."
acceptance_criteria = ["integer, negative, and decimal addition passes"]
allowed_paths = ["workflow-fixture/calc.py"]
forbidden_paths = [
  "workflow-fixture/protected.txt",
  "workflow-fixture/verify_calc.py",
]
dependencies = []
evidence_requirements = ["changed paths and command results"]
consultation_conditions = []

[[tasks.verification]]
name = "check-addition"
argv = ["python", "-B", "workflow-fixture/verify_calc.py"]
timeout_seconds = 30
```

The example retains the bundled `fable` alias. Check `command -v claude` and
`claude --version` before starting: the real Main/Planner path was verified
with Claude Code 2.1.261, while 2.1.112 was too old for Fable. Native `start`, `status`,
`attach`, and `stop` route to the selected native backend; only Main can be attached. Native
ACP completion is published by the runner and is independent of terminal pane
text. The lifecycle still requires `role_read` → `role_release` →
`delivery_ack`; `native.last_ack` is one receipt marker, not Task or goal
completion.

The role and `[[tasks]]` declarations above are shared by all native runtimes;
change only `runtime` to `"herdr"` or `"zellij"` to select the corresponding
terminal backend. The Main, ACP role permissions, TaskSpec catalog, and review
gates remain the same.

The native Claude ACP roles also use the existing assignment when Claude calls
`AskUserQuestion`. With the private question socket available, the pinned
ACP 0.70.0 adapter sends a form elicitation through SDK 1.3.0. No additional
config field or permission is needed. Main answers the observed question
Delivery through `message_reply`, acknowledges it only after every question is
answered, and the same ACP session then resumes. This communication path does
not widen TaskSpec paths or enable Bash, terminal, or other external tools.

The real tmux question round trip, cooperative pending stop, and retained
earlier failures are recorded in [Architecture](architecture.md). Herdr/Zellij
question support has live-terminal/fake-provider contract coverage; real-model
question acceptance for those runtimes is not claimed.

The earlier model-free tmux CLI start/status/stop path succeeded with Orca
and Codex absent, a workspace path containing spaces, and a deleted config. A
real Claude Code 2.1.261 Main with `fable`/`high` and a logged-in `claude.ai`
account also completed a Claude ACP Planner request through MCP, including
read/release/ack and public stop with independent resource cleanup checks.
The Python environment contained only `dotfiles-agent-team`; Orca, Codex,
OpenCode, Zellij, and Herdr were absent from PATH. On 2026-09-07, a separate
Python 3.13.15 wheel-only run completed six native Planner/Worker/Reviewer
assignments: an intentional `a-b` Worker result was rejected, `a+b` was
approved at the same workspace revision, and trusted fixed-argv verification
succeeded. Public stop after removing the original config and prompts left
zero owned processes and artifacts. The repeat included the
startup TaskSpec catalog, durable PID/PGID/argv gate, and four-file dependency
binding. The catalog remained unchanged throughout the run. This was the earlier tmux-generation proof at
`308b1ba`. The older 2.1.112 rejection was a CLI
version error, not an unavailable `fable` alias.

The new terminal drivers have separate public CLI evidence with fake Main and
fake Node, without a model call. Under Python 3.11 and 3.13, tmux, Herdr, and
Zellij each pass MCP read/release/ack, active cancellation, and natural Main
exit followed by original config/prompt deletion and cold status/stop. PID,
socket, state, config, and private-root absence are checked independently.
Herdr 0.8.2 uses the normal-shell bootstrap without faking `HERDR_ENV`.
The Zellij compatibility version tested at 0.44.1 uses detached mode, no persistent
client, no `--max-panes 1`, and a held Main pane. These are fake-provider tests only.

Separate real Claude Code 2.1.261 workflow runs on Herdr and Zellij used isolated
Python 3.13.15 wheel-only environments with Node 22.23.2, Claude ACP 0.70.0,
SDK 1.3.0, and Claude SDK 0.3.232 selected. Each direct Claude Main used Fable
5.1 at high effort with the Claude Max header and completed six autonomous
Planner/Reviewer/Worker assignments through plan approval, implementation
request-changes, and implementation approval. Trusted fixed-argv verification
returned `FIXED_ARGV_OK`; the catalog, Worker scopes, protected files, and
kernel identities remained consistent. Public stop after deleting the original
config and prompts left no owned PID/PGID, state, socket, or private path, while
normal interactive Main history remained and automated SDK calls used
`persistSession=false`. The 51 runtime package files byte-matched the built
wheel (`655c3bc3c24a278c366cd6282bb2870d10129806f6f312d765329463b47afb7b`);
the selected ACP dependency audit inspected 122 package metadata records.
The dependency inventory confirmed no unselected packages; separate runtime
`PATH` checks confirmed no unselected CLIs, `npm`, `npx`, or `uv`.
Herdr's first attempt pasted the text and Enter together
but left the text in the paste field; a separate Enter then submitted that same
initial message. No additional instructions were sent; typed verification
completed, but its final `NATIVE_WORKFLOW_OK` screen marker was not observed.
Zellij accepted the complete initial submission automatically and its final
marker was observed. The earlier real-model tmux proof remains the run at
`308b1ba`.

A separate real Herdr active-cancel probe dispatched a Worker through public
MCP, observed `CANCEL_STARTED`, rechecked the live kernel PID/PGID and native
result absence immediately before stop, and independently confirmed that no
owned PID/PGID, process reference, or path remained. This is representative
Claude ACP cancellation evidence for Herdr, not all-harness coverage. It
verified OS group/path cleanup only and did not prove an explicit ACP session
close; the earlier process-group-based cleanup promotion has the same limitation.

## Top-level fields define one team contract

| Field | Contract |
|---|---|
| `version` | Must be integer `3`. No automatic migration is performed. |
| `runtime` | Must be `"orca"`, `"tmux"`, `"herdr"`, or `"zellij"`. `orca` uses four roles; each native runtime uses the shared NativeBackend and its selected terminal driver. |
| `team_prefix` | Must match `[a-z][a-z0-9-]{0,23}`. It contributes to the runtime team ID. |
| `max_review_rounds` | Positive integer. Counts the first Reviewer decision and every retry for one stage. |
| `main` | Required Main role table. |
| `roles` | `orca` must contain exactly `planner`, `worker`, and `reviewer`; each native runtime may contain optional `planner`, `worker`, and `reviewer`. Main is declared separately and is always required. A native Worker requires a Reviewer. |
| `tasks` | Native runtimes only: optional `[[tasks]]` TaskSpec catalog with `[[tasks.verification]]` entries. Orca rejects this field. Without it, read-only `role_prompt` remains available but structured `task_dispatch` is rejected. |

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

The native terminal capability matrix is smaller:

| Role | Allowed provider / transport | Required permission |
|---|---|---|
| Main | Claude / `direct` | `orchestrator` |
| Planner | Claude / `acp` (optional) | `read-only` |
| Worker | Claude / `acp` (optional, scoped) | `workspace-write` |
| Reviewer | Claude / `acp` (optional) | `read-only` |

Direct Worker/Reviewer, Codex ACP, Main ACP, workspace-write ACP outside the
scoped Claude profile, and all other native provider profiles fail before
startup effects.

## ACP dependencies are explicit and selected-only

An Orca config that selects Claude `acp` requires Node.js `22.13.0` or newer
and the exact packages `acpx@0.13.2` and
`@agentclientprotocol/claude-agent-acp@0.70.0`. Install them explicitly
outside `agent-team`, for example:

```bash
npm install --prefix /path/to/agent-team-acp acpx@0.13.2 @agentclientprotocol/claude-agent-acp@0.70.0
export PATH="/path/to/agent-team-acp/node_modules/.bin:$PATH"
```

For Orca, startup resolves `node`, `acpx`, and
`claude-agent-acp`, checks the exact package manifests, and saves absolute file
paths with SHA-256 fingerprints in the launch snapshot. The runner verifies and
uses that saved binding. Missing or changed files fail closed. Runtime commands
never invoke `npm` or `npx`; a direct-only config does not resolve ACP
dependencies.

Native runtimes use a separate binding. They resolve Node.js `22.0.0` or newer,
the installed `@agentclientprotocol/claude-agent-acp@0.70.0` command, and its
dependency `@agentclientprotocol/sdk@1.3.0`. It also binds the actual
`dist/lib.js` import, saving absolute paths and SHA-256 fingerprints for all four
files, then uses one direct public ACP SDK
connection per assignment. Native does not select `acpx` and does not invoke
`npm` or `npx` at runtime. Normal SDK persistence is disabled with
`persistSession=false` and `autoMemoryEnabled=false`; interactive Main history
remains in the normal Claude store. This is a direct SDK connection, not the
provider's direct/model transport. When the selected native role is Claude,
`AskUserQuestion` is enabled only with its owned private `q.sock`; the Node
client supports form elicitation and the Python channel records the durable
answer/receipt sequence. Codex has no question socket, and its public ACP
profile remains disabled.

The terminal behavior was verified with Herdr `0.8.2` and Zellij `0.44.1`.
Herdr's handshake requires exact version `0.8.2` and protocol 20. Zellij's
preflight checks the executable and known inventory, not an exact CLI version;
the tested compatibility version uses a detached session with no persistent
client and no `--max-panes 1`.

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
- Orca Worker must use `workspace-write` and direct Codex.
- Native Worker must use the scoped Claude ACP `workspace-write` profile.

For direct Codex, agent-team creates an isolated `CODEX_HOME` and derives a
profile from `:read-only` or `:workspace`. For read-only Claude ACP, the client
limits tools to `Read`, `Grep`, and `Glob`, approves reads, and fails when a
non-interactive permission question cannot be resolved. Native runtimes' scoped
Worker can read the workspace with Read/Grep/Glob, subject to protected-path
and link/file-type checks. TaskSpec `allowed_paths` and `forbidden_paths` apply
to Write/Edit only, with forbidden paths taking precedence. It denies Bash,
terminal, and other RPC operations. Its Planner and Reviewer are read-only.
All native ACP roles run as launcher-owned background processes.

Questions are an additional communication channel, not a permission or file
scope change. Only native Claude ACP assignments with the private socket
advertised in their launch snapshot enable `AskUserQuestion`; Codex question
handling remains disabled. Main must answer every question before
`delivery_ack`. While that Delivery is pending, `role_read`, `role_release`,
another dispatch, and `task_verify` are rejected. A successful completion is
also rejected until the same ACP assignment consumes the answers.

## TaskSpec catalog is optional; required for native task dispatch

Native task dispatch is catalog-driven. Declare one `[[tasks]]` table per
task and one or more `[[tasks.verification]]` tables for its fixed argv:

```toml
[[tasks]]
task_id = "addition-workflow"
objective = "Implement add(a, b) in the allowed source file."
acceptance_criteria = ["integer, negative, and decimal addition passes"]
allowed_paths = ["workflow-fixture/calc.py"]
forbidden_paths = [
  "workflow-fixture/protected.txt",
  "workflow-fixture/verify_calc.py",
]
dependencies = []
evidence_requirements = ["changed paths and command results"]
consultation_conditions = []

[[tasks.verification]]
name = "check-addition"
argv = ["python", "-B", "workflow-fixture/verify_calc.py"]
timeout_seconds = 30
```

The TaskSpec parser accepts exactly these fields: `task_id`, `objective`,
`acceptance_criteria`, `allowed_paths`, `forbidden_paths`, `dependencies`,
`verification`, `evidence_requirements`, and `consultation_conditions`.
Verification entries contain `name`, `argv`, and `timeout_seconds` from 1 to
900. Paths are workspace-relative POSIX paths; a `forbidden_paths` match wins
over an allowed match. Startup rejects duplicate task IDs, undeclared
dependencies, and dependency cycles before state or provider effects.

Main receives the catalog in its native startup instructions. `task_dispatch`
must exactly match one declared TaskSpec. Main cannot add a task ID or change
its path scope, dependencies, evidence requirements, or verification argv at
dispatch time. If a native config has no `[[tasks]]`, read-only `role_prompt`
remains available, but structured task dispatch is rejected. The `tasks` field
is rejected by the Orca config loader.

The native task lifecycle uses the ten public tools:

1. `task_dispatch` assigns the declared TaskSpec to Planner, Worker, or Reviewer.
2. `role_wait`, `role_read`, `role_release`, and `delivery_ack` consume the result in that order.
3. `task_get` returns the durable stage, review evidence, and verification evidence.
4. A Planner or Worker result moves to `awaiting_plan_review` or `awaiting_implementation_review`.
5. Reviewer output is exact JSON with `task_id`, `stage`, `revision`, `decision`, and `findings`.
6. `approve` advances the stage; `request_changes` returns to the original writer; `consult` records a bounded user consultation for a named native graph.
7. After implementation approval, `task_verify` runs every declared fixed argv command.

If `role_wait` returns a native `question`, Main calls `message_reply` once for
each event `message_id` and then `delivery_ack`. The backend persists the
answers before sending them through the assignment's private socket. The
client returns a `received` receipt, Python records its hash-only receipt while
retaining the protected outbox, and only then does it send `recorded` and allow
the same ACP session to continue.
Retrying the same message ID with the same body is idempotent; a different body
is rejected. A batch has one to four questions, each question or answer is at
most 20,000 characters, each frame is at most 512 KiB, and an assignment may
receive at most 64 batches. Consumed receipts retain identities and hashes,
while the protected outbox may retain raw question and answer text until the
next question or terminal completion so publication failures can be recovered.
A native ACP question is separate from Reviewer `consult`. For a named native
version-5 `agent` or `program` team, `status` exposes the opaque consultation
ID, findings, task/stage, and answer state, and
`answer --consultation-id ID --body ...` stores the bounded human answer. The
same ID and body may be replayed idempotently; a replacement or stale ID is
rejected. If review rounds remain, the original writer must run again before
another review. At the limit, the answer is saved but redispatch stays blocked.
The answer is not an implicit approval and does not reset review-round limits. ACP questions continue to use
`answer --message-id ID --body ...` and the program coordinator acknowledges
only after every question in the batch is answered.

Plan and implementation review rounds are counted separately and both obey
`max_review_rounds`. Implementation review captures the workspace revision when
the Reviewer assignment is prepared. `task_verify` requires that same revision,
no active role or Delivery, and confirmed cleanup. It runs with `shell=False`,
checks the revision before and after commands, and stores bounded errors plus
stdout/stderr SHA-256 hashes. Only when all commands pass and cleanup is confirmed
can the task become `completed`; a Reviewer approval alone is not completion.

If verification is interrupted or cleanup is unconfirmed, the saved task
remains `verifying` or `verification_failed` with the available evidence and
blocks the next role, verification, or stop as required. There is no automatic
recovery claim. A `verification_failed` record with valid executed-command evidence and
confirmed cleanup may return to Worker within the implementation review-round
limit; unconfirmed cleanup requires user consultation.
Revision drift or a clean interruption may leave only an executed prefix,
including no commands if interrupted before execution. The prefix must match
the declaration; it permits repair but cannot establish successful completion.

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
the saved `runtime`; `attach` supports Main only for native runtimes.

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
file or copy fields between state versions. Existing native state from an older
native-terminal contract that lacks the frozen `supervisor_argv` and complete
Main process receipt is retained and fails closed; it is not reconstructed or
migrated automatically. Stop it with the matching executable/version before
upgrading.
