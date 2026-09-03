# Shared Hooks

Japanese version: [README_JA.md](README_JA.md)

This directory contains shared hook scripts used by multiple local AI agents.
`dotfiles/.agent/sync.sh` links these scripts into agent-specific hook locations.

## Hooks

| Hook | Purpose |
|---|---|
| `agent_context_reminder.sh` | Emits repository-specific reminder context for supported agent prompt or session hook phases. |
| `agent_turn_done_notify.sh` | Plays the shared completion sound for agents that support end-of-turn notifications. |
| `japanese_prose_lint.sh` | Checks Japanese Markdown and text prose, and returns advisory feedback after supported file edits. In-place edits (Edit / MultiEdit / apply_patch) report only lines the edit introduced, so document-wide counts such as repeated terms surface only when their first occurrence is on an edited line; whole-file writes lint everything. Files under `.agent/work/` are skipped. |
| `jupytext_sync.sh` | Keeps paired Jupyter notebooks synchronized after agents edit paired `.py` files. |

Agent-specific hook registration lives under `../apps/`.
Some agents use JSON hook maps, while others consume shell scripts from hook directories.
Japanese prose lint runs automatically for all supported agents. Claude Code, Codex, Copilot, Cursor, Devin, and Antigravity use native post-tool hooks. Hermes, OpenCode, and OpenClaw use small plugins so findings are included in the next model-visible tool result. Grok loads the Claude-compatible hook configuration and therefore does not need a duplicate registration.

## Japanese prose lint

Run the shared profile against Markdown or plain text:

```bash
dotfiles/.agent/hooks/japanese_prose_lint.sh --check path/to/document.md
```

Use `--profile longform` to additionally flag document-progress narration in articles and essays. Findings are advisory and never rewrite files. Hook feedback is capped at 20 findings per edit; rerunning the lint reveals any remaining findings. The hook ignores Markdown frontmatter, fenced and indented code, blockquotes, inline code, link destinations, autolinks, and raw ASCII URLs. Exit status is `0` for a clean document, `1` for findings, and `2` for invalid input or unreadable files.

Agent adapters are selected explicitly with `--hook-agent`; payload-shape detection and silent compatibility fallbacks are intentionally avoided. OpenClaw must be restarted after its plugin or plugin configuration changes. Codex may require reviewing the changed hook definition in `/hooks` before it becomes trusted.

| Rule | Detection |
|---|---|
| `JP001` | Repeated full-width dashes. |
| `JP002` | Decorative emoji. |
| `JP003` | Stacked dramatic fragments such as "simple; that's all." |
| `JP004` | Two or more unnecessary-looking `AではなくB` contrasts. |
| `JP005` | Two or more vague uses of `効く`. |
| `JP006` | The same recognized sentence ending three times in one paragraph. |
| `JP007` | At least three polite and three plain endings in one document. |
| `JP008` | Document-progress narration under the `longform` profile. |

## Update Rules

- Keep hook behavior tool-agnostic when it is shared by multiple agents.
- Do not put secrets in hook scripts.
- Validate shell syntax after edits.
- When changing hook outputs, check both the script and the agent config that invokes it.
- If a hook parses JSON from an agent, test a representative payload with `python3 -m json.tool`.

## Common Checks

```bash
bash -n dotfiles/.agent/hooks/agent_context_reminder.sh
bash -n dotfiles/.agent/hooks/agent_turn_done_notify.sh
bash -n dotfiles/.agent/hooks/japanese_prose_lint.sh
bash -n dotfiles/.agent/hooks/jupytext_sync.sh
zsh tests/test_japanese_prose_lint.sh
printf '{}' | dotfiles/.agent/hooks/agent_context_reminder.sh | python3 -m json.tool
zsh tests/test_agent_sync.sh
```
