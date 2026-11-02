# session 内の補助ファイル

- `plan.md` には目的、制約、対象ファイル、完了条件を短く書いてください。`changes.md` には何を変えたかと理由、`verification.md` には実行した検証、未検証事項、残リスクを書いてください。
- `handoff.md` は計画的に別 agent・別 sessionへ引き継ぐ場合だけ作成してください。突発的なusage limitやmodel limitでは直近の`checkpoint.md`を引き継ぎ元とします。差分、issue、PR、ADR、検証ログに既にある内容を重複コピーせず、参照先と次に見るべき順序を中心に書いてください。
- `CONTEXT.md` や ADR は有用ですが、repo が明示的に採用していない限り repo 直下へ作成しないでください。用語整理や設計判断のメモは、まず該当 session directory 内に置いてください。恒久化する場合は、なぜ残すのかと後から参照する読者を明確にしたうえで、ユーザー確認を取ってください。
