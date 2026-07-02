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

# Pydantic BaseModel（原則こちらを使用）
from pydantic import BaseModel, Field

class Config(BaseModel):
    name: str
    value: int
    enabled: bool = True

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

```python
from typing import Protocol
from abc import ABC, abstractmethod

class FileLoader(Protocol):
    def load(self, path: str | Path) -> Any: ...

class BaseProcessor(ABC):
    @abstractmethod
    def process(self, data: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError
```

### ファクトリパターン

```python
from typing import ClassVar, Literal

FileFormat = Literal['json', 'yaml', 'toml']

class FileHandlerFactory:
    _handlers: ClassVar[dict[FileFormat, type[FileHandler]]] = {
        'json': JsonFileHandler,
        'yaml': YamlFileHandler,
    }

    @classmethod
    def create(cls, format_type: FileFormat) -> FileHandler:
        handler_class = cls._handlers.get(format_type)
        if handler_class is None:
            supported = ', '.join(cls._handlers.keys())
            msg = f'Unsupported format: {format_type}. Supported: {supported}'
            raise ValueError(msg)
        return handler_class()
```

## テスト

```python
import pytest

# テスト関数にも戻り値型を明示
def test_create_handler() -> None:
    handler = Factory.create('json')
    assert isinstance(handler, JsonHandler)

def test_validation_error() -> None:
    with pytest.raises(ValueError, match='missing'):
        validate({})

# パラメータ化
@pytest.mark.parametrize(('input_val', 'expected'), [
    ('hello', 5),
    ('', 0),
])
def test_length(input_val: str, expected: int) -> None:
    assert len(input_val) == expected

# 非同期テスト
@pytest.mark.asyncio
async def test_async_fetch() -> None:
    result = await fetch_data()
    assert result is not None
```

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

- **コーディング規約詳細**: [references/coding-standards.md](references/coding-standards.md)
- **テストガイド**: [references/testing-guide.md](references/testing-guide.md)
- **よく使うパターン集**: [references/common-patterns.md](references/common-patterns.md)
