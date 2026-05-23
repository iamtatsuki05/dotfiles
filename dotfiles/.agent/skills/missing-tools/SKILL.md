---
name: missing-tools
description: コマンドが見つからない、shell が command not found を返す、CLI tool が未導入、または brew install / npm install -g / uv tool install などの global install や永続的な環境変更なしで一時実行したい場合に使う。
---

# Missing Tools

## USE FOR:

- `command not found`、missing CLI、実行ファイルが見つからない。
- global install なしの一時実行。
- project env / `mise` / comma / Nix の選択。

## DO NOT USE FOR:

- tool は存在するが実行時に失敗する。debugging 系 skill を使う。
- 永続的な tool 追加・install を明示された場合。
- CI 側の tool 設定を未確認の CI failure。

## 手順

1. `command -v <command>` で本当に未導入か確認する。
2. project-local 優先。必要なら `direnv exec . <command> <args>`。
3. `config/mise/config.toml` にある tool は `mise exec <tool>@<version> -- <command> <args>`。
4. nixpkgs の ad-hoc command は `, <command> <args>`。
5. package 名が分かる場合は `nix run nixpkgs#<package> -- <args>`。
6. 最後に `nix shell nixpkgs#<package> --command <command> <args>`。

## 安全弁

- 明示承認なしに global installer を実行しない。
- 永続 install 指示がない限り、mise / Nix / Homebrew / shell / hook 設定を編集しない。
- network、credential、telemetry、外部書き込みは非自明な影響を先に説明する。

## Troubleshooting

- 全 fallback が失敗したら、試した wrapper と最小の永続 install 変更を報告する。

## Examples

`mise exec 'pipx:markitdown' -- markitdown --version`, `, jq --version`.
