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
- `antigravity-cli`
- `hermes`
- `opencode`
- `openclaw`
- `grok`

CLI 本体は可能な範囲で `mise` から導入します。Herdr も `mise` で導入しますが、canonical agent ではなく terminal multiplexer / agent runtime として扱います。Antigravity CLI は Homebrew Cask `antigravity` として管理し、`agy` binary もそこから提供されます。このディレクトリでは prompt、agent 別設定、MCP、hooks、skills、Waza eval suite を管理します。

## 構成

- `AGENTS.md`: 共通 prompt。対応する tool home に symlink します。リポジトリルートには `AGENTS.md` symlink を置きません。
- `apps/`: agent 別の設定ファイル。詳細は [apps/README_JA.md](apps/README_JA.md) にまとめています。
- `hooks/`: `jupytext_sync.sh`、`agent_context_reminder.sh`、`agent_turn_done_notify.sh` などの共通 hook script。詳細は [hooks/README_JA.md](hooks/README_JA.md) にまとめています。
- `skills/`: Codex 互換 agent と Waza で使う共通 skill。階層、由来、各 skill の概要は [skills/README_JA.md](skills/README_JA.md) にまとめています。
- `evals/`: skill ごとの Waza eval suite。詳細は [evals/README_JA.md](evals/README_JA.md) にまとめています。
- `pets/`: packaged Codex pet asset。詳細は [pets/README_JA.md](pets/README_JA.md) にまとめています。
- `sync.sh`: `scripts/setup_agent_files.sh` への wrapper。
- `changes/`: 現在の作業メモ。ユーザー向けドキュメントではありません。

## 同期

`dotfiles/.agent/` 配下を変更したら次を実行します。

```bash
zsh dotfiles/.agent/sync.sh
```

`sync.sh` は `scripts/setup_agent_files.sh` を呼びます。各 tool home への symlink を作り、必要な agent 固有 env file を `~/.config/shell/secrets.env` から生成します。

## Herdr

Herdr 本体は `mise` の `github:ogulcancelik/herdr` で導入します。公式 Herdr skill は upstream license と local safety overlay 付きで `skills/herdr/` に vendoring しています。

### 通常のCLI挙動を変えずにOrcaのagent teamを起動する

Quick Start、アーキテクチャ、設定、troubleshootingは、専用の
[Agent Teamガイド](apps/agent-team/README_JA.md)にまとめています。

`agent-team`はprojectごとにOrca Runを作ります。オーケストレーションbackendはOrcaだけです。最初に起動するのは、ユーザーと対話するMainだけです。Mainは必要に応じてPlanner、Worker、Reviewer用のOrca Taskを作り、専用terminalをsupervised Dispatchとして起動します。roleごとのprovider、model、effort、prompt、permission、transportは`apps/agent-team/config.toml`と`apps/agent-team/prompts/`で管理します。launcherが起動したprocessだけにrole別設定を渡すため、通常の`claude`と`codex`の挙動は変わりません。

config version 3では、すべてのroleに`transport = "direct"`または`transport = "acp"`を明示します。欠落や未対応の値はfail-fastで拒否します。canonical teamは次のとおりです。

| Role | Provider / transport | Model / effort | Permission |
|---|---|---|---|
| Main | Claude / `direct` | `fable` / `high` | `orchestrator` |
| Planner | Claude / `acp` | `fable` / `high` | `read-only` |
| Worker | Codex / `direct` | `gpt-6-astra` / `medium` | `workspace-write` |
| Reviewer | Codex / `direct` | `gpt-6-astra` / `high` | `read-only` |

初期のACP対応は、Claudeのread-only background roleだけです。MainのACP、CodexのACP、workspace-writeのACPはfail-fastで拒否します。互換性probeで、Codex ACPの`deny-all`/`read-only`設定ではCodex internal toolの書き込みを止められないことが分かったためです。ACPのpermission制御はproviderやOSのsandboxではありません。write roleはdirect Codexのpermissionを維持します。

MainはRun限定の`agent_team` MCP serverでroleを操作します。MCP serverは固定された3 roleに対するTask作成、起動、待機、読み取り、解放、質問への返信だけを公開します。Orcaはargv配列で直接呼び出します。Claude MainにはBash toolを与えません。PlannerのACP invocationは`Read,Grep,Glob`のallowlistを使います。Reviewerはdirect Codexの組み込み`:read-only` permission profileで起動します。

ACPのpackageは`acpx@0.13.2`と`@agentclientprotocol/claude-agent-acp@0.70.0`に固定します。初回実行では`npx`によるdownload/networkが発生する可能性がありますが、global installは行いません。Claude ACPはambientな`claude.ai` loginを使い、API credentialをchildへ渡しません。subscription billing ledgerそのものは検証していません。

起動した各Codex roleは、teamのruntime state directory配下にある専用の`CODEX_HOME`を使います。通常のhomeから共有するのは`auth.json`と、存在する場合の`AGENTS.md`、`skills`だけです。通常の`config.toml`、hook、plugin、MCP serverは引き継がないため、普段のCodexとteamのtool surfaceを分離できます。

各Codex processでは、対象workspaceのGit rootを`untrusted`として指定します。Git管理外ではworkspace自体を指定します。これにより起動時のdirectory trust確認を出さず、project固有の`.codex` config、hook、実行policyは無効のままにします。通常のCodexに保存されたtrust設定は変更しません。

```bash
# role metadataとdirect argvだけを確認する。ACP argvはrole起動時に生成される。
agent-team start --dry-run

# 初回だけ、対象repositoryをOrcaへ明示的に登録する。
orca repo add --path "$PWD"

# 現在のrepository用teamを起動し、Orca上のMainへfocusする。
agent-team start

agent-team status
agent-team stop
```

MainがWorker Dispatchを起動済みの場合だけ、Workerへfocusします。

```bash
# MainがWorker Dispatchを起動済みの場合だけ実行する。
agent-team attach worker
```

既存の`start`、`status`、`attach`、`stop`コマンドは変わりません。`start`は設定した外部agent CLIを呼び出すため、provider quotaを消費する可能性があります。commit、push、publish、integration install、通常のClaude/Codex config変更は行いません。runtime stateは`$XDG_STATE_HOME/agent-team/`（既定は`~/.local/state/agent-team/`）へ保存します。同じteam stateが存在する場合、`start`はfailし、明示的な`attach`か`stop`を要求します。`stop`はこのteamが所有するterminalだけを停止し、Orca Runは監査記録として残します。

config version 3では、`workspace-write` roleはCodex providerのdirect transportに限定します。launcherはCodex組み込みの`:workspace`または`:read-only`を継承し、起動中のOrca runtime socketだけを許可するpermission profileをprocessごとに生成します。外部domainは許可しません。workspace内の通常ファイルだけを書き込み可能にし、`.git/`と`.codex/`を保護します。Mainとread-only roleではClaudeも選べます。write可能なClaude roleが指定された場合、隔離を静かに弱めず起動を拒否します。version 2のコードで起動中のteamをversion 3へ上げる場合は、先に旧コードの`agent-team stop`で停止してから切り替えてください。legacy fallbackはありません。

Herdr の integration installer は各 agent の config home を直接変更します。`sync.sh` はそれらの home をこの repo へ symlink するため、live home に対して installer を実行すると tracked config を直接汚す可能性があります。まず scratch home に生成し、差分を確認してから、必要な生成物だけをこの repo の管理ファイルとして取り込んでください。

```bash
scratch_home="$(mktemp -d)"
mkdir -p "$scratch_home/codex"
CODEX_HOME="$scratch_home/codex" herdr integration install codex
find "$scratch_home" -maxdepth 3 -type f -print
```

他の integration (`claude`, `copilot`, `devin`, `opencode`, `hermes`, `cursor`) も、対象 agent 側の documented config-home 変数を scratch home に向けて確認します。生成物を review した後、意図した変更だけを `dotfiles/.agent/apps/*` に反映し、必要なら `zsh dotfiles/.agent/sync.sh` を再実行してください。

## ファイル対応表

| Source | Destination / 役割 |
|---|---|
| `AGENTS.md` | `~/.codex/AGENTS.md` |
| `AGENTS.md` | `~/.claude/CLAUDE.md` |
| `AGENTS.md` | `~/.copilot/copilot-instructions.md` |
| `AGENTS.md` | `~/.gemini/antigravity-cli/plugins/dotfiles-agent/rules/AGENTS.md` |
| `AGENTS.md` | `~/.cursor/AGENT.md` |
| `AGENTS.md` | `~/.config/opencode/AGENTS.md` |
| `AGENTS.md` | `~/.hermes/AGENTS.md` |
| `AGENTS.md` | `~/.openclaw/workspace/AGENTS.md` |
| `AGENTS.md` | `~/.grok/AGENTS.md` |
| `../../scripts/agent-run-compact` | `~/.local/bin/agent-run-compact` |
| `apps/claude/settings.json` | `~/.claude/settings.json` |
| `apps/claude/.mcp.json` | `~/.claude/.mcp.json` |
| `apps/copilot/settings.json` | `~/.copilot/settings.json` |
| `apps/copilot/mcp-config.json` | `~/.copilot/mcp-config.json` |
| `apps/codex/config.toml` | `~/.codex/config.toml` |
| `apps/codex/hooks.json` | `~/.codex/hooks.json` |
| `../../scripts/agent-team/agent-team` | `~/.local/bin/agent-team` |
| `../../scripts/agent-team/agent_team/mcp_server.py` | 同じpackage entrypointの`agent-team _mcp-server`経由で起動する |
| `../../scripts/agent-team/agent_team/runtime.py` | packageがimportするため、managed runtime linkは作らない |
| `apps/cursor/cli-config.json` | `~/.cursor/cli-config.json` |
| `apps/cursor/hooks.json` | `~/.cursor/hooks.json` |
| `apps/cursor/mcp.json` | `~/.cursor/mcp.json` |
| `apps/devin/config.json` | `~/.config/devin/config.json` |
| `apps/antigravity-cli/settings.json` | `~/.gemini/antigravity-cli/settings.json` |
| `apps/antigravity-cli/plugins/dotfiles-agent/plugin.json` | `~/.gemini/antigravity-cli/plugins/dotfiles-agent/plugin.json` |
| `apps/antigravity-cli/plugins/dotfiles-agent/mcp_config.json` | `~/.gemini/antigravity-cli/plugins/dotfiles-agent/mcp_config.json` |
| `apps/antigravity-cli/plugins/dotfiles-agent/hooks.json` | `~/.gemini/antigravity-cli/plugins/dotfiles-agent/hooks.json` |
| `apps/hermes-agent/config.yaml` | `~/.hermes/config.yaml` |
| `apps/opencode/opencode.json` | `~/.config/opencode/opencode.json` |
| `apps/opencode/plugins/` | `~/.config/opencode/plugins/` |
| `apps/openclaw/openclaw.json` | `~/.openclaw/openclaw.json` |
| `apps/grok/config.toml` | `~/.grok/config.toml` |

`skills/` は各対応 agent の home に symlink します。Antigravity CLI では `~/.gemini/antigravity-cli/plugins/dotfiles-agent/skills` に symlink します。OpenClaw では `~/.openclaw/workspace/skills` に symlink します。Hermes だけは例外で、`~/.hermes/skills` は Hermes が所有する実ディレクトリのまま残し、bundled skill の同期、`.bundled_manifest`、hub、curator の書き込みをそこに集めます。共有 tree は `~/.hermes/dotfiles-skills` に symlink し、managed `config.yaml` の `skills.external_dirs` から読み込みます。Hermes はこの external tree を外部所有として扱いますが、ユーザー指示による `skill_manage` の編集は repository に書き込まれます。共通 hook は `~/.claude/hooks/`、`~/.codex/hooks/`、`~/.copilot/hooks/`、`~/.cursor/hooks/`、`~/.config/devin/hooks/`、`~/.gemini/antigravity-cli/hooks/`、`~/.config/opencode/hooks/`、`~/.hermes/agent-hooks/` に symlink します。

Hermes では `apps/hermes-agent/agent-hooks/` のファイルも `~/.hermes/agent-hooks/` に symlink します。

`agent_context_reminder.sh` は、Claude Code、Codex、Copilot、Cursor、Devin、Antigravity CLI、Hermes の session / prompt 系 hook で同じリポジトリ向け reminder を注入します。opencode は Claude 型の prompt hook ではなく plugin event 方式のため、plugin 経由で compaction context に同じ hook 出力を入れます。OpenClaw は bundled internal hook の `bootstrap-extra-files` で、managed workspace の `AGENTS.md` を bootstrap context として読みます。

`agent_turn_done_notify.sh` は、Claude Code、Copilot、Cursor、Devin、Antigravity CLI、Hermes、opencode の対応する turn 完了イベントに登録します。Claude Code の完了通知は `Stop` hook を使います。`Notification` は権限要求または入力 idle 通知用です。Codex は native の `notify` 設定を使い続け、同じ共有 hook も再利用できるように `~/.codex/hooks/` へ symlink します。

## Ignore と secrets

project-level の除外は agent の機能に合わせて分けています。

- Cursor は repo root の `.cursorignore` を使います。実体は `apps/cursor/.cursorignore` です。
- Copilot は `respectGitignore` により `.gitignore` を使います。
- Devin は `respect_gitignore` と `apps/devin/config.json` の permission deny を使います。
- Codex、Claude、Antigravity CLI、opencode、Cursor、Devin、Hermes はそれぞれ app config 側で ignore または permission rule を持ちます。OpenClaw は workspace、skills、bootstrap hook、`mcp.servers` を共通設定に寄せています。ファイル単位の secret deny は、既存の共通 shell hook と OpenClaw の hook/policy 面が直接互換ではないため、現時点では移植していません。

secret はこのディレクトリには置かず、`~/.config/shell/secrets.env` に置きます。`sync.sh` は現在 `DEVIN_API_KEY` を次のファイルへ書き出します。

- `~/.gemini/antigravity-cli/.env`
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
mise run waza-eval-model -- --agent all --dry-run
mise run waza-dashboard
```

model eval task を特定の CLI agent で実行する場合:

```bash
mise run waza-eval-model -- --agent codex --allow
mise run waza-eval-model -- --agent claude --allow
mise run waza-eval-model -- --agent antigravity --allow
mise run waza-eval-model -- --agent copilot --allow
mise run waza-eval-model -- --agent devin --allow
mise run waza-eval-model -- --agent cursor --allow
mise run waza-eval-model -- --agent opencode --allow
mise run waza-eval-model -- --agent hermes --allow
mise run waza-eval-model -- --agent openclaw --allow
mise run waza-eval-model -- --agent grok --allow
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

`update` は、デフォルトで登録済み upstream すべての最新 branch head を対象にします。review prompt を生成して選択した Agent を実行し、review report を `work/skill-upstream-reviews/` に保存します。全 report が Critical / High finding なしで `update recommendation: approve` の場合だけ更新を適用します。

```bash
python3 scripts/agent_skill_upstreams.py update --dry-run
python3 scripts/agent_skill_upstreams.py update --review-agent antigravity-cli
python3 scripts/agent_skill_upstreams.py update --review-agent claude-code
python3 scripts/agent_skill_upstreams.py update --id superpowers --commit <40-char-sha>
```

review agent の default は `codex` です。選択できる agent は `codex`、`claude-code`、`antigravity-cli`、`copilot`、`cursor-agent`、`devin`、`hermes`、`opencode`、`openclaw`、`grok` です。既定の review prompt は日本語で、`skills/review-prompts/skill-upstream-security.md` に置いています。別 prompt を使う場合は `--review-prompt <path>` を指定します。`update recommendation` などの report key は updater が読むため英語のままにしてください。

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
  --review-report dotfiles/.agent/work/<review-report>.md \
  --security-reviewed
```

更新コマンドは vendored files、固定 commit、local tree hash、security review metadata を manifest に反映します。
