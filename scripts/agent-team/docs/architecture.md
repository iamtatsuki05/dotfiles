# Architecture

[日本語](architecture_JA.md) · [README](../README.md) ·
[Configuration](configuration.md)

## Orca is the only orchestration backend

`agent-team` separates orchestration from agent execution. Orca owns the Run,
Tasks, Dispatches, messages, and terminals. The launcher owns role-specific
arguments, private runtime state, and the bridge between ACP completion and an
Orca `worker_done` message.

```mermaid
flowchart TD
    User --> Main[Canonical Main: direct Claude]
    Main --> MCP[agent_team MCP server]
    MCP --> Run[Orca Run]
    Run --> Planner[Planner: Claude through ACP]
    Run --> Worker[Worker: direct Codex]
    Run --> Reviewer[Reviewer: direct Codex]
    Planner --> Done[worker_done / question / escalation]
    Worker --> Done
    Reviewer --> Done
    Done --> Main
```

Herdr and Zellij can host an outer terminal, but they are not agent-team
orchestration backends. Giving two systems ownership of the same worker would
make completion and cleanup ambiguous.

The repository also contains an isolated tmux terminal driver and a live driver
test. It is not wired into team launch or orchestration; passing that test
covers terminal operations only and does not prove the full team workflow.

## Components have narrow responsibilities

| Component | Responsibility |
|---|---|
| `config.toml` | Declares fixed roles, providers, transports, models, efforts, prompts, and permissions. |
| `agent_team/config_v4.py`, `topology.py` | Validate named team catalogs and render their graphs. Runnable catalog entries explicitly reference a matching version-3 launch configuration. |
| `agent_team/cli.py` | Parses and validates config/arguments, composes `WorkflowEngine(OrcaBackend)`, renders compatibility JSON, and runs ACP turns. |
| `agent_team/backend.py` | Owns the CLI `start`/`status`/`attach`/`stop` workflow adapter, state-v3 identity checks, and compatibility receipts. |
| `agent_team/orca.py` | Owns the fixed Orca argv/envelope decoder. It does not own MCP role operations. |
| `agent_team/locking.py` | Owns the stable per-team lifecycle reservation, shared by state writes and runtime operations without importing a backend. |
| `agent_team/cleanup.py` | Owns the private stop journal, startup-recovery sidecar, and exact local cleanup/rollback phases. |
| `agent_team/mcp_server.py` | Exposes seven fixed Main-facing tools, maps role operations to Orca, and holds the shared lifecycle reservation from state load through remote effect and save/rollback. It is launched through the same `agent-team _mcp-server` entrypoint. |
| `agent_team/runtime.py` | Shares identity, private-file, state-v3, command, environment, and cleanup safety helpers; state writes take the shared reservation unless the caller already holds it. |
| `agent_team/registry.py` | Records recognized harnesses and exact verified role profiles; it never falls through to another provider. |
| `agent_team/adapters.py` | Provides the provider-independent background seam, bounded process runner, exact identity checks, and Copilot/OpenCode read-only adapters. It has no Orca lifecycle authority. |
| `agent_team/acp_dependencies.py` | Resolves selected ACP dependencies, verifies exact package manifests, and records absolute executable paths with SHA-256 fingerprints. |
| `agent_team/defaults/` | Bundled config and Japanese prompts used when no user config is selected. |
| `prompts/*.md` | Defines the Japanese role contracts. |
| Orca | Stores the Run/Task/Dispatch lifecycle and owns managed terminals. |
| Node.js, `acpx`, `claude-agent-acp` | Run the pinned Claude ACP adapter through the saved executable binding and return final text plus an exit status. |

Copilot read-only Planner and Reviewer profiles run through the common Orca
lifecycle and state-v3 snapshot integration. The OpenCode provider adapter is
also implemented, but remains rejected until its profile-specific boundary and
lifecycle are verified live. A background profile runs one fixed provider
invocation against a fresh read snapshot rather than a TUI terminal or ACP
session. The snapshot excludes `.git`, symlinks, special files, ignored files,
secret-like paths, provider configuration, and agent instructions.

## Canonical Main is direct Claude and the only user-facing agent

In the canonical config, Main starts as a direct Claude process with the
`agent_team` MCP server and no Bash tool. A custom config may select direct
Codex Main; it keeps the same fixed MCP surface but uses Codex-specific launch
and permission settings. In either case, Main is the only user-facing role.

The bundled defaults use `fable` for Main and Planner and `gpt-6-astra` for
Worker and Reviewer. The role graph does not change those launch-config model
choices.

The MCP server exposes only:

- `role_get`
- `role_prompt`
- `role_wait`
- `role_read`
- `role_release`
- `delivery_ack`
- `message_reply`

Main cannot choose an arbitrary command or role name through this MCP surface.
The fixed surface keeps agent output separate from process-control authority.

## Direct roles use Orca-supervised terminals

Worker and Reviewer currently use direct Codex.

1. The MCP bridge creates an Orca Task.
2. It starts a launcher-owned Codex terminal with an isolated `CODEX_HOME`.
3. It waits for the TUI and configured model/effort to become ready.
4. `worker-start` binds the terminal to the Task and creates a Dispatch.
5. Orca injects the task and lifecycle commands.
6. The role reports `worker_done`, `question`, or `escalation`.

Codex roles inherit either the built-in `:workspace` or `:read-only` profile.
Only the current Orca Unix socket is added to the profile; no external domain
is allowed by agent-team.

## ACP roles use a bare Dispatch and a trusted runner

Planner currently uses Claude through ACP. acpx is not an Orca-recognized TUI,
so agent-team uses a bare terminal without pretending that it is a supervised
native agent.

Before creating the Orca Run, ACP startup requires Node.js `22.13.0` or newer
and the exact `acpx@0.13.2` and
`@agentclientprotocol/claude-agent-acp@0.70.0` packages. It resolves only the
selected ACP roles' `node`, `acpx`, and `claude-agent-acp` files, verifies their
package manifests, and stores absolute paths with SHA-256 fingerprints. The
role-start path rechecks that binding before creating the Orca Task. The runner
rechecks it before starting ACP execution and uses the same files for each
session operation. It never invokes `npm` or `npx`; a direct-only launch does
not resolve ACP dependencies.

1. The MCP bridge creates a Task and a private prompt sidecar.
2. It creates a launcher-owned bare terminal.
3. `orchestration dispatch` binds the Task and terminal with `injected=false`.
4. The bridge saves the assignment before sending the trusted runner command.
5. The runner creates an acpx session, selects model and effort, submits the
   prompt through stdin, and reads `--format quiet` output.
6. The runner closes and prunes its exact acpx session.
7. The runner, not the agent text, sends one matching Orca `worker_done`.

The agent command includes a team/role/nonce marker. Pruning is restricted to
that exact command, so unrelated acpx sessions are not removed.

## Lifecycle advances only on matching identities

At most one background role can be active. A new role cannot start while an
assignment or an unacknowledged Delivery exists.

```text
role_prompt
  -> role_wait
     -> worker_done: role_read -> role_release -> delivery_ack
     -> question: message_reply -> delivery_ack -> role_wait
     -> escalation: retain evidence and stop for user review
```

`worker_done` is accepted only when Task, Dispatch, sender terminal, and Run
match the active assignment. `question` and `escalation` are not completion.
A failed worker outcome is terminal for that Dispatch but is not successful
work.

## State is private and launch-scoped

The launcher writes version-3 runtime state below:

```text
$XDG_STATE_HOME/agent-team/<team-id>/state.json
```

The default base is `~/.local/state/agent-team/`. The state snapshot records the
workspace, config path, Run, Main terminal, role specifications, and active
assignment. Model, effort, permission, and instructions are copied at launch;
an ACP runner does not reinterpret a changed config during the same team run.

An ACP role specification also stores the resolved absolute `node`, `acpx`, and
`claude-agent-acp` paths and their SHA-256 fingerprints. The runner uses and
verifies this saved binding for every ACP lifecycle operation; missing or
changed files fail closed.

ACP prompt sidecars and state files are current-user-owned private files.
State writes are atomic and fsync their parent directory after replace. Prompt
reads use non-following file descriptors.
Codex runtime homes are isolated below the same team directory.
If the replacement succeeds but directory durability is unknown, the state is
treated as published and the startup marker is retained for management retry.

## Failure handling is fail-closed

- If role startup cannot confirm remote rollback, it retains the assignment and
  private resources. Before a complete assignment exists, `pending_role_start`
  records only known resource identities. `status` shows `cleanup_pending`, and
  another role launch or team-state deletion is blocked. Resolving this unknown
  outcome is not automated; deleting the record to force a restart is unsafe.
- A partial start stops or closes only resources whose exact IDs were returned.
- Cleanup errors are reported together with the original failure.
- CLI and MCP stateful operations share a stable per-team reservation outside
  the removable state root; management operations re-read state under that
  lock and MCP holds it through remote effects and save/rollback.
- Worker-stop and terminal-close require typed identity/process-stop verdicts;
  an agent terminal already closed by worker-stop is not closed twice, and an
  unconfirmed PTY keeps the journal and local resources for recovery.
- Method-specific `terminal_*`, `dispatch_not_found`, `run_not_found`, and
  `task_not_found` absence codes are normalized as read-only absence; stop
  persists the affected stage as unknown and never treats absence as process
  success.
- A durable startup marker is written before Main terminal creation; a lost
  create response keeps local preparation and blocks the next start until an
  explicit no-tab close receipt proves `ptyKilled=true`.
- Startup recovery never treats a stale/gone read-only terminal show as process
  stop proof; local homes remain until a verified close receipt is durable.
- CLI runtime errors use fixed classifications and the existing `ERROR: <message>`
  body with an explicit bound; Orca stderr/stdout, argv, IDs, paths, and control
  characters are never rendered. The redaction/legacy-body golden is covered by
  the CLI compatibility tests.
- ACP subprocesses run in their own process group; normal parent exit and
  timeout/output-limit paths verify and reap descendants before returning.
- The Orca lifecycle and bounded provider runner fail fast on Windows; the
  current contract requires a Unix socket and POSIX process-group semantics.
- Orca CLI selection is deterministic: `orca` on macOS and `orca-ide` on Linux;
  there is no silent PATH fallback or environment override.
- The ACP child receives a small environment allowlist, including `HOME` for
  ambient Claude login but excluding API keys and Orca control variables.
- `stop` validates the exact private team root and removes entries without
  following symlinks. Special files and ownership mismatches are rejected.
- The Orca Run remains after stop as an audit record.

CLI lifecycle operations use `WorkflowEngine(OrcaBackend)`. Role operations
use the MCP server's Orca implementation and the same state and reservation
helpers. The role methods on the abstract backend contract are not an
implemented replacement for this MCP path.

The MCP path records each observed Delivery and enforces reading the result,
releasing the owned role resources, and then acknowledging completion. Questions
must be answered before acknowledgment; escalations remain pending. Failed
operations retain their pending state. CLI inspection commands load no Orca
implementation and start no external process.

When Orca returns `retained`, the assignment stays pending and the launcher does
not close the terminal. A `no_owned_resource` result for a launcher-created
background terminal still needs an ownership-aware release path; its cleanup is
not treated as successful.

If a role-start or release response is lost, inspect the recorded Dispatch and
terminal before retrying. Role operations do not provide automatic crash
replay or an exactly-once guarantee. SQLite coordination, schema migration,
and general backup/restore are outside this runtime; Orca owns coordination
and the launcher retains its existing private version-3 state.

## Security limits remain explicit

ACP is a communication protocol, not a sandbox. A compatibility probe showed
that Codex internal tools could still write when the ACP client advertised
read-only/deny-all settings. For that reason, Codex ACP and workspace-write ACP
are rejected. The write-capable role remains direct Codex with provider-native
permissions.

The Claude ACP turn was verified with an ambient `claude.ai` Max login and no
API-key environment. This proves the observed authentication path, not the
provider's subscription billing ledger.

## Non-goals keep the runtime small

- No Herdr fallback
- No arbitrary role graph or concurrent background roles
- No arbitrary ACP server command in config
- No automatic provider or transport fallback
- No automatic commit, push, publishing, or deployment
- No support for running two configs concurrently in the same workspace
