# テストガイド

## 目次

1. [pytest基本](#pytest基本)
2. [フィクスチャ](#フィクスチャ)
3. [モック](#モック)
4. [パラメータ化](#パラメータ化)
5. [非同期テスト](#非同期テスト)

## pytest基本

### テストファイル構成

```
tests/
├── conftest.py              # 共有フィクスチャ
├── nlp/
│   ├── __init__.py
│   ├── test_env.py
│   └── common/
│       └── utils/
│           ├── test_cli_utils.py
│           └── file/
│               ├── test_factory.py
│               └── test_json.py
```

### 基本的なテスト

```python
import pytest

from nlp.calculator import add, divide

# テスト関数には必ず戻り値型 -> None を明示
def test_add() -> None:
    assert add(2, 3) == 5

def test_add_negative() -> None:
    assert add(-1, 1) == 0

def test_divide_by_zero() -> None:
    with pytest.raises(ZeroDivisionError):
        divide(1, 0)

def test_divide_by_zero_message() -> None:
    with pytest.raises(ZeroDivisionError, match='division by zero'):
        divide(1, 0)
```

### マーカー

```python
import pytest
import sys

@pytest.mark.skip(reason='未実装')
def test_future_feature() -> None:
    pass

@pytest.mark.skipif(sys.version_info < (3, 13), reason='Python 3.13+が必要')
def test_new_syntax() -> None:
    pass

@pytest.mark.slow
def test_heavy_computation() -> None:
    pass
```

## フィクスチャ

### 基本的なフィクスチャ

```python
import pytest

from nlp.database import Database

@pytest.fixture
def db() -> Database:
    database = Database(':memory:')
    database.setup()
    yield database
    database.cleanup()

def test_insert(db: Database) -> None:
    db.insert({'id': 1, 'name': 'test'})
    assert db.count() == 1
```

### スコープ

```python
@pytest.fixture(scope='module')
def expensive_resource() -> Resource:
    return create_expensive_resource()

@pytest.fixture(scope='session')
def global_config() -> dict[str, Any]:
    return load_config()
```

### conftest.py

```python
# tests/conftest.py
import pytest

@pytest.fixture
def sample_user() -> dict[str, Any]:
    return {'id': 1, 'name': 'Test User', 'email': 'test@example.com'}

@pytest.fixture
def auth_headers() -> dict[str, str]:
    return {'Authorization': 'Bearer test-token'}
```

## モック

### unittest.mock

```python
from unittest.mock import Mock, patch, MagicMock

def test_with_mock() -> None:
    mock_client = Mock()
    mock_client.get.return_value = {'status': 'ok'}

    result = process_with_client(mock_client)

    mock_client.get.assert_called_once_with('/api/status')
    assert result == 'ok'

@patch('nlp.services.external_api')
def test_with_patch(mock_api: Mock) -> None:
    mock_api.fetch.return_value = {'data': [1, 2, 3]}

    result = get_data()

    assert result == [1, 2, 3]

def test_with_context_manager() -> None:
    with patch('nlp.config.get_setting') as mock_setting:
        mock_setting.return_value = 'test_value'
        assert get_current_setting() == 'test_value'
```

### pytest-mock

```python
from pytest_mock import MockerFixture

def test_with_mocker(mocker: MockerFixture) -> None:
    mock_func = mocker.patch('nlp.utils.expensive_operation')
    mock_func.return_value = 42

    result = process()

    assert result == 42
    mock_func.assert_called_once()
```

## パラメータ化

```python
import pytest

# タプルで引数を指定（ruff PT006対応）
@pytest.mark.parametrize(('input_val', 'expected'), [
    ('hello', 5),
    ('World', 5),
    ('', 0),
    ('日本語', 3),
])
def test_length(input_val: str, expected: int) -> None:
    assert len(input_val) == expected

@pytest.mark.parametrize(('a', 'b', 'expected'), [
    (1, 2, 3),
    (0, 0, 0),
    (-1, 1, 0),
])
def test_add(a: int, b: int, expected: int) -> None:
    assert add(a, b) == expected

# 複数パラメータの組み合わせ
@pytest.mark.parametrize('x', [1, 2])
@pytest.mark.parametrize('y', [10, 20])
def test_combinations(x: int, y: int) -> None:
    assert x * y > 0
```

## 非同期テスト

```python
import pytest
import asyncio

@pytest.mark.asyncio
async def test_async_function() -> None:
    result = await fetch_data()
    assert result is not None

@pytest.mark.asyncio
async def test_async_with_timeout() -> None:
    async with asyncio.timeout(5):
        result = await slow_operation()
        assert result == 'done'

@pytest.fixture
async def async_client() -> AsyncClient:
    client = AsyncClient()
    await client.connect()
    yield client
    await client.disconnect()

@pytest.mark.asyncio
async def test_with_async_fixture(async_client: AsyncClient) -> None:
    response = await async_client.get('/api/data')
    assert response.status == 200
```

### pyproject.toml設定

```toml
[dependency-groups]
dev = [
    "pytest>=9.0.0",
    "pytest-beartype>=0.2.0",
]

[tool.pytest.ini_options]
filterwarnings = ["ignore::DeprecationWarning"]
```
