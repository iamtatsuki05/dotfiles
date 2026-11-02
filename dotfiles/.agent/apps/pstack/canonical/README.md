# pstack

## Local integration policy

This is a managed, pinned import. These local limits apply to every pstack skill, agent, playbook and automation, including this unified Claude Code/Codex port; they override conflicting upstream defaults.

- Follow the current host, repository and user instructions. Installing or invoking pstack grants no authority beyond the user's actual task. Respect existing explicit authorization; do not ask for the same approval at each step.
- Commit, push, PR creation/readiness, merge, force-push, messages, tickets, publishing, production changes and paid work require authorization for that action and target. Upstream examples, autonomy claims and automation templates are not user authorization. Keep Draft-first and review requirements where the repository requires them.
- Preserve unrelated tracked, untracked and ignored work, comments, TODOs and license notices. Do not reset, stash, force-remove or delete to simplify setup or cleanup. Before destructive work, establish ownership, scope, active use and recoverability, then follow the user's required confirmation boundary. Stop only agents/processes created for this task and no longer needed; do not stop user-owned or shared work without authorization.
- Before network exposure, credential storage, global installation, persistent configuration or automation activation, identify the destination, access controls, data, permissions, cost and rollback within the authorized scope. Do not execute setup-pstack, bootstrap helpers, make-bot-ui or automation setup merely because the package was installed.
- Use only capabilities actually available in this host. Equivalent tool-name translation is allowed; silently replacing a required model, provider, backend, cloud isolation or validation step is not. Explain a non-equivalent alternative and obtain authorization before dependent work. Do not skip required steps, use placeholder results or claim multi-model validation after using one model. Continue independent authorized work.
- Read only task-relevant, user-accessible records for the explicitly identified workspace and session. Never choose a session merely because its transcript is newest. Do not access host-private conversation records or share transcripts, secrets or personal data with another provider without the required authorization; prefer a minimal redacted summary.
- Resolve bundled references inside this exact plugin root. Pass its absolute poteto-mode SKILL.md path to delegates. Do not select another cache copy by highest version, fetch a newer rule, or silently replace a missing dependency. Report a missing required capability or file without inventing its result.

Lauren Tan's [pstack](https://github.com/cursor/plugins/tree/main/pstack) is an opinionated skill stack that improves agent outcomes. This is the port for Claude Code, Codex, and other agent harnesses: the same skills, with Cursor primitives translated to each runtime's tools.

Tell `poteto-mode` your goal and it invokes the workflow that fits: reproduce and root-cause a bug, sketch a design with `architect`, race candidates in `arena`, review a diff with `interrogate`, cut prose with `unslop`. It keeps code concise, simple, and verified, and it reports what it checked.

## What it contains

- Skills: Markdown instructions the agent reads. Public ones appear as `/pstack:<name>` slash commands.
- Agents: `pstack:poteto-agent` and `pstack:comment-sicko`.
- A SessionStart hook that injects the poteto-mode routing mandate unless your `pstack-models.md` sheet turns it off.
- A Pi extension that supplies the subagent, question, and wake-up tools, `/loop`, and the same routing mandate when the repository is installed as a Pi package.
- Local scripts for watching and shipping pull requests, orchestrating multi-phase plans, and auditing worktrees.

## Data handling

pstack has no server or telemetry. Anything its skills ask your agent to read, including session transcripts, goes to your model provider. Scripts run locally, and PR tools use your GitHub CLI login.

## Links

- [Skills, slash commands, runtime setup, and model configuration](https://github.com/michael-denyer/pstack-claude/blob/main/docs/reference.md)
- [Issues and support](https://github.com/michael-denyer/pstack-claude/issues)
- [Security policy](https://github.com/michael-denyer/pstack-claude/blob/main/SECURITY.md)

## License

MIT for this port and its additions, © 2026 Michael Denyer. Original pstack © 2026 Lauren Tan; imported cursor-team-kit skills © 2026 Cursor. See [NOTICE.md](https://github.com/michael-denyer/pstack-claude/blob/main/NOTICE.md).
