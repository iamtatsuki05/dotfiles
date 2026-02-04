# コーディング規約詳細

## 目次

1. [pyproject.toml参照](#pyprojecttoml参照)
2. [命名規則](#命名規則)
3. [型ヒント](#型ヒント)
4. [インポート](#インポート)
5. [docstring](#docstring)
6. [クラス設計](#クラス設計)
7. [ruffルール対応](#ruffルール対応)

## pyproject.toml参照

実装前に必ずプロジェクトのpyproject.tomlを確認する。主要な設定項目:

```toml
[tool.ruff]
target-version = "py313"  # Pythonバージョン
line-length = 119         # 行の最大長
indent-width = 4

[tool.ruff.format]
quote-style = "single"    # シングル or ダブル
indent-style = "space"

[tool.ruff.lint]
select = ["ALL"]          # 有効なルール
ignore = ["D100", ...]    # 無視するルール

[tool.mypy]
python_version = "3.13"
disallow_untyped_defs = true  # 型定義必須
```

## 命名規則

```python
# モジュール: snake_case
# user_authentication.py

# クラス: PascalCase
class UserAuthentication:
    pass

# 関数・メソッド: snake_case
def validate_user_input(data: dict[str, Any]) -> bool:
    pass

# 変数: snake_case
user_count = 0
is_valid = True

# 定数: UPPER_SNAKE_CASE
MAX_RETRY_COUNT = 3
DEFAULT_TIMEOUT = 30

# プライベート: 先頭にアンダースコア
_internal_cache: dict[str, Any] = {}

def _helper_function() -> None:
    pass
```

## 型ヒント

### 基本的な型（Python 3.12+）

```python
from collections.abc import Callable, Iterator, Sequence
from typing import Any, TypeVar

# 基本型
name: str = 'example'
count: int = 42
ratio: float = 0.5
is_active: bool = True

# コレクション（ビルトイン型を直接使用）
items: list[str] = []
mapping: dict[str, int] = {}
unique: set[int] = set()
coordinates: tuple[float, float] = (0.0, 0.0)

# Optional（Union記法）
value: str | None = None

# Callable（collections.abcから）
handler: Callable[[str, int], bool]
```

### 新しいジェネリクス記法（Python 3.12+）

```python
# 関数のジェネリクス
def first[T](items: Sequence[T]) -> T | None:
    return items[0] if items else None

def process[T, R](items: list[T], func: Callable[[T], R]) -> list[R]:
    return [func(item) for item in items]

# クラスのジェネリクス
class Container[T]:
    def __init__(self, value: T) -> None:
        self.value = value

# 型エイリアス（type文）
type JsonValue = dict[str, Any] | list[Any] | str | int | float | bool | None
type Handler[T] = Callable[[T], None]
```

### TypedDict, Literal

```python
from typing import TypedDict, Literal

class UserDict(TypedDict):
    id: int
    name: str
    email: str | None

FileFormat = Literal['json', 'yaml', 'toml', 'xml']

def set_format(fmt: FileFormat) -> None:
    pass
```

## インポート

```python
# 標準ライブラリ
import asyncio
import os
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, ClassVar, Literal

# サードパーティ
import numpy as np
import polars as pl
from pydantic import BaseModel

# ローカル（絶対インポート推奨）
from nlp.common.utils import helper
from nlp.models import User
```

順序（isort準拠）:
1. future
2. standard-library
3. third-party
4. first-party
5. local-folder

## docstring

公開APIにのみ必要。Google style。

```python
def calculate_discount(
    price: float,
    discount_rate: float,
    max_discount: float | None = None,
) -> float:
    """割引後の価格を計算する.

    Args:
        price: 元の価格
        discount_rate: 割引率（0.0〜1.0）
        max_discount: 最大割引額。Noneの場合は制限なし

    Returns:
        割引後の価格

    Raises:
        ValueError: discount_rateが0.0〜1.0の範囲外の場合

    """
    if not 0.0 <= discount_rate <= 1.0:
        msg = 'discount_rate must be between 0.0 and 1.0'
        raise ValueError(msg)

    discount = price * discount_rate
    if max_discount is not None:
        discount = min(discount, max_discount)

    return price - discount
```

## クラス設計

### Pydantic BaseModel（原則）

データクラスにはPydantic BaseModelを原則使用する。バリデーション、シリアライゼーション、型安全性を提供。

```python
from pydantic import BaseModel, Field, field_validator

class User(BaseModel):
    id: int
    name: str = Field(min_length=1, max_length=100)
    email: str
    tags: list[str] = Field(default_factory=list)

    @field_validator('email')
    @classmethod
    def lowercase_email(cls, v: str) -> str:
        return v.lower()

# イミュータブル
class Point(BaseModel):
    model_config = {'frozen': True}

    x: float
    y: float

# ネストしたモデル
class Order(BaseModel):
    id: int
    user: User
    items: list[str]
```

### Pydanticの主要機能

```python
from pydantic import BaseModel, Field, ConfigDict, field_validator, model_validator

class Product(BaseModel):
    model_config = ConfigDict(
        frozen=True,           # イミュータブル
        str_strip_whitespace=True,  # 文字列の前後空白を削除
        validate_assignment=True,   # 代入時もバリデーション
    )

    name: str = Field(min_length=1)
    price: float = Field(gt=0)
    quantity: int = Field(ge=0, default=0)

    @field_validator('name')
    @classmethod
    def name_must_not_be_empty(cls, v: str) -> str:
        if not v.strip():
            msg = 'name cannot be empty'
            raise ValueError(msg)
        return v

    @model_validator(mode='after')
    def check_total(self) -> 'Product':
        if self.price * self.quantity > 1000000:
            msg = 'total value too high'
            raise ValueError(msg)
        return self
```

### dataclass（軽量な用途のみ）

Pydanticのオーバーヘッドを避けたい場合のみdataclassを使用。

```python
from dataclasses import dataclass

@dataclass(frozen=True, slots=True)
class Coordinate:
    x: float
    y: float
```

### Protocol

```python
from typing import Protocol

class Readable(Protocol):
    def read(self) -> str: ...

class Writable(Protocol):
    def write(self, data: str) -> None: ...

def process(source: Readable, dest: Writable) -> None:
    data = source.read()
    dest.write(data)
```

### ABC

```python
from abc import ABC, abstractmethod

class BaseProcessor(ABC):
    @abstractmethod
    def process(self, data: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    def validate(self, data: dict[str, Any]) -> bool:
        return bool(data)
```

### ClassVar

```python
from typing import ClassVar

class Registry:
    _instances: ClassVar[dict[str, 'Registry']] = {}

    @classmethod
    def register(cls, name: str, instance: 'Registry') -> None:
        cls._instances[name] = instance
```

## ruffルール対応

### EM101/EM102: エラーメッセージ

```python
# NG
raise ValueError('invalid input')

# OK
msg = 'invalid input'
raise ValueError(msg)

# OK（f-string）
msg = f'invalid value: {value}'
raise ValueError(msg)
```

### TRY003: 長いメッセージ

```python
# NG
raise ValueError('This is a very long error message that explains the problem')

# OK
msg = 'This is a very long error message that explains the problem'
raise ValueError(msg)
```

### ANN401: Any型の使用

```python
# 必要な場合はnoqaを使用
def load(self, path: str | Path) -> Any:  # noqa: ANN401
    ...
```

### S101: assertの使用（テスト以外）

```python
# テストファイルでは自動的に無視される（per-file-ignores設定）
# プロダクションコードでは使用しない
```
