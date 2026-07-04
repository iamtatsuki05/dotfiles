---
name: python-dev
description: "Use when the user asks to implement, refactor, test, debug, or review Python code, pytest/unittest behavior, typing, ruff/mypy issues, packaging, Pydantic models, or Python runtime errors."
---

# Python開発スキル

Pythonコードの実装、テスト、デバッグ、リファクタリングを効率的に行うためのガイド。

## 実装前の必須確認

**pyproject.tomlを必ず確認する。** プロジェクトのコーディング規約（ruff, mypy設定等）に従う。

確認項目:
- `[tool.ruff]`: line-length, quote-style, indent-style
- `[tool.ruff.lint]`: select, ignore（有効/無効なルール）
- `[tool.ruff.format]`: quote-style（シングル/ダブル）
- `[tool.mypy]`: python_version, disallow_untyped_defs
- `[tool.pytest.ini_options]`: テスト設定
- Python バージョンと既存記法。Python 3.12+ 構文は、対象プロジェクトが対応している場合だけ使う
- 既存の依存、例外設計、テストヘルパー、型スタイル

`pyproject.toml` がない場合は、`setup.cfg`、`tox.ini`、`requirements*.txt`、`uv.lock`、`poetry.lock`、既存コードのスタイルを確認する。Pydantic は既に採用されている、または要件に合う場合に使い、単純なデータ構造へ不用意に導入しない。

## コーディング規約

### 基本スタイル

以下のサンプルは Python 3.12+ 前提（`def f[T]`、`type X = ...`）。3.11 以下では `TypeVar` / `TypeAlias` を使う。

```python
# Python 3.12+のジェネリクス記法
def process_items[T](items: list[T], predicate: Callable[[T], bool]) -> list[T]:
    return [item for item in items if predicate(item)]

# 型エイリアス
type JsonValue = dict[str, Any] | list[Any] | str | int | float | bool | None

# Pydantic BaseModel（Pydantic が採用済み、または外部入力の検証が必要な場合に使う）
from pydantic import BaseModel, Field

class User(BaseModel):
    id: int
    name: str = Field(min_length=1)
    email: str | None = None

    model_config = {'frozen': True}  # イミュータブル
```

### エラーハンドリング

```python
# エラーメッセージは変数に代入してからraise（ruff EM101/EM102対策）
def validate(data: dict[str, Any]) -> None:
    if 'required_field' not in data:
        msg = 'required_field is missing'
        raise ValueError(msg)

# カスタム例外
class ValidationError(Exception):
    pass
```

### Protocol/ABCパターン

- 構造的部分型（実装側に継承を強制しない）には `Protocol`、共通実装や明示的な継承階層が必要なら `ABC` を使う。例は [references/coding-standards.md](references/coding-standards.md) の「クラス設計」を参照。

### ファクトリパターン

- 種別文字列は `Literal` 型に、登録テーブルは `ClassVar` にして型チェックを効かせる。実装例は [references/common-patterns.md](references/common-patterns.md) の「ファクトリ（ClassVar使用）」を参照。

## テスト

実装例（フィクスチャ、モック、パラメータ化、非同期テスト）は [references/testing-guide.md](references/testing-guide.md) を参照。ruff/mypy 対策の注意点:

- テスト関数にも戻り値型 `-> None` を明示する（mypy の untyped def 検出対策）。
- `@pytest.mark.parametrize` の引数名はタプルで指定する（ruff PT006 対策）: `@pytest.mark.parametrize(('input_val', 'expected'), [...])`
- 非同期テストは `@pytest.mark.asyncio` を付ける（pytest-asyncio の設定は pyproject.toml を確認）。

## 高度なパターン

詳細なコード例（非同期ユーティリティ、DI、Pydantic Settings 等）は [references/common-patterns.md](references/common-patterns.md) を参照。判断基準:

- **非同期**: 同期関数を async 文脈から呼ぶときは `asyncio.to_thread` でオフロードする。並行数を制限するときは `asyncio.Semaphore` を使う。
- **DI**: injector / dependency-injector はプロジェクトで既に採用されている場合に使い、小規模なら手動 DI（コンストラクタ渡し）で十分。
- **設定管理**: 環境変数や `.env` からの読み込みは pydantic-settings の `BaseSettings` を第一候補にする。

最小例（pydantic-settings）:

```python
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env')

    database_url: str
    debug: bool = False
```

## エンジニアリング作法（共通）

Small CL、テスト同梱、Why コメント、PR description の共通規範は `eng-practices` スキルを参照する。
Python では特に、機能変更に対応する pytest の追加・更新を同じ PR に含めることを徹底する。

## コード品質チェック

実装後に確認:
- ruff check / ruff format を通過するか
- mypy --strict を通過するか（プロジェクト設定に応じて）
- テストが通過するか
- 変更に対応する単体テストを追加または更新したか。難しい場合は理由と代替検証を報告する
- 実行不能な検証があれば、コマンド、失敗理由、未確認リスクを最終報告に含める

## リファレンス

詳細なガイドは以下を参照:

- **コーディング規約詳細**: [references/coding-standards.md](references/coding-standards.md) — 型ヒント、Pydantic / dataclass / Protocol / ABC の使い分け、ruff ルール（EM101 / TRY003 / ANN401 等）への対応方法が必要なとき
- **テストガイド**: [references/testing-guide.md](references/testing-guide.md) — pytest のフィクスチャ、モック、パラメータ化、非同期テストの実装例が必要なとき
- **よく使うパターン集**: [references/common-patterns.md](references/common-patterns.md) — ファクトリ、非同期ユーティリティ、DI、pydantic-settings の実装例が必要なとき
