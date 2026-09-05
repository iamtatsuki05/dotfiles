# Agent Team user configuration

This directory contains the dotfiles-managed user configuration and Japanese
role prompts for the standalone `scripts/agent-team` project. The executable,
architecture documentation, support matrix, and ACP documentation live in
[`scripts/agent-team/README.md`](../../../../scripts/agent-team/README.md).

The setup sync links this directory to `$XDG_CONFIG_HOME/agent-team` (normally
`~/.config/agent-team`). Keep `config.toml` and `prompts/` here when customizing
the team; do not edit the bundled package defaults directly.

`teams.toml` is the named-team catalog. Its `agent-team` entry explicitly uses
`config.toml`; select it with `--config ~/.config/agent-team/teams.toml --team agent-team`.
