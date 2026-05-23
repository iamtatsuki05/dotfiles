# Nix Configuration

English version: [README.md](README.md)

このディレクトリは、この repo が使う Nix package list、nix-darwin module、Home Manager module、migration report を置く場所です。

## 構成

| Path | 用途 |
|---|---|
| `darwin/` | macOS system settings、Homebrew fallback、auto-update timer 用の nix-darwin module。 |
| `home-manager/` | macOS / Linux profile で共有する Home Manager module。 |
| `packages/` | `dotfiles-packages.nix` から使う custom package 定義。 |
| `package-names.nix` | CLI package name 一覧。 |
| `gui-*-package-names.nix` | platform 別 GUI package list。 |
| `packages.nix` / `gui-packages.nix` | name list から package list を materialize する file。 |
| `dotfiles-packages.nix` | Waza など、flake が expose する custom package。 |
| `homebrew-fallback.nix` | まだ Nix に移せない Homebrew fallback entry。 |
| `mas-apps.nix` | `scripts/install_mas_apps.sh` が使う Mac App Store app list。 |
| `*-to-*.tsv`, `migrated-*`, `unmapped-homebrew.tsv` | Homebrew / MAS から Nix への migration map と report。 |
| `nix.conf` | Nix client 設定。 |

## 更新ルール

- Homebrew fallback より Nix package を優先します。
- Nix に移せないものは `unmapped-homebrew.tsv` に理由を残します。
- fallback や migration report は `scripts/migrate_brew_to_nix.sh` で再生成します。generator で表現できない修正以外は手編集を避けます。
- mise 管理 tool に関わる変更では、`config/mise/config.toml` と `home/.chezmoitemplates/mise-config.toml` を揃えます。
- `darwin/` や `home-manager/` 配下の変更は、Nix test または dry-run build で確認します。

## よく使う確認コマンド

```bash
zsh scripts/nix_install.sh --cli-only --dry-run
zsh tests/test_nix_migration.sh
mise run nix-build
```
