# MarkItDown 詳細リファレンス

markitdown 0.1.7 時点。

## CLI オプション

```
markitdown [OPTIONS] [FILENAME]

引数:
  FILENAME        変換対象のファイルパス or URL(省略時は stdin から読み込み)

オプション:
  -o, --output    出力ファイルパス(省略時は stdout)
  -x, --extension 入力の拡張子ヒント(stdin 入力時に必須級)
  -m, --mime-type MIME type ヒント
  -c, --charset   文字コードヒント(例: UTF-8)
  --keep-data-uris  base64 画像などの data URI を切り詰めずに残す
  -p, --use-plugins / --list-plugins  サードパーティ plugin の利用・一覧
  -d, --use-docintel -e <ENDPOINT>    Azure Document Intelligence で抽出(外部送信)
  -v, --version / -h, --help
```

## 使用例

```bash
markitdown report.pdf -o report.md
markitdown document.docx -o document.md
markitdown slides.pptx -o slides.md
markitdown data.xlsx -o data.md            # シートごとに Markdown table
markitdown data.csv -o data.md
markitdown https://example.com -o page.md
markitdown index.html -o index.md
markitdown archive.zip -o archive.md       # 内包ファイルを一括変換

# 一括変換
for f in *.pdf; do markitdown "$f" -o "${f%.pdf}.md"; done

# markitdown[all] が必要
markitdown screenshot.png -o text.md       # 画像 OCR
markitdown interview.mp3 -o transcript.md  # 音声文字起こし
```

Python API(markitdown を import できる interpreter で実行する):

```python
from markitdown import MarkItDown

result = MarkItDown().convert("input.xlsx")   # URL も同じ形で渡せる
print(result.text_content)
```

## インストールバリアント

| コマンド | 用途 |
|---|---|
| `uvx --from 'markitdown[pdf]' markitdown <args>` | PDF 込みの ad-hoc 実行(install しない) |
| `pip install markitdown` | 標準(Office・HTML など。PDF は含まない) |
| `pip install 'markitdown[pdf]'` | PDF サポートを追加 |
| `pip install 'markitdown[docx]'` / `'[pptx]'` | Word / PowerPoint のみ追加 |
| `pip install 'markitdown[all]'` | 全機能(OCR・音声文字起こしを含む) |

永続 install はユーザー承認後に行う。

## トラブルシューティング

- `markitdown: command not found`: `mise exec 'pipx:markitdown' -- markitdown <args>`。`python3 -m markitdown` は system python では `No module named 'markitdown'` になる。
- `FileConversionException: File conversion failed after 1 attempts`(PDF): `pdfminer` が無い。`uvx --from 'markitdown[pdf]' markitdown <file>` か `pdftotext -layout <file> -`(poppler、無ければ `nix shell nixpkgs#poppler-utils --command pdftotext ...`)に切り替える。
- PDF の変換結果が空: スキャン PDF。`markitdown[all]` で OCR を有効化するか、元文書を案内する。
- 音声・画像が変換できない: `markitdown[all]` の追加依存が必要。
- 文字化け: 出力は UTF-8。`-c` で入力文字コードを指定するか `-o` でファイルに保存する。

リポジトリ: https://github.com/microsoft/markitdown
