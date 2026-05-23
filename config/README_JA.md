# Configuration Sources

English version: [README.md](README.md)

このディレクトリは、Nix、chezmoi template、setup script が参照する設定ソースを置く場所です。
ここにあるファイルがそのまま `$HOME` にコピーされるとは限らず、script で生成・render・import されるものもあります。

## 構成

| Path | 用途 |
|---|---|
| `alacritty/` | Alacritty terminal config の source。 |
| `ghostty/` | Ghostty terminal config の source。 |
| `mise/` | repo 管理の mise tools、tasks、update command。 |
| `mouse/` | pointing device 設定 export。 |
| `nix/` | Nix、nix-darwin、Home Manager、package list、migration report。 |
| `nvim/` | Home Manager で使う Neovim config source。 |
| `shell/` | chezmoi が使う shell template と local secret example。 |
| `zellij/` | Zellij config source。 |

互換用に空または agent 名の directory が存在する場合があります。
削除前に、その directory を消費する script を確認してください。

## 更新ルール

- `home/` に render される file を変更する場合は、source と generated chezmoi state を揃えます。
- 実 secret は commit しません。追跡する template は `shell/secrets.env.example` を使います。
- Nix package / module の変更では通常 `nix/README_JA.md` の確認も必要です。
- mise tool 変更は `home/.chezmoitemplates/mise-config.toml` と同期させます。

## よく使う確認コマンド

```bash
zsh tests/run.sh
zsh tests/test_chezmoi_source_state.sh
zsh tests/test_nix_migration.sh
```
