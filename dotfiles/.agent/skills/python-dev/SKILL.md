---
name: python-dev
description: "Use when the user asks to implement, refactor, test, debug, or review Python code, pytest/unittest behavior, typing, ruff/mypy findings, packaging, Pydantic models, or runtime errors. Read once per session, not on every turn. Do not use for notebook-only analysis, Slurm/shell/env scripts, prompt iteration, or Markdown docs."
---

# Python 開発スキル

Python の実装、テスト、デバッグ、リファクタリングを、対象プロジェクトの規約と検証手順に合わせて進めるための手順と判断基準。テスト先行と互換レイヤの扱いも本文に含むので、`test-driven-development` や `compatibility-safety` を実装開始時にまとめて読まない。

この skill は session につき 1 回だけ読む。compaction 後は `checkpoint.md` の「適用中の skill と解決済みの実行形」を見て続け、SKILL.md を読み直さない。`$python-dev` で本文が注入済みの場合も再読しない。

## 着手前の確認

次の順に読み、決めた内容を `checkpoint.md` に 1〜2 行で残す。repo が無く相談だけの依頼では設定ファイルを探さず、前提にしたバージョンや設定を回答に明記する。

1. `pyproject.toml`
   - `[project] requires-python` と既存コードの記法。`def f[T]` や `type X = ...` などの 3.12+ 構文は、対象バージョンが対応する場合だけ使う。
   - `[tool.ruff]` の `line-length`、`[tool.ruff.lint]` の `select` / `ignore` / `per-file-ignores`、`[tool.ruff.format]` の `quote-style`。
   - `[tool.mypy]` の `strict` / `disallow_untyped_defs`、`[tool.pytest.ini_options]` の `testpaths` / `asyncio_mode` / marker。
   - `pyproject.toml` がなければ `setup.cfg`、`tox.ini`、`requirements*.txt`、`uv.lock` / `poetry.lock`、既存コードの順に見る。
2. 実行形を 1 つに決める。`uv run` / `poetry run` / `python -m` のうちプロジェクトが使っているものを選び、`uv` が PATH にないときは `mise exec uv -- uv run ...` を試し、それでも無ければ `missing-tools` を読む。以後は決めた実行形だけを使う。
3. 変更対象の周辺コード: 例外設計、logger の取り方、テストヘルパーと `conftest.py`、Pydantic / dataclass の使い分け、型の書き方。プロジェクトの既存パターンは、この skill の既定より優先する。

## 進め方

1. 振る舞いの変更ごとに、失敗するテストを先に書き、失敗を確認してから最小の実装で通し、その後に整理する。テストは既存の配置、fixture、`parametrize` の流儀に合わせる。
   - 対象外: notebook、一回限りの分析 script、config / env / sbatch の修正、prompt の反復。これらは既存の check を回すだけにし、テスト先行の手順を適用しない。
2. alias、silent fallback、default 値補完、legacy path、非等価な代替経路(別 runner や別実装での近似)は、明示要件か既存契約がなければ追加せず、明確なエラーにする。実際に書きそうになった時点、またはそれを含む差分をレビューする時点でだけ `compatibility-safety` を読む。
3. lint・型エラーはコード側を直す。`noqa`、`cast`、`type: ignore`、`pragma: no cover` は理由を添えられる場合だけ使い、ruff / mypy 設定の緩和で通さない。
4. 変更に近いテストを先に実行し、通ってから全体を回す。

## 規約と判断基準

プロジェクトに規約や既存パターンがあればそれに従う。無い場合の既定:

- 出力は `print` でなく `logging` の logger を使う。
- データ構造は `dataclass` より Pydantic を優先する。単純な dict / tuple で足りる場面には持ち込まない。
- CLI は `argparse` を直接書かず、プロジェクトが採用している宣言的ライブラリ(fire、typed-argument-parser など)に合わせる。
- `__init__.py` は空にし、`__all__` を書かない。
- `*args` / `**kwargs` で実引数を曖昧にせず、引数を明示的に列挙する。
- `sys.path` 操作、`PYTHONPATH`、subprocess や動的 import による import ハックをしない。package として import できる構成にする。
- 型は builtin generics(`list[str]`)、`X | None`、`collections.abc` の `Callable` を使う。`Any` は理由を添える場合だけ。
- エラーメッセージは変数に代入してから `raise` する(ruff EM101 / EM102 / TRY003)。ruff ルール別の直し方は [references/coding-standards.md](references/coding-standards.md) の「ruffルール対応」を見る。
- 構造的部分型には `Protocol`、共通実装や明示的な継承階層には `ABC` を使う。迷ったら同ファイルの「クラス設計」を見る。
- 種別文字列で実装を選ぶファクトリは `Literal` と `ClassVar` の登録テーブルで型を効かせる。実装例は [references/common-patterns.md](references/common-patterns.md) の「ファクトリ」。
- 同期関数を async 文脈から呼ぶときは `asyncio.to_thread`、並行数の制限は `asyncio.Semaphore`。DI コンテナはプロジェクトが採用済みの場合だけ使い、小規模ならコンストラクタ渡しで足りる。環境変数や `.env` の読み込みは pydantic-settings の `BaseSettings` を第一候補にする。いずれも実装例は同ファイル。

テストの既定:

- テスト関数にも `-> None` を付ける。類似ケースは `@pytest.mark.parametrize(('input_val', 'expected'), [...])` にまとめ、引数名はタプルで書く(ruff PT006)。
- 非同期テストは `@pytest.mark.asyncio` を付けるか、`asyncio_mode = "auto"` の設定に従う。
- tests も ruff / mypy の対象に含める。
- fixture のスコープ、`conftest.py`、`unittest.mock` / pytest-mock、非同期テストの書き方が必要なら [references/testing-guide.md](references/testing-guide.md) を見る。

## 完了前の確認

決めた実行形で次を実行する(`uv run` の例)。

```bash
uv run ruff check . && uv run ruff format --check .
uv run mypy <変更した package>   # --strict はプロジェクト設定が採用している場合だけ
uv run pytest <変更に近い tests>  # 通ったら uv run pytest で全体
```

- 変更した振る舞いに対応するテストを追加・更新したか。難しい場合は理由と代替の検証を報告に書く。
- 実行できない検証は、コマンド、失敗理由、残るリスクを報告に回す。

## 報告に含めること

- 変更ファイルと目的。規約の根拠(`pyproject.toml` の設定か、この skill の既定か)。
- 実行した検証コマンドと結果(失敗 0 件、テスト件数など)。未実行の検証とその理由。
- 追加・更新したテスト。
- 互換レイヤや default 補完を入れた場合は、その根拠と削除条件。
- PR description を書く段階になったら `eng-practices` を読む。それ以外の場面では読まない。
