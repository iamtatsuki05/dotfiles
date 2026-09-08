# ACP boundary

[日本語](acp_JA.md) · [README](../README.md) ·
[Support matrix](support-matrix.md)

ACP (Agent Client Protocol) is the message protocol between an ACP client and
an agent adapter. It is not an operating-system sandbox and it does not turn a
provider subscription into an API-key contract.

## What agent-team runs

On Orca, the verified ACP profile is a read-only Planner or Reviewer using Claude:

- Node.js `22.13.0` or newer
- `acpx@0.13.2`
- `@agentclientprotocol/claude-agent-acp@0.70.0`
- ambient Claude login, with no API-key variables copied into the child
- `Read,Grep,Glob` tools, read approval, and non-interactive permission failure
- bare Orca terminal plus a trusted outer runner that sends exactly one matching
  `worker_done`

The exact adapter command, task identity, and nonce are generated at dispatch
time. Agent output is data; it never gets authority to send lifecycle messages.

## Dependencies are explicit and launch-scoped

Install the selected ACP packages outside `agent-team`. For example, install
both exact packages into a directory you choose and put that directory's bin
directory on `PATH` before starting the team:

```bash
npm install --prefix /path/to/agent-team-acp acpx@0.13.2 @agentclientprotocol/claude-agent-acp@0.70.0
export PATH="/path/to/agent-team-acp/node_modules/.bin:$PATH"
```

When an Orca launch plan contains an ACP role, startup resolves `node`, `acpx`, and
`claude-agent-acp`, verifies the exact package manifests, and records the
absolute paths and SHA-256 fingerprints in that role's launch snapshot. The
role-start path rechecks the saved binding before creating the Orca Task. The
runner rechecks it before starting ACP execution and uses the same files for
each session operation. A missing, replaced, or changed executable fails
closed.

Runtime operations use those resolved files directly. They never invoke `npm`
or `npx`. If no selected role uses ACP, these ACP dependencies are not
resolved, and a direct-only team does not require them. The static harness
inventory remains separate from this launch preflight and does not install or
start providers.

Unwrapped Codex ACP remains rejected. A negative test showed that ACP
`deny-all`/read-only mediation did not prevent an internal write. Direct Codex
uses its isolated `CODEX_HOME` and provider-native permission profiles for the
verified workspace-write Worker and read-only Reviewer.

## Native Claude ACP

Native tmux, Herdr, and Zellij use a separate client with one public ACP connection per assignment.
It selects Node.js, `@agentclientprotocol/claude-agent-acp@0.70.0`, and that
adapter's installed `@agentclientprotocol/sdk@1.3.0`; it does not select `acpx`.
Startup records four absolute paths and SHA-256 fingerprints: Node, the adapter
entry, its actual `dist/lib.js` import, and the SDK entry.
The client creates the session, sets the configured model and effort, prompts,
closes the session, and waits for child cleanup. Automated sessions disable
transcript persistence and automatic memory; the interactive Main retains its
normal CLI history.

Planner and Reviewer permit `Read`, `Grep`, and `Glob`. Worker also permits
`Write` and `Edit` within the user-declared TaskSpec scope. A fixed wrapper
enforces these tool and path checks. The TaskSpec's allowed and forbidden paths
restrict Write/Edit only. Read/Grep/Glob can read the workspace, subject to
protected-path and link/file-type checks.
TaskSpecs must match the catalog captured from `[[tasks]]` at startup, so Main
cannot introduce another scope or verification command through a dispatch.
For native `agent`/`parallel`, the empty-catalog path fails before dependency or
profile checks, and `role_prompt` is rejected even for read-only research.
Parallel research uses a declared plan-only TaskSpec. The serial read-only
`role_prompt` path is unchanged. Dependency and profile bindings selected at
startup remain fixed; no provider or transport fallback is introduced.
See [configuration](configuration.md) for the schema and review/verification flow.
Installed dependency trees remain trusted; entry-file fingerprints do not make
the entire import closure hermetic or prevent hostile same-user filesystem races.

### Native Claude question path

The native Claude profile uses the existing `AskUserQuestion` tool and ACP
form elicitation. The selected 0.70.0 adapter and SDK 1.3.0 client enable it
only when the assignment owns its private `q.sock`. The question remains in
the same Task and Dispatch. The Python side persists the question outbox and
each `message_reply`; after every answer is acknowledged with `delivery_ack`,
it sends the answers through `q.sock`, receives Node's `received` receipt,
records its hash-only receipt while retaining the protected outbox, and sends
`recorded` before the same ACP session resumes.
The channel stores an internal `received` phase after validating the client
receipt and advances to `recorded` only after the `recorded` frame is sent
successfully. A failure while it is only `received` keeps the raw protected
outbox. No new wire fields are added.

The bounded contract accepts one to four questions per batch, 20,000 characters
per question or answer, 512 KiB per JSONL frame, and 64 batches per assignment.
The same message ID and body may be retried idempotently; a different body is
rejected. Consumed receipts retain assignment/session/delivery/tool-call IDs and
question/answer hashes. The protected outbox may retain raw question and answer
text until the next question or terminal completion so post-replace fsync or
`recorded` publication failures can be recovered; the receipt itself contains
no raw question text. While a question Delivery is pending, that assignment's
`role_read`, `role_release`, and another dispatch for that assignment are
rejected, and its successful completion cannot be published. In version-3/4
native serial state, the pending question also blocks the next dispatch for the
run. In version-5 native `agent`/`parallel` and `program`/`parallel`, an
independent admitted assignment may continue within `max_active` when Worker
scopes do not overlap, but the batch/coordinator verification barrier remains
blocked until every active assignment and Delivery is drained. Stop marks
the question as cancelling and never fabricates an acknowledgment; unproven
provider, process-group, socket, or private-root cleanup retains state.

Version-5 native `agent`/`parallel` and `program`/`parallel` state stores a result, question, and
pending Delivery container per active node. Completion follows
`role_read` → `role_release` → `delivery_ack`, and a released assignment stays
in state until its matching acknowledgment. The private Stop path sets
`native.phase=stopping`, drains safe peers in that order, and retains any node
with unknown identity, a missing typed result, or unproven cleanup while
continuing safe peers. Focused contract checks and earlier bounded
real-terminal/fake-provider cases cover the program path. The bounded live
Main-parallel acceptance is recorded in [Architecture](architecture.md), and
real-model parallel acceptance is pending.

In Main-coordinated `agent`/`parallel`, Main first calls `task_batch_open` with
the exact declared IDs in any order; the saved IDs are catalog-ordered. The
first final-review dispatch seals the batch after all writers and Delivery
drain. All same-revision reviewers must approve, and all roles and Delivery
must be consumed, before fixed-argv verification. A plan-only final review
keeps the plan-body SHA-256 separate from `workspace_revision`. For a retryable
member, an unanswered consultation blocks retry. After the answer is saved,
all roles and Delivery are consumed, and all member review-round limits remain,
Main calls `task_dispatch` for the
original writer. That request atomically reopens the exact peer set, including
completed peers, and dispatches the requested writer; peers are not
auto-dispatched. There is no public reopen tool, and `task_batch_open` cannot
reopen or replace an unfinished batch. The additional `task_batch_open` tool is
present only in the explicit Claude Main `--tools` and `--allowedTools` lists
for this mode.

The real-model tmux run `dc101afd-87bf-4697-9bbb-0d1339d381a8` confirmed a full
question round trip with Fable at high effort, Planner omitted, and the same
Worker ACP session. Main's answers and acknowledgment were followed by Reviewer
approval, fixed-argv verification of the same revision, Task completion, and
public stop. A separate run confirmed stop during unanswered questions with
typed ACP cleanup evidence. [Architecture](architecture.md) records both runs,
the retained earlier failures, and the limits for Herdr/Zellij.

The native client publishes a fixed private-root `client-result.json` before
ordinary stdout. It is mode 0600, atomically created without overwrite, limited
to 1 MiB, and bound to the launch nonce, requested model, and effort. Exit 0
is a typed success, exit 1 a typed failure, and exit 2 publication uncertainty.
Python must retrieve and verify the artifact, client exit status, stdout parity
at retrieval, session identity, and process-group proof together. A signal or
cancellation Event is only a control request, not cleanup evidence. Missing,
damaged, mismatched, exit-2, or cleanup-unconfirmed results retain the
assignment and private root.

The signal handler only sets the per-run `threading.Event`. An explicit
ProcessRunner checkpoint begins cleanup exactly once; asynchronous exceptions
do not interrupt cleanup after it starts. The runner signals the Node client
leader first, drains its streams for a bounded period, and escalates to the
owned process group only if the leader remains live. Public stop saves
`native.phase=stopping` before signaling; the publication reservation gives
that phase priority over provider success, discards Reviewer evidence, and
persists a failed outcome. The CLI result is derived from the saved outcome.

The pending stop attempts
`1000018d-3ae0-4d62-b2af-a5c89be31c6a` and
`c945f3f5-8dcf-4643-a05a-72d1a62bab78` both failed because Python lost the
client exit status despite an actual ACP session-close receipt. A public-stop
`returncode=1` is not the client exit code. All workload processes exited and
the owned idle tmux terminals were recovered separately; failed state, private
root, snapshot, artifact, and fixture were retained. Neither run is successful
cleanup evidence.

## Scoped Codex ACP implementation: not enabled

The native backend contains a scoped Codex implementation, but the registry still
rejects Codex ACP configurations. It is not a runnable profile. Real authenticated
model turns and their permission/cleanup checks remain required before activation.

The implementation selects Node.js 22 or newer,
`@agentclientprotocol/codex-acp@1.10.0`, its SDK `1.4.0`, and
a Codex binary reporting `codex-cli 0.153.4` (npm distribution:
`@openai/codex@0.153.4`). It uses the shared native lifecycle and public ACP client;
it does not select `acpx` or use the direct Codex role path. Python creates a
private launch manifest, runs a bounded configuration inspection, and binds the
result to the assignment before the ACP runner starts.

The app-server proxy fixes the model, effort, instructions, read-only sandbox,
empty execution environments, and disabled auxiliary features. It disables each
configured MCP server before thread creation. The host exposes `read_text`,
`list_files`, `write_text`, and `edit_text`; writes require the Worker's declared
TaskSpec scope. Planner/Reviewer policies reject writes. Shared path checks protect
state, authentication, configuration, dependency files, and the scoped runtime.
Other tool requests and additional threads are rejected.

The private `CODEX_HOME` links to existing ChatGPT file authentication. Startup
binds the credential digest/expiry and project configuration; unsupported system
configuration or project settings fail before app-server launch. Provider token
refresh can update the linked credential source. The normal-authentication trial
is still pending, and cached Pro metadata does not prove authentication or billing.
If configuration inspection cannot confirm process cleanup, the native state
retains the private directory and blocks dispatch, resume, and successful stop.

Tests cover the file policy, protocol/transport, dependency selection, and native
assignment wiring with synthetic authentication or fake agents. A separate
network-blocked trial checked actual Codex thread settings without a model turn.
These checks do not establish authenticated model behavior.

Codex question handling remains disabled: there is no question socket or
`AskUserQuestion` capability for the Codex profile, and the public Codex ACP
registry entry remains rejected. The pending normal-authentication trial and
its permission/cleanup checks are unchanged.

## Authentication and subscription

ACP does not select an account or bypass a provider's billing policy. The
Claude profile reuses the ambient `claude.ai` login available to the adapter.
Whether a particular turn is counted against a subscription quota is a
provider-account matter and is not asserted by this tool. API-key based
adapters are not silently substituted. Login, account changes, and package
installation remain outside `agent-team`.

## Adding an ACP profile

An adapter must be registered in the support matrix only after an exact
version policy, authentication path, positive lifecycle smoke test, and
read/write/process/network negative tests are recorded. Adapter availability
alone is not enough. Until those checks pass, configuration rejects the profile
before runtime resources are created.

The resolved dependency binding establishes executable identity for the
selected Claude profile. It does not promote other ACP adapters to runnable
profiles or change their documented scope or status in the [support
matrix](support-matrix.md).
