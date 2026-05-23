# Chezmoi Home Source

English version: [README.md](README.md)

このディレクトリは chezmoi の source state です。
repo root の `.chezmoiroot` はここを指しています。

## 構成

| Path | 用途 |
|---|---|
| `dot_*` | 先頭 dot 付きで `$HOME` に render される file。 |
| `.chezmoitemplates/` | chezmoi source file や sync check が使う共有 template。 |
| `private_dot_config/` | `~/.config/` 配下へ render される source file。 |

## 更新ルール

- chezmoi で `$HOME` に直接適用する file はここを編集します。
- `config/` に対応 source がある場合は、generated source state と source 側を揃えます。
- 実 secret は commit しません。secret 関連は template または example にします。
- live home に適用する前に `scripts/chezmoi_apply.sh --dry-run` を使います。

## よく使う確認コマンド

```bash
zsh scripts/chezmoi_apply.sh --dry-run
zsh tests/test_chezmoi_source_state.sh
zsh tests/test_chezmoi_rendered_home.sh
```
