# RTK integration provenance

[RTK](https://github.com/rtk-ai/rtk) is installed by mise as
`github:rtk-ai/rtk@0.51.0`. The following plugin files are copied unchanged from
release `v0.51.0`, commit `e001f773f80b22b7dc4c7a79521b30e35aaef026`, under the
[Apache-2.0 license](LICENSE):

- `../opencode/plugins/rtk.ts` — SHA-256 `6530c131946c84892f9522abd68d4e513e1e658d8ddbad1f59388c86ebbcb6bb`
- `../hermes-agent/plugins/rtk-rewrite/__init__.py` — SHA-256 `1c211b6248d9277fed7d615faa7287db5174462c92efcecff3ed1165af81d5bb`
- `../hermes-agent/plugins/rtk-rewrite/plugin.yaml` — SHA-256 `2f285a22ad9958ef0084c75a1b42ea350139bf35a7c3835626d93abac84c601a`

- `../openclaw/extensions/rtk-rewrite/index.ts` — SHA-256 `43f104b5a44f10ffd8143de891a3f6469ceb58a178fe101c4f5943a9ff7d21e4`
- `../openclaw/extensions/rtk-rewrite/openclaw.plugin.json` — SHA-256 `2203e8f992a30cdec6192e17ea48fdf82d793cd482097d764292444c3d5618c5`
The native Claude Code, Codex, Copilot, Cursor, and Antigravity hook entries
match the same release's `rtk init` output. Antigravity uses its current global
plugin path, `~/.gemini/config/plugins/rtk/`. Optional awareness prompts from
`rtk init` are not installed; no prompt is needed for these native hooks.
The Copilot hook uses the upstream standalone `hooks/rtk-rewrite.json` file.
No shared prompt, permission allowlist, or sandbox setting is changed.

The official Hermes, OpenCode, and OpenClaw plugins invoke `rtk rewrite` and leave commands
unchanged when no rewrite is available. They do not execute the target command;
the agent still performs execution. This is upstream plugin behavior, not a
second runner. Keep these files byte-for-byte upstream copies, including any
license or attribution. When upgrading RTK, update both mise declarations and
these plugin files together, review the new behavior, and rerun the integration
checks. Generate `rtk init` output in a scratch home: live homes may contain
symlinks into the repository.

Permission checks can see the rewritten `rtk ...` command, especially in
Codex, Antigravity, and OpenClaw. Command-specific rules may therefore prompt
or deny differently; no broad `rtk *` allow rule is added by this setup.
