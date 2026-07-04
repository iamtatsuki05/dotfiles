# MarkItDown 詳細リファレンス

## CLI オプション

```
markitdown [OPTIONS] [INPUT]

引数:
  INPUT           変換対象のファイルパス or URL（省略時は stdin から読み込み）

オプション:
  -o, --output    出力ファイルパス（省略時は stdout）
  -x, --extension 入力の拡張子を明示指定（自動判定を上書き）
  --version       バージョン表示
  -h, --help      ヘルプ表示
```

## 使用例集

### ファイル変換

```bash
# PDF → Markdown
markitdown report.pdf -o report.md

# Word → Markdown
markitdown document.docx -o document.md

# PowerPoint → Markdown
markitdown slides.pptx -o slides.md

# Excel → Markdown（テーブル形式）
markitdown data.xlsx -o data.md

# CSV → Markdown
markitdown data.csv -o data.md

# Jupyter Notebook → Markdown
markitdown notebook.ipynb -o notebook.md
```

### URL / HTML 変換

```bash
# Web ページを Markdown に変換
markitdown https://example.com -o page.md

# ローカル HTML ファイル
markitdown index.html -o index.md
```

### 画像・音声（`markitdown[all]` が必要）

```bash
# 画像内テキストを OCR で抽出
markitdown screenshot.png -o text.md

# 音声を文字起こし
markitdown interview.mp3 -o transcript.md
```

### 一括変換（シェルスクリプト例）

```bash
# カレントディレクトリの全 PDF を変換
for f in *.pdf; do
  markitdown "$f" -o "${f%.pdf}.md"
done
```

### Python API

基本例は SKILL.md の「Python API での変換」を参照。URL もファイルパスと同様に `md.convert("https://example.com")` で変換でき、結果は `result.text_content` から取り出す。

## インストールバリアント

| コマンド                           | 用途                                          |
|------------------------------------|-----------------------------------------------|
| `pip install markitdown`           | 標準（PDF・Office・HTML等）                   |
| `pip install 'markitdown[all]'`    | 全機能（OCR・音声文字起こし等）               |
| `pip install 'markitdown[pdf]'`    | PDF サポートのみ追加                          |
| `pip install 'markitdown[docx]'`   | Word サポートのみ追加                         |
| `pip install 'markitdown[pptx]'`   | PowerPoint サポートのみ追加                   |

## トラブルシューティング

- **`markitdown: command not found`** → まず `mise exec 'pipx:markitdown' -- markitdown` などの ad-hoc 実行（SKILL.md 冒頭参照）。永続インストール（`pip install markitdown`）はユーザー確認後に行う（`python3 -m markitdown` でも実行可能）。
- **PDF の変換結果が空** → スキャン PDF（画像ベース）の場合は `markitdown[all]` で OCR を有効化する。
- **音声・画像が変換できない** → `pip install 'markitdown[all]'` で追加依存をインストール。
- **文字化け** → 出力は UTF-8 。ターミナルのエンコーディングを確認するか `-o` でファイルに保存する。

## リポジトリ

https://github.com/microsoft/markitdown
