# ACPの境界

[English](acp.md) · [README](../README_JA.md) ·
[対応matrix](support-matrix_JA.md)

ACP（Agent Client Protocol）は、ACP clientとagent adapterの間でmessageを交換するprotocolです。
OS sandboxではなく、providerのsubscriptionをAPI keyの契約へ変えるものでもありません。

## agent-teamが実行するもの

検証済みのACP profileは、Claudeを使うread-onlyのPlannerまたはReviewerだけです。

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

起動planにACP roleが含まれる場合だけ、起動時に`node`、`acpx`、`claude-agent-acp`を解決し、
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
