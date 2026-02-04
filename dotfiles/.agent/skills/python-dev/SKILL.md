---
name: python-dev
description: Python開発のための汎用スキル。コード実装、リファクタリング、テスト作成、デバッグ、コードレビューを支援。Pythonファイル(.py)の作成・編集、pytest/unittestによるテスト、型ヒント追加、コード品質改善、エラー解決、ベストプラクティス適用時に使用。
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

## コーディング規約

### 基本スタイル

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

### 非同期ユーティリティ

```python
import asyncio
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any

def sync_to_async[R](sync_func: Callable[..., R]) -> Callable[..., Awaitable[R]]:
    async def wrapper(*args: object, **kwargs: object) -> R:
        return await asyncio.to_thread(sync_func, *args, **kwargs)
    return wrapper

async def run_with_semaphore[R](
    func: Callable[..., Awaitable[R]],
    sema: asyncio.Semaphore | None,
    *args: object,
    **kwargs: object,
) -> R:
    if sema is not None:
        async with sema:
            return await func(*args, **kwargs)
    return await func(*args, **kwargs)
```

### 依存性注入（injector）

```python
from injector import Injector, Module, provider, singleton

class AppModule(Module):
    @singleton
    @provider
    def provide_config(self) -> Config:
        return Config.from_env()

injector = Injector([AppModule()])
config = injector.get(Config)
```

### Pydantic Settings

```python
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file='.env',
        env_file_encoding='utf-8',
    )

    database_url: str = Field(alias='DATABASE_URL')
    api_key: str = Field(alias='API_KEY')
    debug: bool = False
```

## コード品質チェック

実装後に確認:
- ruff check / ruff format を通過するか
- mypy --strict を通過するか（プロジェクト設定に応じて）
- テストが通過するか

## リファレンス

詳細なガイドは以下を参照:

- **コーディング規約詳細**: [references/coding-standards.md](references/coding-standards.md)
- **テストガイド**: [references/testing-guide.md](references/testing-guide.md)
- **よく使うパターン集**: [references/common-patterns.md](references/common-patterns.md)
