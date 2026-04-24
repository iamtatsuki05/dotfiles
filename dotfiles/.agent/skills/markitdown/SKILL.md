---
name: markitdown
description: "Use when the user asks to convert PDF, Word, PowerPoint, Excel, HTML, image, audio, URL, or another supported source into Markdown, or explicitly asks to use MarkItDown."
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

インストールは環境変更を伴うため、ユーザーに確認してから実行する。OCR・音声文字起こしが必要な場合だけ `[all]` を検討する。

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
2. 入力ファイル/URL、出力先、既存ファイルの上書き可否を確認する
3. 未インストールなら `pip install markitdown` を案内し、ユーザー承認後に実行する
4. 変換コマンドを実行する
5. 先頭・末尾・見出し・表・コードブロックなどを確認し、変換崩れや欠落を報告する
6. ファイル保存が必要なら `-o` を使う。既存ファイルを上書きする場合は事前確認する

## 注意

- このリポジトリでは `.ipynb` を直接読み書きしない方針がある。ノートブック変換が必要な場合は、まず jupytext のペア `.py` があるか確認し、`.ipynb` 本体を丸ごと読み込まない。
- URL 変換では外部アクセスが発生する。認証付きページ、社内資料、個人情報を含む URL はユーザー確認後に扱う。
- 画像 OCR や音声文字起こしは追加依存・処理時間・外部モデル利用の可能性があるため、必要性を確認してから進める。

## 詳細リファレンス

詳細なオプションや利用例は [references/usage.md](references/usage.md) を参照。
