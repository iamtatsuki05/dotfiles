# Chezmoi Home Source

English version: [README.md](README.md)

このディレクトリは chezmoi の source state です。
repo root の `.chezmoiroot` はここを指しています。

## 構成

| Path | 用途 |
|---|---|
| `dot_*` | 先頭 dot 付きで `$HOME` に render される file。 |
| `.chezmoitemplates/` | chezmoi source file や sync check が使う canonical な共有 template。 |
| `private_dot_config/` | `~/.config/` 配下へ render される source file。 |

## 更新ルール

- chezmoi で `$HOME` に直接適用する file はここを編集します。
- `config/` に対応 source がある場合は、generated source state と source 側を揃えます。
- shell integration の値は `home/.chezmoidata.toml` を唯一の source とし、
  `config/nix/home-manager/session.nix` で重複して定義しません。
- Bash/Zsh 共通実装は `home/.chezmoitemplates/dotfiles-shell-common.sh` だけに置きます。
  Fish と csh/tcsh は、それぞれの `private_dot_config/` template にある native adapter です。
- 実 secret は commit しません。secret 関連は template または example にします。
- live home に適用する前に `scripts/chezmoi_apply.sh --dry-run` を使います。
- shell 専用 host では `bash scripts/setup_shell.sh --dry-run` を使います。
  対象は6つの target に固定され、無関係な home file は適用しません。
- shell の管理境界、固定 managed path、custom XDG、起動ルールは、[設定の管理境界](../docs/configuration-ownership_JA.md) にまとめています。

## よく使う確認コマンド

```bash
zsh scripts/chezmoi_apply.sh --dry-run
zsh tests/test_chezmoi_source_state.sh
zsh tests/test_chezmoi_rendered_home.sh
```
