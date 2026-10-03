# ACPの境界

[English](acp.md) · [README](../README_JA.md) ·
[対応matrix](support-matrix_JA.md)

ACP（Agent Client Protocol）は、ACP clientとagent adapterの間でmessageを交換するprotocolです。
OS sandboxではなく、providerのsubscriptionをAPI keyの契約へ変えるものでもありません。

## agent-teamが実行するもの

固定version 3のOrcaで検証済みのACP profileは、Claudeを使うread-onlyのPlannerまたはReviewerです。

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

固定version 3のOrcaにACP roleが含まれる場合だけ、起動時に`node`、`acpx`、`claude-agent-acp`を解決し、
package manifestのexact versionを確認します。解決した3つのabsolute pathとSHA-256 fingerprintを
roleのlaunch snapshotへ保存します。role起動経路はOrca Taskを作る前に保存bindingを再検証し、
runnerもACP実行の前に再検証して、各session operationで同じfileを使います。実行ファイルが不足、
置換、変更された場合はfail-closedで停止します。

実行時は保存したfileを直接使い、`npm`や`npx`を呼び出しません。選択したroleにACPがなければ、
これらのACP依存関係を解決せず、directだけのteamにも必要ありません。static harness inventoryは
この起動前検査とは別であり、providerのinstallや起動を行いません。

追加の制御を持たないCodex ACPは、引き続き拒否しています。negative testで、ACPの`deny-all`/read-only制御を設定しても
Codex internal toolのwriteを防げないことを確認したためです。検証済みのworkspace-write Workerと
read-only Reviewerには、隔離した`CODEX_HOME`とprovider native permission profileを持つdirect
Codexを使います。

## native runtimeのClaude ACP

Version 5の名前付きOrcaも、この節で説明するscoped Claude ACPを使います。
`agent`/`serial`はstate version 4、`agent`/`parallel`はstate version 5です。
Codexの内部実装も接続していますが、公開設定では無効です。assignmentごとに所有するOrca terminalで実行し、
OrcaのTask・Dispatch・Deliveryを維持します。固定version 3の`acpx` clientは使いません。Mainはdirect Claude、
Planner、Worker、Reviewerはscoped Claude ACPです。providerを呼ばないparallel protocol proofと、明示的なClaude専用構成によるOrcaの`agent`/`parallel`・`program`/`parallel`の実モデルrunは
[アーキテクチャ](architecture_JA.md)に記載しています。

native tmux、Herdr、Zellijでは専用clientが、assignmentごとに公開ACPの接続を1本使います。
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
native `agent`/`parallel`では、catalogが空の場合にdependencyやprofile確認より前に失敗し、
read-only調査を含む`role_prompt`も拒否します。parallelの調査には宣言済みplan-only TaskSpecを使います。
serialのread-only `role_prompt`は変わりません。起動時に選択したdependency bindingとprofileは固定し、
providerやtransportへのfallbackは行いません。
schemaとレビュー・検証の流れは[設定リファレンス](configuration_JA.md)を参照してください。
導入済みの依存package自体は信頼する前提です。entry fileのfingerprintは、読み込まれる
全依存fileの固定や、同じユーザー権限の別プロセスによる悪意ある同時差し替えを保証しません。

### native Claudeのquestion path

native Claude profileは既存の`AskUserQuestion`とACP form elicitationを使います。選択した
0.70.0 adapterとSDK 1.3.0 clientがこれを有効にするのは、assignmentがprivateな`q.sock`を
所有している場合だけです。questionは同じTaskとDispatchに属します。Python側でquestion outboxと
各`message_reply`を保存し、全回答を`delivery_ack`でacknowledgeした後にだけ`q.sock`へ回答を送り、
Nodeの`received` receiptを受け取ります。Pythonがhashだけのreceiptを保存し、protected outboxを保持してから
`recorded`を返し、同じACP sessionを再開します。
channel内部ではclient receiptの検証後に`received`を保存し、`recorded` frameの送信成功後にだけ
`recorded`へ進めます。`received`の段階で失敗した場合はraw protected outboxを保持します。wire fieldは増やしません。

制限付きの通信契約では、1 batchあたり1〜4問、question/answer各20,000文字以内、JSONL frame 512 KiB以内、
1 assignmentあたり64 batchまでです。同じmessage IDと本文の再送はidempotentですが、異なる本文は
拒否します。消費済みreceiptにはassignment、session、delivery、tool-callのidentityとquestion/answerの
hashだけを残します。protected outboxには、replace後のfsyncや`recorded`公開の失敗から復旧できるよう、
次のquestionまたはterminal completionまでquestion/answer本文を保持する場合があります。receipt自体に
raw本文は含めません。question Deliveryが保留中は、そのassignmentの`role_read`、`role_release`、
別のdispatchを拒否し、そのassignmentのsuccessful completionもpublishできません。version 3/4のnative serial stateでは、
questionが消費されるまでrun全体の次のdispatchも止まります。version 5のnative `agent`/`parallel`と`program`/`parallel`では、
Worker scopeが重ならず`max_active`内でadmissionを通る独立assignmentは継続できますが、batch/coordinatorの検証barrierは
全active assignmentとDeliveryのdrainが終わるまでrun全体で拒否します。
stopはquestionをcancellingへ進め、acknowledgeを偽装しません。provider、process group、socket、private rootのcleanupを
確認できない場合はstateを保持します。

version 5のnative `agent`/`parallel`と`program`/`parallel` stateは、activeなnodeごとにresult、question、pending Deliveryのcontainerを保存します。
完了Deliveryは`role_read` → `role_release` → `delivery_ack`の順で処理し、release後のassignmentも一致するackまでstateに残します。
private Stopは`native.phase=stopping`を保存して安全なpeerを同じ順でdrainし、identity不明、typed result不足、cleanup未確認のnodeを保持したまま
安全なpeerを続けます。このprogram経路はfocused contract testと過去のboundedな実端末・fake providerのcaseで確認しています。
明示的なClaude専用構成による`agent`/`parallel`・`program`/`parallel`の実モデルrunは[アーキテクチャ](architecture_JA.md)に記載しています。

Mainが調整する`agent`/`parallel`では、任意順の宣言済みIDを`task_batch_open`へ渡し、保存するIDはcatalog順に正規化します。
最初のfinal review dispatchは全writerとDeliveryをdrainしてからbatchをsealし、同じrevisionの全Reviewer承認と全role/Deliveryの消費後にfixed argv検証へ進みます。
plan-onlyのfinal reviewではplan本文SHA-256と`workspace_revision`を分けて保存します。未回答のconsultationがある間はretryできません。
回答を保存し、全roleとDeliveryを消費し、全memberにreview roundが残る場合だけ、Mainが元のwriterへ`task_dispatch`を呼びます。そのrequestが`completed`済みpeerを含む
正確なpeer集合のreopenと要求したwriterのdispatchを原子的に行います。peerを自動dispatchせず、公開reopen toolもありません。
`task_batch_open`で未完了batchをreopenまたは置換することもできません。このtoolはClaude Mainの明示的な`--tools`と`--allowedTools`にだけ存在します。

### 名前付きOrcaのparallel FIFO Delivery

名前付きOrcaの`agent`/`parallel`はTaskBatchとACP assignmentのcontractを再利用しますが、Run単位のFIFO Deliveryを1つ使います。
`role_wait`は他nodeのownerを含むDelivery内の正規化済みeventをすべて返します。Mainは各eventを保存済みTask/Dispatch identityへ照合し、
ownerごとに`role_read`/`role_release`または正確な`message_reply`を処理してから、共有`delivery_ack`を1回呼びます。
rootの`orca_delivery_batch` envelopeとroleごとのjournalに、Run単位とowner単位の証拠を保持します。未回答questionは共有ACKを止めますが、
`stop`中も安全なpeerのcleanupは進められます。unknownなreply/ACK effectは保持し、自動再送しません。
serialとnativeのsemanticsは変えません。正確なenvelopeとprovider-free proofは[アーキテクチャ](architecture_JA.md)を参照してください。

実モデルを使ったtmuxのrun `dc101afd-87bf-4697-9bbb-0d1339d381a8`では、Fable・effort `high`を使い、Plannerを省略して質問応答を一巡させました。
Mainの回答と受領確認後、同じWorkerのACPセッションが再開し、Reviewer承認、同一リビジョンの固定コマンド検証、Task完了、公開`stop`まで確認しています。
別のrunでは、未回答の質問を待つ間の停止と、型付きACP終了記録も検証しました。
[アーキテクチャ](architecture_JA.md)に両試験、保持している過去の失敗、Herdr/Zellijの確認範囲を記載しています。

native clientは通常のstdoutを書く前に、private root固定の`client-result.json`を公開します。現在user所有の
mode 0600、atomicかつ上書き不可で、1 MiB以下です。artifactはlaunch nonce、要求したmodel、effortに束縛します。
終了code 0はtyped success、1はtyped failure、2は公開不確実を示します。Pythonはartifact、client exit status、取得時のstdout
parity、session identity、process groupの証明をすべて検証します。signalやcancellation Event、artifactの単独の存在はcleanupの
証拠ではありません。artifactの欠落、破損、identity不一致、exit 2、cleanup不確認があればassignmentとprivate rootを保持します。

signal handlerはrunごとの`threading.Event`を設定するだけです。明示的なProcessRunner checkpointがcleanupを一度だけ開始し、
cleanup開始後は非同期例外で中断しません。runnerはNode clientのleaderへsignalし、streamを上限時間までdrainし、それでもleaderが
生きている場合だけ所有process groupへ段階的にescalateします。public stopはsignal前に`native.phase=stopping`を保存し、publication
reservation内ではstopをprovider successより優先し、Reviewer evidenceを破棄してfailed outcomeを保存します。CLI結果は保存済みoutcomeから決めます。

実pending stopの`1000018d-3ae0-4d62-b2af-a5c89be31c6a`と`c945f3f5-8dcf-4643-a05a-72d1a62bab78`は、実ACP session-close receiptがあった
一方でPythonがclient exit statusを失ったため、どちらも失敗しました。public stopの`returncode=1`はclientのexit codeではありません。
全workload processは終了し、所有idle tmuxは別途回収しました。failed state、private root、snapshot、artifact、fixtureは保持しており、
いずれもcleanup成功の証拠として扱いません。

## 範囲を制限したCodex ACPの実装：公開設定では未有効

nativeと名前付きOrcaの内部経路には、変更範囲を制限したCodexの実装を追加しています。
ただし、registryは引き続きCodex ACPの設定を拒否します。実行可能な対応済み構成ではありません。
有効化には、実際の認証を使うモデル実行と、その権限・終了処理の検証が必要です。

この実装が選択する依存は、Node.js 22以上、
`@agentclientprotocol/codex-acp@1.10.0`、そのSDK `1.4.0`、
versionを`codex-cli 0.153.4`と返すCodex実行fileです
（npmでの配布名は`@openai/codex@0.153.4`）。共通のnative進行処理と公開ACP clientを使います。
`acpx`やdirect Codexの実行経路には切り替えません。
Python側で専用の起動manifestを作り、時間・出力量を制限した設定検査を行います。
その結果をassignmentに固定してからACP runnerを起動します。

app-serverのproxyはmodel、effort、指示、読み取り専用sandbox、空の実行環境一覧を固定し、
補助機能を無効にします。設定されたMCP serverもthread作成前に個別に無効化します。
ホスト側で提供するtoolは`read_text`、`list_files`、`write_text`、`edit_text`の4つです。
書き込みにはWorkerのTaskSpecで宣言された変更範囲が必要です。PlannerとReviewerは書き込めません。
共通のpath検査で、状態、認証、設定、依存file、制御用runtimeを保護します。
それ以外のtool要求や、追加threadの作成は拒否します。

専用の`CODEX_HOME`から、既存のChatGPT認証fileへlinkを張ります。
起動時には認証のdigest・有効期限とproject設定を固定します。
未対応のsystem設定やproject設定がある場合は、app-server起動前に拒否します。
providerによるtoken更新はlink先の認証fileを変更する可能性があります。
通常の認証を使う実機試験は未実施で、保存済みのPro情報だけでは認証成功や課金を確認できません。
設定検査のプロセス終了を確認できなかった場合は、専用directoryを状態に記録して保持し、
dispatch、再開、停止成功の判定を止めます。

file制御、通信、依存選択、native assignmentの接続は、模擬認証や偽のagentでテストしています。
ネットワークを遮断した別試験では、実際のCodexのthread設定をモデル実行なしで確認しました。
これらは、実認証を使うモデルの挙動を検証した証拠ではありません。

Codexの質問応答は無効のままです。Codex profileにはquestion socketも
`AskUserQuestion` capabilityもなく、公開Codex ACP registry entryも引き続き拒否します。
通常認証の保留中試験と、そのpermission/cleanup確認も変わりません。

## 範囲を制限したCopilot ACPの実装：公開設定では未有効

nativeと名前付きOrcaの内部経路には、GitHub Copilot CLIをACPで動かすroleの経路も追加しています。
これは内部専用です。
registryはCopilot ACPを引き続き`known-unverified`と報告し、設定は`copilot`/`acp`を実行できない構成として拒否します。
この経路に入るのは、保存済みのrole specか、テストや実機試験用のharnessが組み立てたstart specだけです。
実機での受け入れ試験は未実施で、この経路を通る認証済みのモデル実行、permission要求、中断、終了処理はどれもまだ確認していません。

この実装は、1つのnpm prefixに`@github/copilot@1.0.91`と`@agentclientprotocol/sdk@1.4.0`を入れることを求めます
（`npm install --prefix DIR @github/copilot@1.0.91 @agentclientprotocol/sdk@1.4.0`）。
`PATH`の先頭には`DIR/node_modules/.bin`とNode.js 22以上を置きます。
選ばれる`copilot`はこのpackageの`npm-loader.js`でなければならず、
packageの`optionalDependencies`は`@github/copilot-darwin-arm64@1.0.91`に固定されている必要があります。
loaderはpackageの特定に使うだけで、実行しません。
ACP serverとして動くのはplatform binaryで、
そのSHA-256は`87f04922933c139cf4af7cb6a80b96161428618a6275fea4b8dfe7e7a69c9518`でなければなりません。
対応するhostはdarwin-arm64だけです。
起動時はmanifestの読み取りとfileのdigest計算だけを行い、Copilotは実行しません（`--version`も呼びません）。
Copilotのroleを含まないteamでは、Copilotの依存を探しません。
dispatchは専用directory、状態、processを作る前に依存の固定を確かめ直し、runnerはclientを起動する前にもう一度確かめます。

server argvはPythonだけが組み立てます。
`--acp --stdio`、model、effortに加えて、
`--no-auto-update --no-custom-instructions --disable-builtin-mcps --no-remote --no-remote-export --disallow-temp-dir --no-ask-user`を必ず付けます。
PlannerとReviewerには`--available-tools view,grep,glob`を指定し、`shell`、`write`、`url`を拒否します。
Workerには`--available-tools view,grep,glob,edit,create`を指定し、`shell`と`url`を拒否します。
`--allow-tool`、`--allow-all*`、`--yolo`、`--mode`は付けません。
modelは`^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$`に一致する必要があり、`auto`は使えません。
effortは`low`、`medium`、`high`、`xhigh`、`max`のいずれかです。
それ以外の値は、起動時に状態を書き込む前に拒否します。
clientとCopilotに渡す環境変数は、`HOME`（通常のhome）、`PATH=/usr/bin:/bin`、
専用directory内の`TMPDIR`と`COPILOT_HOME`、`USER`、`LOGNAME`、`LANG`、`LC_*`だけです。
token、BYOKやprovider用の変数、proxy、`NODE_OPTIONS`、`XDG_*`は渡しません。
専用の`COPILOT_HOME`には、生成した`settings.json`だけを置きます。
この設定はhookとremote接続を無効にし、専用directory、`.git`、状態directory、network接続、
keychainへのアクセスを拒否するsandboxを指定します。
agent-teamはloginせず、通常の`~/.copilot`、その`config.json`、keychainの項目を読み取ることも、
linkやcopyをすることもありません。

共通のclientは、`session/request_permission`ごとに生のJSON-RPC paramsと固定済みのwrite policyから可否を決めます。
選ぶのは`allow_once`だけで、`allow_always`は選びません。
readとsearchを許可するのは、対象pathがすべて共通のworkspace検査を通る場合だけです。
対象pathはtool callのlocationsと、`rawInput.path`または`rawInput.file_path`です
（後者の2つはlocationsにも含まれている必要があります）。
editはWorkerだけに許可し、対象pathがすべてTaskSpecの書き込み範囲内にある場合に限ります。
実機の観測で各toolの引数の形を固定するまでは、`rawInput`にそれ以外のfield（globのpatternや編集内容など）がある要求も拒否します。
execute、fetch、delete、move、other、未知の種類、形の不正な要求も拒否します。
searchやdirectoryのreadは起点のpathだけで判定します。
その配下の`.git`、入れ子のrepository、symlinkをCopilotがどう走査するかは、この判定では検査しません。

clientはagentの出力も監視します。
承認していないedit、delete、move、execute、fetchの開始または完了、承認範囲外のlocationを報告した承認済みedit、
範囲外のreadの完了、未知のtool kindやstatus、sessionのmode変更、報告されたmodelの変更、JSONでない出力、
SDKのschemaが捨ててしまうsession updateのいずれかを検出すると、turnを中断して失敗にします。
promptの終了後に検出した場合も失敗にします。
この監視は違反を事後に検出するもので、防ぐのはflagとpermissionの判定です。
clientは`authenticate`、sessionの設定変更、modeの変更を送りません。
`session/close` capabilityを必須とし、
報告された`agentInfo.version`が`1.0.91`以外の場合や、報告されたmodelが指定と異なる場合は拒否します。
session開始の失敗は、loginや他のproviderへの切り替えをせずにそのまま報告します。

テストでは偽のnpm prefixと偽のACP agentを使い、依存選択、argv、環境変数、専用file、接続処理、
permission判定表の全行を確認しています。
これらはCopilotの実際の挙動を検証した証拠ではありません。
ACP modeでCopilotがこれらのflagに従うか、permission要求、toolの引数、tool更新の実際の形、
searchが`.git`やsymlinkをどう扱うか、loginをどこから読むか、中断と終了処理の挙動は、
実機での受け入れ試験まで未確定です。

## 認証とsubscription

ACPはaccountを選択したり、providerのbilling policyを回避したりしません。Claude profileは
adapterが利用できるambientな`claude.ai` loginを再利用します。特定のturnがsubscription quotaに
どう計上されるかはprovider accountの問題であり、このtoolは保証しません。API key用adapterへの
自動置換も行いません。login、account変更、package installは`agent-team`の外で行います。

## ACP profileを追加する条件

adapterを対応matrixへ登録するには、exact version policy、認証経路、positive lifecycle smoke test、
read/write/process/networkのnegative testを記録する必要があります。adapterが存在するだけでは
不十分です。条件が揃うまで、その構成はruntimeのresourceを作成する前に拒否します。

この依存関係bindingで確認できるのは、選択したClaude profileの実行ファイルidentityです。
他のACP adapterをrunnableへ昇格させたり、[対応matrix](support-matrix_JA.md)に記録したscopeや
statusを変更したりはしません。
