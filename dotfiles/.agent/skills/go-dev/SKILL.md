---
name: go-dev
description: "Use when the user asks to implement, refactor, test, debug, or review Go code, Go modules, error handling, concurrency, interfaces, generics, or go test/build/vet failures. Read once per session. Do not use for shell scripts or CI YAML that only invoke go, or for non-Go services in the same repository."
---

# Go 開発スキル

Go の実装、テスト、デバッグ、リファクタリングを、対象 module のバージョンと既存の慣習に合わせて進めるための手順と判断基準。テスト先行と互換レイヤの扱いも本文に含む。

この skill は session につき 1 回だけ読む。compaction 後は `checkpoint.md` の「適用中の skill と解決済みの実行形」を見て続け、SKILL.md を読み直さない。`$go-dev` で本文が注入済みの場合も再読しない。

## 着手前の確認

次の順に読み、決めた内容を `checkpoint.md` に 1〜2 行で残す。repo が無く相談だけの依頼では設定ファイルを探さず、前提にしたバージョンや設定を回答に明記する。

1. `go.mod`: `go` ディレクティブのバージョンと依存。ループ変数の per-iteration 化(1.22+)、`range over int`(1.22+)、`slices` / `maps`(1.21+)、`log/slog`(1.21+)は、対象バージョンが対応する場合だけ使う。`go.work` があれば workspace 構成も見る。
2. `Makefile` / `Taskfile.yml` / `.golangci.yml`: build、test、lint の実行形。あればそれを使い、無ければ `go build ./...`、`go test ./...`、`go vet ./...` を実行形として決める。
3. 変更対象の周辺コード: package 構成(`cmd/` + `internal/` か否か)、エラーのラップ方法、`context.Context` の受け渡し、テストヘルパーとモックの作り方。既存パターンはこの skill の既定より優先する。

## 進め方

1. 振る舞いの変更ごとに、失敗するテストを先に書き、`go test ./<package>/... -run <TestName>` で失敗を確認してから最小の実装で通し、その後に整理する。テストは既存のテーブル駆動の流儀に合わせる。
   - 対象外: 一回限りの script、config / CI の修正、生成コード(`go generate` の出力)。これらは既存の check を回すだけにする。
2. alias、silent fallback、default 値補完、legacy path、非等価な代替経路は、明示要件か既存契約がなければ追加せず、明確なエラーを返す。実際に書きそうになった時点でだけ `compatibility-safety` を読む。
3. lint 指摘はコード側を直す。`//nolint` は理由を添えられる場合だけ使い、`.golangci.yml` の緩和で通さない。
4. goroutine、channel、共有 map / slice を触った変更は `go test -race` を必ず通す。

## 規約と判断基準

プロジェクトに規約や既存パターンがあればそれに従う。無い場合の既定:

- interface は提供側でなく使用側の package で、必要なメソッドだけ定義する。実装の準拠は `var _ UserRepository = (*userRepository)(nil)` でコンパイル時に確認する。
- エラーは `fmt.Errorf("find user %d: %w", id, err)` のように `%w` で文脈を付けてラップし、判定は文字列比較でなく `errors.Is` / `errors.As` を使う。sentinel error とカスタム error 型の使い分けは [references/coding-standards.md](references/coding-standards.md) の「エラーハンドリング」を見る。
- package 名は小文字 1 語でディレクトリ名と一致させ、`user.UserService` のような stuttering を避ける。
- 外部 I/O やブロックし得る処理は `context.Context` を第一引数で受け、タイムアウトやキャンセルは呼び出し側が `context.WithTimeout` + `defer cancel()` で制御する。
- エラーを返す goroutine 群は `errgroup` を第一候補にする。並行数の制限はセマフォ、ストリーム処理はワーカープールや fan-in / fan-out。実装例は [references/common-patterns.md](references/common-patterns.md) の「並行処理パターン」。
- 省略可能な設定が多いコンストラクタは functional options(`Option func(*Config)`)にする。実装例は同ファイル。
- golangci-lint の個別ルール(errcheck、govet、ineffassign、unconvert、revive の exported)で迷ったら [references/coding-standards.md](references/coding-standards.md) の「golangci-lint対応」を見る。
- 標準ライブラリ(context / errors / io / net/http / encoding/json / sync / time / log/slog)の signature や定番の使い方に迷ったら [references/api_reference.md](references/api_reference.md) を見る。

テストの既定:

- テーブル駆動テスト + `t.Run` のサブテストを既定にし、ヘルパーには `t.Helper()` を付ける。
- `t.Parallel()` を使うときは、グローバル変数やテストデータの使い回しなど、ケース間の共有状態がないことを確認する。
- モックは interface を満たす手書きの fake を既定とし、`moq` などの生成はプロジェクトが採用している場合だけ使う。Golden files、ベンチマーク、ビルドタグによる統合テストの書き方は [references/testing-guide.md](references/testing-guide.md) を見る。

## 完了前の確認

決めた実行形で次を実行する(`Makefile` があればその target を優先する)。

```bash
go build ./... && go vet ./...
go test ./<変更した package>/...   # 通ったら go test ./... で全体
go test -race ./...               # 並行処理や共有状態を触った場合
golangci-lint run                 # .golangci.yml がある場合。未導入なら missing-tools を読む
```

- 変更した振る舞いに対応する `_test.go` を追加・更新したか。難しい場合は理由と代替の検証を報告に書く。
- 実行できない検証は、コマンド、失敗理由、残るリスクを報告に回す。

## 報告に含めること

- 変更ファイルと目的。規約の根拠(既存パターンか、この skill の既定か)。
- 実行した検証コマンドと結果(失敗 0 件、`-race` の有無など)。未実行の検証とその理由。
- 追加・更新したテスト。
- 互換レイヤや default 補完を入れた場合は、その根拠と削除条件。
- PR description を書く段階になったら `eng-practices` を読む。それ以外の場面では読まない。
