# よく使うパターン集

## 目次

1. [デザインパターン](#デザインパターン)
2. [関数型パターン](#関数型パターン)
3. [非同期パターン](#非同期パターン)
4. [DI（依存性注入）](#di依存性注入)
5. [設定管理](#設定管理)

## デザインパターン

### ファクトリ（ClassVar使用）

```python
from typing import ClassVar, Literal

FileFormat = Literal['json', 'yaml', 'toml', 'xml']

class FileHandlerFactory:
    _handlers: ClassVar[dict[FileFormat, type[FileHandler]]] = {
        'json': JsonFileHandler,
        'yaml': YamlFileHandler,
        'toml': TomlFileHandler,
        'xml': XmlFileHandler,
    }

    @classmethod
    def create(cls, format_type: FileFormat) -> FileHandler:
        handler_class = cls._handlers.get(format_type)
        if handler_class is None:
            supported = ', '.join(cls._handlers.keys())
            msg = f'Unsupported format: {format_type}. Supported: {supported}'
            raise ValueError(msg)
        return handler_class()

    @classmethod
    def from_path(cls, path: str | Path) -> FileHandler:
        suffix = Path(path).suffix.lstrip('.')
        if not suffix:
            msg = f'Cannot detect file format: no extension in {path}'
            raise ValueError(msg)

        extension_map: dict[str, FileFormat] = {
            'json': 'json',
            'yaml': 'yaml',
            'yml': 'yaml',
            'toml': 'toml',
            'xml': 'xml',
        }

        format_type = extension_map.get(suffix.lower())
        if format_type is None:
            msg = f'Unsupported extension: .{suffix}'
            raise ValueError(msg)

        return cls.create(format_type)
```

### ビルダー

```python
from pydantic import BaseModel, Field

class Request(BaseModel):
    model_config = {'frozen': True}

    url: str
    method: str = 'GET'
    headers: dict[str, str] = Field(default_factory=dict)
    body: str | None = None

class RequestBuilder:
    def __init__(self, url: str) -> None:
        self._url = url
        self._method = 'GET'
        self._headers: dict[str, str] = {}
        self._body: str | None = None

    def method(self, method: str) -> 'RequestBuilder':
        self._method = method
        return self

    def header(self, key: str, value: str) -> 'RequestBuilder':
        self._headers[key] = value
        return self

    def body(self, body: str) -> 'RequestBuilder':
        self._body = body
        return self

    def build(self) -> Request:
        return Request(
            url=self._url,
            method=self._method,
            headers=self._headers,
            body=self._body,
        )

# 使用例
request = (
    RequestBuilder('https://api.example.com/users')
    .method('POST')
    .header('Content-Type', 'application/json')
    .body('{"name": "test"}')
    .build()
)
```

### ストラテジー（Protocol使用）

```python
from typing import Protocol
from pydantic import BaseModel

class PricingStrategy(Protocol):
    def calculate(self, base_price: float) -> float: ...

class RegularPricing:
    def calculate(self, base_price: float) -> float:
        return base_price

class DiscountPricing:
    def __init__(self, discount_rate: float) -> None:
        self.discount_rate = discount_rate

    def calculate(self, base_price: float) -> float:
        return base_price * (1 - self.discount_rate)

class Order(BaseModel):
    model_config = {'arbitrary_types_allowed': True}

    items: list[float]
    pricing: PricingStrategy

    def total(self) -> float:
        return sum(self.pricing.calculate(price) for price in self.items)
```

## 関数型パターン

### returnsライブラリ

```python
from returns.result import Result, Success, Failure
from returns.maybe import Maybe, Some, Nothing
from returns.pipeline import pipe

def divide(a: float, b: float) -> Result[float, str]:
    if b == 0:
        return Failure('Division by zero')
    return Success(a / b)

def process_result(result: Result[float, str]) -> str:
    return result.map(lambda x: f'Result: {x}').value_or('Error occurred')

# Maybe
def find_user(user_id: int) -> Maybe[User]:
    user = db.get(user_id)
    return Some(user) if user else Nothing

# Pipeline
result = pipe(
    10,
    lambda x: x * 2,
    lambda x: x + 5,
    str,
)  # '25'
```

### デコレータ

```python
import functools
import time
import logging
from collections.abc import Callable
from typing import ParamSpec, TypeVar

P = ParamSpec('P')
R = TypeVar('R')

def log_calls(func: Callable[P, R]) -> Callable[P, R]:
    logger = logging.getLogger(func.__module__)

    @functools.wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        logger.info('Calling %s', func.__name__)
        result = func(*args, **kwargs)
        logger.info('Finished %s', func.__name__)
        return result

    return wrapper

def retry(max_attempts: int = 3, delay: float = 1.0):
    def decorator(func: Callable[P, R]) -> Callable[P, R]:
        @functools.wraps(func)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            for attempt in range(max_attempts):
                try:
                    return func(*args, **kwargs)
                except Exception:
                    if attempt == max_attempts - 1:
                        raise
                    time.sleep(delay * (attempt + 1))
            msg = 'Unreachable'
            raise RuntimeError(msg)
        return wrapper
    return decorator
```

## 非同期パターン

### 同期/非同期変換

```python
import asyncio
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any

def sync_to_async[R](sync_func: Callable[..., R]) -> Callable[..., Awaitable[R]]:
    async def wrapper(*args: object, **kwargs: object) -> R:
        return await asyncio.to_thread(sync_func, *args, **kwargs)

    wrapper.__name__ = sync_func.__name__
    wrapper.__doc__ = sync_func.__doc__
    return wrapper

def async_to_sync[R](async_func: Callable[..., Coroutine[Any, Any, R]]) -> Callable[..., R]:
    def wrapper(*args: object, **kwargs: object) -> R:
        return asyncio.run(async_func(*args, **kwargs))

    wrapper.__name__ = async_func.__name__
    wrapper.__doc__ = async_func.__doc__
    return wrapper
```

### セマフォ付き並行実行

```python
import asyncio
from collections.abc import Awaitable, Callable, Sequence

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

async def fetch_all_with_limit(
    urls: Sequence[str],
    limit: int = 5,
) -> list[str]:
    semaphore = asyncio.Semaphore(limit)

    async def fetch_one(url: str) -> str:
        async with semaphore:
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as response:
                    return await response.text()

    tasks = [fetch_one(url) for url in urls]
    return await asyncio.gather(*tasks)
```

### AsyncResource基底クラス

```python
from abc import ABC, abstractmethod
import asyncio

class AsyncResource[R](ABC):
    def __init__(self, concurrency: int = 1) -> None:
        self.semaphore = asyncio.Semaphore(concurrency)

    async def task(self, *args: object, **kwargs: object) -> R:
        async with self.semaphore:
            return await self.call(*args, **kwargs)

    @abstractmethod
    async def call(self, *args: object, **kwargs: object) -> R:
        raise NotImplementedError
```

## DI（依存性注入）

### injector

```python
from injector import Injector, Module, provider, singleton, inject

class DatabaseModule(Module):
    @singleton
    @provider
    def provide_connection(self) -> Connection:
        return Connection(os.environ['DATABASE_URL'])

class ServiceModule(Module):
    @provider
    def provide_user_service(self, conn: Connection) -> UserService:
        return UserService(conn)

class UserService:
    @inject
    def __init__(self, conn: Connection) -> None:
        self.conn = conn

injector = Injector([DatabaseModule(), ServiceModule()])
service = injector.get(UserService)
```

### dependency-injector

```python
from dependency_injector import containers, providers

class Container(containers.DeclarativeContainer):
    config = providers.Configuration()

    database = providers.Singleton(
        Database,
        url=config.database_url,
    )

    user_service = providers.Factory(
        UserService,
        database=database,
    )

container = Container()
container.config.database_url.from_env('DATABASE_URL')
service = container.user_service()
```

## 設定管理

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
    max_connections: int = 10

settings = Settings()
```

### 遅延初期化パターン

```python
from functools import lru_cache
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

class Config(BaseSettings):
    model_config = SettingsConfigDict(
        env_file='.env',
        env_file_encoding='utf-8',
    )

    database_url: str = Field(alias='DATABASE_URL')
    api_key: str = Field(alias='API_KEY')
    debug: bool = False
    max_connections: int = 10

@lru_cache
def get_config() -> Config:
    return Config()
```
