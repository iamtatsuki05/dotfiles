---
name: deslop
description: Remove AI-generated code slop and clean up code style
---

Local integration: Limit cleanup to the current authorized change, not unrelated branch work. Preserve unrelated existing comments, TODOs, licenses, nontrivial WHY comments and dirty files. Keep behavior unchanged; a newly noticed bug is not permission to expand this cleanup. Never weaken validation, error handling or tests merely to remove a warning. This managed skill comes from cursor-team-kit and is available to pstack by its name. Existing user/repository instructions and authorization take precedence; do not request redundant approval for already authorized work.

# Remove AI code slop

Check the diff against main and remove AI-generated slop introduced in the branch.

## Focus Areas

- Extra comments that are unnecessary or inconsistent with local style
- Defensive checks or try/catch blocks that are abnormal for trusted code paths
- Casts to `any` used only to bypass type issues
- Deeply nested code that should be simplified with early returns
- Other patterns inconsistent with the file and surrounding codebase

## Guardrails

- Keep behavior unchanged unless fixing a clear bug.
- Prefer minimal, focused edits over broad rewrites.
- Keep the final summary concise (1-3 sentences).
