# dotfiles リポジトリの作業規約

このファイルは、このリポジトリ内の作業にだけ適用します。各エージェントへ配布する共通規約は `dotfiles/.agent/AGENTS.md`、条件付き手順は共有 skills で管理します。このファイルを tool home へ配布しないでください。

## 設定の管理境界

- shell の役割と起動境界は、[設定の管理境界](docs/configuration-ownership_JA.md#shell-の役割と起動境界)を正とします。ローカルは zsh、server は Bash、新しい portable script は Bash と対応する shebang、既存 script は宣言済み shell を保ちます。
- Fish と csh/tcsh は optional adapter です。Fish は interactive 時だけ mise を activate し、csh/tcsh は shim または `mise exec` / `mise run` を使って手動 opt-in します。

## 共有 skill の管理

- 複数エージェントへ配布する共有 skill は `dotfiles/.agent/skills/<name>/SKILL.md` に置いてください。このリポジトリだけで使う project 固有の skill は共通規約どおり `.agent/skills/` に置き、共有用と区別してください。
- 外部 skill を dotfiles repo に追加・更新する場合は、`dotfiles/.agent/skills/upstreams.json` と `scripts/agent_skill_upstreams.py` の枠組みを使い、upstream repository、pinned commit、mapping、license / attribution、security review、local validation を残してください。
- 共通規約や共有 skill の分割・移動では、`scripts/setup_agent_files.sh` と `scripts/sync_agent_skills.py` の配布先、Grok の明示 skill paths、参照文書・テストの整合性を確認してください。リポジトリ固有の規約を共通指示へ戻さないでください。

## 検証

- 影響する検証から実行してください。共通指示・skill 配布は `zsh tests/test_agent_sync.sh` と `python3 tests/test_skill_distribution.py`、外部 skill の登録は `python3 scripts/agent_skill_upstreams.py check` で確認します。
- 全体検証は `zsh tests/run.sh` です。必要な環境と設定の責務は [設定の管理境界](docs/configuration-ownership_JA.md) と [AI Agent ファイル](dotfiles/.agent/README_JA.md) を参照してください。
