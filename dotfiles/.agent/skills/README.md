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
| `agent-cli-consult` | Consult an external agent CLI (Codex CLI / Claude Code CLI) for review, debugging, and analysis. | Use only when the user explicitly asks for a CLI consultation. |
| `agent-job-scheduler` | Queue, inspect, retry, and cancel long non-interactive jobs for multiple agent CLIs. | Larger skill with an internal app, README, and pytest coverage. |
| `alphaxiv-paper-lookup` | Summarize, compare, and extract implementation details from arXiv / alphaxiv papers. | Paper research workflow. |
| `auto-debugger` | Investigate errors, stack traces, and failing tests. | Emphasizes reproduction, hypotheses, and verification before fixing. |
| `ci-cd` | Design, edit, and debug CI/CD workflows such as GitHub Actions. | Workflow YAML and log investigation. |
| `compatibility-safety` | Avoid unrequested compatibility layers, aliases, silent fallbacks, and default-value fallbacks. | Use before adding compatibility behavior or legacy paths. |
| `database-dev` | Design and review schemas, queries, indexes, migrations, and database performance. | Covers SQL and NoSQL work. |
| `eng-practices` | CL/PR descriptions, small CLs, and handling reviewer comments. | Repo-oriented summary of Google eng-practices. Read when writing a PR description, not for every review. |
| `go-dev` | Implement, test, and review Go code, modules, concurrency, and interfaces. | Links to `eng-practices`. |
| `git-github-flow` | Organize Git/GitHub work from authentication and branch topology through Issues, PRs, review, CI, merge, and recovery. | Uses `gh`, prefers isolated worktrees, and gates every external mutation. |
| `goal-prompt-builder` | Create durable Codex `/goal` prompts. | Clarifies durable objectives and verification conditions. |
| `gws` | Operate Google Calendar, Drive, Gmail, and Tasks through the `gws` CLI. | Be conservative around external actions. |
| `html-preview-review` | Render verified agent results as safe, self-contained local HTML for visual review. | The main agent presents the artifact after required independent review. |
| `markdown-docs` | Create, edit, review, and lint the structure and syntax of README and Markdown documentation. | This README is also covered by this skill. Japanese naturalness belongs to `natural-japanese`. |
| `markitdown` | Convert PDF, Word, PowerPoint, Excel, HTML, and other sources into Markdown. | MarkItDown CLI workflow. |
| `missing-tools` | Resolve unavailable commands without changing global machine state. | Prefers project env, mise, comma, and Nix fallbacks. |
| `prompt-tuner` | Improve, evaluate, and rewrite LLM prompts and templates. | Prompt tuning workflow. |
| `python-dev` | Implement, test, and debug Python, pytest, typing, Pydantic, and packaging work. | Links to `eng-practices`. |
| `retrospective-codify` | Turn repeated lessons into rules, skills, or lint checks near the end of a task. | For codifying repeated mistakes. |
| `security-check` | Review secret leaks, injection, auth, and OWASP-style risks. | Use explicitly for high-risk changes. |
| `shaping-japanese-longform` | Revise long-form Japanese articles, essays, and explanations without inventing facts or drama. | Separates subject-matter content from document-progress narration and checks question/evidence links. |
| `terraform-dev` | Work on Terraform / OpenTofu modules, state, plans, imports, and security. | Infrastructure changes. |
| `typescript-dev` | Implement, test, and debug TypeScript / TSX, Vitest/Jest, Zod, and ESLint/Biome work. | Frontend and Node work. |

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
| `brainstorming` | Route feasibility probes, bounded changes, and architectural work without adding an automatic approval gate, server, or telemetry. |
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
