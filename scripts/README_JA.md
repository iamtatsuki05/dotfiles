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
| `agent-run-compact` | Agentが明示的に使う、成功時の出力を抑えつつ失敗時の診断ログを残すcommand wrapper。 |
| `agent_skill_upstreams.py` | 外部 skill update と security review manifest の管理 tool。 |
| [`agent-team/`](agent-team/README_JA.md) | opt-inなOrca teamのstandalone Python project。package、bundled default、MCP entrypoint、対応matrix、ACP docs、testを含む。 |
| `analyze_agent_delegation.py` | prompt、response、tool引数・出力を表示せず、Codex subagent の起動バッチと観測上の重複実行時間を集計する。 |
| `setup_agent_files.sh` | AI agent config、hook、skill、pet sync の canonical script。 |
| `setup_hermes_agent.sh` | upstream が pip/PyPI と Homebrew 配布を廃止したため、Hermes Agent を公式 shell installer で導入・更新する script。 |

## 更新ルール

- test や automation から呼ばれる script は、可能な限り non-interactive にします。
- shell behavior の重複は `lib/` の shared helper に寄せます。
- secret を hard-code しません。
- 破壊的操作には dry-run または明示確認 path を残します。
- script 挙動を変えた場合は test も更新します。

## Agent向けの出力抑制

`agent-run-compact`は、明示的に渡されたcommandにだけ作用します。人間がcommandを直接実行した場合の表示は変わりません。

```bash
agent-run-compact -- pytest tests/
agent-run-compact --verbose -- pytest tests/
```

compact modeでは、成功時の表示を要約と末尾数行に抑え、長時間実行中は低頻度のheartbeatを出します。成功後、一時ログは削除されます。失敗または中断時に表示するのは、診断用の抜粋と権限を絞った完全ログのpathです。この場合も元のshell終了状態を維持します。`--verbose`は、captureやwrapperの要約を挟まず、child commandの出力をそのまま表示するmodeです。

秘密情報を出力する可能性があるcommandには使わないでください。保持された失敗ログは機密情報として扱い、診断後に削除してください。

## よく使う確認コマンド

```bash
zsh tests/run.sh --syntax-only
zsh tests/run.sh
python3 scripts/agent_skill_upstreams.py check
```
