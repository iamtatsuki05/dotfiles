---
name: goal-prompt-builder
description: "Use when the user asks to create, rewrite, evaluate, or tighten a Codex /goal prompt for long-running work, durable objectives, migrations, large refactors, experiments, deployment retry loops, prototypes, prompt optimization, or any task that should keep Codex working until a verifiable stopping condition is reached."
---

# Goal Prompt Builder

通常の依頼や粗い目的を、Codex CLI の `/goal` に渡せる耐久目標プロンプトへ変換する。目的、制約、検証方法、checkpoint、停止条件を明示し、長時間の自走で迷子にならない契約を作る。生成するプロンプトの言語は、ユーザー入力の主言語に合わせる。

## 前提

- `/goal` は、1 ターンで終わらないが、検証可能な完了条件を定義できる作業に使う。
- 公式の基本形は `/goal Complete [objective] without stopping until [verifiable end state].`
- Codex CLI 側で goal 機能が無効なら、ユーザーに `/experimental` で有効化するか、`config.toml` の `[features]` に `goals = true` を追加するよう短く案内する。
- 公式ガイド: https://developers.openai.com/codex/use-cases/follow-goals
- `/goal` の挙動、推奨形式、機能有効化手順に不明点がある場合だけ公式ガイドを確認する。生成する goal prompt には、ユーザーが明示的に求めた場合を除き、この URL を含めない。

## ワークフロー

1. **入力を整理する**
   - ユーザー入力の主言語を判定し、出力する goal prompt も同じ言語にする。日本語入力なら見出し、箇条書き、停止条件まで日本語で書く。コード、ファイルパス、コマンド、API 名、エラー文は原文を保持する。
   - 達成したい目的を 1 つに絞る。
   - 最初に読むべきファイル、issue、PR、ログ、設計メモ、スクリーンショットを列挙する。
   - 変えてよい範囲と変えてはいけない範囲を分ける。
   - 成功を証明するコマンド、テスト、スクリーンショット、成果物、メトリクスを確認する。
   - 本番影響、課金、外部 API、権限昇格、破壊的操作があり得る場合は、必ず停止してユーザー確認する条件に入れる。

2. **goal に向くか判定する**
   - 向く: 明確な成功条件がある移行、広めのリファクタ、テスト改善、プロトタイプ完成、デプロイ再試行、評価スコア改善。
   - 向かない: 関係の薄い TODO 群、調査だけで完了条件が曖昧な依頼、ユーザー判断が頻繁に必要な作業、セキュリティ/本番/課金判断を委ねる作業。
   - 向かない場合は、`/goal` プロンプトを生成しない。理由、足りない決定事項、goal 化できる形に直す条件だけを短く返す。
   - 依頼が危険操作、本番変更、課金発生、秘密情報、法的/契約判断、アクセス権限変更を Codex に委ねる内容なら拒否し、ユーザー確認や別の安全な進め方を示す。

3. **プロンプトを作る**
   - 成功時の最終出力は、説明、前置き、Markdown 見出し、コードフェンスを付けず、必ず `/goal` から始める。
   - 「何を完了するか」と「いつ止まるか」を 1 文目で明確にする。
   - その後に、参照先、作業範囲、制約、checkpoint、検証コマンド、進捗ログ、停止/確認条件を続ける。
   - 不足情報や仮定がある場合も、別見出しの Notes を作らず、goal prompt 内の「前提」または「確認が必要な条件」に短く含める。
   - テストや確認コマンドが不明な場合は、まず既存の README、package scripts、CI、Makefile、task runner を調べてから選ぶよう指示する。
   - 公式 URL は goal prompt 内に入れない。公式ガイドは、プロンプト作成中に `/goal` の仕様や推奨形式が気になった場合の確認先としてだけ使う。

4. **出力前に締める**
   - 完了条件が「頑張る」「改善する」だけになっていないか確認する。
   - checkpoint ごとに検証があるか確認する。
   - 失敗時の次アクションと、ユーザーへ戻す条件が入っているか確認する。
   - 依頼と無関係なリファクタや仕様変更を禁止できているか確認する。

## 出力形式

goal に向く場合は、次のように `/goal` から始まる prompt 本文だけを返す。見出し、説明、コードフェンス、`## Goal Prompt`、別枠の notes は付けない。ラベルはユーザー入力の主言語に合わせる。

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

goal に向かない場合は `/goal` を含めず、次の形で拒否する。

```markdown
## Goal 化しません

理由:
- <goal に向かない理由>

先に決めること:
- <検証可能な停止条件、作業範囲、ユーザー判断が必要な点>

goal 化するなら:
- <安全で検証可能な依頼への直し方>
```

英語入力なら同じ内容を英語で出す。

## テンプレート例

### コード移行

```text
/goal <legacy stack> から <target stack> への移行を完了してください。新しい経路が legacy 経路と同じ contract test に通り、legacy 経路が rollback 用に残っていることを確認できるまで止まらないでください。

参照:
- <migration plan>
- <test docs>
- <entry points>

作業範囲:
- 変更してよい: 移行に必要なコード、テスト、ドキュメント。
- 変更しない: 無関係な整形、計画にない public API 挙動、本番 secret。

作業ループ:
- module または route ごとに checkpoint を切る。
- 各 checkpoint 後に <unit/contract/e2e command> を実行する。
- 変更ファイル、検証結果、次の checkpoint を短く記録する。

停止条件:
- 計画された module がすべて移行済み。
- <full validation command> が通る。

確認が必要な条件:
- rollback 経路の削除、外部 contract の変更、deployment credential への接触、挙動差分の許容。
```

### プロトタイプ作成

```text
/goal <plan file or feature description> を実装してください。アプリが build でき、ローカルで起動し、実装した挙動が依頼された workflow と一致するまで止まらないでください。

参照:
- <PLAN.md or issue>
- <existing app structure>

作業範囲:
- 変更してよい: prototype に必要なファイル、focused tests、軽量な docs。
- 変更しない: 無関係な app architecture、必要性を説明できない dependency version。

作業ループ:
- milestone を 1 つずつ実装する。
- 実用的な範囲で milestone ごとに test を追加または更新する。
- user-facing な作業では <browser/playwright/manual check> で動作中の UI を確認する。

停止条件:
- <plan> の各 milestone が完了している。
- <build/test/e2e command> が通る。

確認が必要な条件:
- paid service の追加、大きな dependency の導入、取り消し不能な data change。
```

### プロンプト最適化

```text
/goal <prompt path> の prompt を最適化してください。<eval command> が <target score/pass rate> に到達するか、これ以上の改善に product、data、policy の判断が必要になるまで止まらないでください。

参照:
- <prompt files>
- <eval suite>
- <recent failure logs>

作業範囲:
- 変更してよい: prompt text、および意図した挙動を保つために必要な tests/fixtures。
- 変更しない: 明示的に理由を説明して報告しない限り、grader logic や evaluation data。

作業ループ:
- <eval command> を実行して baseline を取る。
- 編集前に failing cases を確認する。
- 小さく狙った prompt 変更を行い、eval を再実行し、score/change log を短く残す。

停止条件:
- target score/pass rate に到達する。
- または、同じ failure class に対する targeted iteration が 2 回続けて改善しない。

確認が必要な条件:
- policy-sensitive な挙動、evaluation criteria、product requirements の変更。
```

## 良い goal のチェックリスト

- 目的が 1 つである。
- 完了条件が観測可能である。
- 最初に読む資料が指定されている。
- 変更してよい範囲と禁止範囲が分かれている。
- checkpoint ごとの検証方法がある。
- 失敗時に再試行する方法がある。
- ユーザー確認が必要な境界が明示されている。
