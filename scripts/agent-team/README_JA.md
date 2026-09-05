# Agent Team

[English](README.md)

`agent-team`は、通常の`claude`や`codex`の設定を変えずに、project単位の
Planner → Worker → ReviewerフローをOrca上で起動します。Task、message、terminal、
lifecycleはOrcaが管理し、各roleは通常のCLIを使う`direct`またはAgent Client Protocol
経由の`acp`で動く構成です。

初めて使う場合は、「managed commandを導入する」「起動前の条件を満たす」
「teamを起動する」を読んでください。実装や設定を変える場合は、詳細ドキュメントも
参照してください。

## 最初に読む場所

- [teamを起動する](#teamを起動する)は通常の利用手順です。
- [アーキテクチャ](docs/architecture_JA.md)はruntimeと安全境界を説明します。
- [設定リファレンス](docs/configuration_JA.md)はconfig version 3と、対応する
  provider/transportの組み合わせを説明します。[Version 4の設定](docs/configuration-v4_JA.md)
  では、team名による選択、graphの確認、起動設定への参照を説明します。
- [Harness対応matrix](docs/support-matrix_JA.md)は、認識済み・利用可能・実行可能・
  拒否を区別します。
- [ACPの境界](docs/acp_JA.md)はadapter pin、認証、ACPがsandboxではない理由を説明します。
- [Direct background adapter](docs/background-adapters_JA.md)はCopilot/OpenCode向けread-only
  adapter実装、snapshot境界、復旧方法を説明します。

現在の設定は次のとおりです。

| Role | Provider / transport | Model / effort | Permission |
|---|---|---|---|
| Main | Claude / `direct` | `fable` / `high` | `orchestrator` |
| Planner | Claude / `acp` | `fable` / `high` | `read-only` |
| Worker | Codex / `direct` | `gpt-6-astra` / `medium` | `workspace-write` |
| Reviewer | Codex / `direct` | `gpt-6-astra` / `high` | `read-only` |

起動直後に動くのはMainだけです。Planner、Worker、Reviewerは必要なときだけ
起動し、background roleは同時に1つしか動きません。

## checkoutから実行する、またはprojectをinstallする

このprojectはPython標準libraryだけで動きます。Python 3.11以降が必要です。checkoutからは
launcherを直接実行できます。

Orcaのライフサイクルbackendとbounded provider runnerはPOSIX専用です。現在のruntime metadataが
Unix socketとprocess groupを必要とするため、Windowsでは実行前に明示的に拒否します。
CLI名はplatformごとに固定し、macOSでは`orca`、Linuxでは`orca-ide`を使います。PATH fallbackや環境変数overrideは行いません。

```bash
scripts/agent-team/agent-team harnesses
scripts/agent-team/agent-team start --dry-run
```

隔離した環境へinstallする場合は、任意のPython環境でprojectをbuild/installします。
console scriptと`python -m agent_team`は同じpackageとbundled defaultを使います。team起動時は
同じPython環境のconsole scriptを解決し、別のinstallへfallbackしません。

```bash
python3.13 -m venv /tmp/agent-team-venv
/tmp/agent-team-venv/bin/python -m pip install scripts/agent-team
/tmp/agent-team-venv/bin/agent-team harnesses --json
```

## managed commandを導入する

このdotfiles repositoryで、通常のagent file syncを実行します。

```bash
zsh dotfiles/.agent/sync.sh
command -v agent-team
```

syncはproject launcherを`~/.local/bin/agent-team`へmanaged linkとして配置し、dotfiles側の
config/promptsを`$XDG_CONFIG_HOME/agent-team`へlinkします。Python packageのinstallやteamの
起動は行いません。config directoryに空でない既存directoryがある場合は触らず、bundled default
を利用できる状態を保ちます。

## 起動前の条件を満たす

現在の実装でlive Orca runtimeを使ったsmoke testを行ったのはmacOSです。
Linuxの実行ファイルは実装上`orca-ide`に固定されていますが、このcheckoutではLinuxの
Orca実機smoke testを行っていません。Windowsは非対応で、実行前に明示的に拒否します。
Orcaの実行ファイルはplatformごとに固定し、PATH fallbackや環境変数overrideは行いません。

- macOS: `orca`
- Linux: `orca-ide`

起動前に次を確認してください。

1. 上記のplatform固有のOrca実行ファイル、`claude`、`codex`、Node.js、`npx`を利用できる。
2. Orcaを起動し、platform固有の`status --json`でruntimeとgraphがreadyである。
3. 利用するClaude/Codex accountへloginしている。
4. 対象repositoryをOrcaへ一度登録している。

```bash
claude auth status
codex login status
# macOS
orca status --json
orca repo add --path "$PWD"
# Linux
orca-ide status --json
orca-ide repo add --path "$PWD"
```

ACP Plannerは`acpx@0.13.2`と
`@agentclientprotocol/claude-agent-acp@0.70.0`を固定して使います。初回は
`npx`がpackageをdownloadする場合があります。global installは行いません。

config version 2で起動したteamが残っている場合は、version 3へ切り替える前に、
旧codeの`agent-team stop`で停止してください。legacy fallbackはありません。

## teamを起動する

Orcaやagentを起動せず、role metadataとdirect agentの引数を確認します。

```bash
agent-team start --dry-run
```

dry runでは、Taskごとに生成するACP commandまでは表示しません。ACP commandは、
ACP roleをDispatchするときに作ります。

Mainを起動し、Orca上のterminalへfocusします。

```bash
agent-team start
```

backgroundで起動する場合は`--no-attach`を付けます。

```bash
agent-team start --no-attach
```

Mainへ開発作業を依頼してください。MainはPlannerが必要か判断し、`agent_team`
MCP serverを通じてWorkerとReviewerを起動します。ユーザーと対話するroleはMainだけです。

team名で選ぶ場合は、同梱の一覧、またはsync後の`teams.toml`を指定します。

```bash
agent-team start --config ~/.config/agent-team/teams.toml --team agent-team --dry-run
```

teamの追加、graphの確認、選択したteamの起動は、[Version 4の設定](docs/configuration-v4_JA.md#名前を指定してteamを起動する)
を参照してください。

## teamを確認して停止する

```bash
# Run、Main terminal、workerの状態を確認する。
agent-team status

# Mainへfocusする。
agent-team attach main

# Mainがbackground roleを起動した後だけ、そのroleへfocusできる。
agent-team attach worker

# teamが所有するterminalを停止し、runtime stateを削除する。
agent-team stop
```

`stop`後も、Orca Runは監査記録として残ります。project fileのcommit、push、
publish、削除は行いません。

管理commandでは、`start`と同じ`--config`と`--cwd`を指定してください。

```bash
agent-team start \
  --config /absolute/path/to/config.toml \
  --cwd /absolute/path/to/project

agent-team status \
  --config /absolute/path/to/config.toml \
  --cwd /absolute/path/to/project
```

## 安全境界を理解する

- 未対応のprovider、transport、permission、config version、state formatは、
  起動前に拒否します。別transportへ自動で切り替えません。
- ACPを使えるのはClaudeのread-only background roleだけです。Main ACP、
  Codex ACP、workspace-write ACPは拒否します。
- ACPのpermission制御はOS sandboxではありません。書き込みは、専用permission
  profileを持つdirect Codexに限定します。
- Agentの出力は信頼しません。Task、Dispatch、terminal、sender、Deliveryの
  identityが一致したときだけlifecycleを進めます。
- Claude ACPはambientな`claude.ai` loginを使い、API keyをchild processへ
  渡しません。ただしsubscription billing ledgerそのものは未確認です。
詳しい境界と失敗時の流れは、[アーキテクチャ](docs/architecture_JA.md)を参照してください。

## よくある失敗を調べる

Orca 1.4.190では、非表示の検出済みworktreeに作ったterminalの終了が
`runtime_error: tab_not_found`で失敗する場合があります。Mainだけでなく、
単純な`sleep`でも再現しました。この場合は停止失敗を報告し、`state.json`と
`.cleanup.json`を保持します。一覧からterminalが消えたことだけでは、processの停止を
確認済みにはしません。teamを再利用する前にOrca側の終了処理を解決してください。
再起動を通すためにstateを削除しないでください。残件は[#11](https://github.com/iamtatsuki05/dotfiles/issues/11)
で追跡します。

| 症状 | 確認する内容 |
|---|---|
| `workspace is not managed by Orca` | macOSでは`orca repo add --path "$PWD"`、Linuxでは`orca-ide repo add --path "$PWD"`を実行する。 |
| `agent-team state already exists` | 2つ目を起動せず、`status`、`attach`、`stop`を使う。 |
| `role has no active Orca Dispatch` | Mainがそのroleを未起動か、すでにrelease済み。 |
| authenticationを求められる | agent-team外で`claude auth status`か`codex login status`を確認する。 |
| 初回のACP起動に失敗する | 固定した`npx` packageへのnetwork accessを確認し、明示的に再実行する。 |
| roleが`escalation`を返す | 保持されたterminalとRunを調べる。完了として扱わない。 |

## 用語と問い合わせ時の情報を揃える

このガイドで解決しない場合は、repository maintainerへ次の情報を渡してください。
実行command、config path、workspace、Orca version、Run/Task/Dispatch ID、関係する
最小限のerrorです。認証token、prompt本文、無関係なterminal出力は含めません。

- **Run**: 1回のteam実行で使うOrcaのnamespaceとcoordinator inbox。
- **Task**: Planner、Worker、Reviewerへ渡す、範囲を限定した1件の作業。
- **Dispatch**: Taskとterminalを結ぶ1回の実行attempt。
- **Delivery**: Mainが内容を処理し、acknowledgeするmessage batch。
- **direct**: providerの通常のinteractive CLI。
- **ACP**: Agent Client Protocol。固定したacpx client経由で使う。

## 変更を検証する

開発用toolは`pyproject.toml`に宣言し、解決したバージョンとhashを`uv.lock`へ保存します。
実行時の依存は増やしません。次のcommandはリポジトリのrootから実行してください。

```bash
uv sync --locked --project scripts/agent-team --python 3.13
uv run --locked --project scripts/agent-team python -m unittest discover -s scripts/agent-team/tests
uv run --locked --project scripts/agent-team ruff check \
  scripts/agent-team/agent_team \
  scripts/agent-team/tests \
  tests/test_agent_team.py \
  tests/test_agent_team_mcp.py
uv run --locked --project scripts/agent-team ruff format --check \
  scripts/agent-team/agent_team \
  scripts/agent-team/tests \
  tests/test_agent_team.py \
  tests/test_agent_team_mcp.py
uv run --locked --project scripts/agent-team mypy --strict --python-version 3.11 scripts/agent-team/agent_team
uv run --locked --project scripts/agent-team python -m build --no-isolation scripts/agent-team
DOTFILES_TEST_PYTHON=python uv run --locked --project scripts/agent-team /bin/zsh tests/run.sh
```

CIもPython 3.11と3.13で同じlockを使います。buildではlockから導入したsetuptoolsを使い、
別のbuild環境は作りません。通常の隔離installでも版が変わらないよう、build-system側の
要求も固定しています。ソース配布物にも`uv.lock`を含めます。

開発依存を変更するときは、`uv lock --project scripts/agent-team`を実行し、両ファイルを
commitしてください。`--locked`は不整合のあるlockを自動更新せず、エラーにします。
詳しくは[uvのlock管理ドキュメント](https://docs.astral.sh/uv/concepts/projects/sync/)を参照してください。

OrcaやACPの連携を変更した場合は、実環境で範囲を限定したsmoke testも行います。
`stop`後にterminal、state、prompt file、session、adapter processが残っていないことを
確認してください。
