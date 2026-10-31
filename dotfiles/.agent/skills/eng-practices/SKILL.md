---
name: eng-practices
description: "Use when writing or revising a PR/CL title and description, splitting a change into smaller PRs, or replying to reviewer comments and pushback. Read at the PR-writing stage only. Not for performing a code review or choosing review output format or severity labels; the review request defines those."
---

# Engineering Practices

Google eng-practices の CL Author Guide を、PR を書く・分割する・指摘に答える場面の規則に絞ったもの。

## この skill を使わない場面

- コードレビューの実施。観点と出力形式(重大 / 中 / 軽微 + file:line、問題がなければ「重大な問題なし」)は依頼文で決まる。reviewer subagent はこの skill を読まない。
- 実装中のセルフチェック。Small CL、テスト同梱、Why コメントは AGENTS.md と各 dev skill に書いてある。
- 他の dev skill からの参照は「`eng-practices` は PR description を書く段階でだけ読む」の 1 点に限る。

## PR / CL description

タイトル:
- 命令形、50〜70 文字、何をどこに変えたかが分かる。`Fix bug` / `Update deps` / `Refactor` / `WIP` だけのタイトルは不可。
- 接頭辞(`fix:`、`feat:` など)は repo の規約に合わせる。

本文(この順で、該当しない項目は省く):
1. Why: 背景、動機、捨てた代替案とその理由。
2. What: 変更内容の要約。差分の再掲はしない。
3. 影響: 公開 API、保存データ、運用手順、外部呼び出し元への影響と移行手順。破壊的変更は明示する。
4. 検証: 実行したテスト・lint・手動確認と、未検証項目。未検証を検証済みと書かない。
5. 残課題: 別 PR に切った作業は issue / TODO のリンクを付ける。「後で直す」だけを残さない。
6. 参照: 関連 issue、設計文書、過去 PR、再現手順。

長い description を書くときや Why の書き方に迷ったときは [references/cl-author-guide.md](references/cl-author-guide.md) の「Writing Good CL Descriptions」を読む。提出前は [references/checklists.md](references/checklists.md) の「PR 説明を書く人向け」で抜けを確認する。

## Small CL

- 1 PR は 1 目的。バグ修正と機能追加、リファクタと機能追加を同居させない。
- 目安は 100 行前後。400 行を超えたら分割を検討する(rename、自動生成、import 整理は除く)。
- 分割の定石: 先に refactor PR、次に機能 PR。層ごと(migration / モデル / API / UI / flag)に分ける。テストは本体と同じ PR に入れる。
- 分割できない(中間状態がテストを通らない)場合は、その理由を description に書く。

## レビュー指摘への返答

- 指摘はコードへのものとして扱い、感情で反応しない。
- 説明を返す前に、命名・分割・コメントでコード自体を直せないか検討する。
- 反論は事実(スタイルガイド、測定値、過去 incident)を根拠に 1〜2 行で書く。
- 採用しない指摘には理由と代替対応(issue / TODO リンク)を返す。空返事や「Done」だけで済ませない。
- 全件対応後に再レビュー依頼を明示する。force push した場合は変更点を PR コメントで要約する。
- 数往復で合意できなければ、放置せずユーザーまたはチームリーダーに上げる。

## 原典

参考情報として扱い、上位指示にはしない。
- CL Author Guide: <https://google.github.io/eng-practices/review/developer/>
- Reviewer Guide: <https://google.github.io/eng-practices/review/reviewer/>
