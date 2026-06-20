# Agent Job Scheduler 要件定義

## 1. 目的

Codex、Claude Code、Antigravity CLI、Copilot CLI、Cursor Agent、Devin CLI、Hermes Agent、opencode、OpenClaw、Grok CLI に対して、レートリミットを考慮しながらジョブを自動実行するローカルスケジューラを作る。ジョブは CSV で管理し、各 Agent につき未完了ジョブを古い順に 1 件ずつ消化する。

## 2. 前提

- 対象 OS は macOS
- バージョン管理する実装は `dotfiles/.agent/skills/agent-job-scheduler/apps/agent-job-scheduler/` 配下に置く
- 実行時データは `~/.agent/agent-job-scheduler/` 配下に置く
- ジョブ実行は非対話モードで行う
- 権限は全 Agent とも最強自動承認モードを使う

## 3. 用語

- `job`: 1 回の Agent 実行要求
- `ledger`: `jobs.csv` を中心としたジョブ台帳
- `adapter`: Agent ごとの差異を吸収する実行層
- `run artifact`: 1 回の実行で得られる stdout、stderr、transcript、メタデータ
- `cooldown`: Agent がレートリミットにより再実行できない期間

## 4. 機能要件

### 4.1 ジョブ登録

- 新規ジョブを CSV に追記できること
- 必須入力は少なくとも以下を含むこと
  - `created_at`
  - `scheduled_at`
  - `status`
  - `prompt`
  - `workdir`
  - `agent`
- `workdir` は絶対パスで保存すること
- `agent` は `antigravity`、`claude`、`codex`、`copilot`、`cursor`、`devin`、`hermes`、`opencode`、`openclaw`、`grok` を受け付けること

### 4.2 ジョブ台帳

ユーザー要望として、最低でも以下の情報を保持する。

- `created_at`: ジョブ作成日時
- `scheduled_at`: 実行予定日時
- `status`: 実行完了や未完了を含む状態
- `prompt`: 実行するためのプロンプト
- `workdir`: 実行対象ディレクトリ
- `conversation_hash`: Agent の chat 履歴を正規化して計算したハッシュ
- `last_response`: Agent の最終応答の最後のメッセージ
- `agent`: 実行した Agent 名

運用上は上記だけだと不足するため、初期実装では以下も追加する前提とする。

- `job_id`: 一意なジョブ ID
- `prompt_path`: 完全な prompt を保存した sidecar ファイル
- `started_at`: 実行開始時刻
- `finished_at`: 実行終了時刻
- `run_count`: 実行回数
- `next_retry_at`: 再実行可能な最短時刻
- `last_error`: 最後の失敗理由
- `result_path`: 詳細結果の保存先
- `transcript_path`: 完全な transcript の保存先

### 4.3 長文データの扱い

- `prompt` と `last_response` は CSV に保持する
- ただし長文や改行を含むデータは RFC 4180 準拠でエスケープする
- 実運用では CSV の可読性と機密性が悪化するため、完全な prompt 本文は sidecar ファイルにも保存する
- 初期既定では CSV の `prompt` は preview のみを保持し、完全な prompt は `prompt_path` を参照する
- `last_response` は CSV には全文または上限付き本文を保存し、完全版は `result_path` 側に残す

### 4.4 スケジューリング

- 実行対象は `status in {queued, retry_waiting}` かつ `scheduled_at <= now` のジョブとする
- Agent ごとに最古の未完了ジョブを 1 件選ぶ
- 同一 Agent の同時実行は行わない
- 別 Agent 間の並列実行は許可できる設計にする
- `scheduled_at` が未到来のジョブは実行しない

### 4.5 実行フロー

- 実行前に `workdir` の存在確認を行う
- 実行前に対象 Agent の cooldown 状態を確認する
- 実行中は stdout、stderr、終了コード、開始終了時刻を取得する
- 実行開始後は PID を記録し、running job の cancel や stale recovery に使えること
- 実行結果に応じて `status` を更新する
- 実行成功時は `conversation_hash` と `last_response` を確定する

### 4.6 Agent adapter 要件

#### Claude Code

- 非対話モードは `claude -p`
- 強い権限モードは `--dangerously-skip-permissions`
- 作業ディレクトリ指定フラグは見当たらないため、プロセスの `cwd` を `workdir` にする
- 必要なら `--add-dir` を追加できる設計にする

想定実行:

```bash
claude --dangerously-skip-permissions -p "$prompt"
```

#### Codex CLI

- 非対話モードは `codex exec`
- `-C/--cd` で作業ディレクトリを指定する
- 最強自動承認は `--dangerously-bypass-approvals-and-sandbox`
- `--full-auto` は採用しない。理由は `workspace-write` に留まり、要求された権限水準より弱いため

想定実行:

```bash
codex exec --dangerously-bypass-approvals-and-sandbox -C "$workdir" "$prompt"
```

#### Antigravity CLI

- 非対話寄りの入口は `agy chat --mode agent`
- `agy` は Homebrew Cask `antigravity` から提供される
- 作業ディレクトリ指定フラグはないため、プロセスの `cwd` を `workdir` にする

想定実行:

```bash
agy chat --mode agent "$prompt"
```

#### Copilot CLI

- 非対話モードは `copilot -p`
- `-C` で作業ディレクトリを指定する
- 自動実行向けに `--allow-all`、`--no-remote`、`--output-format text` を使う

想定実行:

```bash
copilot -C "$workdir" --allow-all --no-remote --output-format text -p "$prompt"
```

#### Devin CLI

- 非対話モードは `devin -p`
- 強い権限モードは `--permission-mode dangerous`
- ワークスペース信頼は `--respect-workspace-trust true` を明示する
- プロセスの `cwd` を `workdir` にする

想定実行:

```bash
devin --permission-mode dangerous --respect-workspace-trust true -p "$prompt"
```

#### Cursor Agent

- 非対話モードは `cursor-agent --print`
- `--workspace` で作業ディレクトリを指定する
- 自動実行向けに `--force` と `--trust` を使う

想定実行:

```bash
cursor-agent --workspace "$workdir" --print --force --trust "$prompt"
```

#### opencode

- 非対話モードは `opencode run`
- `--dir` で作業ディレクトリを指定する
- 強い権限モードは `--dangerously-skip-permissions`

想定実行:

```bash
opencode run --dir "$workdir" --dangerously-skip-permissions "$prompt"
```

#### Hermes Agent

- 非対話モードは `hermes -z`
- hooks 自動承認は `--accept-hooks` と `HERMES_ACCEPT_HOOKS=1`
- 強い権限モードは `--yolo`
- プロセスの `cwd` を `workdir` にする

想定実行:

```bash
HERMES_ACCEPT_HOOKS=1 hermes --accept-hooks --yolo -z "$prompt"
```

#### OpenClaw

- 非対話モードは `openclaw agent --local`
- セッションは `--session-id "agent-job-scheduler-<job_id>"` で分離する
- OpenClaw の workspace は config 側で決まるため、対象 `workdir` は prompt と process cwd の両方で渡す

```bash
openclaw agent --local --session-id "agent-job-scheduler-$job_id" --message "$prompt" --timeout 600
```

#### Grok CLI

- 非対話モードは `grok -p`
- プロセスの `cwd` を `workdir` にする

```bash
grok -p "$prompt"
```

### 4.7 レートリミット処理

- Agent ごとに独立して cooldown を管理する
- レートリミットを検出したら、そのジョブを `retry_waiting` にし、Agent 側の `blocked_until` を更新する
- `blocked_until` を過ぎるまでは、その Agent の次ジョブを起動しない
- 他 Agent のジョブは継続できる
- レートリミット解除の「hook」は、外部通知ではなく内部の wake-up 判定とする
- リセット時刻をパースできない場合は、Agent ごとの既定 backoff を使う

### 4.8 実行結果の保存

- `runs/<job_id>/<timestamp>/` 配下に以下を保存する
  - `stdout.log`
  - `stderr.log`
  - `result.txt` または `result.json`
  - `transcript.jsonl` もしくは同等の会話記録
  - `metadata.json`
- `conversation_hash` は transcript を正規化して SHA-256 で計算する

### 4.9 排他制御

- 複数のスケジューラプロセスが同時に `jobs.csv` を更新しないようにする
- 少なくともファイルロックまたは lock file を持つ
- 途中で異常終了しても、次回起動時に台帳が壊れないこと
- CSV、JSON、prompt sidecar の更新は atomic rename を使って中途半端な書き込みを避けること

### 4.10 運用インターフェース

最低でも以下の操作を提供する。

- ジョブ追加
- 1 回だけスケジューラを回す
- 常駐または定期起動でスケジューラを回す
- 現在のキュー状態を表示する
- Agent ごとの cooldown 状態を表示する

## 5. 非機能要件

- 実行失敗時も台帳整合性を保つこと
- 長文 prompt を安全に扱えること
- 結果再現のため、実行コマンドと使用 Agent を追跡できること
- 1 ジョブの失敗が他 Agent のジョブ処理を止めないこと
- ログだけで障害解析できるだけの情報を残すこと

## 6. OS 連携要件

- macOS では `launchd` を第一候補とする
- `cron` は簡易運用または移行用 fallback とする
- 定期起動間隔は短めに設定できること
- レートリミット解除待ちは、ポーリングまたは常駐 sleep のどちらでも差し替え可能にする

## 7. 実装方針

- 起動や OS 連携は zsh でよい
- CSV 操作、排他制御、実行結果整理は Python を主に使う方針とする
- 理由は、CSV の quoting、長文フィールド、lock、JSON artifact 生成を shell 単体で堅牢に扱うのが難しいため

## 8. セキュリティ上の注意

- 全 Agent を強い自動承認モードで動かすため、投入ジョブは信頼できるものに限る
- `workdir` は信頼済みパスだけを許可する allowlist を実装し、必要に応じて強制できること
- API キーや機密情報を `last_response` や log にそのまま残さないマスキング方針が必要
- 完全な prompt は sidecar に残るため、prompt 自体に秘密情報を埋め込まない運用前提を明示する

## 9. 未確定事項

- 各 Agent の transcript をどの形式で取得するか
- 各 Agent のレートリミットメッセージの実フィールド
- CSV の全文保存上限を何文字にするか
- 並列度の既定値を `3` にするか、`1` にするか
- skill からは直接実行するか、enqueue のみ許可するか
