# Shared Hooks

English version: [README.md](README.md)

このディレクトリは、複数の local AI agent で共有する hook script を置く場所です。
`dotfiles/.agent/sync.sh` により、agent ごとの hook location へ symlink されます。

## Hooks

| Hook | 用途 |
|---|---|
| `agent_context_reminder.sh` | 対応 agent の prompt / session hook で、この repo 向け reminder context を出力する。 |
| `agent_turn_done_notify.sh` | turn 完了通知に対応する agent で使う共有完了音を鳴らす。 |
| `japanese_prose_lint.sh` | 日本語の Markdown / text 文書を検査し、対応する file edit 後に修正候補を返す。in-place edit(Edit / MultiEdit / apply_patch)では編集で入った行だけを報告するため、繰り返し語のような文書全体の集計は初出行を編集したときだけ出る。file 全体の write は全文が対象で、`.agent/work/` 配下は対象外。 |
| `jupytext_sync.sh` | agent が paired `.py` を編集したあと、対応する Jupyter notebook を同期する。 |

agent 固有の hook 登録は `../apps/` 配下にあり、agent によって JSON hook map または hook directory の shell script を読みます。
日本語 lint は、対応する全 agent で自動実行します。Claude Code、Codex、Copilot、Cursor、Devin、Antigravity は post-tool hook を使い、Hermes、OpenCode、OpenClaw は lint 結果を次の model 入力へ含めるための小さな plugin を使います。Grok は Claude 互換の hook 設定を自動的に読むため、二重登録しません。

## 日本語 lint

Markdown または plain text を共通 profile で検査します。

```bash
dotfiles/.agent/hooks/japanese_prose_lint.sh --check path/to/document.md
```

記事や論考では `--profile longform` を指定すると、文書の進行だけを述べる表現も検査します。検出結果は修正候補の提示だけに使い、ファイルを自動で書き換えません。Hook が一度に返すのは20件までで、残りは lint の再実行後に表示します。Markdown の frontmatter、fenced / indented code、引用、inline code、link destination、autolink、ASCII の raw URL は検査対象外です。終了 status は、問題なしが `0`、検出ありが `1`、入力不正または読み取り失敗が `2` です。

Agent adapter は `--hook-agent` で明示的に選び、payload 形式の推測や silent fallback は行いません。OpenClaw の plugin または設定を変更した後は Gateway の再起動が必要です。Codex では、変更された hook 定義を `/hooks` で確認し、信頼済みにする必要がある場合があります。

| Rule | 検出内容 |
|---|---|
| `JP001` | 全角ダッシュの連続。 |
| `JP002` | 装飾目的の絵文字。 |
| `JP003` | `シンプル。それだけ` のような劇的な断片化。 |
| `JP004` | `AではなくB` 型の対比が2回以上ある。 |
| `JP005` | 対象や変化が不明な「効く」が2回以上ある。 |
| `JP006` | 同じ種類の文末が1段落で3文以上続く。 |
| `JP007` | 敬体と常体が文書内でそれぞれ3文以上ある。 |
| `JP008` | `longform` profile で文書進行だけを述べている。 |

## 更新ルール

- 複数 agent で共有する hook は tool-agnostic に保ちます。
- hook script に secret を置きません。
- 編集後は shell syntax を検証します。
- hook 出力を変更した場合は、script 本体と呼び出し元の agent config を両方確認します。
- hook が agent 由来の JSON を読む場合は、代表 payload を `python3 -m json.tool` に通して確認します。

## よく使う確認コマンド

```bash
bash -n dotfiles/.agent/hooks/agent_context_reminder.sh
bash -n dotfiles/.agent/hooks/agent_turn_done_notify.sh
bash -n dotfiles/.agent/hooks/japanese_prose_lint.sh
bash -n dotfiles/.agent/hooks/jupytext_sync.sh
zsh tests/test_japanese_prose_lint.sh
printf '{}' | dotfiles/.agent/hooks/agent_context_reminder.sh | python3 -m json.tool
zsh tests/test_agent_sync.sh
```
