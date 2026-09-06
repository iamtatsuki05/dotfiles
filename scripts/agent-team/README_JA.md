# Agent Team

[English](README.md)

`agent-team`は、通常の`claude`や`codex`の設定を変えずに、選択した実行環境で
プロジェクト単位のチームを起動します。既定の`runtime = "orca"`では、
Planner → Worker → Reviewerの流れと、Task・メッセージ・端末・実行状態をOrcaが管理します。
実験的な`runtime = "tmux"`ではMainと、任意のClaude ACP read-only Planner/Reviewerを
利用できます。native Workerには対応していません。

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

bundled Orca configでは起動直後に動くのはMainだけです。Planner、Worker、Reviewerは
必要なときだけ起動し、background roleは同時に1つしか動きません。

bundled configは、これまでどおり4 roleのOrca構成です。customなtmux configでは、
Mainをdirect Claude・permission `orchestrator`として定義し、必要に応じてPlannerと
Reviewerだけをverified Claude ACP・permission `read-only`として追加できます。Workerと
その他のnative profileは、state、Task、Dispatch、processに影響する前に拒否します。

## checkoutから実行する、またはprojectをinstallする

このprojectはPython標準libraryだけで動きます。Python 3.11以降が必要です。checkoutからは
launcherを直接実行できます。

Orcaのライフサイクルbackend、実験的なtmux backend、bounded provider runnerはPOSIX専用です。
runtime metadataがUnix socketまたはprivateなprocess groupを必要とするため、Windowsでは
実行前に明示的に拒否します。
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

Orcaの実機確認はmacOSで行っています。tmuxでは、OrcaとCodexがない環境、空白を含む
作業パス、削除済み設定ファイルを使い、CLIの起動・状態確認・停止に成功しました。
さらに実tmuxと模擬providerを使い、MCPでのread→release→ack、別CLIからの実行中処理の
中断・停止、所有資源の回収を確認しました。これらは実モデルを使った検証ではありません。

Claude Code 2.1.112のMainを`fable`/`high`、ログイン済みの`claude.ai`アカウントで起動すると、
`fable`が存在しないか利用できないという応答になりました。代替モデルは使っていません。
実モデルで全工程を試すには、アカウントで利用できる正式なモデルIDの指定が必要です。
LinuxのOrca実行ファイルは`orca-ide`に固定していますが、Linuxでの実機確認は未実施です。
Windowsは非対応で、実行前に拒否します。Orcaの実行ファイルはOSごとに固定し、
別名のPATH探索や環境変数による置き換えは行いません。

- macOS: `orca`
- Linux: `orca-ide`

起動前に次を確認してください。

1. 選択したruntimeとharnessのcommandを利用できる。既定のteamは上記のOrca実行ファイル、
   `claude`、`codex`と、後述のACP toolを使います。tmux configでは`tmux`も必要です。
2. `runtime = "orca"`ではOrcaを起動し、platform固有の`status --json`でruntimeとgraphが
   readyであることを確認します。`runtime = "tmux"`では`tmux`が利用できることを確認し、
   Orcaは必要ありません。
3. 選択したproviderを使うaccountへloginしている。bundled Orca roleではClaudeとCodexの両方、
   native tmuxではClaudeが必要です。
4. `runtime = "orca"`では対象repositoryをOrcaへ一度登録している。

```bash
# bundled Orca configのprovider
claude auth status
codex login status
# macOSのOrca runtime
orca status --json
orca repo add --path "$PWD"
# LinuxのOrca runtime
orca-ide status --json
orca-ide repo add --path "$PWD"
# native tmux runtime
tmux -V
```

ACP roleを選択するconfigにはNode.js 22.13以降と、`acpx@0.13.2`、
`@agentclientprotocol/claude-agent-acp@0.70.0`のcommandが必要です。
利用するtoolは、例えば次のように指定したdirectoryへ事前に導入してください。

```bash
npm install --prefix /path/to/agent-team-acp acpx@0.13.2 @agentclientprotocol/claude-agent-acp@0.70.0
export PATH="/path/to/agent-team-acp/node_modules/.bin:$PATH"
```

起動時に実行ファイルのpathとfingerprintを保存し、実行時はそのファイルを直接使います。
`npm`や`npx`は呼び出しません。依存が不足した場合やファイルが変わった場合はエラーにします。
direct transportだけのteamにはACP toolは不要です。

config version 2で起動したteamが残っている場合は、version 3へ切り替える前に、
旧codeの`agent-team stop`で停止してください。legacy fallbackはありません。

## teamを起動する

Orcaやagentを起動せず、role metadataとdirect agentの引数を確認します。

```bash
agent-team start --dry-run
```

dry runでは、Taskごとに生成するACP commandまでは表示しません。ACP commandは、
ACP roleをDispatchするときに作ります。

Mainを起動し、管理対象のterminalへfocusします。

```bash
agent-team start
```

backgroundで起動する場合は`--no-attach`を付けます。

```bash
agent-team start --no-attach
```

Mainへ開発作業を依頼してください。bundled Orca configでは、MainがPlannerの要否を判断し、
`agent_team` MCP serverを通じてWorkerとReviewerを起動します。native tmuxでは、configに
含まれるClaude ACPのPlanner/Reviewerだけを依頼できます。native Workerは利用できません。
ユーザーと対話するroleはMainだけです。

team名で選ぶ場合は、同梱の一覧、またはsync後の`teams.toml`を指定します。

```bash
agent-team start --config ~/.config/agent-team/teams.toml --team agent-team --dry-run
```

teamの追加、graphの確認、選択したteamの起動は、[Version 4の設定](docs/configuration-v4_JA.md#名前を指定してteamを起動する)
を参照してください。

## teamを確認して停止する

```bash
# 選択したruntime、Main terminal、active roleの状態を確認する。
agent-team status

# Mainへfocusする。
agent-team attach main

# Orcaの場合だけ: Mainがbackground roleを起動した後に、そのroleへfocusする。
agent-team attach worker

# teamが所有するterminalを停止し、runtime stateを削除する。
agent-team stop
```

`stop`は選択したruntimeのbackendを使い、そのruntimeが所有するresourceだけを削除します。
Orcaの場合はRunを監査記録として残します。project fileのcommit、push、publish、削除は
行いません。

管理コマンドは、起動時に保存した設定を使います。元の設定やpromptファイルを
変更・削除しても管理できます。対象の実行を選ぶには、`start`と同じ`--cwd`と、
指定した場合は同じ`--config`・`--team`を使ってください。`--config`はversion 4の
カタログも含めた元の入力パスと照合し、ファイルを読み直しません。選択条件の省略は、
一致する保存済みの実行が1件だけの場合に限ります。

```bash
agent-team start \
  --config /absolute/path/to/config.toml \
  --cwd /absolute/path/to/project

agent-team status \
  --config /absolute/path/to/config.toml \
  --cwd /absolute/path/to/project
```

`status`・`attach`・`stop`では、`--state /absolute/path/to/state.json`で
現在のディレクトリに関係なく保存済みの実行を選べます。`--team`とは併用できません。
`--config`も指定する場合は、保存したパスとの一致が必要です。

## 安全境界を理解する

- 未対応のruntime、provider、transport、permission、config version、state formatは、起動前に
  拒否します。別backendや別transportへ自動で切り替えません。
- Orcaは4 role固定です。native tmuxはMainを必須とし、verified Claude ACPのread-only
  Planner/Reviewerだけを任意に追加できます。native Workerとその他のnative profileは、
  起動処理の効果が発生する前に拒否します。
- nativeの`start`、`status`、`attach`、`stop`は`TmuxBackend`を使います。`attach`できるのは
  Mainだけです。`native_main`が所有するMainのprocess groupを監督します。
- native ACPの完了はtmux paneの文字列ではなく`publish_completion`で通知します。lifecycleの
  順序は`role_read` → `role_release` → `delivery_ack`です。nativeの`last_ack`は1つのreceipt
  markerを記録するだけで、Taskやユーザーのgoal全体の完了を意味しません。
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
| `role has no active Orca Dispatch` | Orca runtimeで、Mainがそのbackground roleを未起動か、すでにrelease済み。 |
| `native role is not a Claude ACP role` | native tmux runtimeでは、configにあるPlanner/ReviewerのClaude ACP roleだけを使う。 |
| authenticationを求められる | agent-team外で`claude auth status`か`codex login status`を確認する。 |
| ACPの依存検査に失敗する | 固定したACP packageを明示的にインストールし、その`node_modules/.bin`とNode >=22.13を`PATH`に含める。 |
| roleが`escalation`を返す | 保持されたterminalとRunを調べる。完了として扱わない。 |

## 用語と問い合わせ時の情報を揃える

このガイドで解決しない場合は、repository maintainerへ実行command、config path、workspace、
選択したruntime、関係する最小限のerrorを渡してください。Orca runtimeではOrca versionと
Run/Task/Dispatch IDを、tmux runtimeではnativeのrun/assignment IDを添えます。認証token、
prompt本文、無関係なterminal出力は含めません。

- **Run**: 1回のteam実行を識別するruntime identity。Orcaではnamespaceとcoordinator inboxも含む。
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

Orca、native tmux、ACPの連携を変更した場合は、実環境で範囲を限定したsmoke testも行います。
`stop`後に選択したruntimeのterminal、state、prompt file、session、adapter processが残って
いないことを確認してください。

tmux端末driverの実機確認は、tmuxを導入した環境で、リポジトリのrootから明示的に実行します。

```bash
uv run --locked --project scripts/agent-team python scripts/agent-team/tests/live_tmux.py
```

専用tmux serverを作り、引数の保持とprocessの終了状態を検証して、自分の資源を回収します。
このtestではtmux未導入をエラーとします。通常のsuiteはtmuxを要求せずdriverの契約を検証します。
実機testの対象は端末操作であり、チーム全工程の動作を証明するものではありません。

模擬providerによるnative CLI/MCPの一巡と中断処理は、次のコマンドで検証できます。

```bash
AGENT_TEAM_RUN_LIVE_NATIVE=1 uv run --locked --project scripts/agent-team python -m unittest scripts/agent-team/tests/live_native_contract.py -v
```

実tmuxと、試験用のClaude・Node・ACPX・ACP adapterコマンドを使います。他のbackendと
ハーネスをPATHから除外し、read→release→ackの順序と、プロセス・socket・設定・prompt・
sessionの回収を確認します。モデルへの問い合わせや、作成・レビューの全工程は対象外です。
