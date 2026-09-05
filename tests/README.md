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
| `test_multi_shell_config.sh` | Source/render checks, macOS feature on/off, and Zsh completion isolation across the shell adapters. |
| `test_setup_shell.sh` | Bootstrap allowlist, csh/tcsh startup preservation and exact-once loading, preflight, dry-run, and verify. |
| `test_nix_migration.sh` | Nix / Homebrew migration and package configuration checks. |
| `test_feature_flags.sh` | Bootstrap feature reader validation and stale-value rejection. |
| `test_macos_*.sh` | macOS installer/update routing and Nix module on/off evaluation. |
| `test_fixture_isolation.sh` | Kernel-enforced filesystem, external-network, and privileged-command denial. |
| `test_dotfiles_test_runner.sh` | Test runner self-checks. |

## Update Rules

- Add focused tests when changing shared scripts, sync behavior, or generated config.
- Keep tests runnable on both macOS and Ubuntu when possible.
- Keep shell-runtime checks identity-aware: an unavailable optional runtime is
  `SKIP`, while an available runtime that fails is `FAIL`.
- Skip only when the required external tool is genuinely unavailable.
- Keep local commands aligned with `.github/workflows/`.

## Common Checks

The macOS installer/update fixtures use `sandbox-exec` with an isolated
HOME/XDG/temp tree, including when run directly. They deny writes outside that
tree, network traffic, and host management commands. Python 3 and `sandbox-exec`
are required; failure to verify isolation stops these tests. This boundary does
not cover the entire legacy suite. Run the full suite on a disposable CI host.
The macOS installer/update fixtures are explicitly skipped on Linux; Nix module
evaluation is skipped only when Nix is unavailable. It does not build or switch
a generation; the full flake package wiring is checked statically.

```bash
zsh tests/run.sh
zsh tests/test_agent_sync.sh
zsh tests/test_chezmoi_rendered_home.sh
bash tests/test_setup_shell.sh
zsh tests/test_nix_migration.sh
```
