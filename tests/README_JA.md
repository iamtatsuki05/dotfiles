# Tests

English version: [README.md](README.md)

このディレクトリは、dotfiles repo の local / CI check を置く場所です。
主な entrypoint は `run.sh` です。

## 構成

| Path | 用途 |
|---|---|
| `run.sh` | local と CI で使う main test runner。 |
| `lib/` | shell test 向けの共通 assertion、fixture、runtime matrix helper。 |
| `test_agent_*.sh` | AI agent config、support matrix、upstream skill の check。 |
| `test_chezmoi_*.sh` | chezmoi source state と rendered-home の check。 |
| `test_multi_shell_config.sh` | 各シェルのsource/render、Mac機能のON/OFF、Zsh補完の分離を確認。 |
| `test_setup_shell.sh` | 展開対象、csh/tcshの既存設定保持と一度だけの読み込み、preflight、dry-run、verifyを確認。 |
| `test_nix_migration.sh` | Nix / Homebrew migration と package config の check。 |
| `test_feature_flags.sh` | 導入前のfeature readerと、不正値による古い値の残留を確認。 |
| `test_macos_*.sh` | Macの導入・更新経路と、Nix moduleのON/OFF評価を確認。 |
| `test_fixture_isolation.sh` | ファイル書き込み・外部通信・実環境管理コマンドの遮断を確認。 |
| `test_dotfiles_test_runner.sh` | test runner 自体の self-check。 |

## 更新ルール

- shared script、sync behavior、generated config を変更した場合は focused test を追加・更新します。
- 可能な限り macOS と Ubuntu の両方で動く形にします。
- shell runtime の check は identity を区別します。optional runtime がなければ `SKIP`、
  runtime があるのに失敗した場合は `FAIL` とします。
- skip は、必要な外部 tool が本当に利用できない場合に限ります。
- local command は `.github/workflows/` と揃えます。

## よく使う確認コマンド

Macの導入・更新試験は、個別実行でも `sandbox-exec` を使い、HOME・XDG・
一時ファイルを試験用ディレクトリへ隔離します。その外への書き込み、ネットワーク通信、
実環境の管理コマンドを遮断します。Python 3と `sandbox-exec` が必要です。
隔離を検証できなければ試験を停止します。この制限は既存の全試験には及ばないため、
全体テストは使い捨てのCI環境で実行してください。
LinuxではMacの導入・更新試験を明示してSKIPします。Nix module評価はNix不在時に
SKIPし、世代のビルドや切り替えは実行しません。flake全体のパッケージ結合は静的に確認します。

```bash
zsh tests/run.sh
zsh tests/test_agent_sync.sh
zsh tests/test_chezmoi_rendered_home.sh
bash tests/test_setup_shell.sh
zsh tests/test_nix_migration.sh
```
