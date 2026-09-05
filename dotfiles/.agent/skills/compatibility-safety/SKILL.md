---
name: compatibility-safety
description: "Use when a code, config, schema, API, or workflow change you are writing or reviewing adds an alias, silent fallback, default-value fallback, legacy path, alternate name, or a non-equivalent substitute runner/backend without an explicit contract. Not a routine pre-implementation read; plain renames or new code without such branches do not need it."
---

# Compatibility Safety

互換レイヤ、alias、silent fallback、default 値 fallback、legacy path は、明示要件か既存契約がある場合だけ追加する。根拠がなければ fail fast にする。

## USE FOR:

- rename で新旧名を両方受け付けたくなった。
- missing config / env / arg を default で補いたくなった。
- 古い path、古い API、別名、互換 wrapper を残すか迷った。
- 「念のため」「利用者がいるかも」だけで分岐を増やしそう。
- 要求された実行経路(runner、backend、ライブラリ)が失敗し、非等価な代替へ切り替えたくなった。
- 削除・刷新リファクタで legacy 定数、互換 default、旧 wrapper を残しそう。
- 差分レビューで上記を含む変更を見つけた。

## DO NOT USE FOR (互換動作が正当なケース):

- ユーザーが後方互換や段階移行を明示している。
- 公開 API、保存済みデータ、外部連携、運用手順を壊す影響が確認済み。
- 既存仕様やテストが互換動作を要求している。

これらのケースでは互換動作を追加してよい。その際は互換対象、削除条件、検証方法を明記して進める。

## 根拠のない fallback と判定するもの

- `os.getenv("X", default)`、`cfg.get("key", default)`、空文字や `/tmp/...` の default で、設定不足を隠すもの。
- `cfg.get("new") or cfg.get("old")`、新旧 key の両受け、`hasattr` / `try: import ... except ImportError` による旧名・旧依存の吸収。
- 要求された runner / backend / ライブラリが失敗したときに、別のもの(例: Singularity → uv、GCS mount → ローカル path、指定モデル → 別モデル)へ黙って切り替える。
- リファクタ後も参照されない legacy 定数、旧 wrapper、re-export、「後で消す」コメント付きの分岐を残す。
- 壊れた状態を warning だけで続行し、静かに補正する。

## STEPS

1. 差分に上記の分岐が含まれていないか見る。含まれるなら、根拠をユーザー指示、仕様、テスト、運用制約のどれかに結びつける。
2. 根拠がなければ削り、欠落は不足している key / 経路名を含む明確なエラーにする。
3. 非等価な代替経路へ切り替えたい場合は、実装せずに先にユーザーへ明示して確認する。
4. 根拠があって残す場合は、互換対象、削除条件(期限または移行完了条件)、検証方法をコードコメントか PR description に書く。

## REVIEW / 報告

- reviewer として読む場合は、該当箇所を file:line で列挙し、根拠あり / 根拠なし / 要確認に分ける。
- 最終報告と checkpoint.md には「互換レイヤ: なし」または「あり(対象、根拠、削除条件)」を 1 行で書き、compaction 後にこの skill を読み直さなくても判断を引き継げるようにする。
