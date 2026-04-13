---
name: markitdown
description: "Microsoft製のファイル→Markdown変換ツール「MarkItDown」を使って、PDF・Word・PowerPoint・Excel・HTML・画像・音声・URLなど多様なファイルをMarkdownに変換する。「PDFをMarkdownに変換したい」「WordファイルをMarkdownにしたい」「markitdownを使って」「HTMLをMarkdownに変換」「ファイルをMarkdownに変換」などのリクエストでトリガー。MarkItDownがインストールされていない場合はインストール手順も案内する。"
---

# MarkItDown

## Overview

MarkItDown（Microsoft製）を使って、PDF・Word・PowerPoint・Excel・HTML・画像・URL など多様なソースを Markdown テキストに変換するスキル。

## インストール確認と手順

まず `markitdown` が利用可能かを確認する。

```bash
markitdown --version
```

**インストールされていない場合:**

```bash
# 標準インストール（PDF・Word・Excel・HTML 等に対応）
pip install markitdown

# 全形式サポート（画像OCR・音声文字起こし等を含む）
pip install 'markitdown[all]'
```

> `pip` が使えない場合は `pip3` または `python3 -m pip` を使用する。

## 対応フォーマット

| フォーマット              | 備考                                       |
|---------------------------|--------------------------------------------|
| PDF (`.pdf`)              | テキスト抽出                               |
| Word (`.docx`)            | テキスト・見出し構造を保持                 |
| PowerPoint (`.pptx`)      | スライドごとにセクション化                 |
| Excel (`.xlsx`, `.csv`)   | テーブル形式で変換                         |
| HTML (`.html`, URL)       | ページコンテンツを抽出                     |
| 画像 (`.png`, `.jpg` 等)  | `[all]` インストール時に OCR で文字抽出   |
| 音声 (`.mp3`, `.wav` 等)  | `[all]` インストール時に文字起こし         |
| Jupyter Notebook (`.ipynb`) | セルとアウトプットを変換                 |
| JSON / XML                | 構造化テキストとして変換                   |
| ZIP                       | 内包ファイルを一括変換                     |

## 使い方

### CLI での変換

```bash
# 基本（標準出力）
markitdown input.pdf

# ファイルに保存
markitdown input.pdf -o output.md

# URL を変換
markitdown https://example.com

# パイプ経由
cat input.pdf | markitdown
```

### Python API での変換

```python
from markitdown import MarkItDown

md = MarkItDown()
result = md.convert("input.xlsx")
print(result.text_content)
```

## ワークフロー

1. `markitdown --version` でインストールを確認
2. 未インストールなら `pip install markitdown` を案内・実行
3. 変換コマンドを実行
4. 出力を確認し、必要なら `-o` でファイルに保存

## 詳細リファレンス

詳細なオプションや利用例は [references/usage.md](references/usage.md) を参照。
