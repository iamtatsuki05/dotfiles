---
name: handoff
description: Compact the current conversation into a handoff document for another agent to pick up.
metadata:
  invocation: "Explicit handoff focus arguments are supported."
---

Write a handoff document summarising the current conversation so a fresh agent can continue the work.

Choose the destination explicitly:

- If the current workspace has `.agent`, save the document as `handoff.md` in the active session under `.agent/work/sessions/`. If the current task has no active session, create one according to the repository's `AGENTS.md` before writing the handoff. Do not silently use another path.
- If the current workspace has no `.agent`, save the document to the temporary directory of the user's OS.

Use `handoff.md` for a planned agent or session transfer. A workspace session's `checkpoint.md` is the recovery source when a usage or model limit prevents a final handoff.

If the user explicitly requests a handoff and the agent can still write it, create `handoff.md` even when a usage or model limit is imminent. Use `checkpoint.md` alone only when the interruption prevented the final handoff from being created.

For a workspace with `.agent`, give the next agent this resume sequence:

1. Read `checkpoint.md`.
2. Read `handoff.md`, `changes.md`, and `verification.md` when they exist.
3. Inspect the current Git status and diff.
4. Run the relevant focused test before continuing implementation.

Include a "suggested skills" section in the document, naming which skills the next agent should invoke explicitly.

Do not duplicate content already captured in other artifacts (specs, plans, ADRs, issues, commits, diffs). Reference them by path or URL instead.

Redact any sensitive information, such as API keys, passwords, or personally identifiable information.

Treat user-provided arguments as the next session's focus and tailor the document accordingly.

Verify that the document exists at the selected destination and report its path.
