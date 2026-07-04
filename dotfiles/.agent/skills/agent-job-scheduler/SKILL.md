---
name: agent-job-scheduler
description: "Use when the user wants to queue, inspect, retry, cancel, or run long non-interactive AI agent CLI jobs through agent-job-scheduler, especially when rate limits, cooldowns, failed jobs, or background execution state matter."
---

# Agent Job Scheduler

`dotfiles/.agent/skills/agent-job-scheduler/` 配下を skill パッケージとして扱い、その中の `apps/`、`assets/`、`references/` を使ってジョブ管理を行う。

## 使う場面

- 今すぐ対話実行せず、後で Agent に処理させたい
- Codex、Claude Code、Antigravity CLI、Copilot CLI、Cursor Agent、Devin CLI、Hermes Agent、opencode、OpenClaw、Grok CLI のジョブをキュー管理したい
- レートリミット中の Agent を避けて実行状況を見たい
- ジョブの enqueue、status、run-once を行いたい
- launchd で周期実行させたい
- failed ジョブを retry / cancel / requeue したい
- allowlist や stale running recovery の設定を触りたい
- 実行中 PID を確認したい

## 実行場所

- skill パッケージ: `dotfiles/.agent/skills/agent-job-scheduler/`
- app 実装: `dotfiles/.agent/skills/agent-job-scheduler/apps/agent-job-scheduler/`
- 実行コマンド: `dotfiles/.agent/skills/agent-job-scheduler/apps/agent-job-scheduler/bin/agent-job-scheduler`
- 運用リファレンス: `dotfiles/.agent/skills/agent-job-scheduler/references/`
- サンプル資産: `dotfiles/.agent/skills/agent-job-scheduler/assets/`
- Waza eval: `dotfiles/.agent/evals/agent-job-scheduler/`
- runtime 既定値: `~/.agent/agent-job-scheduler/`

## 基本フロー

1. まず [README.md](apps/agent-job-scheduler/README.md) と [usage.md](references/usage.md) を見て、対象 Agent と `workdir` が妥当か確認する。
2. ジョブ追加時は `enqueue` を使う。
3. 状態確認は `status` を使う。
4. 単発で消化を進めたいときは `run-once` を使う。
5. 定期実行を入れたいときは `scripts/install_launch_agent.sh` を使う。
6. 失敗ジョブの扱いは `show` / `retry` / `cancel` / `requeue` を使う。
7. 実行中 PID の確認は `active-runs` を使う。
8. 安全設定は `show-config` / `allow-workdir` / `set-allowlist-enforcement` / `set-stale-running-timeout` を使う。
9. prompt や CSV の叩き台が必要なら `assets/examples/` を使う。

## 実行前確認と検証

- `status` で queued / running / failed / cooldown を確認し、操作対象の `job_id` と Agent 種別を取り違えない。
- `enqueue` する prompt は、対象 `workdir`、期待する成果物、禁止操作、検証方法が明確なものにする。破壊的操作・認証情報参照・本番影響があり得る prompt はユーザー確認なしで積まない。
- `run-once` の前に、実行される可能性がある先頭ジョブを `status` / `show` で確認する。
- `retry` / `cancel` / `requeue` は `show <job_id>` で失敗理由と現在状態を見てから選ぶ。判断に迷う場合は、勝手に再実行せず候補と理由をユーザーへ提示する。
- `allow-workdir`、allowlist enforcement、stale timeout、launchd 導入は実行環境の安全設定を変えるため、対象パス・影響・戻し方を示してから実行する。
- 操作後は `status` または `show <job_id>` を再実行し、結果を job id、状態、次に必要なアクションと一緒に報告する。

## コマンド例

```bash
AJS=dotfiles/.agent/skills/agent-job-scheduler/apps/agent-job-scheduler/bin/agent-job-scheduler

"$AJS" status
"$AJS" enqueue --agent codex --workdir /abs/path --prompt "調査して修正してください"
"$AJS" run-once
"$AJS" show <job_id>
"$AJS" retry <job_id>
"$AJS" cancel <job_id>
"$AJS" requeue <job_id>
"$AJS" active-runs
"$AJS" allow-workdir /abs/path
"$AJS" set-allowlist-enforcement on
"$AJS" set-stale-running-timeout 600
dotfiles/.agent/skills/agent-job-scheduler/apps/agent-job-scheduler/scripts/install_launch_agent.sh --interval-seconds 60
```

## 注意

- 全 Agent を強い自動承認モードで動かす前提なので、危険な prompt を勝手に積まない。
- `workdir` は絶対パスの実在ディレクトリを使う。
- 直接実行より enqueue を優先し、ユーザーの意図しない即時変更を避ける。
- `install_launch_agent.sh` は既定では `launchctl bootstrap` まで行う。生成だけしたい場合は `--no-load` を使う。
- allowlist enforcement を有効にする場合は、先に必要な `workdir` を登録してから切り替える。
- `apps/agent-job-scheduler/` が、この skill の canonical な app 実装です。skill 配下だけで完結して辿れること、実装の置き場所と skill の参照先を一致させることが理由です。
- skill の挙動確認は `dotfiles/.agent/evals/agent-job-scheduler/` の Waza suite と app 側 pytest を併用する。
