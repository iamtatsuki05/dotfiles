# Tests

Japanese version: [README_JA.md](README_JA.md)

This directory contains local and CI checks for the dotfiles repository.
`run.sh` is the main test entrypoint.

## Layout

| Path | Purpose |
|---|---|
| `run.sh` | Main test runner used locally and by CI. |
| `lib/` | Shared assertions, fixture helpers, and shell-runtime matrix helpers. |
| `test_agent_*.sh` | AI agent config, support matrix, and upstream skill checks. |
| `test_chezmoi_*.sh` | Chezmoi source state and rendered-home checks. |
| `test_multi_shell_config.sh` | Public source/render checks for Bash, Zsh, Fish, csh, and tcsh adapters. |
| `test_setup_shell.sh` | Bash-only bootstrap allowlist, preflight, dry-run, verify, and runtime checks. |
| `test_nix_migration.sh` | Nix / Homebrew migration and package configuration checks. |
| `test_dotfiles_test_runner.sh` | Test runner self-checks. |

## Update Rules

- Add focused tests when changing shared scripts, sync behavior, or generated config.
- Keep tests runnable on both macOS and Ubuntu when possible.
- Keep shell-runtime checks identity-aware: an unavailable optional runtime is
  `SKIP`, while an available runtime that fails is `FAIL`.
- Skip only when the required external tool is genuinely unavailable.
- Keep local commands aligned with `.github/workflows/`.

## Common Checks

```bash
zsh tests/run.sh
zsh tests/test_agent_sync.sh
zsh tests/test_chezmoi_rendered_home.sh
bash tests/test_setup_shell.sh
zsh tests/test_nix_migration.sh
```
