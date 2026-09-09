# Scripts

Japanese version: [README_JA.md](README_JA.md)

This directory contains setup, migration, update, sync, and test helper scripts used by the dotfiles workflow.

## Layout

| Path | Purpose |
|---|---|
| `agent/` | Agent and Waza eval implementations. Top-level Waza scripts are compatibility wrappers. |
| `lib/` | Shared shell helper libraries used by setup scripts. |
| `utils/` | Smaller utility scripts that are not part of the primary setup path. |
| `*_install.sh` | Installation and apply entrypoints for Nix, Homebrew, MAS, and rootless Nix variants. |
| `waza_eval_*.sh` | Compatibility wrappers for Waza / agent eval entrypoints. |
| `agent-run-compact` | Opt-in command wrapper that bounds successful agent output while retaining diagnostic failure logs. |
| `agent_skill_upstreams.py` | External skill update and security review manifest tool. |
| [`agent-team/`](agent-team/README.md) | Standalone Python project for the opt-in Orca-backed team, including the package, bundled defaults, MCP entrypoint, support matrix, ACP docs, and tests. |
| `analyze_agent_delegation.py` | Aggregates Codex subagent dispatch and observed-overlap metrics without emitting prompt, response, tool-argument, or tool-output content. |
| `setup_agent_files.sh` | Canonical AI agent config, hook, skill, and pet sync script. |
| `setup_hermes_agent.sh` | Installs or updates Hermes Agent through its official shell installer, because upstream dropped pip/PyPI and Homebrew distribution. |

## Update Rules

- Keep scripts non-interactive by default where they are used from tests or automation.
- Prefer shared helpers in `lib/` for repeated shell behavior.
- Do not hard-code secrets.
- For destructive operations, keep dry-run or explicit confirmation paths.
- Update tests when changing script behavior.

## Compact Agent Output

`agent-run-compact` affects only commands explicitly passed to it. Direct human invocation remains unchanged.

```bash
agent-run-compact -- pytest tests/
agent-run-compact --verbose -- pytest tests/
```

Compact mode prints a bounded success summary, emits a low-frequency heartbeat, and removes the successful temporary log. On failure or interruption it returns the original shell status, prints diagnostic excerpts, and retains the private full log at the reported path. `--verbose` streams the child command directly without capture or wrapper summaries.

Do not wrap commands that may print secrets. Treat a retained failure log as sensitive and delete it after diagnosis.

## Common Checks

```bash
zsh tests/run.sh --syntax-only
zsh tests/run.sh
python3 scripts/agent_skill_upstreams.py check
```
