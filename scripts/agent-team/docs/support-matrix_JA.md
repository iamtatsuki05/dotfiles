# Harness対応matrix

[English](support-matrix.md) · [README](../README_JA.md)

このrepositoryが管理するharness identityは10種類です。認識していることと、実行できる
ことは別です。`recognized`は名前をregistryが知っていること、`available`は期待するcommand
がPATH上にあること、`implemented`は少なくとも1つのrole・transport・permissionの組み合わせが
実装・検証済みであること、`runnable`は実装済みprofileのcommandも利用できることを示します。
`agent-team harnesses`はstatic registryとPATH解決だけを行い、login確認、
package download、process起動、workspace書き込みは行いません。

| Harness | 現在実行できるdirect profile | 既知のACP adapter | agent-team status | 広く対応していない理由 |
|---|---|---|---|---|
| Claude | Main `orchestrator`、Planner/Reviewer `read-only` | `claude-agent-acp@0.70.0` | 検証済み | ACPはread-only background roleに限定。 |
| Codex | Main `orchestrator`、Planner/Reviewer `read-only`、Worker `workspace-write` | `codex-acp` | direct検証済み、ACP拒否 | ACPのpermission制御がinternal writeを止めないnegative test結果。 |
| GitHub Copilot | Planner/Reviewer `read-only`（direct background、厳密な`1.0.81`） | native `copilot --acp`、acpx built-in `copilot` | 厳密なGitHub CLIを解決できた場合は検証済み | read-onlyのPlanner/Reviewerに限定。Workerは引き続き拒否。 |
| Cursor | なし | native `cursor-agent acp`、acpx built-in `cursor` | 認識済み・拒否 | 現在のCLIは未認証。permission negative testを完走できていない。 |
| Devin | なし | native `devin acp` | 認識済み・拒否 | no-tool smokeだけ成功。tool taskは完了せず、model overrideはProを要求した。 |
| Antigravity | なし | 登録なし | 認識済み・拒否 | read-only probeでworkspace外のsibling fileを読めた。 |
| Hermes Agent | なし | native `hermes acp` | 認識済み・拒否 | read-only probeで通常file、`.git`、workspace外siblingへの書き込みを確認した。 |
| OpenCode | なし（adapterは実装済みだが未登録） | native `opencode acp`、acpx built-in `opencode` | 認識済み・拒否 | raw workspaceのsymlink escapeを確認済み。isolated XDG root、`--pure`、closed read-only policy、snapshotを含むprofile固有の実機E2Eが通るまで登録しない。 |
| OpenClaw | なし | native `openclaw acp`、acpx built-in `openclaw` | 認識済み・拒否 | one-shotは動くが、sandboxに必要なDocker daemonを利用できずnegative test未完了。 |
| Grok | なし | native `grok agent stdio`、acpx built-in `grok-build` | 認識済み・拒否 | 現在のCLIは未認証。direct/ACPのpermission negative testを完走できていない。 |

ACP adapterがインストールされていることやacpxが表示することだけでは、安全なrole用adapterで
あることは証明できません。adapterの存在とagent-teamの検証済みprofileは別々に表示します。
unknown providerと認識済みだが拒否されたprofileは、Orca Task、terminal、ACP processを作る前に
失敗します。別harnessへのfallbackはありません。

```bash
agent-team harnesses
agent-team harnesses --json
```

JSONには`recognized`、`available`、`command_resolution_status`、`implemented`、`runnable`、
`runnable_profiles`、`acp_adapter`、`acp_status`、`rejection_reason`が含まれます。人間向け表示を
parseせずに状態を区別できます。
