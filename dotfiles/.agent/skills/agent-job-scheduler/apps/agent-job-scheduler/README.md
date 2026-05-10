# Agent Job Scheduler

Codex、Claude Code、Copilot CLI、Cursor Agent、Devin CLI、Gemini CLI、Hermes Agent、opencode、OpenClaw を対象にした、レートリミット考慮付きのジョブスケジューラです。ジョブは CSV 台帳で管理し、各 Agent について未完了ジョブを古い順に 1 件ずつ実行します。ある Agent がレートリミットに入った場合はその Agent だけを停止し、リセット時刻以降に自動で再開する前提です。

現在は、runtime 初期化、CSV 台帳、prompt sidecar、atomic write、`enqueue`、`run-once`、`status`、`show`、`retry`、`cancel`、`requeue`、`active-runs`、allowlist、stale recovery、`launchd` 連携まで入った実用初版です。

内部実装は、モデル層と設定層を `pydantic`、CLI 境界を `fire` ベースに寄せています。既存の `run-once` や `show-config` のような hyphen 付きコマンド名はそのまま使えます。

## この場所に置く理由

- `dotfiles/.agent/` 配下は Agent 関連の設定、hooks、skills をまとめて管理している
- 今回のツールも Agent 運用基盤の一部なので、同じ階層に置くのが自然
- 将来的に `dotfiles/.agent/skills/agent-job-scheduler/` からこのアプリを呼び出しやすい

## 推奨する責務分離

- バージョン管理するコードと設計資料: `dotfiles/.agent/skills/agent-job-scheduler/apps/agent-job-scheduler/`
- ローカルの可変データ: `~/.agent/agent-job-scheduler/`

可変データには、以下を置く想定です。

- `jobs.csv`: ジョブ台帳
- `agent_state.json`: Agent ごとのレートリミット状態や次回再開時刻
- `active_runs.json`: 実行中プロセスの PID とメタデータ
- `settings.json`: allowlist や stale recovery の設定
- `prompts/`: enqueue 時点の完全な prompt 本文
- `runs/`: 実行ごとの transcript、stdout、stderr、メタデータ
- `logs/`: スケジューラ本体のログ

`jobs.csv` を git 管理対象のリポジトリ内に直接置くと、実行結果や履歴で差分が荒れやすいため、実運用ではホーム配下の隠しディレクトリに逃がす前提にしています。

## ローカル CLI 仕様の確認結果

2026-04-19 時点で、ローカルの `--help` 出力を確認した結果です。

### Claude Code

- 非対話実行は `claude -p "$prompt"`
- 強い権限モードは `--dangerously-skip-permissions`
- 作業ディレクトリを直接指定する `--cd` 相当は見当たらない
- そのため、スケジューラ側で subprocess の `cwd` を `job.workdir` にして起動する
- 追加の参照先が必要な場合は `--add-dir` を併用できる

想定コマンド:

```bash
claude --dangerously-skip-permissions -p "$prompt"
```

### Codex CLI

- 非対話実行は `codex exec "$prompt"`
- `-C, --cd <DIR>` があるため、作業ディレクトリを明示できる
- `--full-auto` は `workspace-write` の自動承認だが、最強権限ではない
- 最強権限相当は `--dangerously-bypass-approvals-and-sandbox`

想定コマンド:

```bash
codex exec --dangerously-bypass-approvals-and-sandbox -C "$workdir" "$prompt"
```

### Gemini CLI

- 非対話実行は `gemini -p "$prompt"`
- 強い権限モードは `--yolo` または `--approval-mode yolo`
- headless 実行で trusted directory 判定に止められないように `--skip-trust` を使う
- Claude と同様に、作業ディレクトリを直接指定する `--cd` 相当は見当たらない
- そのため、スケジューラ側で subprocess の `cwd` を `job.workdir` にして起動する
- 必要であれば `--include-directories` を追加で使える

想定コマンド:

```bash
gemini -m gemini-3.1-pro-preview --yolo --skip-trust -p "$prompt"
```

### Copilot CLI

- 非対話実行は `copilot -p "$prompt"`
- 作業ディレクトリは `-C "$workdir"` で指定する
- 自動実行向けに `--allow-all`、`--no-remote`、`--output-format text` を使う

想定コマンド:

```bash
copilot -C "$workdir" --allow-all --no-remote --output-format text -p "$prompt"
```

### Devin CLI

- 非対話実行は `devin -p "$prompt"`
- 強い権限モードは `--permission-mode dangerous`
- ワークスペース信頼は `--respect-workspace-trust true` を明示する
- スケジューラ側で subprocess の `cwd` を `job.workdir` にして起動する

想定コマンド:

```bash
devin --permission-mode dangerous --respect-workspace-trust true -p "$prompt"
```

### Cursor Agent

- 非対話実行は `cursor-agent --print "$prompt"`
- 作業ディレクトリは `--workspace "$workdir"` で指定する
- 自動実行向けに `--force` と `--trust` を使う

想定コマンド:

```bash
cursor-agent --workspace "$workdir" --print --force --trust "$prompt"
```

### opencode

- 非対話実行は `opencode run "$prompt"`
- 作業ディレクトリは `--dir "$workdir"` で指定する
- 強い権限モードは `--dangerously-skip-permissions`

想定コマンド:

```bash
opencode run --dir "$workdir" --dangerously-skip-permissions "$prompt"
```

### Hermes Agent

- 非対話実行は `hermes -z "$prompt"`
- hooks を自動承認するため `--accept-hooks` と `HERMES_ACCEPT_HOOKS=1` を併用する
- 強い権限モードは `--yolo`
- スケジューラ側で subprocess の `cwd` を `job.workdir` にして起動する

想定コマンド:

```bash
HERMES_ACCEPT_HOOKS=1 hermes --accept-hooks --yolo -z "$prompt"
```

### OpenClaw

- 非対話実行は `openclaw agent --local --message "$prompt"`
- セッション衝突を避けるため `--session-id "agent-job-scheduler-<job_id>"` を指定する
- OpenClaw の実ツール実行 workspace は OpenClaw config 側の `agents.defaults.workspace` に依存するため、prompt 内で対象 `workdir` を明示する

想定コマンド:

```bash
openclaw agent --local --session-id "agent-job-scheduler-$job_id" --message "$prompt" --timeout 600
```

## 実行モデルの初期方針

- ジョブは `created_at` 昇順で処理する
- 同一 Agent の同時実行数は常に 1
- Agent 間は並列実行可能にする余地を残す
- `workdir` はジョブごとに必須の絶対パスとする
- 実行後は transcript のハッシュ、最後の応答、終了時刻、実行 Agent を記録する
- レートリミット時は、その Agent の `next_retry_at` を保存して再開待ちにする

## 「hook」での再開について

外部サービス側から「レートリミットが解除された」という push 型 hook を受ける前提は、現時点では置きません。代わりに、スケジューラ自身が次回再開時刻を保持し、その時刻以降に再度ジョブ取得を試みる方式にします。

macOS 前提なら、実運用の第一候補は `launchd` です。`cron` でも最小構成は作れますが、常駐管理、再起動制御、ログ、ユーザーセッションとの相性まで含めると `launchd` の方が自然です。初期 PoC では `cron` 互換も残せます。

## 今回用意した文書

- [要件定義](docs/requirements.md)
- [実装ロードマップ](docs/roadmap.md)

## 現在の CLI

`uv run` またはラッパースクリプトから使えます。

```bash
dotfiles/.agent/skills/agent-job-scheduler/apps/agent-job-scheduler/bin/agent-job-scheduler status
dotfiles/.agent/skills/agent-job-scheduler/apps/agent-job-scheduler/bin/agent-job-scheduler enqueue --agent codex --workdir /abs/path --prompt "調査して修正してください"
dotfiles/.agent/skills/agent-job-scheduler/apps/agent-job-scheduler/bin/agent-job-scheduler run-once
dotfiles/.agent/skills/agent-job-scheduler/apps/agent-job-scheduler/bin/agent-job-scheduler show <job_id>
dotfiles/.agent/skills/agent-job-scheduler/apps/agent-job-scheduler/bin/agent-job-scheduler retry <job_id>
dotfiles/.agent/skills/agent-job-scheduler/apps/agent-job-scheduler/bin/agent-job-scheduler cancel <job_id>
dotfiles/.agent/skills/agent-job-scheduler/apps/agent-job-scheduler/bin/agent-job-scheduler requeue <job_id>
dotfiles/.agent/skills/agent-job-scheduler/apps/agent-job-scheduler/bin/agent-job-scheduler active-runs
dotfiles/.agent/skills/agent-job-scheduler/apps/agent-job-scheduler/bin/agent-job-scheduler show-config
dotfiles/.agent/skills/agent-job-scheduler/apps/agent-job-scheduler/bin/agent-job-scheduler set-stale-running-timeout 600
```

### 現在の実装内容

- runtime ディレクトリの自動初期化
- `jobs.csv`、`agent_state.json`、`active_runs.json` の管理
- `settings.json` による allowlist と stale recovery 設定
- enqueue 時の prompt sidecar 保存と CSV 上の preview 保持
- モデル層と設定層の `pydantic` ベース化
- `fire` ベースの CLI と既存 hyphen command 互換
- file lock による最低限の排他制御
- atomic rename による ledger / settings / prompt の安全な書き込み
- Agent ごとに 1 件ずつの due job 実行
- 実行結果の artifact 保存
- transcript 相当テキストのハッシュ計算
- stdout / stderr からの簡易レートリミット検出
- `launchd` 用 plist の生成と install / uninstall スクリプト
- stale `running` ジョブの自動回収
- `retry` / `cancel` / `requeue` / `show` / `active-runs`
- `running` ジョブの PID 追跡と cancel 時の停止
- 出力ログと artifact の最低限の秘密情報マスキング

### まだ未実装のもの

- 実 Agent 各種の本番出力を使ったレートリミット実測チューニング
- CLI 固有 transcript の高精度収集
- 実運用ベースのマスキング拡充
- 長時間の `launchd` soak test

## launchd 連携

常駐 daemon はまだ入れていませんが、`launchd` から `run-once` を 60 秒間隔で叩く構成は使えます。rate limit 中の Agent は `agent_state.json` を見て自動的にスキップされるため、周期実行だけでも十分回ります。

### plist を出力する

```bash
dotfiles/.agent/skills/agent-job-scheduler/apps/agent-job-scheduler/bin/agent-job-scheduler print-launchd-plist
```

### LaunchAgent をインストールする

```bash
dotfiles/.agent/skills/agent-job-scheduler/apps/agent-job-scheduler/scripts/install_launch_agent.sh
```

主なオプション:

- `--runtime-root /abs/path`
- `--interval-seconds 60`
- `--label io.github.iamtatsuki05.agent-job-scheduler`
- `--output /abs/path/to/custom.plist`
- `--no-load`

`--no-load` を付けると plist を生成するだけで `launchctl bootstrap` は行いません。

### LaunchAgent をアンインストールする

```bash
dotfiles/.agent/skills/agent-job-scheduler/apps/agent-job-scheduler/scripts/uninstall_launch_agent.sh
```

主なオプション:

- `--label io.github.iamtatsuki05.agent-job-scheduler`
- `--output /abs/path/to/custom.plist`
- `--no-unload`

### 生成される挙動

- `RunAtLoad = true`
- `StartInterval = 60` 秒が既定
- 実行コマンドは `bin/agent-job-scheduler --runtime-root ~/.agent/agent-job-scheduler run-once`
- `stdout` / `stderr` は `~/.agent/agent-job-scheduler/logs/launchd.*.log` に保存

## 運用コマンド

### ジョブ詳細を見る

```bash
dotfiles/.agent/skills/agent-job-scheduler/apps/agent-job-scheduler/bin/agent-job-scheduler show <job_id> --tail-lines 20
```

`result.txt` と `transcript.txt` の末尾もあわせて見られます。

完全な prompt 本文を見たい場合だけ `--include-prompt` を付けてください。通常は CSV に保存された preview のみを返します。

### failed / retry_waiting を再投入する

```bash
dotfiles/.agent/skills/agent-job-scheduler/apps/agent-job-scheduler/bin/agent-job-scheduler retry <job_id>
```

### 既存ジョブを複製して新しいジョブを積む

```bash
dotfiles/.agent/skills/agent-job-scheduler/apps/agent-job-scheduler/bin/agent-job-scheduler requeue <job_id>
```

### キュー上のジョブを止める

```bash
dotfiles/.agent/skills/agent-job-scheduler/apps/agent-job-scheduler/bin/agent-job-scheduler cancel <job_id>
```

`running` のジョブは、記録済み PID を使って停止を試みたうえで `cancelled` に遷移させます。実際にはプロセスグループ単位で `SIGTERM`、必要なら `SIGKILL` を送ります。

### 実行中プロセスを見る

```bash
dotfiles/.agent/skills/agent-job-scheduler/apps/agent-job-scheduler/bin/agent-job-scheduler active-runs
```

`job_id`、Agent 名、PID、開始時刻、`workdir`、実行コマンドを JSON で確認できます。

## allowlist と stale recovery

`settings.json` に以下を持ちます。

- `allowed_workdirs`
- `enforce_workdir_allowlist`
- `stale_running_timeout_seconds`
- `store_prompt_body_in_csv`
- `prompt_preview_chars`
- `rate_limit_profiles`

初期状態では allowlist enforcement は無効です。有効にすると、allowlist に登録されたパス配下でしか enqueue できません。

```bash
dotfiles/.agent/skills/agent-job-scheduler/apps/agent-job-scheduler/bin/agent-job-scheduler allow-workdir /abs/path
dotfiles/.agent/skills/agent-job-scheduler/apps/agent-job-scheduler/bin/agent-job-scheduler set-allowlist-enforcement on
dotfiles/.agent/skills/agent-job-scheduler/apps/agent-job-scheduler/bin/agent-job-scheduler set-stale-running-timeout 600
dotfiles/.agent/skills/agent-job-scheduler/apps/agent-job-scheduler/bin/agent-job-scheduler list-allowed-workdirs
dotfiles/.agent/skills/agent-job-scheduler/apps/agent-job-scheduler/bin/agent-job-scheduler show-config
```

また、`running` のまま一定時間更新されないジョブは `retry_waiting` に自動回収されます。既定値は 1800 秒です。`active_runs.json` に PID が残っていても、その PID が消えていれば即座に回収します。

## 将来の実装対象

- CSV 台帳と実行アーティファクトのフォーマット確定
- Agent ごとの adapter 実装
- レートリミット検出と再開制御
- `launchd` 連携
- ジョブ投入や状態確認を行う skill

## 注意

このツールは、全 Agent を強い自動承認モードで動かす前提です。誤った `workdir` や危険な `prompt` を投入すると、ユーザー確認なしでファイル変更やコマンド実行が走ります。対象ディレクトリと投入権限は、要件と実装の両方で明示的に制御する必要があります。

artifact には最低限の秘密情報マスキングを入れていますが、完全ではありません。完全な prompt は `prompts/<job_id>.txt` に保存されるため、機密値を prompt に直接埋め込む運用は避けてください。既定では `jobs.csv` には preview だけを保存します。
