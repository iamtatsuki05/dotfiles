---
name: goal-prompt-builder
description: "Use when the user asks to create, rewrite, evaluate, or tighten a Codex /goal prompt: long-running work with a verifiable stopping condition (migrations, refactors, eval-score improvement, deployment retries, prototypes). Not for prompts sent to a model API (prompt-tuner) or agent instructions such as skills and AGENTS.md (empirical-prompt-tuning)."
---

# Goal Prompt Builder

`$goal-prompt-builder` で呼ばれた場合、本文はこの context に注入済みなので、SKILL.md を再読しない。

依頼や粗い目的を、Codex CLI の `/goal` に渡せる耐久目標プロンプトへ変換する。目的、参照先、範囲、検証、checkpoint、停止条件を 1 通の契約にまとめ、言語はユーザー入力の主言語に合わせる。

## 対象の切り分け

- Codex に長時間自走させる目標の prompt → 本 skill。
- モデル API へ送る system prompt や template の改善 → `prompt-tuner`。
- skill、slash command、AGENTS.md など agent 向け指示の改善 → `empirical-prompt-tuning`。

## 前提

- `/goal` は、1 ターンで終わらないが検証可能な完了条件を定義できる作業に使う。基本形は `/goal Complete [objective] without stopping until [verifiable end state].`
- goal 機能が無効なら、`/experimental` で有効化するか `config.toml` の `[features]` に `goals = true` を追加するよう短く案内する。
- 公式ガイド(https://developers.openai.com/codex/use-cases/follow-goals)は、挙動や有効化手順に不明点があるときだけ読む。生成する prompt には、ユーザーが求めない限り URL を入れない。

## 手順

1. 入力を整理する
   - 主言語を判定し、goal prompt の見出し、箇条書き、停止条件まで同じ言語で書く。コード、パス、コマンド、API 名、エラー文は原文を保持する。
   - 目的を 1 つに絞る。最初に読むファイル、issue、PR、ログ、設計メモを列挙する。
   - 変えてよい範囲と変えてはいけない範囲を分ける。
   - 成功を証明するコマンド、テスト、成果物、メトリクスを確認する。不明なら、README、package scripts、CI、Makefile、task runner を先に調べるよう prompt 内で指示する。
   - 本番影響、課金、外部 API、権限昇格、破壊的操作があり得る場合は、必ず停止してユーザー確認する条件に入れる。
2. goal に向くか判定する
   - 向く: 明確な成功条件がある移行、広めのリファクタ、テスト改善、プロトタイプ完成、デプロイ再試行、評価スコア改善。
   - 向かない: 関係の薄い TODO 群、完了条件が曖昧な調査、ユーザー判断が頻繁に要る作業、セキュリティ・本番・課金・秘密情報・法的判断・権限変更を Codex に委ねる依頼。
   - 向かない場合は `/goal` を生成せず、後述の拒否形式で理由、先に決めること、goal 化できる直し方だけを返す。
3. prompt を書く
   - 1 文目で「何を完了するか」と「いつ止まるか」を書き、参照先、範囲、作業ループ、停止条件、確認条件を続ける。
   - 不足情報や仮定は別見出しの Notes にせず、prompt 内の「前提」または「確認が必要な条件」に短く含める。
   - 評価スコア改善の goal では、grader、評価データ、評価基準の変更を禁止範囲に入れ、各 run 後に failing cases を確認する手順を作業ループに書く。
   - 停止条件には、目標到達に加えて「同じ failure class への targeted iteration が 2 回続けて改善しない」など進展しない場合の打ち切りも入れる。
4. 出力前に確認する
   - 目的が 1 つで、完了条件が観測可能である(「頑張る」「改善する」だけになっていない)。
   - 最初に読む資料、変更可否の範囲、checkpoint ごとの検証、失敗時の再試行、ユーザー確認の境界がそろっている。
   - 依頼と無関係なリファクタや仕様変更を禁止している。
   - 全体が要点だけになっている。長い goal は Codex の入力上限(目安 4,000 字)で拒否または切り詰められるため、詳細は参照ファイルへ逃がす。

## 出力形式

goal に向く場合は、`/goal` から始まる prompt 本文だけを返す。見出し、説明、コードフェンス、`## Goal Prompt`、別枠の notes は付けない。ラベルは主言語に合わせる(日本語なら「参照」「作業範囲」「作業ループ」「停止条件」「確認が必要な条件」)。

```text
/goal Complete <objective> without stopping until <verifiable end state>.

Read first:
- <files/docs/issues/logs>

Scope:
- Change: <allowed changes>
- Do not change: <protected areas>

Work loop:
- Work in checkpoints. At each checkpoint, summarize what changed, what was verified, what remains, and whether anything is blocked.
- After each meaningful change, run <validation command/artifact check>.
- If validation fails, inspect the failure, make the smallest targeted fix, and rerun the relevant validation.

Stop when:
- <specific stopping condition>
- <final validation command/artifact> passes.

Pause and ask me before:
- <production/permission/billing/destructive/ambiguous decisions>
```

日本語入力の例(移行):

```text
/goal <legacy stack> から <target stack> への移行を完了してください。新しい経路が legacy 経路と同じ contract test に通り、legacy 経路が rollback 用に残っていることを確認できるまで止まらないでください。

参照:
- <migration plan>、<test docs>、<entry points>

作業範囲:
- 変更してよい: 移行に必要なコード、テスト、ドキュメント。
- 変更しない: 無関係な整形、計画にない public API 挙動、本番 secret。

作業ループ:
- module または route ごとに checkpoint を切り、各 checkpoint 後に <unit/contract/e2e command> を実行する。
- 変更ファイル、検証結果、次の checkpoint を短く記録する。

停止条件:
- 計画された module がすべて移行済みで、<full validation command> が通る。

確認が必要な条件:
- rollback 経路の削除、外部 contract の変更、deployment credential への接触、挙動差分の許容。
```

goal に向かない場合は `/goal` を含めず、次の形で返す。英語入力なら同じ内容を英語で書く。

```markdown
## Goal 化しません

理由:
- <goal に向かない理由>

先に決めること:
- <検証可能な停止条件、作業範囲、ユーザー判断が必要な点>

goal 化するなら:
- <安全で検証可能な依頼への直し方>
```
