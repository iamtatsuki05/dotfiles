---
name: requesting-code-review
description: "Independently review a completed local code change before an authorized commit or handoff; report defects and verification gaps."
version: 2.0.0
author: Hermes Agent (adapted from obra/superpowers + MorAlekss)
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [code-review, security, verification, quality, pre-commit, auto-fix]
    related_skills: [dispatching-parallel-agents, plan, test-driven-development, git-github-flow]
---

# Pre-Commit Code Verification

Review the requested local change against its intended behavior and verification evidence. Use the repository's review requirements and scale the review to the risk. This skill does not authorize staging, committing, pushing, resetting, or stashing files.

## Establish the review scope

Use the user's specified files or commit range. Otherwise inspect `git status --short`, `git diff`, and `git diff --cached` to distinguish this task's changes from unrelated work. Review an unstaged change directly; staging is not a prerequisite. An empty diff is not permission to substitute the previous commit.

For an existing GitHub PR or a requested review post, use `git-github-flow`.

## Check evidence

- Inspect the changed behavior and necessary callers. Check concrete risks in the affected path, such as authorization, unsafe input handling, secret exposure, data loss, or concurrency.
- Use applicable project tests, lint, and type checks. Preserve exit codes and capture large output with the project's compact runner or a temporary log. Do not weaken checks, hide missing tools, or treat keyword matches alone as vulnerabilities.
- Reuse valid verification already performed for the same change. New edits, failures, or unresolved risks justify further checks. If a baseline comparison is needed, use an isolated checkout or fixture; preserve the user's working tree.

## Independent review

When repository policy or the change's risk calls for a separate reviewer, use an available read-only subagent. Provide the exact diff or artifact, expected behavior, relevant evidence, and permission boundaries. Request findings by severity with file and line, or an explicit statement that no material issues were found. If an independent reviewer is unavailable, state that limitation; self-review is not independent evidence.

The main agent decides which findings apply. For a review-only request, return findings without editing. When fixes are authorized, repair relevant defects and rerun affected checks. Keep existing failures and environment limitations separate from regressions; do not expand the patch to make unrelated checks green.

## Completion

Report material findings, accepted fixes, verification results, and remaining limitations. A passed review is evidence about the reviewed change, not permission to publish it. Perform a commit or push only when the user has authorized that action and its exact scope, following `git-github-flow`.
