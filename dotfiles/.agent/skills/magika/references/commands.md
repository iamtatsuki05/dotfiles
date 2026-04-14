# Magika コマンドリファレンス

## インストール

| 方法 | コマンド | 用途 |
|------|---------|------|
| pipx（推奨） | `pipx install magika` | CLI ツールとして隔離インストール |
| pip | `pip install magika` | Python ライブラリ + CLI |
| pip（最新RC） | `pip install --pre magika` | リリース候補版 |

## CLI オプション

```
magika [OPTIONS] [PATH...]

引数:
  PATH          識別対象のファイルパス（複数指定可）。`-` を指定すると stdin から読み込む。

オプション:
  -r, --recursive       ディレクトリを再帰的に処理する
  --no-dereference      シンボリックリンクをたどらず、リンク自体を識別する
  -s, --output-score    予測スコア（信頼度）を出力に含める
  -i, --mime-type       説明の代わりに MIME タイプを出力する
  -l, --label           説明の代わりにシンプルなラベルを出力する（自動化に推奨）
  --json                JSON 形式で出力する（全ファイルをまとめた配列）
  --jsonl               JSONL 形式で出力する（1ファイル1行）
  --format <FORMAT>     カスタムフォーマットで出力する（後述）
  --colors              色付き出力を強制する
  --no-colors           色なし出力を強制する
  -V, --version         バージョン情報を表示する
  -h, --help            ヘルプを表示する
```

### --format プレースホルダー

| プレースホルダー | 内容 |
|----------------|------|
| `%p` | ファイルパス |
| `%l` | ラベル（例: `python`, `jpeg`, `pdf`） |
| `%d` | 説明（例: `Python source`, `JPEG image`） |
| `%g` | グループ（例: `code`, `image`, `document`） |
| `%m` | MIME タイプ（例: `text/x-python`） |
| `%e` | 推定拡張子（例: `py`） |
| `%s` | スコア（0.0〜1.0） |
| `%S` | スコアのパーセンテージ（例: `99.3%`） |
| `%b` | モデル出力が上書きされた場合に理由を表示 |

## JSON 出力構造

```json
[
  {
    "path": "./code.py",
    "result": {
      "status": "ok",
      "value": {
        "dl": {
          "label": "python",
          "description": "Python source",
          "group": "code",
          "mime_type": "text/x-python",
          "extensions": ["py", "pyi"],
          "is_text": true
        },
        "output": {
          "label": "python",
          "description": "Python source",
          "group": "code",
          "mime_type": "text/x-python",
          "extensions": ["py", "pyi"],
          "is_text": true
        },
        "score": 0.99
      }
    }
  }
]
```

- `dl`: ディープラーニングモデルの生の出力
- `output`: 閾値適用後の最終出力（通常はこちらを使う）
- `score`: 信頼度スコア（0.0〜1.0）

## 主なグループ一覧

| グループ | 代表的なラベル |
|---------|--------------|
| `code` | `python`, `javascript`, `go`, `rust`, `java`, `c`, `cpp` ... |
| `document` | `pdf`, `docx`, `xlsx`, `pptx`, `html`, `markdown` ... |
| `image` | `jpeg`, `png`, `gif`, `webp`, `svg`, `bmp` ... |
| `audio` | `mp3`, `wav`, `flac`, `ogg` ... |
| `video` | `mp4`, `avi`, `mkv`, `mov` ... |
| `archive` | `zip`, `tar`, `gz`, `bz2`, `7z`, `rar` ... |
| `executable` | `elf`, `macho`, `pe` ... |
| `text` | `txt`, `csv`, `tsv`, `ini`, `json`, `yaml`, `xml` ... |

## Python API リファレンス

### Magika クラスの初期化

```python
from magika import Magika, PredictionMode

# デフォルト
m = Magika()

# 予測モードを指定
m = Magika(prediction_mode=PredictionMode.HIGH_CONFIDENCE)   # 高信頼度のみ
m = Magika(prediction_mode=PredictionMode.MEDIUM_CONFIDENCE) # 中程度以上
m = Magika(prediction_mode=PredictionMode.BEST_GUESS)        # 常にベストな推定を返す

# シンボリックリンクをたどらない
m = Magika(no_dereference=True)
```

### 識別メソッド

```python
# ファイルパスで識別（Path または str）
result = m.identify_path("./file.txt")

# 複数ファイルを一括識別（効率的）
results = m.identify_paths(["a.txt", "b.bin"])

# バイト列で識別
result = m.identify_bytes(b"<html><body>Hello</body></html>")

# バイナリストリームで識別
with open("file.bin", "rb") as f:
    result = m.identify_stream(f)
```

### MagikaResult の主要属性

```python
result.ok                   # bool: 識別成功かどうか
result.status               # Status enum
result.score                # float: 信頼度スコア（0.0〜1.0）
result.output.label         # str: ラベル（例: "python"）
result.output.mime_type     # str: MIMEタイプ
result.output.group         # str: グループ
result.output.description   # str: 説明
result.output.extensions    # List[str]: 推定拡張子リスト
result.output.is_text       # bool: テキストファイルかどうか
result.dl                   # モデル生出力（output と同構造）
```

### ユーティリティメソッド

```python
m.get_output_content_types()  # 出力可能な全コンテンツタイプのラベルリスト
m.get_model_content_types()   # モデルが扱う全コンテンツタイプのラベルリスト
m.get_module_version()        # パッケージバージョン
m.get_model_version()         # 使用モデル名
```

## 使用例集

### CLI

```bash
# 複数ファイルを一括識別（ラベルのみ）
magika --label *.unknown

# ディレクトリを再帰的にスキャンして JSONL 出力
magika --recursive --jsonl ./uploads/ > result.jsonl

# jq でグループごとに集計
magika --jsonl ./uploads/* | jq -r '.result.value.output.group' | sort | uniq -c | sort -rn

# 信頼度が低いファイルを抽出（score < 0.8）
magika --json ./files/* | jq '[.[] | select(.result.value.score < 0.8) | .path]'

# 拡張子なしファイルの種別を調べる
find . -type f ! -name "*.*" | xargs magika --label
```

### Python

```python
from magika import Magika
from pathlib import Path

m = Magika()

# アップロードディレクトリのファイルを分類
upload_dir = Path("./uploads")
paths = list(upload_dir.rglob("*"))
results = m.identify_paths(paths)

for r in results:
    if r.ok:
        print(f"{r.path}: {r.output.label} ({r.output.group}) score={r.score:.2f}")
    else:
        print(f"{r.path}: error - {r.status}")

# 危険なファイルタイプをフィルタ
dangerous_groups = {"executable"}
flagged = [r for r in results if r.ok and r.output.group in dangerous_groups]
```

## トラブルシューティング

- **`magika: command not found`** → `pipx install magika` でインストール。PATH に `~/.local/bin` が含まれているか確認（`export PATH="$HOME/.local/bin:$PATH"`）。
- **`pip` でインストールしたのに使えない** → `python3 -m magika` で実行できるか試す。または `pipx install magika` を使う。
- **識別結果が `unknown` になる** → ファイルが非常に小さい・空・暗号化されているなどが原因の可能性がある。`--output-score` でスコアを確認する。
- **精度を上げたい** → `PredictionMode.HIGH_CONFIDENCE` を使うと閾値が上がり、不確かな場合は `unknown` を返すようになる。

## リポジトリ

https://github.com/google/magika
