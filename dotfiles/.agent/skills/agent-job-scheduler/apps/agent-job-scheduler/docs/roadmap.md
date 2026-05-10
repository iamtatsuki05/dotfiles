# Agent Job Scheduler 実装ロードマップ

## 現在地

2026-04-19 時点で、Phase 1 から Phase 7 の基礎部分までは実装済みです。現状の主な残作業は、実 Agent 出力に合わせたレートリミット実測、マスキング拡充、`launchd` の長時間運用検証です。

## Phase 0: 設計固め

目的は、配置場所、責務分離、CLI 仕様差分を確定することです。

- `dotfiles/.agent/skills/agent-job-scheduler/apps/agent-job-scheduler/` を初期配置として作成する
- README、要件定義、ロードマップを作成する
- 各 Agent の非対話実行と作業ディレクトリ指定方法を確認する

完了条件:

- 実装前提が文書で合意できる状態になっている

## Phase 1: 最小スキャフォールド

目的は、手でジョブを投入して 1 回実行できる最小骨格を作ることです。

- `bin/` に操作用エントリポイントを作る
- `src/` に scheduler 本体を置く
- `~/.agent/agent-job-scheduler/` に runtime ディレクトリを初期化する処理を作る
- `jobs.csv` の初期ヘッダとサンプルを用意する

完了条件:

- `enqueue`、`run-once`、`status` の 3 コマンドが最低限動く

## Phase 2: CSV 台帳と排他制御

目的は、ジョブ台帳を壊さず安全に更新できるようにすることです。

- CSV 読み書きレイヤを実装する
- `job_id` 生成と状態遷移を定義する
- lock file または OS ロックで二重起動を防ぐ
- 長文 prompt と結果本文の sidecar 保存を実装する

完了条件:

- 異常終了しても `jobs.csv` が壊れない
- 同時起動時に二重実行されない

## Phase 3: Agent adapter 実装

目的は、repo 管理対象の AI agent CLI を同じインターフェースで扱えるようにすることです。

- `codex` adapter を実装する
- `claude` adapter を実装する
- `copilot` adapter を実装する
- `cursor` adapter を実装する
- `devin` adapter を実装する
- `gemini` adapter を実装する
- `hermes` adapter を実装する
- `opencode` adapter を実装する
- `workdir`、prompt、モデル指定、結果保存を共通化する
- stdout、stderr、終了コードの収集を統一する

完了条件:

- 対応 Agent すべてで手動投入したジョブが 1 件ずつ成功する

## Phase 4: レートリミット対応

目的は、Agent ごとに止めて、Agent ごとに再開できるようにすることです。

- Agent ごとのレートリミット検出ロジックを追加する
- `agent_state.json` に `blocked_until` と失敗理由を保存する
- `retry_waiting` 状態のジョブ再開を実装する
- パースできない場合の既定 backoff を実装する

完了条件:

- 1 Agent がレートリミットになっても、他 Agent のジョブは継続する
- `blocked_until` 経過後に対象 Agent のジョブが再開される

## Phase 5: 常駐化と macOS 連携

目的は、手動起動なしでスケジューラを回し続けることです。

- `launchd` 用の `plist` テンプレートを作る
- 短周期ポーリング方式を先に実装する
- 必要なら常駐 sleep 型 supervisor に切り替えられるようにする
- `cron` fallback の設定例も用意する

完了条件:

- ログイン後に自動でスケジューラが動く
- 手作業なしで due job が消化される

## Phase 6: Skill 追加

目的は、Codex などからこのツールを安全に使えるようにすることです。

- `dotfiles/.agent/skills/agent-job-scheduler/` を追加する
- skill から以下を扱えるようにする
  - ジョブ追加
  - ジョブ一覧確認
  - 直近失敗確認
  - cooldown 状態確認
- 初期版では直接実行より enqueue を優先する

完了条件:

- Agent から自然言語でジョブ投入と状況確認ができる

## Phase 7: 品質強化

目的は、実運用に耐える堅牢性を確保することです。

- 単体テストを追加する
- Agent adapter の統合テストを追加する
- dry-run モードを追加する
- ログ整形とサマリ出力を追加する
- シークレットのマスキングを追加する

完了条件:

- 主要な状態遷移とレートリミット分岐がテストで保証される

## 直近の優先順

次に着手すべき順番は以下です。

1. 対応 Agent 各種の実出力に合わせて `rate_limit_profiles` を実測ベースで補正する
2. 実運用ログをもとに秘密情報マスキングのパターンを拡張する
3. `launchd` の長時間運用で stuck job や再開挙動を検証する
4. 必要なら dry-run や rate limit profile 編集用 CLI を追加する
