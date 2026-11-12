---
name: consulting-pptx-skill
description: スライド設計規約 slide-rules.md を核に、経営会議品質のスライドを作るスキル。規約は工程ごとに必要な節だけを scripts/rules.py で引き、規約を満たすことを確かめた部品（new_deck.py --list の20種類）で組み、check_deck.py・check_layout.mjs の機械チェック FAIL 0 で仕上げる。既存の PowerPoint 資料を渡され、そこへ差し込むページを PPTX で求められたときだけ、その資料のマスターの上に直接組む（slide-rules §8.7）。トリガー例:「コンサル品質のスライドを作って」「規約に沿ったデッキで」「この pptx に足すページを同じ書式で」。
---

# コンサル型スライド作成スキル

ローカル改変版です。上流は Carnot AI Inc. の consulting-pptx-skill です。ブラウザ接続・一時ファイル・画像削除と更新手順を、この dotfiles 環境向けに修正しています。上流公式の配布版ではありません。

規約の正典は `references/slide-rules.md`。全文は読まず、**工程ごとに `scripts/rules.py --phase <工程>` の要点を切らずに読み、根拠が要る項だけ `--sec 4.12` で引く。** 成果物は HTML（16:9・1 section = 1スライド）と `snapshot.py` で印刷した PDF。

| 工程 | 読む・使う |
| --- | --- |
| 1. 定義 | `--phase plan`、`references/working-practices.md` |
| 2. 調べる | 一次情報。確かめた数字は `sources.md` に控える |
| 3. ストーリーライン | `new_deck.py --list` |
| 4. 骨組み | `new_deck.py --parts ...`（作りを見るときだけ `--show b09`） |
| 5. 中身を書く | `--phase build`、形に応じて `chart` / `table` / `process` |
| 6. 文章を仕上げる | `--phase text`、`references/ai-smell-lexicon.md` |
| 7. 検査と直し | `check_all.py`、指摘に出た節だけ `--sec` |
| 8. 最後の確認 | `--phase check`、`snapshot.py` の PDF と画像 |
| 9. フレッシュアイ・レビュー | `references/content-review-prompt.md` |

- 工程1で足して読むもの：`local/`（git 管理外）の `slide-rules.local.md`・`forbid.txt`・`old-colors.txt`（local が優先。他社の書式で組むときは check_deck に `--brand none`）／テーマが AI・業務の自動化なら `references/jinba-context.md`／PPTX にするなら `--phase pptx`／登壇用ピッチなら `--phase pitch`。

## 手順

1. **定義する**: 目的・成果物の定義・スコープ IN/OUT を3〜5行で決める。誰に・どの会議で・何を判断してもらうかが分からなければ聞く。聞けなければ前提を置いて報告に書き、デッキにせず1枚の論点整理で出す。
2. **調べる（必須）**: 載せる数字・事例・固有名詞は一次情報で確かめ、`sources.md` に1行ずつ控える（数字・何の数字か・出典名・URL・時点）。試算は `python3 -c` で計算し、式・入力・結果を貼る。見つからない数字は作らず「要確認」と書く。`sources.md` と `qa.md` はデッキと同じフォルダに置く。
3. **ストーリーラインを書く**: 1枚1行の主張の列を書き、材料の関係から形を決め（§5.12）、最後に部品を選ぶ。型どおりの並びにせず（§4.103）、薄いページは隣とまとめる（§5.13）。各ページの「タイトル／結論／載せる内容／見せ方」を文章で出し、合意してから組む。
4. **骨組みを作る**: `python3 scripts/new_deck.py --parts b01,m01,b09:axb,m05,b10 --title "資料名" -o mydeck.html`。合う部品が無ければ `--list --all` を見るか、規約の範囲で自由に組む。
5. **中身をまとめて書く**: 伏せ字（`Text N`・`ラベル N`・`パーツNN｜…`）を全部実物に置き換えた完成版を1回の Write で書く。数字は `sources.md` の値だけを使う。
6. **文章を仕上げる**: タイトルを上から通し読みし、要るページだけ Edit する。
7. **検査して直す**: `python3 scripts/check_all.py mydeck.html --sources sources.md`（`PLAYWRIGHT_MODULE_DIR` が無いときだけ `npm run setup`）。指摘の節を `--sec` で引き、ページごとにまとめて形で直す（直し方は `working-practices.md`）。FAIL 0 まで回す。WARN も直すのが既定で、規約の例外に当たるものだけ § を qa.md に書いて残す。
8. **PDF で全ページを見る**: `python3 scripts/snapshot.py mydeck.html` の PDF と画像を全部見て（pdftoppm は別途入れる）、§8 の目視の基準で確かめた結果を `qa.md` にページごとに書く。直したら `check_all.py` を回し直す。
9. **フレッシュアイ・レビュー**（速度優先と言われたとき以外）: `content-review-prompt.md` を作り方を伏せて別エージェントに渡し、採用した指摘を直して検査を回し直す。1ラリーで止めて、続けるかを聞く。

## PowerPoint（.pptx）が要るとき

HTML で仕上げ、「PPTX で」と言われたときだけ最後に1回 `html_to_pptx.py` で変換する。既存の資料へ差し込むページは、その資料のマスターの上で組む（`measure_deck.py` → `deck_pptx.py`）。詳しくは `--phase pptx`。

## 本スキル使用の注釈

裏表紙（b10）の出典行に「本資料は consulting-pptx-skill（github.com/carnot-tech/consulting-pptx-skill）で作成」が既定で入っている。ツール名を置けるのは最終ページの出典行だけで、他のページに出ると check_deck が FAIL にする。

## ローカルでの更新・記録

- この skill は dotfiles の vendored ファイルです。更新は、依頼された場合だけ `dotfiles/.agent/skills/upstreams.json` と `scripts/agent_skill_upstreams.py` で固定コミット・ローカル差分・レビューを管理します。資料作成中に skill 内で `git fetch` / `git pull` を実行しないでください。
- 利用者の規約や案件情報は、依頼で指定された保存先にだけ記録してください。顧客名・案件コード・資料・skin.json を skill の配布ディレクトリや upstream に保存しないでください。永続的な規約追加は、対象と文面を提示し、既存の許可範囲を確認してから行います。
- skill の利用・更新依頼を、upstream の issue / PR、commit、push、外部送信の許可と解釈しないでください。外部投稿は明示的に依頼された範囲だけ行います。ライセンスと商標の扱いは同梱の LICENSE / TRADEMARK.md を保持します。
