# Agent Team

[日本語](README_JA.md)

`agent-team` starts a project-scoped team on an explicitly selected runtime
without changing ordinary `claude` or `codex` sessions. The bundled
`runtime = "orca"` path provides the Planner → Worker → Reviewer workflow, with
Orca owning Task, message, terminal, and lifecycle coordination. The experimental
Native runtimes `tmux`, `herdr`, and `zellij` provide the same direct Claude
Main plus optional Claude ACP Planner, Worker, and Reviewer roles. Native
Worker assignments require a config-declared TaskSpec and use the scoped Claude
ACP policy; only the terminal driver changes.

Read the install, prerequisite, and start sections to launch a team. Use the
linked reference documents when changing the implementation or configuration.

## Read this first

- [Quick start](#start-a-team) explains the normal workflow.
- [Architecture](docs/architecture.md) explains the runtime and safety
  boundaries.
- [Configuration](docs/configuration.md) lists the version-3 schema, native
  TaskSpec catalog, and supported provider/transport combinations.
  [Version-4 configuration](docs/configuration-v4.md)
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

In the bundled Orca configuration, only Main starts immediately. Planner,
Worker, and Reviewer start on demand, and only one background role may be active
at a time.

The bundled configuration remains the four-role Orca configuration above. A
custom native configuration must select direct Claude Main with `orchestrator`
permission and may include verified Claude ACP Planner/Reviewer roles with
`read-only` permission and a scoped Claude ACP Worker with `workspace-write`
permission. Dispatching a native Worker requires a matching `[[tasks]]` entry;
other unsupported native profiles are rejected before state, Task, Dispatch, or
process effects are created.

## Run from a checkout or install the project

The project has no third-party Python dependency. Python 3.11 or newer is
required. From a checkout, the launcher is directly executable:

The Orca lifecycle backend, native terminal backends, and bounded provider
runner are POSIX-only. They fail fast on Windows because their runtime metadata
contracts require Unix sockets or private process-group semantics.
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

The current implementation has been smoke-tested against a live Orca runtime
on macOS. The model-free native tmux CLI start/status/stop path also succeeded
with Orca and Codex absent, a workspace path containing spaces, and a deleted
config file. A separate real-tmux test with disposable provider fixtures passed the
public MCP read/release/ack cycle and active cancellation from another CLI
process, including independent cleanup checks.

On 2026-09-06, a real Claude Code 2.1.261 Main with `fable`/`high` completed a
Claude ACP Planner request through MCP: prompt, wait, read, release, then ack.
The Planner read the requested file successfully. Public stop removed the owned
processes, state, socket, prompts, and private directories. The isolated Python
environment contained only this package; Orca, Codex, OpenCode, Zellij, and Herdr
were absent from PATH. This verifies only the earlier read-only path; it is
separate from the native TaskSpec write/review workflow described below.

The earlier rejection came from selecting an old Nix-provided Claude Code
2.1.112. Requesting `claude-fable-5-1` directly exposed the
`claude_code_version_too_old` error; the already-installed 2.1.261 executable
accepted the same `fable` alias. Check the executable and version selected by
PATH; changing the model is unnecessary for this failure. See the official
[Claude model configuration](https://code.claude.com/docs/en/model-config) for
current version requirements. The Linux executable mapping is implemented as
`orca-ide`, but this checkout has not had a live Linux Orca smoke test. Windows
is unsupported and fails fast. Orca executable selection is exact by platform,
with no PATH fallback or environment override:

- macOS: `orca`
- Linux: `orca-ide`

The same version check matters for Codex. In separate direct Worker and Reviewer
probes,
Codex 0.152.1 rejected `gpt-6-astra` with a newer-client requirement, while the
already-installed 0.153.4 CLI ran both configured Astra roles.
The Worker created the requested file, the Reviewer read it, and an independent
`:read-only` sandbox probe denied a write. This does not verify the unfinished
team review/verification workflow or Orca cleanup.

On 2026-09-07, an isolated Python 3.13.15 wheel-only environment ran a real
Claude Code 2.1.261 Main with `fable`/`high` and native Claude ACP
Planner/Worker/Reviewer assignments. The bounded run completed six assignments
(Planner 1, plan Reviewer 1, Worker 2, implementation Reviewer 2): an
intentional `a-b` implementation was rejected, `a+b` was approved at the same
workspace revision, and trusted fixed-argv verification succeeded. Public
`stop` after the original config and prompts were removed left zero owned
processes and artifacts. This repeat used the startup TaskSpec catalog,
PID/PGID/argv gate, and four-file dependency binding. Its dedicated npm install
contained only the selected Claude ACP packages and their dependencies, with no
acpx or other harness packages. The startup catalog stayed unchanged, and Main
reported `NATIVE_WORKFLOW_OK`.
This was the earlier tmux-generation proof at `308b1ba`.

A separate live SDK probe edited an allowed file and denied a forbidden path,
with `persistSession=false`, `autoMemoryEnabled=false`, no residual selected
SDK process, and no Claude project directory. A separate active-cancel probe
stopped an active native Worker with the same final runtime and found zero owned processes
and artifacts. These are bounded native checks, not evidence for every runtime,
harness, or recovery path.

The new terminal drivers have separate public CLI evidence with a fake Main and
fake Node, without a model call. Under Python 3.11 and 3.13, each of tmux,
Herdr, and Zellij passed three cases: MCP read/release/ack, active cancellation,
and natural Main exit followed by deletion of the original config/prompts and
cold status/stop. Independent checks found no owned PID, socket, state, config,
or private root. This proves the fake-provider terminal contract only.

Separate real Claude Code 2.1.261 workflow runs completed on both Herdr and
Zellij in isolated Python 3.13.15 wheel-only environments with Node 22.23.2,
Claude ACP 0.70.0, SDK 1.3.0, and Claude SDK 0.3.232 selected. Each direct
Claude Main used Fable 5.1 at high effort with the Claude Max header and
completed six autonomous Planner/Reviewer/Worker assignments: plan approval,
implementation request-changes, then implementation approval. The approved
workspace revision passed trusted fixed-argv verification with `FIXED_ARGV_OK`;
TaskSpec catalogs, Worker scopes, protected files, and captured kernel
identities remained consistent. Public stop after deleting the original config
and prompts left no owned PID/PGID, state, socket, or private path; normal
interactive Main history remained, while automated SDK calls used
`persistSession=false`.
The 51 runtime package files byte-matched the built wheel
(`655c3bc3c24a278c366cd6282bb2870d10129806f6f312d765329463b47afb7b`); the
selected ACP dependency audit inspected 122 package metadata records. The
dependency inventory confirmed no unselected packages; separate runtime `PATH`
checks confirmed no unselected CLIs, `npm`, `npx`, or `uv`.

Herdr's first attempt pasted the text and Enter together but left the text in
the paste field; a separate Enter then submitted that same initial message.
No additional instructions were sent, and all six assignments were autonomous.
Its typed verification completed, although the final
`NATIVE_WORKFLOW_OK` screen marker was not observed before stop. Zellij accepted
the complete initial submission automatically and its final marker was observed.
The earlier real-model workflow proof remains the tmux run at `308b1ba`; these
bounded Herdr/Zellij runs do not establish coverage for every harness or
recovery path.

A separate real Herdr active-cancel probe dispatched a Worker through the public
MCP, observed `CANCEL_STARTED`, and rechecked the same live kernel PID/PGID plus
native-result absence immediately before public stop. Independent readback then
found no owned PID/PGID, process reference, or path. This is representative
Claude ACP cancellation evidence for Herdr, not proof for all harnesses.

In the earlier tmux proof at `308b1ba`, the wheel-only setup rejected each
missing required native command (`node`, `claude-agent-acp`, `tmux`, or
`claude`) before state creation. This is preflight evidence for that older tmux
generation, not a claim that the Herdr/Zellij workflow runs omitted those tools.

Before starting a team:

1. Make sure the selected runtime and harness commands are available. The bundled
   team uses the Orca executable above, `claude`, `codex`, and the ACP tools below;
   a native configuration also requires its selected terminal executable:
   `tmux`, `herdr`, or `zellij`.
2. For `runtime = "orca"`, open Orca and confirm that the platform-specific
   `status --json` command reports a ready runtime and graph. For `runtime =
   "tmux"`, `"herdr"`, or `"zellij"`, confirm that the selected terminal is available; Orca is not required.
3. Log in to the selected providers with the accounts you intend to use. The
   bundled Orca roles require both Claude and Codex; native runtimes require Claude.
4. For `runtime = "orca"`, register the target repository with Orca once.

Native runtimes currently use the standard Claude login under the normal home
directory. They resolve the Claude executable directly and do not inherit a
`claude-account` default profile or forward `CLAUDE_CONFIG_DIR`. Confirm that
this standard login with `env -u CLAUDE_CONFIG_DIR claude auth status` before
starting a native team. Named account profiles are not supported by the native
runtime yet.

```bash
# Providers for the bundled Orca configuration
command -v claude
claude --version
claude auth status
command -v codex
codex --version
codex login status
# Orca runtime on macOS
orca status --json
orca repo add --path "$PWD"
# Orca runtime on Linux
orca-ide status --json
orca-ide repo add --path "$PWD"
# Native tmux runtime
tmux -V
# Native Herdr runtime
herdr --version
# Native Zellij runtime
zellij --version
```

An Orca config that selects an ACP role requires Node.js 22.13 or later and the
installed commands from `acpx@0.13.2` and
`@agentclientprotocol/claude-agent-acp@0.70.0`. Install the selected tools
explicitly, for example into a directory you choose:

```bash
npm install --prefix /path/to/agent-team-acp acpx@0.13.2 @agentclientprotocol/claude-agent-acp@0.70.0
export PATH="/path/to/agent-team-acp/node_modules/.bin:$PATH"
```

Startup records the resolved program paths and fingerprints. Execution uses
those programs directly and does not run `npm` or `npx`. A missing or changed
dependency is an error. Teams using only direct transport do not require the
ACP tools.

A native tmux, Herdr, or Zellij ACP role uses Node.js 22.0.0 or later, the installed
`@agentclientprotocol/claude-agent-acp@0.70.0` command, and its dependency
`@agentclientprotocol/sdk@1.3.0`. Native records absolute paths and SHA-256
fingerprints for Node, the Claude ACP entrypoint and `dist/lib.js`, and the SDK, then opens one
direct public ACP SDK connection per assignment. Native does not select or
invoke `acpx`; its normal SDK persistence is disabled with
`persistSession=false` and `autoMemoryEnabled=false`. This is a direct SDK
connection, not the provider's direct/model transport. Interactive Main history
remains in the normal Claude store.

Install the native package outside `agent-team`, for example:

```bash
npm install --prefix /path/to/agent-team-native \
  @agentclientprotocol/claude-agent-acp@0.70.0
export PATH="/path/to/agent-team-native/node_modules/.bin:$PATH"
```

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

Start Main and focus its managed terminal:

```bash
agent-team start
```

Use `--no-attach` when the terminal should remain in the background:

```bash
agent-team start --no-attach
```

Ask Main for the development task. In the bundled Orca configuration, Main
decides whether to run Planner first, then dispatches Worker and Reviewer
through the `agent_team` MCP server. In a native runtime, Main can request only
the configured Claude ACP Planner, Worker, and Reviewer roles. A native Worker
must be dispatched with a complete TaskSpec that exactly matches a `[[tasks]]`
entry in the selected native config. Main is the only role that talks to the user.

For named teams, use the bundled catalog or the synced `teams.toml`:

```bash
agent-team start --config ~/.config/agent-team/teams.toml --team agent-team --dry-run
```

See [Version-4 configuration](docs/configuration-v4.md#launch-a-named-team)
to register more teams, inspect their graphs, and start a selected team.

## Operate and stop a team

```bash
# Inspect the selected runtime, Main terminal, and active-role accounting.
agent-team status

# Focus Main.
agent-team attach main

# Orca only: focus a background role after Main has dispatched it.
agent-team attach worker

# Stop this team's owned terminals and remove its runtime state.
agent-team stop
```

`stop` routes to the selected runtime's backend and removes only its owned
resources. The Orca path keeps the Run as an audit record. It does not commit,
push, publish, or delete project files.

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

## Use the native TaskSpec workflow

Native runtime task specifications are declared by the user in the selected
version-3 config. Each `[[tasks]]` entry is immutable for that run; its
`[[tasks.verification]]` entries provide the fixed argv commands used after
implementation approval:

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

Main can call `task_dispatch` only with a TaskSpec that exactly matches one
declared entry. A new task ID, path, dependency, or verification argv cannot
be invented at dispatch time. Startup rejects duplicate IDs, undeclared
dependencies, and dependency cycles before state or provider effects. If a
native config has no `[[tasks]]`, read-only `role_prompt` remains available but
structured task dispatch is rejected; Orca rejects the `tasks` field.

The practical order is:

```text
task_dispatch(Planner or Worker)
  -> role_wait -> role_read -> role_release -> delivery_ack
  -> task_get
  -> task_dispatch(Reviewer) for plan or implementation review
  -> task_dispatch(Planner or Worker) after request_changes
  -> task_verify after implementation approve
```

Plan and implementation reviews use separate `max_review_rounds` counters.
Reviewer output is one exact JSON object with `task_id`, `stage`, `revision`,
`decision`, and `findings`. Implementation review and `task_verify` use the
same workspace revision. `completed` is reported only after every declared
fixed argv command passes and cleanup is confirmed. See
[Configuration](docs/configuration.md#taskspec-catalog-is-optional-required-for-native-task-dispatch) for
the complete field contract and all ten tools.
If verification fails with complete evidence and confirmed cleanup, Worker may
be retried within the implementation review-round limit. An unconfirmed
cleanup result requires user consultation and remains retained.

## Know the safety boundary

- Unsupported runtime, provider, transport, permission, config version, or state
  format fails before launch. The launcher never silently switches backends or
  transports.
- Orca keeps its fixed four-role contract. Native runtimes require Main and allow
  optional verified Claude ACP Planner/Reviewer roles plus a scoped Claude ACP
  Worker. A native Worker `task_dispatch` requires a matching config-declared TaskSpec;
  unsupported native profiles are rejected before startup effects.
- Native `start`, `status`, `attach`, and `stop` use the shared `NativeBackend`
  with the selected `TmuxBackend`, `HerdrBackend`, or `ZellijBackend`; attach
  is valid only for Main. `native_main` supervises the owned Main process group.
- Native ACP completion comes from `publish_completion`, not terminal pane text.
  The lifecycle order remains `role_read` → `role_release` → `delivery_ack`.
  Native `last_ack` stores one receipt marker and does not mean that a Task or
  the user's overall goal is complete.
- Native Worker Read/Glob/Grep can read the workspace, subject to protected-path
  and link/file-type checks. TaskSpec `allowed_paths` and `forbidden_paths`
  restrict Write/Edit only; forbidden paths take precedence for writes.
  Bash, terminal, and other RPC operations are denied. This is an in-band
  model/tool boundary, not an operating-system sandbox, and it does not cover
  a hostile same-user process swapping files concurrently.
- ACP permission mediation is not an operating-system sandbox. The bundled
  Orca write-capable role remains direct Codex with its isolated permission
  profile; native write is limited to the scoped Worker profile above.
- Agent output is untrusted data. Matching Task, Dispatch, terminal, sender,
  and Delivery identities decide lifecycle state.
- Native task completion requires `task_get` to report `completed`: Reviewer
  approval and `native.last_ack` alone do not complete a Task. Implementation
  review and `task_verify` remain bound to the same workspace revision.
- Workspace revision rejects symlinks and special files and is limited to 5,000
  files, 10 MB per file, and 100 MB total. It does not cover arbitrary repos.
- An interrupted verification or unconfirmed cleanup retains `verifying` or
  another fail-closed state and blocks stop/new roles as required. Automatic
  recovery is not claimed.
- Herdr 0.8.2 uses a private headless server and a normal-shell bootstrap; the
  runtime never fakes `HERDR_ENV`. Natural Main exit may remove Herdr's pane and
  workspace, but terminal absence alone is not stop proof: owned server/socket
  identity and trusted Main process cleanup are still required.
- Zellij was tested for compatibility at 0.44.1 and uses a detached session with no persistent client and no
  `--max-panes 1`. The held Main pane and the expected suppressed `zellij:link`
  plugin are checked from JSON plus process identity; unknown panes/plugins
  remain unproven. The same NativeBackend owns cleanup.
- Existing native state without the frozen `supervisor_argv` and complete Main
  process receipt is intentionally not reconstructed or migrated. Stop such a
  run with the matching executable/version before upgrading.
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
| `role has no active Orca Dispatch` | In the Orca runtime, Main has not started that background role, or it has already been released. |
| `native Worker requires task_dispatch with a TaskSpec` | Use a complete TaskSpec that exactly matches a `[[tasks]]` entry in the selected native config. |
| `native role is not a Claude ACP role` | The selected native runtime accepts only configured Claude ACP Planner/Worker/Reviewer roles. |
| Authentication is required | Run `claude auth status` or `codex login status` outside agent-team. |
| ACP dependency check fails | Orca uses acpx; native uses Claude ACP 0.70.0, its `dist/lib.js`, and SDK 1.3.0. Include the selected `node_modules/.bin` directory and Node >=22.0.0 in `PATH`. |
| Native terminal driver reports `unknown` | Preserve state and inspect the selected driver receipt. Do not treat pane/session absence as cleanup proof. |
| Existing native state lacks `supervisor_argv` | Stop it with the matching executable/version before upgrading; state is not reconstructed or migrated. |
| `approved workspace revision changed` | Re-dispatch Worker, review the new revision, and do not bypass the gate. |
| `verification cleanup is unconfirmed` | Keep the state and inspect process/cleanup evidence. Do not delete state to force a restart. |
| A role reports `escalation` | Inspect the retained terminal and Run; do not treat escalation as completion. |

## Prepare a useful failure report

When this guide does not resolve a failure, report it to the repository
maintainer with the command, config path, workspace, selected runtime, and the
smallest relevant error. Include Orca version and Run/Task/Dispatch IDs when
the Orca runtime is selected; include the native run/assignment IDs when a
native runtime is selected. Do not include authentication tokens, prompt contents, or
unrelated terminal output.

Useful terms:

- **Run**: the runtime identity for one team execution; Orca also provides its
  coordinator namespace and inbox.
- **Task**: one bounded Planner, Worker, or Reviewer assignment.
- **TaskSpec**: the user-declared immutable task policy, including path scope,
  dependencies, evidence requirements, and fixed verification argv.
- **Dispatch**: one attempt that binds a Task to a terminal.
- **Delivery**: a message batch that Main must process and acknowledge.
- **direct**: the provider's normal interactive CLI.
- **ACP**: Agent Client Protocol, using pinned acpx on Orca and the pinned public ACP SDK on native runtimes.

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

When changing Orca, a native terminal backend, or ACP integration, repeat a real bounded
smoke test and confirm that the selected runtime's terminals, state, prompt
files, sessions, and adapter processes are gone after `stop`.

To verify the tmux terminal driver on a machine with tmux installed, explicitly
run this test from the repository root:

```bash
uv run --locked --project scripts/agent-team python scripts/agent-team/tests/live_tmux.py
```

It creates a private tmux server, checks literal arguments and process exit
status, and reclaims its own resources. Missing tmux is an error in this test.
The default suite exercises the driver contract without requiring tmux. This
live test covers terminal operations; it does not prove the full team workflow.

To exercise the native CLI/MCP lifecycle and active cancellation with disposable
provider fixtures, run:

```bash
AGENT_TEAM_RUN_LIVE_NATIVE=1 uv run --locked --project scripts/agent-team python -m unittest scripts/agent-team/tests/live_native_contract.py -v
```

This test uses the selected native terminal with fake Claude, Node, and ACP
adapter commands. It excludes the other backends and harnesses from PATH,
verifies read → release → ack, active cancellation, and natural Main exit with
cold status/stop. It checks process, socket, configuration, prompt, state, and
private-root cleanup. It does not call a model or prove the real-model workflow.

Select the native terminal explicitly:

```bash
for runtime in tmux herdr zellij; do
  AGENT_TEAM_RUN_LIVE_NATIVE=1 AGENT_TEAM_LIVE_RUNTIME="$runtime" \
    uv run --locked --project scripts/agent-team python -m unittest \
    scripts/agent-team/tests/live_native_contract.py -v
done
```

The separate driver tests use their pinned tools directly:

```bash
AGENT_TEAM_RUN_LIVE_HERDR=1 uv run --locked --project scripts/agent-team \
  python -m unittest scripts/agent-team/tests/live_herdr.py -v
AGENT_TEAM_RUN_LIVE_ZELLIJ=1 uv run --locked --project scripts/agent-team \
  python -m unittest scripts/agent-team/tests/live_zellij.py -v
```

The native driver checks are fake-provider terminal evidence; the bounded
real-model Herdr/Zellij workflow and Herdr cancellation evidence is described
above. Do not treat an absent pane or session alone as cleanup success.

The public SDK client contract tests use fake agents. To run every case, provide
SDK `1.3.0` for Claude and SDK `1.4.0` for Codex. This test-only setup uses the
same package alias as CI:

```bash
npm install --prefix /path/to/agent-team-sdk-test --ignore-scripts --no-audit --no-fund \
  @agentclientprotocol/sdk@1.3.0 codex-acp-sdk@npm:@agentclientprotocol/sdk@1.4.0
AGENT_TEAM_SDK_ENTRY=/path/to/agent-team-sdk-test/node_modules/@agentclientprotocol/sdk/dist/acp.js \
AGENT_TEAM_CODEX_SDK_ENTRY=/path/to/agent-team-sdk-test/node_modules/codex-acp-sdk/dist/acp.js \
  uv run --locked --project scripts/agent-team python -m unittest \
  scripts/agent-team/tests/test_scoped_acp_client.py -v
```

There is no personal-path default. The client suite skips when Node or the Claude
SDK entry is absent; once enabled, it fails if the Codex SDK entry is missing.
These fixtures do not establish real-model support for the disabled Codex ACP profile.
