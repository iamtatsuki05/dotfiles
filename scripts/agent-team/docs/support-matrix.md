# Harness support matrix

[日本語](support-matrix_JA.md) · [README](../README.md)

The repository manages these ten harness identities. Recognition is not the
same as execution: `recognized` means the name is known; `available` means the
expected command is found by PATH; `implemented` means at least one exact
role/transport/permission profile is implemented and tested; `runnable` means
that an implemented profile also has its command available. `agent-team
harnesses` performs only static registry and PATH resolution. It never checks
login, downloads packages, starts a process, or writes a workspace.

| Harness | Direct profiles currently runnable | ACP adapter known | agent-team status | Why not broader |
|---|---|---|---|---|
| Claude | Main `orchestrator`; Planner/Reviewer `read-only` | `claude-agent-acp@0.70.0` | Verified | ACP is limited to read-only background roles. |
| Codex | Main `orchestrator`; Planner/Reviewer `read-only`; Worker `workspace-write` | `codex-acp` | Direct verified; ACP rejected | ACP permission mediation did not stop internal writes in the negative test. |
| GitHub Copilot | Planner/Reviewer `read-only` (direct background, exact `1.0.81`) | Native `copilot --acp`; acpx built-in `copilot` | Verified when exact GitHub CLI is resolved | The profile is intentionally limited to read-only Planner/Reviewer; Workers remain rejected. |
| Cursor | None | Native `cursor-agent acp`; acpx built-in `cursor` | Recognized, rejected | The current CLI is unauthenticated, so the permission-negative matrix could not complete. |
| Devin | None | Native `devin acp` | Recognized, rejected | Only the no-tool smoke completed; tool tasks did not, and model overrides required Pro. |
| Antigravity | None | None registered | Recognized, rejected | A read-only probe read a sibling file outside the workspace. |
| Hermes Agent | None | Native `hermes acp` | Recognized, rejected | A read-only probe wrote a normal file, `.git`, and an outside sibling path. |
| OpenCode | None (static profiles only; live not implemented) | Native `opencode acp`; acpx built-in `opencode` | Recognized; both profiles blocked | Current auth provenance reports zero credentials, so raw/snapshot phases are all `not-run`. A prior raw-workspace symlink escape remains a separate historical `rejected` record; neither profile is registered. |
| OpenClaw | None | Native `openclaw acp`; acpx built-in `openclaw` | Recognized, rejected | One-shot execution works, but the sandbox needs a Docker daemon that was unavailable, so negative tests remain incomplete. |
| Grok | None | Native `grok agent stdio`; acpx built-in `grok-build` | Recognized, rejected | The current CLI is unauthenticated, so direct/ACP permission-negative tests could not complete. |

An ACP adapter being installed or listed by acpx does not prove that the
adapter is safe for a role. It is shown separately from the verified
agent-team profile. Unknown providers and recognized-but-rejected profiles
fail before an Orca Task, terminal, or ACP process is created. There is no
fallback to another harness.

```bash
agent-team harnesses
agent-team harnesses --json
```

The JSON output contains `recognized`, `available`, `command_resolution_status`,
`implemented`, `runnable`, `runnable_profiles`, `acp_adapter`, `acp_status`, and
`rejection_reason` so a
caller can make the distinction without parsing human text.
