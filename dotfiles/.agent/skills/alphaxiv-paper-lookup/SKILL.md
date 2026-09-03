---
name: alphaxiv-paper-lookup
description: "Use when the user asks to summarize, read, compare, or extract implementation details from an arXiv paper given an arXiv URL, alphaxiv URL, arXiv ID, title, DOI-like reference, or requests such as この論文を要約して / alphaxiv で調べて. Do not use for non-arXiv papers, general web research, or when the user only wants the PDF downloaded."
---

# alphaxiv-paper-lookup

alphaxiv.org が公開する AI 生成 overview(`overview/{ID}.md`)と PDF 抽出の全文 Markdown(`abs/{ID}.md`)を `WebFetch` で読み、arXiv 論文の要約・精読・比較に答える。認証は不要。

## URL

| 目的 | URL |
|------|-----|
| 構造化 overview(第一選択) | `https://alphaxiv.org/overview/{PAPER_ID}.md` |
| 全文 Markdown(裏取り・詳細) | `https://alphaxiv.org/abs/{PAPER_ID}.md` |
| 元の PDF(最終手段) | `https://arxiv.org/pdf/{PAPER_ID}` |
| ユーザー案内用ページ | `https://alphaxiv.org/abs/{PAPER_ID}`、`https://arxiv.org/abs/{PAPER_ID}` |

overview の節構成は Authors / Landscape / Objectives / Methodology / Findings / Significance。

## 手順

1. Paper ID を決める。
   - URL(`arxiv.org/abs|pdf/...`、`alphaxiv.org/abs|overview/...`)や `arXiv:` 接頭辞から ID を取り出し、`.pdf` / `.md` を落とす。
   - バージョン接尾辞(`v5`)は外して取得し、版差を比較するときだけ付け直す。旧形式(`hep-th/9901001`)の `/` はそのまま渡す。
   - タイトル・DOI しかない場合は Web 検索で候補を探し、タイトル・著者・年を照合する。候補が複数あれば推測せず候補と差分をユーザーに示す。
2. `overview/{ID}.md` を `WebFetch` する。`prompt` に抽出観点(要約 400 字、Methodology だけ、再実装に必要な設定、先行研究との差分など)を必ず書き、生テキストを丸ごと読まない。長い論文は観点を分けて複数回取得する。
3. overview に無い式・擬似コード・実験条件・付録は `abs/{ID}.md` を、節名や Table 番号を `prompt` に書いて取得する。
4. 404 は未収録・処理前。`arxiv.org/abs/{ID}` で論文の存在を確かめ、バージョン接尾辞を外して再試行し、それでも無ければ PDF を案内する(`markitdown` skill で `markitdown https://arxiv.org/pdf/{ID} -o <path>.md`)。図・画像は overview / abs に入らないので PDF を案内する。
5. 報告する。ベンチマーク値、改善率、データセット名、著者の結論として引く記述は `abs` か PDF で裏取りし、出典節(例: Table 2)を併記する。overview だけで返す場合は「overview ベース」、本文で確認できない箇所は「本文未確認」と明記する。

## 用途別

- クイック要約: overview 1 回 → 数値があれば abs で該当箇所だけ確認。
- 再実装: overview で語彙をつかみ、abs を「モデル構造」「学習手順」「ハイパーパラメータ」「データ」「評価」に分けて 2〜4 回取得し、設定表や擬似コードに組み立てる。
- 複数論文の比較(2〜5 本): 同じ `prompt` で各 overview を並列に取得し、節ごとに差分を示す。
- 引用・先行研究: overview の Landscape 節だけを取得し、arXiv ID があれば同じ手順を再帰する。3 段以上は広げず、必要ならユーザー確認。

`WebFetch` の `prompt` は外部に送られるので、ユーザーの私的メモや社内情報を混ぜない。取得結果を保存する場合は session directory か一時ディレクトリに置く。

`prompt` の文例、エンドポイントの観測結果、404 以外のデバッグ手順は [references/workflows.md](references/workflows.md)。
