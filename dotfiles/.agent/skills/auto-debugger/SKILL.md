---
name: auto-debugger
description: "Use when the user gives an error message, failing test, stack trace, or build/runtime failure and asks to debug, fix, or explain it, in any language. Not for bugs without a one-command reproduction, flaky tests, or performance regressions: hand those to diagnosing-bugs. Not for CI workflow failures (use ci-cd)."
---

# Auto Debugger

1 コマンドの再現を確保し、print デバッグで原因を確定してから最小修正とリグレッションテストを入れて報告する。

## diagnosing-bugs との分担

- この skill は「1 コマンドで赤になる再現」が手元にある、またはすぐ作れる bug 向け。
- 次のいずれかに当たったら、以降の Phase を止めて `diagnosing-bugs` に切り替える(feedback loop の構築からやり直す)。
  - 再現コマンドが短時間で作れない、または再現率が低い。
  - 性能退行、メモリリーク、タイミング依存(並行処理、flaky test)。
  - Phase 2 の調査ループを 3 回回しても原因が絞れない。
- 切り替え時は、ここまでの再現手順と、試した仮説と結果を引き継ぎ、同じ調査を繰り返さない。

## Phase 1: 再現と文脈

1. エラーメッセージ、例外型、スタックトレースの失敗行と呼び出し元を抽出する。
2. 「1 コマンドで成否が分かる再現」(既存テスト、最小スクリプト、CLI 呼び出し)を確保して実行する。プロジェクトのテストコマンドは CI 設定や README から特定する。
3. スタックトレースに出るファイル、その呼び出し元、既存テストを読む。`git diff` / `git log -p` で直近の変更が失敗行に関係していないか見る。
4. `.env` や認証情報は読まない。設定値が疑わしいときはユーザーに確認する。

## Phase 2: 仮説と print デバッグ

- 疑わしい箇所を複数挙げ、失敗行 → 呼び出し元 → 入力データ → 設定値の順に検証する。環境差、テスト順序依存、並行処理が疑われるときは [references/error-patterns.md](references/error-patterns.md) を読む。
- 本番コードに print を入れず、問題の関数を直接呼ぶ独立スクリプトを scratch 領域(既存の一時ディレクトリ、`tmp/`)に作り、入力・中間値・戻り値を `[DEBUG]` 付きで出力して値、型、経路を確認する。
- 仮説 → スクリプト実行 → 絞り込みを、原因が確定するまで繰り返す(上限 3 回、超えたら `diagnosing-bugs` へ)。
- 読んだだけの「これかも」で修正しない。出力で確認してから Phase 3 に進む。

## Phase 3: 修正

- 根本原因を解消する最小変更にする。null チェック追加か上流修正かは error-patterns.md の判断基準に従う。
- 呼び出し元を `rg` で洗い出し、影響範囲を確認する。
- 言語の実装規約は `python-dev` / `go-dev` / `typescript-dev` に従う(該当するときだけ読む)。
- 調査中に見つけた別の問題は報告に回し、今回の修正に含めない。

## Phase 4: リグレッションテスト

- 原因となった入力・状態を再現するテストを既存テストファイルに追加し、修正前に失敗、修正後に成功することを確認する。
- 追加したテストと既存テストの両方を実行する。
- テスト基盤がない、外部サービスが要る、再現を自動化できない場合は、代替検証(最小再現コマンド、型チェック、lint、手動確認手順)を実行し、未検証範囲を明示する。
- デバッグ用スクリプトは削除する。削除できない理由があれば報告する。

## Phase 5: 報告

```markdown
## デバッグ報告
### 根本原因: (1〜2 文)
### 調査プロセス: 仮説ごとに検証結果(× / ✓)
### 実施した修正: `path/to/file.py:XX` と内容
### 追加したテスト: ファイルとカバーするケース
### 注意点: 類似パターンの残存、未検証範囲(該当時のみ)
```

修正を PR にする場合は、Phase 4 のテストを同じ PR に含める。`eng-practices` は PR description を書く段階でだけ読む。
