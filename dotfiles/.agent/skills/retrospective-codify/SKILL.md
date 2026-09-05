---
name: retrospective-codify
description: "Use when the user asks to preserve learnings from this session, turn a mistake into a rule, or create/update a skill, lint rule, or CLAUDE.md/AGENTS.md entry. Proactively, offer at most one 3-line proposal per session. DO NOT USE FOR: one-off project fixes, small edits, unfinished work, or repeating a proposal the user did not answer."
---

# Retrospective Codify

session の失敗と最終解から「最初に知っていれば遠回りしなかった」知見を取り出し、lint rule、skill、CLAUDE.md / AGENTS.md のいずれかへ固定する。書き出しはユーザーが採用を指示した項目だけ行い、黙って書き出さない。

## 自発提案(ユーザー依頼がない場合)

- 1 session に 1 回だけ。大きめの作業の完了報告後、またはユーザーの修正フィードバック対応後に、記憶と session 記録(`feedback.md`、`changes.md`、`verification.md`)だけで判断する。横断検索や重複チェックはこの段階では行わない。
- 追記先ファイルと文案を示せる場合だけ、追記先と文案を含めて 3 行以内で提案する。示せなければ提案しない。
- 返答がなければ不要とみなし、同じ session では再提案も「不要なら言ってください」の再掲もしない。ユーザーが乗った場合だけ下のワークフローへ進む。
- 提案しない場面: 一発で通った作業、1 段落の追記や小さな修正、skill の作成・更新自体が依頼だった作業、成果物が未完の段階。

## ワークフロー(ユーザーが依頼した場合)

1. 失敗と成功の対応付け: 最初の試行と失敗、最終解、橋渡しになった気付きを 1 行ずつ書き出す。入力は `feedback.md`(ユーザー指摘)と `changes.md` / `verification.md`(自分の手戻り)。
2. 気付きを、未来の自分への指示形(「〜するな」「〜を先に確認せよ」)で 1〜3 文にする。
3. 再発確認(書き出し前に必須): 気付きから検索キーを 2〜3 語抽出し、プロジェクトの `.agent/work/sessions/` にある直近30日の `feedback.md`、`changes.md`、`verification.md` を横断検索する。ユーザーの明示依頼でも検索キーの抽出は省略しない。前後を読み、同じ失敗と有効だった対処が再発しているかを確認する。
   - 期間は実行環境のローカル日付を基準に、session directory 名の先頭にある `YYYY-MM-DD` を発生日として、実行日を含む直近30暦日を対象にする。日付を解析できない session は回数に含めず、未確認として示す。
   - 同じ原因と対処の組を同じ pattern とみなす。同じ作業文脈での再試行や、同じ事象が複数ファイルに記録されている場合は 1 回と数える。別タスクまたは別の解決記録として独立して確認できる事象は、同一 session 内でも発生回数に含めるが session 数は増やさない。
   - 3回以上かつ2つ以上の session で確認できた知見だけを、エージェント発の恒久化候補にする。同一 session 内の反復回数だけでは条件を満たさない。2回以下、1 session のみ、または履歴がない知見は恒久化候補にしないで、session の根拠として残す。
   - ユーザーが明示的に恒久化を依頼した場合は回数と session 数の再発条件だけを適用しない。後続の分類、重複チェック、確認は省略しない。
4. 分類(上から順に判定):
   - コード・設定の構文レベルで検出可能 → `ast-grep` rule または既存 linter 設定。静的に検出できるものを散文の rule にしない。
   - 短く、常時適用、判断を伴わない → `CLAUDE.md`(言語・ツール横断なら `~/.claude/CLAUDE.md`、特定 repo 限定ならその repo の `CLAUDE.md`)。
   - 手順・文脈判断・テンプレートが必要 → 既存 skill への追記、無ければ新規 skill。
   - 今回使った skill / AGENTS.md の記述が誤り・古い・誤誘導だった → 該当ファイルの修正提案。
   - プロジェクト固有で一回限り → 採用しない(commit message / PR 説明に留める)。
5. 重複チェック(必須): 再発確認と同じ検索キーで、`~/.claude/skills/*/SKILL.md`、`~/.codex/skills/*/SKILL.md`、`<project-root>/.agent/skills/*/SKILL.md`、`~/.claude/CLAUDE.md`、`<project-root>/CLAUDE.md`、`~/.codex/AGENTS.md`、`<project-root>/AGENTS.md`、`<project-root>/rules/` を照合し、次の 4 つに分ける。
   - 新規: ヒット無し → 通常の提案。
   - 既存追記: 関連 skill / rule があり追加情報が補完的 → 「既存に追記」を提案。部分重複もここに含め、重複部分は「重複検出」、新規部分は「採用候補」に分ける。
   - 既存と重複: 既存が完全にカバー済み → 提案ゼロ。ただし「重複検出」行に既存 skill 名と該当節名(または行番号)を残す。
   - 判断保留: 判定できない → 照合結果を見せてユーザーに判断を仰ぐ。
6. 下の提示形式でユーザーに見せ、採用を指示された項目だけ書き出す。棄却された知見は session 内のメモに留める。

## 書き出し先ごとの形式

- `ast-grep` rule: プロジェクトに `rules/` と `rule-tests/` の構成がある場合だけ、その規約に従って YAML と valid / invalid のテストを追加する。無ければ独自形式を作らず、候補 rule と配置案を提案に留める。
- `CLAUDE.md` / `AGENTS.md`: 既存 section へ命令形 1 文で追記し、理由を括弧書きで添える(`- <命令形の 1 文>(理由: <短い根拠>)`)。
- 新規 skill: `writing-skills` の最小テンプレートに従う。`.agent/skills/` があるプロジェクトではそこへ置く。
- 抽象度: 特定の関数名・バージョンなど一回限りの事情ではなく、「何を確認するか」のレベルで書く。失敗の側を省かない。

## 提示形式

```
## Retrospective

### 学び 1: <短いラベル>      # 学びが 1 つならこの見出しは省く
- 最初の失敗: <1 行>
- 最終解: <1 行>
- 気付き: <1 行>

## 提案

採用候補:
- [lint] <rule 名>: <1 行>(artifact: <path>, 学び N 由来)
- [skill 追記] <既存 skill 名>: <1 行>(学び N 由来)
- [skill 新規] <skill 名>: <1 行>(学び N 由来)
- [修正] <使用した skill 名 / AGENTS.md>: <誤り・古い記述の修正 1 行>(学び N 由来)
- [rule] CLAUDE.md(global / project): <1 行>(学び N 由来)

重複検出(提案不要):
- <学び N>: 既存 <skill / rule 名> の <該当節名 or 行番号> が完全カバー → 追加なし

不採用:
- <学び N>: <不採用理由 1 行>

採用するものを番号または項目名で指示してください。提案ゼロも妥当な結論です。
```

- 「採用候補」「重複検出」「不採用」のいずれかが空ならその節ごと省く(「なし」行は書かない)。
- 各提案行末に「学び N 由来」を書く。エージェント発の採用候補には `再発: <発生回数>回 / <session 数> sessions / <最初の日付>〜<最後の日付>` を、ユーザーの明示依頼による候補には `ユーザー明示指示` を添える。
- 採用候補が空なら、末尾文を `採用候補なし。記録目的でレビューしてください。` に置き換える。
- 境界ケース(全学びが既存カバー、部分重複)の提示例は `references/examples.md` を参照する。
