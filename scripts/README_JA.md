# Scripts

English version: [README.md](README.md)

このディレクトリは、dotfiles workflow で使う setup、migration、update、sync、test helper script を置く場所です。

## 構成

| Path | 用途 |
|---|---|
| `agent/` | Agent / Waza eval の実装。top-level の Waza script は互換 wrapper。 |
| `lib/` | setup script が共有する shell helper library。 |
| `utils/` | primary setup path ではない小さな utility script。 |
| `*_install.sh` | Nix、Homebrew、MAS、rootless Nix 系の install / apply entrypoint。 |
| `waza_eval_*.sh` | Waza / agent eval entrypoint の互換 wrapper。 |
| `agent_skill_upstreams.py` | 外部 skill update と security review manifest の管理 tool。 |
| `setup_agent_files.sh` | AI agent config、hook、skill、pet sync の canonical script。 |

## 更新ルール

- test や automation から呼ばれる script は、可能な限り non-interactive にします。
- shell behavior の重複は `lib/` の shared helper に寄せます。
- secret を hard-code しません。
- 破壊的操作には dry-run または明示確認 path を残します。
- script 挙動を変えた場合は test も更新します。

## よく使う確認コマンド

```bash
zsh tests/run.sh --syntax-only
zsh tests/run.sh
python3 scripts/agent_skill_upstreams.py check
```
