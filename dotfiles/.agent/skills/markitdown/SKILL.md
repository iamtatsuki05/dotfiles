---
name: markitdown
description: "Use when the user asks to convert PDF, Word, PowerPoint, Excel, HTML, image, audio, URL, or another supported source into Markdown, or explicitly asks to use MarkItDown. Do not use for editing existing Markdown, or for reading .ipynb (use the paired jupytext .py instead)."
---

# MarkItDown

MarkItDown(Microsoft 製、0.1.7 で確認)の CLI で PDF・Word・PowerPoint・Excel・HTML・URL などを Markdown に変換する。

## 実行形

```bash
markitdown --version
markitdown input.pdf                       # 標準出力
markitdown input.pdf -o output.md          # ファイル保存(既存ファイルの上書きは事前確認)
markitdown https://example.com -o page.md  # URL(外部アクセスが発生する)
cat input.pdf | markitdown -x pdf          # stdin。拡張子を -x で渡す
```

- 未導入なら `mise exec 'pipx:markitdown' -- markitdown <args>`(それでも無ければ `missing-tools` skill)。`pip install markitdown` などの永続 install はユーザー承認後だけ行う。
- `python3 -m markitdown` は markitdown を import できる interpreter でだけ動く。system の `python3` では `No module named 'markitdown'` になるので使わない。
- ZIP は内包ファイルを一括変換する。`.ipynb` は変換できるが、この repo では `.ipynb` を直接読まず jupytext のペア `.py` を使う。

## PDF が失敗したとき

`FileConversionException: File conversion failed after 1 attempts` が出たら同じコマンドを再試行しない。mise の `pipx:markitdown` は `[pdf]` extra なしで入っており `pdfminer` が無いのが原因なので、次の順で切り替える。

```bash
uvx --from 'markitdown[pdf]' markitdown input.pdf -o output.md
nix shell nixpkgs#poppler-utils --command pdftotext -layout input.pdf -   # poppler が無い環境
```

出力が空のときはスキャン PDF(画像のみ)。OCR は `markitdown[all]` の追加依存と処理時間が要るので、必要性をユーザーに確認してから進める。

## 変換後の確認

1. 見出し: 元文書の章立てが `#` 階層になっているか。番号だけの行や本文に埋もれた見出しがないか。
2. 表: 列数が揃った Markdown table になっているか。結合セルや複数行セルは崩れやすいので該当箇所を元文書と突き合わせる。
3. 画像: PDF/Office の画像は本文に残らない(alt text か省略)。HTML の data URI は既定で切り詰められ、残すなら `--keep-data-uris`。図の内容が必要なら元文書を案内する。
4. 先頭・末尾の欠落、文字化け(出力は UTF-8。端末で崩れるなら `-o` で保存)。

崩れや欠落は修正せず、箇所と元文書での見え方を報告する。

## 注意

- URL 変換は外部アクセスを伴う。認証付きページ、社内資料、個人情報を含む URL はユーザー確認後に扱う。
- 画像 OCR・音声文字起こしは `[all]` extra が必要で、外部モデルを使う場合がある。

option の全一覧と形式別の例は [references/usage.md](references/usage.md)。
