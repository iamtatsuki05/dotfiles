# Tests

English version: [README.md](README.md)

このディレクトリは、dotfiles repo の local / CI check を置く場所です。
主な entrypoint は `run.sh` です。

## 構成

| Path | 用途 |
|---|---|
| `run.sh` | local と compatibility wrapper から使う main test runner。 |
| `lib/` | shell test 向けの共通 assertion と fixture helper。 |
| `test_agent_*.sh` | AI agent config、support matrix、upstream skill の check。 |
| `test_chezmoi_*.sh` | chezmoi source state と rendered-home の check。 |
| `test_nix_migration.sh` | Nix / Homebrew migration と package config の check。 |
| `test_dotfiles_test_runner.sh` | test runner 自体の self-check。 |

## 更新ルール

- shared script、sync behavior、generated config を変更した場合は focused test を追加・更新します。
- 可能な限り macOS と Ubuntu の両方で動く形にします。
- skip は、必要な外部 tool が本当に利用できない場合に限ります。
- local command は `.github/workflows/` と揃えます。

## よく使う確認コマンド

```bash
zsh tests/run.sh
zsh tests/test_agent_sync.sh
zsh tests/test_chezmoi_rendered_home.sh
zsh tests/test_nix_migration.sh
```
