# ACP boundary

[日本語](acp_JA.md) · [README](../README.md) ·
[Support matrix](support-matrix.md)

ACP (Agent Client Protocol) is the message protocol between an ACP client and
an agent adapter. It is not an operating-system sandbox and it does not turn a
provider subscription into an API-key contract.

## What agent-team runs

The only verified ACP profile is a read-only Planner or Reviewer using Claude:

- `acpx@0.13.2`
- `@agentclientprotocol/claude-agent-acp@0.70.0`
- ambient Claude login, with no API-key variables copied into the child
- `Read,Grep,Glob` tools, read approval, and non-interactive permission failure
- bare Orca terminal plus a trusted outer runner that sends exactly one matching
  `worker_done`

The exact adapter command, task identity, and nonce are generated at dispatch
time. Agent output is data; it never gets authority to send lifecycle messages.

Codex ACP is intentionally rejected. A negative test showed that ACP
`deny-all`/read-only mediation did not prevent an internal write. Direct Codex
uses its isolated `CODEX_HOME` and provider-native permission profiles for the
verified workspace-write Worker and read-only Reviewer.

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
