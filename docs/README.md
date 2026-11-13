# Documentation

[日本語](README_JA.md) · [Repository README](../README.md)

Read [Getting started](getting-started.md) for a new machine. The remaining
documents are references for maintenance and configuration changes.

## Start here

- [Getting started](getting-started.md) explains supported profiles, initial
  installation, and verification.
- [Configuration ownership](configuration-ownership.md) explains where a
  change belongs before you edit it.
- [Troubleshooting](troubleshooting.md) gives symptom-based recovery steps.

## Operate and extend the environment

- [Maintenance](maintenance.md) covers updates, tests, hooks, scheduled pulls,
  and cleanup.
- [Package management and migration](package-management.md) covers Nix,
  Homebrew fallback entries, Mac App Store apps, and Brewfile migration.
- [AI agent configuration](ai-agents.md) covers managed agent files,
  synchronization, Waza evaluations, and Claude Code login profiles.
- [Agent skill publishing](agent-skills-publishing.md) explains the allowlisted
  mirror that runs only after a skill-changing pull request merges into `main`.
- [Secrets and safety](secrets-and-safety.md) covers local credentials,
  dry-runs, backups, and destructive operations.

## Directory references

These focused READMEs document the files in their own directories:

- [Configuration sources](../config/README.md)
- [Nix configuration](../config/nix/README.md)
- [Chezmoi home source](../home/README.md)
- [Helper scripts](../scripts/README.md)
- [Tests](../tests/README.md)
- [Managed dotfiles](../dotfiles/README.md)
- [AI agent files](../dotfiles/.agent/README.md)

When a command and this documentation disagree, treat the command's `--help`,
the current configuration, and the tests as authoritative. Update the relevant
document in the same change.
