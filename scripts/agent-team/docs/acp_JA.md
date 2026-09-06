# ACPの境界

[English](acp.md) · [README](../README_JA.md) ·
[対応matrix](support-matrix_JA.md)

ACP（Agent Client Protocol）は、ACP clientとagent adapterの間でmessageを交換するprotocolです。
OS sandboxではなく、providerのsubscriptionをAPI keyの契約へ変えるものでもありません。

## agent-teamが実行するもの

Orcaで検証済みのACP profileは、Claudeを使うread-onlyのPlannerまたはReviewerです。

- Node.js `22.13.0`以降
- `acpx@0.13.2`
- `@agentclientprotocol/claude-agent-acp@0.70.0`
- ambientなClaude loginを使い、API key環境変数はchildへコピーしない
- `Read,Grep,Glob` tool、read approval、解決できないnon-interactive permissionの失敗
- bare Orca terminalとtrusted outer runner。runnerだけが一致する`worker_done`を1回送る

正確なadapter command、Task identity、nonceはDispatch時に生成します。Agentの出力はデータとして
扱い、lifecycle messageを送る権限は与えません。

## 依存関係は明示し、起動単位で固定する

選択したACP packageは`agent-team`の外で導入してください。たとえば、次のように2つの
exact packageを任意のdirectoryへ導入し、そのbin directoryをteam起動前の`PATH`へ追加します。

```bash
npm install --prefix /path/to/agent-team-acp acpx@0.13.2 @agentclientprotocol/claude-agent-acp@0.70.0
export PATH="/path/to/agent-team-acp/node_modules/.bin:$PATH"
```

Orcaの起動planにACP roleが含まれる場合だけ、起動時に`node`、`acpx`、`claude-agent-acp`を解決し、
package manifestのexact versionを確認します。解決した3つのabsolute pathとSHA-256 fingerprintを
roleのlaunch snapshotへ保存します。role起動経路はOrca Taskを作る前に保存bindingを再検証し、
runnerもACP実行の前に再検証して、各session operationで同じfileを使います。実行ファイルが不足、
置換、変更された場合はfail-closedで停止します。

実行時は保存したfileを直接使い、`npm`や`npx`を呼び出しません。選択したroleにACPがなければ、
これらのACP依存関係を解決せず、directだけのteamにも必要ありません。static harness inventoryは
この起動前検査とは別であり、providerのinstallや起動を行いません。

Codex ACPは意図的に拒否しています。negative testで、ACPの`deny-all`/read-only制御を設定しても
Codex internal toolのwriteを防げないことを確認したためです。検証済みのworkspace-write Workerと
read-only Reviewerには、隔離した`CODEX_HOME`とprovider native permission profileを持つdirect
Codexを使います。

## native tmuxのACP

native tmuxでは専用clientが、assignmentごとに公開ACPの接続を1本使います。
必要な依存はNode.js、`@agentclientprotocol/claude-agent-acp@0.70.0`、そのadapterに
導入された`@agentclientprotocol/sdk@1.3.0`です。`acpx`は選択しません。
起動時にNode、adapter entry、実際にimportする`dist/lib.js`、SDK entryの計4 fileについて、
absolute pathとSHA-256 fingerprintを保存します。
clientはsession作成、設定済みmodelとeffortの適用、prompt、session終了、子プロセスの
回収確認までを行います。自動実行するsessionは履歴保存と自動memoryを無効にしますが、
対話用Mainの通常CLI履歴は保持します。

PlannerとReviewerは`Read`、`Grep`、`Glob`を使えます。Workerはさらに、ユーザーが
宣言したTaskSpecの範囲で`Write`と`Edit`を使えます。TaskSpecの許可・禁止pathが制限するのは
Write/Editだけです。Read/Grep/Globは、保護pathやlink・file typeの検査を除き、workspace内を
読めます。固定wrapperがこれらを検査します。TaskSpecは起動時の`[[tasks]]`と完全一致する
必要があるため、Mainはdispatchで別の変更範囲や検証commandを追加できません。
schemaとレビュー・検証の流れは[設定リファレンス](configuration_JA.md)を参照してください。
導入済みの依存package自体は信頼する前提です。entry fileのfingerprintは、読み込まれる
全依存fileの固定や、同じユーザー権限の別プロセスによる悪意ある同時差し替えを保証しません。

## 認証とsubscription

ACPはaccountを選択したり、providerのbilling policyを回避したりしません。Claude profileは
adapterが利用できるambientな`claude.ai` loginを再利用します。特定のturnがsubscription quotaに
どう計上されるかはprovider accountの問題であり、このtoolは保証しません。API key用adapterへの
自動置換も行いません。login、account変更、package installは`agent-team`の外で行います。

## ACP profileを追加する条件

adapterを対応matrixへ登録するには、exact version policy、認証経路、positive lifecycle smoke test、
read/write/process/networkのnegative testを記録する必要があります。adapterが存在するだけでは
不十分です。条件が揃うまでは`recognized-but-rejected`のままとし、Orca resource作成前にconfigを
拒否します。

この依存関係bindingで確認できるのは、選択したClaude profileの実行ファイルidentityです。
他のACP adapterをrunnableへ昇格させたり、[対応matrix](support-matrix_JA.md)に記録したscopeや
statusを変更したりはしません。
