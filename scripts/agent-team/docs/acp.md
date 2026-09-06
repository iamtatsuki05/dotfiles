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

Codex ACP is intentionally rejected. A negative test showed that ACP
`deny-all`/read-only mediation did not prevent an internal write. Direct Codex
uses its isolated `CODEX_HOME` and provider-native permission profiles for the
verified workspace-write Worker and read-only Reviewer.

## Native tmux ACP

Native tmux uses a separate client with one public ACP connection per assignment.
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
See [configuration](configuration.md) for the schema and review/verification flow.
Installed dependency trees remain trusted; entry-file fingerprints do not make
the entire import closure hermetic or prevent hostile same-user filesystem races.

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
alone is not enough. Until then it remains `recognized-but-rejected` and
configuration fails before Orca resources are created.

The resolved dependency binding establishes executable identity for the
selected Claude profile. It does not promote other ACP adapters to runnable
profiles or change their documented scope or status in the [support
matrix](support-matrix.md).
