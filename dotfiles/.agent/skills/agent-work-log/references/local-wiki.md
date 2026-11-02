# local wiki

- 大きな作業、長期作業、複数 session にまたがる作業では、`$PWD/.agent/work/wiki/` を LLM-maintained local wiki として使ってください。session directory は一次記録、wiki は再利用しやすい統合メモとして分けてください。小さい作業では wiki 更新を省略して構いません。
- `wiki/purpose.md` には wiki の目的と判断基準、`wiki/index.md` には内容カタログ、`wiki/log.md` には append-only の更新履歴、`wiki/overview.md` には現在状態の synthesis を書いてください。raw event log や command log は session directory 側に残してください。
- `wiki/` には検証済み事実、未検証事項、参照先 session を明示してください。統合対象は、後から再利用する価値がある判断、手順、失敗原因、検証結果に絞ってください。
- subagent や reviewer は原則として `wiki/` を直接更新せず、`sessions/.../subagents/` に結果を残してください。メインの agent が採否を判断し、必要な内容だけ `wiki/` に統合してください。
- この運用は軽量な Markdown wiki に留めてください。外部 wiki アプリ、vector/graph index、常駐プロセス、検索 DB、hook 自動化、raw source の自動複製は導入しないでください。
