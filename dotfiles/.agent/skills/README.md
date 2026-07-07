# Agent Skills

Japanese version: [README_JA.md](README_JA.md)

This directory is the shared skill tree for Codex-compatible agents and Waza eval suites.
`dotfiles/.agent/sync.sh` symlinks it into each supported agent home.

## Overview

```text
skills/
├── .system/                 # OpenAI bundled / system skill
├── <skill>/                 # repo-local skill
├── mattpocock/<skill>/      # vendored external skill group
├── superpowers/<skill>/     # vendored external skill group
├── upstreams.json           # external skill manifest
└── review-prompts/          # upstream review prompt templates
```

Each directory with a `SKILL.md` is one loadable skill.
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
| `api-design` | Design and review REST APIs, OpenAPI specs, versioning, auth, and error responses. | Links to `eng-practices`. |
| `auto-debugger` | Investigate errors, stack traces, and failing tests. | Emphasizes reproduction, hypotheses, and verification before fixing. |
| `chronicle` | Use screen history and recent desktop context to disambiguate user requests. | Only for environments where Chronicle is enabled. |
| `ci-cd` | Design, edit, and debug CI/CD workflows such as GitHub Actions. | Workflow YAML and log investigation. |
| `colab-mcp` | Set up and troubleshoot Google Colab MCP connections. | For Google's official colab-mcp flow. |
| `compatibility-safety` | Avoid unrequested compatibility layers, aliases, silent fallbacks, and default-value fallbacks. | Use before adding compatibility behavior or legacy paths. |
| `database-dev` | Design and review schemas, queries, indexes, migrations, and database performance. | Covers SQL and NoSQL work. |
| `eng-practices` | Code review standards, CL/PR descriptions, small CLs, and review comment etiquette. | Repo-oriented summary of Google eng-practices. |
| `go-dev` | Implement, test, and review Go code, modules, concurrency, and interfaces. | Links to `eng-practices`. |
| `goal-prompt-builder` | Create durable Codex `/goal` prompts. | Clarifies durable objectives and verification conditions. |
| `gws` | Operate Google Calendar, Drive, Gmail, and Tasks through the `gws` CLI. | Be conservative around external actions. |
| `kimi-webbridge` | Control the user's real browser through a local daemon. | For logged-in browser sessions and real-site interaction. |
| `magika` | Identify file types and verify whether extensions match file contents. | Magika CLI workflow. |
| `markdown-docs` | Create, edit, review, and lint README and Markdown documentation. | This README is also covered by this skill. |
| `markitdown` | Convert PDF, Word, PowerPoint, Excel, HTML, and other sources into Markdown. | MarkItDown CLI workflow. |
| `missing-tools` | Resolve unavailable commands without changing global machine state. | Prefers project env, mise, comma, and Nix fallbacks. |
| `pr-code-review` | Review GitHub PR diffs for bugs, risks, and missing tests. | Uses finding-oriented review output. |
| `prompt-tuner` | Improve, evaluate, and rewrite LLM prompts and templates. | Prompt tuning workflow. |
| `python-dev` | Implement, test, and debug Python, pytest, typing, Pydantic, and packaging work. | Links to `eng-practices`. |
| `retrospective-codify` | Turn repeated lessons into rules, skills, or lint checks near the end of a task. | For codifying repeated mistakes. |
| `security-check` | Review secret leaks, injection, auth, and OWASP-style risks. | Use explicitly for high-risk changes. |
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

| Group | Upstream | Local path | Contents |
|---|---|---|---|
| `empirical-prompt-tuning` | `mizchi/skills` | `empirical-prompt-tuning/` | Japanese skill for iteratively improving agent instructions through executor feedback. |
| `modern-web-guidance` | `GoogleChrome/modern-web-guidance` | `modern-web-guidance/` | Search skill for current HTML / CSS / client-side JavaScript best practices. |
| `mattpocock-skills` | `mattpocock/skills` | `mattpocock/` | Design questioning, diagnosis, prototyping, handoff, and architecture review skills. |
| `superpowers` | `obra/superpowers` | `superpowers/` | Workflow skills for TDD, parallel agent dispatch, and skill writing. |
| `report-skills` | `mizuamedesu/ReportSkills` | `report-skills/` | Japanese report and academic assignment writing skill. |
| `herdr` | `ogulcancelik/herdr` | `herdr/` | Herdr pane/workspace control skill with a local safety overlay and copied AGPL/commercial dual-license notice. |
| `stop-ai-slop-jp` | `iKora128/stop-ai-slop-jp` | `stop-ai-slop-jp/` | Japanese AI-slop prose review and rewrite skill. |
| `stop-slop` | `hardikpandya/stop-slop` | `stop-slop/` | English prose review skill for removing predictable AI writing patterns. |

### Matt Pocock Group

| Skill | Purpose |
|---|---|
| `diagnose` | Work through hard bugs and performance regressions with reproduction, minimization, hypotheses, instrumentation, and regression tests. |
| `grill-me` | Ask focused questions to stress-test a plan or design and remove ambiguity. |
| `grill-with-docs` | Challenge design decisions against `CONTEXT.md`, ADRs, and project language. |
| `handoff` | Compact the conversation into a handoff document for another agent. |
| `improve-codebase-architecture` | Find architecture improvements, deep modules, and testability opportunities. |
| `prototype` | Build throwaway prototypes for state design or UI exploration. |
| `zoom-out` | Understand the higher-level structure of an unfamiliar code area. |

### Superpowers Group

| Skill | Purpose |
|---|---|
| `dispatching-parallel-agents` | Decide when independent tasks should be split across parallel agents. |
| `test-driven-development` | Anchor feature and bugfix implementation in TDD. |
| `writing-skills` | Support skill creation, editing, and verification workflows. |

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
