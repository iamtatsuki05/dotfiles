---
name: prompt-tuner
description: "Use when the user asks to improve, tune, or draft a prompt sent to a model API (system prompt, user template, few-shot set) with a run-evaluate-diagnose-fix loop, adding role, constraints, examples, and criteria. Not for agent instructions (skills, slash commands, AGENTS.md: empirical-prompt-tuning) or Codex /goal prompts (goal-prompt-builder)."
---

# Prompt Tuner

モデル API に送るプロンプトを、実行、評価、診断、修正の反復で改善する。評価ケースを先に固定し、ベースラインとの差で改善を示す。

## 対象の切り分け

- API へ送る system prompt、user template、few-shot → 本 skill。
- agent への指示(skill、slash command、AGENTS.md / CLAUDE.md の節、task prompt)→ `empirical-prompt-tuning`。実行者を subagent にし、評価軸も異なる。
- Codex `/goal` に渡す耐久目標 → `goal-prompt-builder`。
- 混在していれば、対象ごとに分けてユーザーへ示す。

## 入力

- 対象プロンプト: 必須。無ければ確認する。
- 改善目標(正確さ、形式、簡潔さ、トーンなど): 無ければベースライン出力を見て決め、報告に書く。
- 実行コード: 無ければ Bash から API を直接呼ぶ。
- 評価コードとデータ: 無ければ評価ケースと採点基準を自作し、最終レポートに残す。
- 実行環境が使えない(API キー未設定、課金・外部送信の可否が不明、データを送れない)場合は実行せず、評価設計と改善案までを返し、その旨を明記する。

## 手順

1. 入力を整理する。評価ケースは中央値 1 件とエッジ 1 件以上を先に固定し、改善後に都合よく変えない。外部 API、課金、ユーザーデータ送信が発生するなら実行前に確認する。
2. ベースラインを実行・評価する。実行コードと評価コードは改変せずそのまま使い、動かないときだけ最小修正して報告する。実行コードが無い場合の例:

   ```bash
   curl https://api.anthropic.com/v1/messages \
     -H "x-api-key: $ANTHROPIC_API_KEY" \
     -H "anthropic-version: 2023-06-01" \
     -H "content-type: application/json" \
     -d '{"model":"<利用可能な軽量モデル id>","max_tokens":1024,"system":"<プロンプト>","messages":[{"role":"user","content":"<テスト入力>"}]}'
   ```

   model id は変わるため、実行前に利用可能な id を確認する。OpenAI API、ローカル実行、既存の評価スクリプトがあればそちらを優先する。評価コードが無ければ、正確性、形式、簡潔さ、トーンの 4 観点で目視評価する。
3. 診断する。失敗を `references/prompt-engineering.md` の失敗パターン表に当て、根本原因ごとに 1〜3 個の修正を適用する。1 回の反復で無関係な修正を混ぜない。
4. 同じ方法で再実行・再評価する。
5. 続行を判断する。
   - 有意に改善(定量なら +1.0 以上、定性なら明らかな改善)→ 続けるか確認する。
   - 収束、またはユーザーが満足 → 最終レポートへ。
   - 同じ失敗が 2 回続く、改善が小さい、評価ケースへの過適合が見える → 修正を増やす前に原因を報告する。
   - 反復回数の指定が無ければ 2〜3 回を目安にし、続ける価値があるときだけ追加を提案する。

## 最終レポート

```
## プロンプトチューニングレポート

### 評価ケースと採点基準
[自作した場合はここに残す]

### ベースライン評価
評価結果: [スコアまたは定性評価]
問題点: [特定した問題]

### 適用した改善
- [改善]: [変更内容と理由、対応する失敗パターン]

### 最終結果
評価結果: [スコアまたは定性評価]([ベースラインからの差])

### 最終プロンプト
[改善後のプロンプト全文]
```

実行しなかった検証と残る懸念(過適合、未検証のケース)もレポートに書く。最終プロンプトは依頼された用途に必要な長さに留め、汎用的な注意書きで膨らませない。
