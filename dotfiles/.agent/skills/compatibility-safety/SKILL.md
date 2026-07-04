---
name: compatibility-safety
description: "Use when a code, config, schema, API, or workflow change might add compatibility behavior, aliases, silent fallbacks, default-value fallbacks, legacy paths, or alternate names without an explicit contract."
---

# Compatibility Safety

UTILITY SKILL. 互換レイヤ、alias、silent fallback、default fallback、legacy path は、明示要件か既存契約がある場合だけ追加する。

## USE FOR:

- rename で新旧名を両方受け付けたくなった。
- missing config / env / arg を default で補いたくなった。
- 古い path、古い API、別名、互換 wrapper を残すか迷った。
- 「念のため」「利用者がいるかも」だけで分岐を増やしそう。

## DO NOT USE FOR (互換動作が正当なケース):

- ユーザーが後方互換や段階移行を明示している。
- 公開 API、保存済みデータ、外部連携、運用手順を壊す影響が確認済み。
- 既存仕様やテストが互換動作を要求している。

これらのケースでは互換動作を追加してよい。その際は互換対象、削除条件、検証方法を明記して進める。

## STEPS

1. alias、fallback、legacy path、default 値補完を追加していないか見る。
2. 必要なら根拠をユーザー指示、仕様、テスト、運用制約に結びつける。
3. 根拠がなければ fail fast に寄せる。

## RULES

- 根拠のない alias、fallback、legacy path、default 値補完は足さない。
- 安全に続行できない状態は、静かに補正せず明確なエラーにする。
- `os.getenv(..., default)`、空文字 default、場当たり的な代替 path で設定不足を隠さない。
- 根拠が読めない場合は、推測で互換レイヤを作らず確認する。

## REVIEW

- 新旧名を同時に受ける契約があるか。
- 移行が必要なら期限と削除予定があるか。
