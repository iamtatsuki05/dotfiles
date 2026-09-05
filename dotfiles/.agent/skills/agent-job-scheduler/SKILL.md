---
name: agent-job-scheduler
description: "Use when the user wants to queue, inspect, retry, cancel, or run long non-interactive AI agent CLI jobs (codex, claude, copilot, cursor, devin, antigravity, hermes, opencode, openclaw, grok) via agent-job-scheduler, or to change its workdir allowlist, stale recovery, or launchd schedule. DO NOT USE FOR: a single interactive run."
---

# Agent Job Scheduler

agent CLI の非対話ジョブを CSV 台帳で queue し、rate limit 中の agent を避けて agent ごとに 1 件ずつ実行する。canonical な実装は skill 配下の `apps/agent-job-scheduler/`、runtime データは `~/.agent/agent-job-scheduler/`(`jobs.csv`、`prompts/`、`runs/`、`logs/`、`settings.json`)にある。

## コマンド

```bash
AJS=dotfiles/.agent/skills/agent-job-scheduler/apps/agent-job-scheduler/bin/agent-job-scheduler

"$AJS" status
"$AJS" enqueue --agent codex --workdir /abs/path --prompt "調査して修正してください"
"$AJS" enqueue --agent codex --workdir /abs/path --prompt-file /abs/prompt.txt --scheduled-at 2026-09-03T09:00:00+09:00
"$AJS" run-once
"$AJS" show <job_id> --tail-lines 20     # 完全な prompt 本文は --include-prompt
"$AJS" retry <job_id>                     # failed / retry_waiting を再投入
"$AJS" requeue <job_id>                   # 既存ジョブを複製して新規に積む
"$AJS" cancel <job_id>                    # running は記録済み PID を停止してから cancelled
"$AJS" active-runs
"$AJS" show-config
"$AJS" allow-workdir /abs/path
"$AJS" list-allowed-workdirs
"$AJS" set-allowlist-enforcement on
"$AJS" set-stale-running-timeout 600
"$AJS" --runtime-root /abs/runtime status
dotfiles/.agent/skills/agent-job-scheduler/apps/agent-job-scheduler/scripts/install_launch_agent.sh --interval-seconds 60
dotfiles/.agent/skills/agent-job-scheduler/apps/agent-job-scheduler/scripts/uninstall_launch_agent.sh
```

`--agent` は `antigravity`、`claude`、`codex`、`copilot`、`cursor`、`devin`、`hermes`、`opencode`、`openclaw`、`grok`。ジョブ状態は `queued` / `running` / `succeeded` / `failed` / `retry_waiting` / `cancelled`。prompt の叩き台は `assets/examples/prompt.<agent>.txt`、agent ごとの実行コマンドと flag は [apps/agent-job-scheduler/README.md](apps/agent-job-scheduler/README.md) にある。

## 手順

1. `status` で queued / running / failed と agent ごとの cooldown を見て、操作対象の `job_id` と agent 種別を確定する。
2. `enqueue` の prompt には、対象 `workdir`(絶対パスの実在ディレクトリ)、期待する成果物、禁止操作、検証方法を書く。全 agent が自動承認モードで動くため、破壊的操作・認証情報参照・本番影響があり得る prompt はユーザー確認なしで積まない。prompt は `prompts/<job_id>.txt` に平文保存されるので、機密値を埋め込まない。
3. `run-once` の前に、実行され得る先頭ジョブを `status` / `show` で確認する。
4. `retry` / `cancel` / `requeue` は `show <job_id>` で失敗理由と現在状態を見てから選ぶ。判断に迷う場合は再実行せず、候補と理由をユーザーに提示する。
5. `allow-workdir`、allowlist enforcement、stale timeout、launchd の install / uninstall は実行環境の安全設定を変えるため、対象パス・影響・戻し方を示してから実行する。enforcement を `on` にする前に必要な `workdir` を登録する。`install_launch_agent.sh` は既定で `launchctl bootstrap` まで行い、生成だけなら `--no-load` を付ける。
6. 操作後は `status` または `show <job_id>` を再実行し、readback した値で報告する。

## 報告

job_id、agent、状態、次に必要なアクションを readback した値で書く。「積んだ」と「実行した」を区別し、実行結果は `show` が返す `result.txt` / `transcript.txt` の末尾を根拠にする。skill の挙動確認は `dotfiles/.agent/evals/agent-job-scheduler/` の Waza suite と app 側の pytest を併用する。
