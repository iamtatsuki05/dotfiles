# OWASP Top 10 チェックリスト

## A01:2021 - アクセス制御の不備

### 検出パターン
- 認証なしでのAPIエンドポイントアクセス
- IDOR（Insecure Direct Object Reference）
- 権限昇格の可能性
- CORSの誤設定

### コード例（脆弱）
```python
# 危険: ユーザーIDを直接使用
@app.get("/api/users/{user_id}")
def get_user(user_id: int):
    return db.get_user(user_id)  # 認可チェックなし
```

### 修正例
```python
@app.get("/api/users/{user_id}")
def get_user(user_id: int, current_user: User = Depends(get_current_user)):
    if current_user.id != user_id and not current_user.is_admin:
        raise HTTPException(status_code=403)
    return db.get_user(user_id)
```

---

## A02:2021 - 暗号化の失敗

### 検出パターン
- ハードコードされたシークレット/APIキー
- 弱いハッシュアルゴリズム（MD5, SHA1）
- HTTP経由での機密データ送信
- 不適切な証明書検証

### Grepパターン
```
# シークレット検出
password\s*=\s*["']
api_key\s*=\s*["']
secret\s*=\s*["']
token\s*=\s*["']

# 弱いハッシュ
hashlib\.md5
hashlib\.sha1
```

---

## A03:2021 - インジェクション

### 検出パターン
- SQLインジェクション
- OSコマンドインジェクション
- XSS（クロスサイトスクリプティング）
- LDAPインジェクション
- テンプレートインジェクション

### コード例（脆弱）
```python
# SQLインジェクション
query = f"SELECT * FROM users WHERE id = {user_id}"

# コマンドインジェクション
os.system(f"ping {user_input}")

# XSS
return f"<div>{user_input}</div>"
```

### 修正例
```python
# パラメータ化クエリ
cursor.execute("SELECT * FROM users WHERE id = %s", (user_id,))

# 入力検証 + shlex
import shlex
subprocess.run(["ping", shlex.quote(user_input)])

# エスケープ
from markupsafe import escape
return f"<div>{escape(user_input)}</div>"
```

---

## A04:2021 - 安全でない設計

### 検出パターン
- レート制限の欠如
- ビジネスロジックの欠陥
- 不十分な入力検証
- エラー処理の不備

---

## A05:2021 - セキュリティ設定のミス

### 検出パターン
- デバッグモードが有効
- デフォルト認証情報
- 不要なサービス/機能
- 詳細すぎるエラーメッセージ

### Grepパターン
```
DEBUG\s*=\s*True
debug=true
FLASK_DEBUG
development
```

---

## A06:2021 - 脆弱で古いコンポーネント

### 検出方法
- package.json / requirements.txt / go.mod の依存関係確認
- 既知の脆弱性を持つバージョンのチェック

---

## A07:2021 - 識別と認証の失敗

### 検出パターン
- 弱いパスワードポリシー
- セッション固定攻撃
- 安全でないセッション管理
- 資格情報の露出

---

## A08:2021 - ソフトウェアとデータの整合性の失敗

### 検出パターン
- 署名なしのデータ受け入れ
- 安全でないデシリアライゼーション
- CI/CDパイプラインの脆弱性

### Grepパターン
```
pickle\.load
yaml\.load\(.*Loader
eval\(
exec\(
```

---

## A09:2021 - セキュリティログとモニタリングの失敗

### 検出パターン
- ログの欠如
- 機密情報のログ出力
- 監査証跡の不備

---

## A10:2021 - SSRF（サーバーサイドリクエストフォージェリ）

### 検出パターン
- ユーザー入力によるURL/IPの直接使用
- 内部サービスへのアクセス
- URLリダイレクトの悪用

### コード例（脆弱）
```python
url = request.args.get('url')
response = requests.get(url)  # SSRF
```
