# Agent Team user configuration

このdirectoryはstandaloneな`scripts/agent-team` projectで使う、dotfiles管理下のuser
configと日本語role promptを置く場所です。実行ファイル、アーキテクチャ、対応matrix、ACPの
説明は[`scripts/agent-team/README_JA.md`](../../../../scripts/agent-team/README_JA.md)を参照してください。

setup syncはこのdirectoryを`$XDG_CONFIG_HOME/agent-team`（通常は`~/.config/agent-team`）へlink
します。teamを変更するときは`config.toml`と`prompts/`をここで編集し、bundled package defaultを
直接編集しないでください。

`teams.toml`は名前付きteamの一覧です。`agent-team`の項目は`config.toml`を明示参照します。
`--config ~/.config/agent-team/teams.toml --team agent-team`で選択してください。
