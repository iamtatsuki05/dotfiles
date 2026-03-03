# エラーパターン別デバッグガイド

## 目次
1. [RuntimeError / 実行時エラー](#runtimeerror)
2. [TypeError / AttributeError](#typeerror)
3. [ImportError / ModuleNotFoundError](#importerror)
4. [ネットワーク・HTTP エラー](#network)
5. [データベースエラー](#database)
6. [テスト失敗](#test-failure)
7. [ビルド・コンパイルエラー](#build)
8. [NULL / nil / undefined エラー](#null)
9. [並行処理エラー](#concurrency)
10. [環境・設定エラー](#environment)

---

## RuntimeError / 実行時エラー {#runtimeerror}

**症状:** `RuntimeError`, `panic`, `Exception`, `Error` など実行中に発生

**調査手順:**
1. スタックトレースの最下部（最も深い呼び出し）から読む
2. 失敗したファイル・行番号を特定してコードを読む
3. その行で使用している変数の型・値を確認
4. 直前に変更されたコードがないか確認（git diff）

**よくある原因:**
- 境界値チェック漏れ（配列外参照、ゼロ除算）
- 状態が想定外のタイミングで変更されている
- 再帰の基底ケース欠如

---

## TypeError / AttributeError {#typeerror}

**症状:** `TypeError`, `AttributeError`, `cannot read property of undefined`

**調査手順:**
1. エラーメッセージから「何の型に何をしようとしたか」を読み取る
   - 例: `'NoneType' object has no attribute 'id'` → None が返ってきているはず
2. 問題の変数がどこで代入されるか Grep で検索
3. 代入箇所でどんな値が入りうるかを確認
4. 型アノテーション・型定義がある場合は確認

**よくある原因:**
- None/null/undefined チェック漏れ
- 関数の戻り値の型が変わった（リファクタ後など）
- オプショナルフィールドへの無条件アクセス

---

## ImportError / ModuleNotFoundError {#importerror}

**症状:** `ImportError`, `ModuleNotFoundError`, `Cannot find module`, `package not found`

**調査手順:**
1. モジュール名のタイポを確認
2. パッケージがインストールされているか確認
   ```bash
   pip list | grep <package>    # Python
   npm list <package>           # Node.js
   go list -m all | grep <pkg>  # Go
   ```
3. インポートパスが正しいか確認（相対 vs 絶対）
4. 仮想環境・実行環境が正しいか確認

**よくある原因:**
- 依存パッケージの未インストール
- 仮想環境の未アクティベート
- パスの大文字小文字の誤り（大文字小文字を区別するOS）

---

## ネットワーク・HTTP エラー {#network}

**症状:** `ConnectionRefused`, `timeout`, `ECONNREFUSED`, HTTP 4xx/5xx

**調査手順:**
1. HTTPステータスコードで判断:
   - 400: リクエストの形式・パラメータが不正
   - 401/403: 認証・認可の問題 → トークン・APIキーを確認
   - 404: URLが間違い → エンドポイントを確認
   - 500: サーバーサイドのエラー → サーバーログを確認
   - 503: サーバーが起動していない
2. `ConnectionRefused`: 接続先サービスが起動しているか確認
3. `timeout`: タイムアウト値・ネットワーク遅延を確認
4. リクエスト内容（URL、ヘッダー、ボディ）をログ出力して確認

**よくある原因:**
- 環境変数でURLが切り替わっていない（dev/staging/prodの混在）
- APIキーの期限切れ・未設定
- 接続先サービスが未起動

---

## データベースエラー {#database}

**症状:** `OperationalError`, `ProgrammingError`, `no such table`, `duplicate key`

**調査手順:**
1. エラーメッセージからSQLを抽出して確認
2. テーブル・カラムが存在するか確認（マイグレーション漏れ）
3. 制約違反（UNIQUE, NOT NULL, FK）の場合は挿入しようとしているデータを確認
4. トランザクションのデッドロックはクエリの実行順序を確認

**よくある原因:**
- マイグレーション未実行
- スキーマ変更後のコードが古い
- NULL不可カラムへのNULL挿入

---

## テスト失敗 {#test-failure}

**症状:** テストが `FAILED` / `FAIL` となる

**調査手順:**
1. 失敗したテスト名・ファイルを確認
2. アサーションの期待値と実際値を比較
3. テスト対象コードを読み、どのパスで実行されたか確認
4. モック・スタブが正しく設定されているか確認
5. テストの前提条件（fixtures, setup）が正しいか確認

**アサーション失敗の読み方:**
```
AssertionError: assert 'foo' == 'bar'
  左辺: 実際の値
  右辺: 期待値
```

**よくある原因:**
- コードが変更されてテストが追いついていない
- テスト間の状態汚染（テストの実行順序に依存している）
- モックが本物の動作と乖離している

---

## ビルド・コンパイルエラー {#build}

**症状:** `syntax error`, `undefined: X`, `cannot use X as type Y`, `TS2345`

**調査手順:**
1. エラーメッセージ中のファイル名・行番号を確認
2. シンタックスエラーは前の行も確認（括弧の対応など）
3. 型エラーは型定義ファイル・インターフェースを確認
4. 未定義参照はインポート・宣言を確認

**TypeScript固有:**
- `TS2345`: 型の不一致 → 型アサーション or 型を修正
- `TS2304`: 名前が見つからない → インポート漏れ
- `TS2339`: プロパティが存在しない → 型定義を確認

**Go固有:**
- `undefined: X` → パッケージのインポート漏れ
- `cannot use X (type Y) as type Z` → 型の変換が必要

---

## NULL / nil / undefined エラー {#null}

**症状:** `null pointer dereference`, `nil pointer`, `undefined is not an object`

**調査手順:**
1. null/nil/undefined になっている変数を特定
2. その変数の代入箇所を Grep で検索
3. どのケースで null が返るかを確認（条件分岐・エラーパス）
4. null チェックを追加すべきか、null が返らないように上流を修正すべきかを判断

**修正パターン:**
```python
# Pythonの場合
if obj is None:
    raise ValueError("obj must not be None")
result = obj.method()

# またはデフォルト値
result = obj.method() if obj else default_value
```

---

## 並行処理エラー {#concurrency}

**症状:** `data race`, `deadlock`, `concurrent map read and map write`

**調査手順:**
1. 競合が起きているリソース（変数、マップ、ファイル）を特定
2. 複数のgoroutine/スレッドが同時にアクセスしている箇所を確認
3. ロックの取得・解放が対称になっているか確認
4. デッドロックはロックの取得順序を確認

**Goのdata race検出:**
```bash
go test -race ./...
go run -race main.go
```

---

## 環境・設定エラー {#environment}

**症状:** 動く環境と動かない環境がある、環境変数エラー

**調査手順:**
1. 動く環境と動かない環境の差分を確認
   - OS、言語バージョン、ライブラリバージョン
   - 環境変数の設定
   - 設定ファイルの内容
2. 環境変数が設定されているか確認
   ```bash
   echo $VARIABLE_NAME
   env | grep PREFIX
   ```
3. `.env` ファイルが存在しているか確認
4. 設定ファイルのパスが正しいか確認

**よくある原因:**
- `.env` ファイルの未コピー（`.env.example` のみある）
- 異なる言語バージョン（pyenv/nodenv/goenv の設定漏れ）
- ライブラリの互換性問題（lock ファイルと実際の差異）
