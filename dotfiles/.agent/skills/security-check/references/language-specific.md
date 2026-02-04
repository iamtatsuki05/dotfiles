# 言語別セキュリティチェックリスト

## Python

### 危険な関数/パターン
```
eval\(
exec\(
compile\(
__import__\(
pickle\.load
yaml\.load\((?!.*Loader=yaml\.SafeLoader)
subprocess\..*shell=True
os\.system\(
os\.popen\(
input\(  # Python 2のみ危険
```

### 推奨される安全な代替
- `eval()` → `ast.literal_eval()`
- `pickle` → `json`
- `yaml.load()` → `yaml.safe_load()`
- `os.system()` → `subprocess.run([...], shell=False)`

---

## JavaScript/TypeScript

### 危険な関数/パターン
```
eval\(
new Function\(
innerHTML\s*=
outerHTML\s*=
document\.write\(
\.html\(  # jQuery
dangerouslySetInnerHTML
child_process\.exec\(
```

### 推奨される安全な代替
- `innerHTML` → `textContent`
- `eval()` → JSONパース
- `child_process.exec()` → `child_process.spawn()` with args

---

## Go

### 危険な関数/パターン
```
fmt\.Sprintf.*%s.*SQL
exec\.Command\(.*\+
template\.HTML\(
template\.JS\(
```

### 推奨
- SQLは`database/sql`のプレースホルダを使用
- テンプレートは`html/template`を使用

---

## Java

### 危険な関数/パターン
```
Runtime\.getRuntime\(\)\.exec\(
ProcessBuilder.*\.command\(
ObjectInputStream
XMLDecoder
Statement.*execute.*\+
```

### 推奨
- PreparedStatementを使用
- シリアライズはJSONを使用

---

## Ruby

### 危険な関数/パターン
```
eval\(
system\(
exec\(
`.*#{
send\(.*params
constantize
```

---

## PHP

### 危険な関数/パターン
```
eval\(
exec\(
system\(
passthru\(
shell_exec\(
\$_GET\[
\$_POST\[
\$_REQUEST\[
include\s*\$
require\s*\$
unserialize\(
```

---

## 共通のセキュリティベストプラクティス

### 入力検証
1. ホワイトリスト方式を優先
2. 長さ制限を設定
3. 型チェックを実施
4. エンコーディングを正規化

### 出力エスケープ
1. コンテキストに応じたエスケープ（HTML, JS, URL, SQL）
2. テンプレートエンジンの自動エスケープを使用
3. Content-Typeヘッダーを正しく設定

### 認証・認可
1. パスワードはbcrypt/Argon2でハッシュ
2. セッショントークンは暗号的に安全な乱数で生成
3. CSRF対策トークンを使用
4. 最小権限の原則を適用
