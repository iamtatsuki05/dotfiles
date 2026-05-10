# AI Agent ファイル

English version: [README.md](README.md)

このディレクトリは、ローカル AI CLI agent 関連ファイルの source of truth です。

Agent CLI を内部で呼び出すコードやツールの対応状況は [AGENT_SUPPORT.md](AGENT_SUPPORT.md) にまとめています。対応 Agent を追加・削除する場合は、この matrix も更新してください。

管理対象:

- `codex`
- `claude-code`
- `copilot`
- `cursor-agent`
- `devin`
- `gemini-cli`
- `hermes`
- `opencode`
- `openclaw`

CLI 本体は `mise` で導入します。このディレクトリでは prompt、agent 別設定、MCP、hooks、skills、Waza eval suite を管理します。

## 構成

- `AGENTS.md`: 共通 prompt。対応する tool home に symlink します。リポジトリルートには `AGENTS.md` symlink を置きません。
- `apps/`: agent 別の設定ファイル。
- `hooks/`: 共通 hook script。現在は `jupytext_sync.sh`。
- `skills/`: Codex 互換 agent と Waza で使う共通 skill。
- `evals/`: skill ごとの Waza eval suite。
- `sync.sh`: `scripts/setup_agent_files.sh` への wrapper。
- `changes/`: 現在の作業メモ。ユーザー向けドキュメントではありません。

## 同期

`dotfiles/.agent/` 配下を変更したら次を実行します。

```bash
zsh dotfiles/.agent/sync.sh
```

`sync.sh` は `scripts/setup_agent_files.sh` を呼びます。各 tool home への symlink を作り、必要な agent 固有 env file を `~/.config/shell/secrets.env` から生成します。

## ファイル対応表

| Source | Destination |
|---|---|
| `AGENTS.md` | `~/.codex/AGENTS.md` |
| `AGENTS.md` | `~/.claude/CLAUDE.md` |
| `AGENTS.md` | `~/.copilot/copilot-instructions.md` |
| `AGENTS.md` | `~/.gemini/GEMINI.md` |
| `AGENTS.md` | `~/.cursor/AGENT.md` |
| `AGENTS.md` | `~/.config/opencode/AGENTS.md` |
| `AGENTS.md` | `~/.hermes/AGENTS.md` |
| `AGENTS.md` | `~/.openclaw/workspace/AGENTS.md` |
| `apps/claude/settings.json` | `~/.claude/settings.json` |
| `apps/claude/.mcp.json` | `~/.claude/.mcp.json` |
| `apps/copilot/settings.json` | `~/.copilot/settings.json` |
| `apps/copilot/mcp-config.json` | `~/.copilot/mcp-config.json` |
| `apps/codex/config.toml` | `~/.codex/config.toml` |
| `apps/codex/hooks.json` | `~/.codex/hooks.json` |
| `apps/cursor/cli-config.json` | `~/.cursor/cli-config.json` |
| `apps/cursor/mcp.json` | `~/.cursor/mcp.json` |
| `apps/devin/config.json` | `~/.config/devin/config.json` |
| `apps/gemini/settings.json` | `~/.gemini/settings.json` |
| `apps/gemini/ignore` | `~/.gemini/ignore` |
| `apps/hermes-agent/config.yaml` | `~/.hermes/config.yaml` |
| `apps/opencode/opencode.json` | `~/.config/opencode/opencode.json` |
| `apps/opencode/plugins/` | `~/.config/opencode/plugins/` |
| `apps/openclaw/openclaw.json` | `~/.openclaw/openclaw.json` |

`skills/` は各対応 agent の home に symlink します。OpenClaw では `~/.openclaw/workspace/skills` に symlink します。共通 hook は `~/.claude/hooks/`、`~/.codex/hooks/`、`~/.copilot/hooks/`、`~/.config/devin/hooks/`、`~/.gemini/hooks/`、`~/.config/opencode/hooks/`、`~/.hermes/agent-hooks/` に symlink します。

Hermes では `apps/hermes-agent/agent-hooks/` のファイルも `~/.hermes/agent-hooks/` に symlink します。

## Ignore と secrets

project-level の除外は agent の機能に合わせて分けています。

- Cursor は repo root の `.cursorignore` を使います。実体は `apps/cursor/.cursorignore` です。
- Copilot は `respectGitignore` により `.gitignore` を使います。
- Devin は `respect_gitignore` と `apps/devin/config.json` の permission deny を使います。
- Codex、Claude、Gemini、opencode、Hermes はそれぞれ app config 側で ignore または permission rule を持ちます。OpenClaw は workspace、skills、`mcp.servers` を共通設定に寄せています。ファイル単位の secret deny は、既存の共通 shell hook と OpenClaw の hook/policy 面が直接互換ではないため、現時点では移植していません。

secret はこのディレクトリには置かず、`~/.config/shell/secrets.env` に置きます。`sync.sh` は現在 `DEVIN_API_KEY` を次のファイルへ書き出します。

- `~/.gemini/.env`
- `~/.hermes/.env`

Waza の model suite は `copilot-sdk` executor を使うため、`GITHUB_TOKEN` が必要です。

## Jupyter Notebook

AI tool は `.ipynb` ではなく、ペアリングされた `.py` を編集します。対応する file edit 後に `hooks/jupytext_sync.sh` が実行され、ペアリング済み notebook を同期します。

新規 notebook をペアリングする場合:

```bash
jupytext --set-formats ipynb,py:percent notebook.py
```

## Waza

Waza は Nix の CLI package set に `dotfiles.waza` として含めています。

よく使うコマンド:

```bash
mise run waza-check
mise run waza-eval
mise run waza-eval-all
mise run waza-eval-model -- --allow
mise run waza-eval-cli-agents -- --dry-run
mise run waza-dashboard
```

model eval task を特定の CLI agent で実行する場合:

```bash
mise run waza-eval-codex -- --allow
mise run waza-eval-claude -- --allow
mise run waza-eval-gemini -- --allow
mise run waza-eval-copilot -- --allow
mise run waza-eval-devin -- --allow
mise run waza-eval-cursor -- --allow
mise run waza-eval-opencode -- --allow
mise run waza-eval-hermes -- --allow
```

AI CLI を起動せず対象 suite だけ確認する場合は `--dry-run` を使います。結果は `.waza-results/` に出力します。

## 外部 skill upstream

他人の skill を vendoring している場合は `skills/upstreams.json` で管理します。この manifest には upstream の GitHub repository、branch、固定 commit、local path、local tree hash を記録します。

よく使うコマンド:

```bash
python3 scripts/agent_skill_upstreams.py check
python3 scripts/agent_skill_upstreams.py updates
python3 scripts/agent_skill_upstreams.py update
mise run agent-skill-update
```

`update` は、デフォルトで登録済み upstream すべての最新 branch head を対象にします。review prompt を生成して選択した Agent を実行し、review report を `changes/skill-upstream-reviews/` に保存します。全 report が Critical / High finding なしで `update recommendation: approve` の場合だけ更新を適用します。

```bash
python3 scripts/agent_skill_upstreams.py update --dry-run
python3 scripts/agent_skill_upstreams.py update --review-agent gemini-cli
python3 scripts/agent_skill_upstreams.py update --id superpowers --commit <40-char-sha>
```

review agent の default は `codex` です。選択できる agent は `codex`、`claude-code`、`copilot`、`cursor-agent`、`devin`、`gemini-cli`、`hermes`、`opencode`、`openclaw` です。既定の review prompt は日本語で、`skills/review-prompts/skill-upstream-security.md` に置いています。別 prompt を使う場合は `--review-prompt <path>` を指定します。`update recommendation` などの report key は updater が読むため英語のままにしてください。

手動 review 用に、低レベルコマンドも残しています。

```bash
python3 scripts/agent_skill_upstreams.py security-prompt \
  --id superpowers \
  --review-agent codex \
  --commit <40-char-sha>
```

```bash
python3 scripts/agent_skill_upstreams.py apply-update \
  --id superpowers \
  --commit <40-char-sha> \
  --review-agent codex \
  --review-report dotfiles/.agent/changes/<review-report>.md \
  --security-reviewed
```

更新コマンドは vendored files、固定 commit、local tree hash、security review metadata を manifest に反映します。
