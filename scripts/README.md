# Scripts

Japanese version: [README_JA.md](README_JA.md)

This directory contains setup, migration, update, sync, and test helper scripts used by the dotfiles workflow.

## Layout

| Path | Purpose |
|---|---|
| `lib/` | Shared shell helper libraries used by setup scripts. |
| `utils/` | Smaller utility scripts that are not part of the primary setup path. |
| `*_install.sh` | Installation and apply entrypoints for Nix, Homebrew, MAS, and rootless Nix variants. |
| `*_eval_*.sh` | Waza / agent eval wrappers. |
| `agent_skill_upstreams.py` | External skill update and security review manifest tool. |
| `setup_agent_files.sh` | Canonical AI agent config, hook, skill, and pet sync script. |

## Update Rules

- Keep scripts non-interactive by default where they are used from tests or automation.
- Prefer shared helpers in `lib/` for repeated shell behavior.
- Do not hard-code secrets.
- For destructive operations, keep dry-run or explicit confirmation paths.
- Update tests when changing script behavior.

## Common Checks

```bash
bash -n scripts/*.sh scripts/lib/*.sh scripts/utils/*.sh
zsh tests/run.sh
python3 scripts/agent_skill_upstreams.py check
```
