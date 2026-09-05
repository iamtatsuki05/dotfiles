# Agent Skills

Japanese version: [README_JA.md](README_JA.md)

This directory is the shared skill tree for Codex-compatible agents and Waza eval suites.
`dotfiles/.agent/sync.sh` symlinks it into each supported agent home.

## Overview

```text
skills/
├── .system/                 # OpenAI bundled / system skill
├── <skill>/                 # repo-local / vendored skill
├── upstreams.json           # external skill manifest
└── review-prompts/          # upstream review prompt templates
```

Each directory with a `SKILL.md` is one loadable skill.
For discovery compatibility, every regular skill lives at the flat path `skills/<name>/SKILL.md`, regardless of provenance.
`references/`, `scripts/`, `agents/`, and `assets/` are supporting files scoped to that skill.

## Ownership Types

- `repo-local`: maintained directly in this dotfiles repository.
- `system`: bundled Codex / OpenAI skill. Treat it as upstream-derived; document the intent when editing it locally.
- `vendored`: imported from an external repository. `upstreams.json` records the repository, pinned commit, mappings, and security review.
- `local-only`: installed or generated local state. Usually not tracked by Git.
- `support`: not a skill itself, but used to manage or review skills.

Use `scripts/agent_skill_upstreams.py` and `upstreams.json` when adding or updating external skills.
Do not manually copy external skill trees without recording provenance and review metadata.

## Root Repo-Local Skills

| Skill | Purpose | Notes |
|---|---|---|
| `agent-cli-consult` | Consult Codex CLI or Claude Code CLI read-only through stdin prompts for review, debugging, and analysis. | Only when the user explicitly asks for an external CLI. |
| `agent-job-scheduler` | Queue, inspect, retry, and cancel long non-interactive jobs for ten agent CLIs, with allowlist, stale recovery, and launchd scheduling. | Bundles the app, README, and pytest. |
| `alphaxiv-paper-lookup` | Summarize, compare, and extract implementation details from arXiv papers via the alphaxiv overview and full-text Markdown. | Verify numbers against the full text before reporting. |
| `auto-debugger` | Fix errors and failing tests that reproduce with one command: print-debug, minimal fix, regression test. | Hands off to `diagnosing-bugs` when there is no tight reproduction, the bug is flaky, or it is a performance regression. |
| `ci-cd` | Create, edit, and debug CI/CD workflows (GitHub Actions, GitLab CI, CircleCI). | Checks permissions and deploy impact first and confirms the real CI run before reporting. Not for app code that merely fails in CI. |
| `compatibility-safety` | Reject unjustified aliases, silent fallbacks, default-value fallbacks, legacy paths, and silent runner/backend substitution. | Read when a diff you write or review contains one, not at implementation start. |
| `database-dev` | Design and review schemas, indexes, queries, and migrations with EXPLAIN-based diagnosis, expand-contract migrations, and approval gates for shared environments. | Read once per session; SQL and NoSQL. |
| `eng-practices` | PR/CL title and description, splitting into small PRs, replying to reviewer comments. | Read only when writing the PR description. Not for performing reviews; the review request defines the output format. |
| `go-dev` | Implement, test, and review Go with go.mod-aware syntax, table-driven tests, errgroup/context rules, and -race checks. | Read once per session; not for CI YAML or non-Go services. |
| `git-github-flow` | Run Git/GitHub work as check, scoped write, readback: owner/repo and login from the remote, Draft PRs with explicit assignee and labels, CI-gated Ready, no force-push. | References cover fork PRs, PR review posting, history cleanup, and gh-stack. |
| `goal-prompt-builder` | Turn a request into a durable Codex `/goal` prompt with scope, checkpoints, and a verifiable stopping condition. | Invoked as `$goal-prompt-builder`; refuses goals that delegate production, billing, or permission decisions. |
| `gws` | Operate Google Calendar, Drive, Gmail, and Tasks through the gws CLI helpers and low-level API. | Reads run immediately; writes go through dry-run or draft and user approval. |
| `html-preview-review` | Render a verified result as a private local HTML review board and show it in one presenter, only when the user asks for a preview. | Never falls back to an OS browser; reports an undisplayed board as unmet. |
| `markdown-docs` | Structure, syntax, links, and tables of a Markdown document itself (README, docs/, guides, release notes): create, edit, review. | Not for README edits riding along with code changes or for slides/PDF/LaTeX. Japanese naturalness belongs to `natural-japanese`. |
| `markitdown` | Convert PDF, Office, HTML, and URLs into Markdown with the markitdown CLI, with a PDF fallback via uvx. | Never retry a failed PDF; switch to markitdown[pdf] or pdftotext. |
| `missing-tools` | Run an unavailable command through project env, mise, Nix, or comma without a global install. | Record the resolved form in checkpoint.md. |
| `prompt-tuner` | Improve prompts sent to a model API (system prompt, template, few-shot) with a run-evaluate-diagnose-fix loop. | Agent instructions go to `empirical-prompt-tuning`; Codex `/goal` prompts go to `goal-prompt-builder`. |
| `python-dev` | Implement, test, and debug Python with the project's pyproject/ruff/mypy/pytest conventions; includes test-first and fail-fast rules. | Read once per session; not for notebooks, Slurm/env scripts, or docs. |
| `retrospective-codify` | Codify a session's lessons into a rule, skill, or lint check when the user asks; otherwise offer one 3-line proposal per session at most. | Agent-initiated candidates require cross-session recurrence. |
| `security-check` | Attacker-view review: secret leaks, injection, auth gaps, vulnerable dependencies. | Only when security is asked for explicitly. "Safety" reviews of launchers, rollouts, or destructive ops are normal reviews. Phase 1 secret grep is reusable alone. |
| `shaping-japanese-longform` | Restructure long-form Japanese articles, essays, and explanations without inventing facts, causes, or drama. | Removes document-progress narration and connects claims to evidence; sentence-level naturalness stays with `natural-japanese`. |
| `terraform-dev` | Write, validate, and review Terraform / OpenTofu with a plan-first workflow, moved/import blocks, state and secret handling, and approval gates for apply. | Read once per session; plan by default, apply only on request. |
| `typescript-dev` | Implement, test, and debug TypeScript / TSX using the project's tsconfig, lint, and test runner; covers Zod, type guards, and API-change sync. | Read once per session; HTML/CSS layout goes to `modern-web-guidance`. |

## System Skills

`skills/.system/` contains bundled skills.
They use the same shape as repo-local skills, but their origin is Codex / OpenAI.

| Skill | Purpose |
|---|---|
| `imagegen` | Generate or edit AI bitmap assets and images. |
| `openai-docs` | Check current official OpenAI API / product documentation. |
| `plugin-creator` | Scaffold Codex plugin directories and manifests. |
| `skill-creator` | Guide new skill creation and skill improvements. |
| `skill-installer` | Install curated skills or GitHub-hosted skills into `$CODEX_HOME/skills`. |

## Vendored External Skills

These external skills are registered in `upstreams.json`.
Updates write security review reports under `dotfiles/.agent/work/skill-upstream-reviews/`.
Declare flat-layout-specific reference changes in `local_text_replacements`.
An update fails before overwriting local files if a source match count differs from `expected_count`.

| Group | Upstream | Local path | Contents |
|---|---|---|---|
| `empirical-prompt-tuning` | `mizchi/skills` | `empirical-prompt-tuning/` | Japanese skill for iteratively improving agent instructions through executor feedback. |
| `modern-web-guidance` | `GoogleChrome/modern-web-guidance` | `modern-web-guidance/` | Search skill for current HTML / CSS / client-side JavaScript best practices. |
| `mattpocock-skills` | `mattpocock/skills` | `grilling/`, `diagnosing-bugs/`, `domain-modeling/`, etc. | Current design, diagnosis, handoff, and architecture skills without deprecated aliases. Each skill includes the upstream LICENSE. |
| `superpowers` | `obra/superpowers` | `brainstorming/`, `dispatching-parallel-agents/`, `software-development/systematic-debugging/`, `test-driven-development/`, `writing-skills/` | Five selected workflow areas. Three skills are vendored directly; systematic debugging keeps the richer local skill with pinned upstream waiting references, and brainstorming is a minimal local Three Paths router. |
| `natural-japanese` | `coji/natural-japanese` | `natural-japanese/` | Japanese work-document writing and revision with deterministic linting, document-type guidance, and a local safety overlay. |
| `herdr` | `ogulcancelik/herdr` | `herdr/` | Herdr pane/workspace control skill with a local safety overlay and copied Apache-2.0 license. |
| `stop-slop` | `hardikpandya/stop-slop` | `stop-slop/` | Strict English AI-pattern checklist; voice matching routes to `humanizer`. |
| `delegate-skills` | `amElnagdy/delegate-skills` | `codex-delegate/`, `claude-delegate/` | Delegate one bounded coding task to a separate Codex CLI or Claude Code process through a relay that never commits, then review the diff and land it yourself. Local safety overlay pins read-only defaults and forbids permission-bypass flags. |
| `chatgpt-pro-line` | `pauljunsukhan/codex-chatgpt-pro-plugin` | `chatgpt-pro-line/` | Ask a logged-in ChatGPT Pro browser profile (dedicated Chrome, CDP on 127.0.0.1) for architecture, design, research, and debugging judgment with receipts and transcripts. Runtime only (no self-tests, no Codex MCP config); the local overlay requires an explicit user request, per-call approval for repo-context uploads, and keeps `.devspace/` uncommitted. |

### Matt Pocock Group

| Skill | Purpose |
|---|---|
| `codebase-design` | Provide shared vocabulary and principles for deep-module design. |
| `diagnosing-bugs` | Work through hard bugs and performance regressions with a red-capable feedback loop, minimization, hypotheses, instrumentation, and regression tests. |
| `domain-modeling` | Sharpen project terminology and prepare `CONTEXT.md` or ADR updates. |
| `grilling` | Stress-test a plan in dependency-aware rounds and wait for feedback between frontiers. |
| `grill-with-docs` | Compose `grilling` with `domain-modeling`. |
| `handoff` | Compact the conversation into a handoff document for another agent. |
| `improve-codebase-architecture` | Find architecture improvements, deep modules, and testability opportunities. |

### Superpowers Group

| Skill | Purpose |
|---|---|
| `brainstorming` | Route a software request to a spike, bounded change, or architectural design, and state the next user decision and durable artifact for each path; no approval gate, server, or telemetry. |
| `dispatching-parallel-agents` | Decide when independent tasks should be split across parallel agents. |
| `systematic-debugging` | Keep the local root-cause workflow and load pinned condition-based waiting guidance for flaky asynchronous tests. |
| `test-driven-development` | Anchor feature and bugfix implementation in TDD. |
| `writing-skills` | Support skill creation, editing, and verification workflows. |

`brainstorming` intentionally adapts only the upstream Three Paths idea. It does not vendor the upstream visual companion, background server, telemetry, automatic design commits, or universal approval gate.

## Local-Only / Ignored Skills

| Path | Contents |
|---|---|
| `hatch-pet/` | Curated skill for creating Codex pet spritesheets and packages. Installed through `skill-installer`; currently ignored as a local installation. |
| `codex-primary-runtime/` | Local Codex runtime state. Currently not tracked by Git. |
| `.hub/`, `.curator_state` | Skill hub / curator cache and state files. Not tracked by Git. |

## Support Files

| Path | Contents |
|---|---|
| `upstreams.json` | Manifest for vendored external skills. Stores repository, branch, pinned commit, mappings, tree hash, and security review metadata. |
| `review-prompts/skill-upstream-security.md` | Security review prompt template used when updating external skills. |

## Add Or Update Skills

- Add new local skills as `skills/<name>/SKILL.md`.
- Register external skills in `upstreams.json` with a pinned commit and security review.
- Use `references/` for long supporting material, `scripts/` for reusable validation or conversion scripts, and `agents/` for agent-specific files.
- Do not track secrets, caches, work logs, or local-only installations.
- When changing the `dotfiles/.agent/skills` layout, update Waza evals and the parent `dotfiles/.agent/README.md` / `README_JA.md` if needed.

## Common Checks

```bash
python3 scripts/agent_skill_upstreams.py check
find dotfiles/.agent/skills -name SKILL.md -print | sort
git status --short --ignored dotfiles/.agent/skills
```
