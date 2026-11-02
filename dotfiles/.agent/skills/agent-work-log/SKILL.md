---
name: agent-work-log
description: repo を変更する作業、引き継ぎ、subagent 利用時の session と checkpoint を管理する。単純な読み取り回答には使わない。
---

# 作業ログと引き継ぎ

## 記録先と開始時の確認

- 現在の作業ディレクトリに `$PWD/.agent` が存在する場合は、作業ログを `$PWD/.agent/work/` 配下に残してください。このディレクトリは作業用のローカルな記録であり、Git 管理対象にしません(commit しないでください)。ユーザーが読む最終成果物は `.agent/work/` 配下に置かず、依頼で指定された場所か repo 内の適切な場所に置いてください。`html-preview-review` の private artifact はこの原則の例外とし、共通規約で指定した active session 配下へ置いてください。
- repo の状態を変える作業や引き継ぎ情報が必要な作業では、`$PWD/.agent/work/sessions/<YYYY-MM-DD-HHMMSS>-<short-slug>-<agent-id>/` を新規作成し、同時に `checkpoint.md` へ現在地を書いてください。読み取りだけの単純な質問応答では省略して構いません。
  - `<agent-id>` には実行中の agent 種別を使います。例: `.agent/work/sessions/2026-05-23-171230-agent-worklog-prompt-codex/`(slug が `agent-worklog-prompt`、agent-id が `codex`)。
  - 同名の directory が既に存在する場合は上書きせず、短い乱数や連番を付けて別名で作成してください。
- 過去の判断や続き作業に必要な場合だけ、`$PWD/.agent/work/sessions/` の関連 session を確認してください。再開時は `checkpoint.md` を入口に、必要な `handoff.md`、変更・検証記録と現在の `git status` / `git diff` を照合し、状態が変わった箇所を検証してください。無関係な最新 session を読む必要はありません。
- subagent、parallel agent、reviewer を使う場合は、メインの agent が session directory を作り、subagent ごとに `subagents/<role>-<timestamp>.md` など重複しない出力先を指定してください。subagent には既存ログの削除・上書きや最終判断を任せないでください。

## 作業中の更新

- `checkpoint.md` には目的、現在地、未完了、次の1〜3手、変更ファイル、適用中の skill と解決済みの実行形(例: `mise exec uv -- uv run …`)、最後に通った検証、background job・外部操作の状態を短く書き、まとまった変更や検証が終わるたびに更新してください。tool callごとの生ログは残さないでください。
- session directory には `checkpoint.md` を置き、必要に応じて `plan.md`、`changes.md`、`verification.md`、`feedback.md`、`handoff.md`、`subagents/` を置いてください。
- 失敗、手戻り、誤った仮定、検証漏れは、再発防止に使える形で `changes.md` または `verification.md` に要約してください。ユーザーの修正フィードバックに対応したら、その都度 session の `feedback.md` に指摘と対応を 1 行追記してください。

## 必要なときだけ読む詳細

- `plan.md`、変更・検証記録、`handoff.md` を作るときや、ADR などへの恒久化を判断するときは [session 内の補助ファイル](references/session-files.md) を読んでください。
- 大きな作業、長期作業、複数 session にまたがる作業では [local wiki](references/local-wiki.md) を読んでください。小さい作業では読み込みと wiki 更新を省略できます。
- 記録内容の機密性、学びの恒久化、最終報告には共通規約を適用してください。
