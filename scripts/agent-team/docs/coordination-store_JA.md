# CoordinationStoreの選定

[English](coordination-store.md)

## SQLiteを採用する

将来のLocalBackendが使うdurableなCoordinationStoreにはSQLiteを採用する。atomic fileへのruntime fallbackは追加しない。

対象は、単一hostのlocal filesystemである。SQLiteなら、複数recordのtransaction、writerの直列化、schema制約、診断用query、整合したbackupを標準機能で実装できる。atomic fileは、restore後のfencing token再利用、空storeを変更するdiagnose、provider証拠なしのsynthetic receipt受理を再現したため、production候補から外す。

どちらの方式でも、外部effectの実行後、receipt保存前にprocessが終了した場合、そのeffectが起きたかどうかをstoreだけでは確定できない。この状態は`UNKNOWN_EFFECT`として保持し、自動retry、resource close、別backendへの切替を行わない。

## 同じ条件で比較した

両方のprototypeをPython 3.13.15、macOS arm64、標準libraryだけで実装した。main agentがcodeを確認したうえで、全scenarioを再実行した。

| 観点 | SQLite | atomic file |
|---|---|---|
| scenario | safety scenario 14/14成功 | probe 14/14成功。ただし3件は危険な挙動の再現 |
| concurrent writer | 2 writerが合計64行を欠落なく追加 | blocking `flock`で2 writerを直列化 |
| stale writer | fake providerが新しいtokenを観測した後、古いtokenのcallを拒否 | revision CASとowner、attempt、fencing tokenで古いlocal receiptを拒否 |
| crash診断 | reopen後にintent-onlyとeffect-without-receiptを区別 | partial temp、complete candidate、renamed primary、backupを区別 |
| lock競合 | `BEGIN IMMEDIATE`がtimeout後に`database is locked`を返す | non-blocking `flock`が`LockBusyError`を返す |
| backup | `Connection.backup()`でintegrity確認済みcopyを復元 | 直前の1 revisionを明示的に復元 |
| cleanup | connection close、WAL checkpoint、SQLite管理のWAL/SHM lifecycle確認 | known tempの分類、file/directory fsync、backup管理 |
| portability | 対応platformのPython `sqlite3` | POSIX `fcntl.flock`。Windows実装ではない |
| 実装risk | schemaとtransactionの実装 | database、lock、CAS、backup、recoveryを独自実装 |

## 選定とproduction readinessを分ける

SQLiteを選ぶ根拠は揃った。ただし、production LocalBackendの実装準備が完了したわけではない。今回の条件では、local transaction、同一operationの競合、process-safeなfake provider、unknown effect、lease expiry、read-only doctor、WAL cleanup、recovery epochを検証した。

production実装では、次を専用contract testで閉じるまで完了扱いにしない。

- `RECEIPTED`と`COMPLETED`をrestoreしても操作を孤立・再実行させず、`CLEANED`のtombstoneは変更しない
- provider DBに未checkpoint WALがある場合も、status queryが誤って`ABSENT`を返さない
- reclaim後のold-call-firstとnew-call-firstの両方でstale effectを拒否する
- provider receiptのprovenance、effect identity、fencing token、epochを検証する
- recoveryのexpiry・force・audit条件とdoctorの禁止mutation一覧を固定する
- clock、writer marker race、atomic restore、directory durability、append-only transition journalを検証する

これらはproduction store/LocalBackend Issueのblockerである。SQLiteとatomic fileの選定をやり直す理由にはせず、atomic fileへfallbackしない。

SQLite spikeではPython SQLite 3.53.1、`WAL`、`synchronous=FULL`、`BEGIN IMMEDIATE`、150 msのbusy timeoutを使った。競合writerは`database is locked`を返した。接続初期化も含めたend-to-end時間は設定timeoutを超えるため、設定値を処理全体の上限とは扱わない。WALとSHMは全connectionを閉じた後に消えた。

## production実装で守るcontract

- 外部effectを呼ぶ前にoperation intentをcommitする
- operation ID、attempt、owner、`lease_epoch`、heartbeat/expiry、fencing token、idempotency key、phase、exact resource receiptを保存する
- 全mutationでowner、attempt、fencing tokenの一致を要求する
- provider側でもlease epoch/fencingを検査し、old-call-firstとnew-call-firstの両方を拒否する
- 外部側のidempotency keyまたはstatus照会を使う。SQLiteをexactly-once effect機構として扱わない
- effect-without-receiptは`UNKNOWN_EFFECT`とする。`doctor`は報告だけを行い、変更やretryをしない
- 通常の`CoordinationStore`再openでは`FENCE_RESERVATION_STARTED`や`EFFECT_PREPARED`を自動で
  変更しない。これらはrecovery-required markerとして保持する。`UNKNOWN_EFFECT`への遷移は、
  `CoordinationStore._recovery_transaction()`が返すprivateなtrusted transactionの
  `_RecoveryStoreTx.mark_prepared_unknown`だけに限定する
- recovery floorのadvanceはglobalにstale authorityをfenceするが、`CLEANED`のtombstone rowと
  eventは書き換えない。typed rebaseの対象は`INTENT`、`RECEIPTED`、`COMPLETED`に限る
- typed recovery seamでは、SQLite snapshot queryの失敗と保存済み観測値のmalformed値を
  stableな`StoreIntegrityError`へ正規化し、既存のStoreErrorは二重wrapしない
- `BEGIN IMMEDIATE`、bounded busy timeout、`foreign_keys=ON`、`WAL`、`synchronous=FULL`を明示する
- databaseとsidecarをprivateなagent-team state rootへ置く。cleanup前に全connectionを閉じ、checkpointする
- SQLiteのbackup APIを使う。migrationとrestoreは、versionとrollbackを検証する明示操作にする
- SQLiteが利用不能、timeout超過、破損、version不一致の場合、atomic fileや別backendへfallbackしない

## 追加の環境検証

- disk full、I/O error、書込み中backup
- 実Orca/Herdrのeffect照会とidempotency
- process `SIGKILL`では再現できないpower lossとfilesystem fault
- 選定したschemaでのmigration中断、backup restore、rollback

これらはLocalBackend実装の完了条件にする。store選定を保留する理由にはしない。

## 証拠

throwaway codeと詳細な測定結果はruntime packageへ入れず、実行sessionに保存した。

- SQLite harness SHA-256: `6dc978a709f3e7511956bb4206701495beb0371e9ef7c9933a811a5ead3ca9e5`
- SQLite tests SHA-256: `66159d6e70f3d3766196daee4909a4dcd5e1a254169cb9757de2a52bd0cc5c75`
- atomic-file harness SHA-256: `3798bdeef42629555b08aca4a0ef222efad476f257ffd15d4eae5099785ec490`

prototypeは使い捨ての証拠である。production codeはこのcontractから実装し、別のtestで検証する。

## read-only doctor substrate

`agent_team.doctor` は、復旧処理から共有する read-only の
`ReadOnlyDoctor`、`StateFilesystem`、`RecoveryLedgerReader` seam を提供する。
stable writer marker と recovery ledger の basename は、marker/ledger実装が所有するため、
呼び出し側が明示的に渡す。doctor がファイル名を暗黙に
補完したり、`CoordinationStore` を構築したり、recovery を実行したりすることは
ない。

filesystem reader は、既存の owner-only directory/file だけを
`O_RDONLY|O_NOFOLLOW|O_CLOEXEC|O_NONBLOCK` で開き、存在する lifetime gate と stable writer marker を shared lock で保持して
root 直下の全 name を inventory に記録する。安全な regular file について type、owner、
mode、link 数、device/inode、size、timestamp、SHA-256 を保持する。unsafe な root entry
と identity の変化は拒否し、report を返す前に fileset 全体も再確認する。missing
root/gate は観測するが、作成しない。non-zero WAL、pending restore ledger、active
writer marker、schema mismatch、identity race は fail-closed のまま扱う。report は
有限の state/action/mutation と検証済み opaque owner identity だけを含み、path、
SQLite row、provider payload は公開しない。

既存の primary DB を読むときは、検証済みの read-only descriptor を開いたまま、
descriptor から bounded に読み込んだ bytes を in-memory SQLite DB へ deserialize
する。SQLite が state pathname を再 open する経路はないため、path が FIFO や別 DB に
置き換わっても保持した fd を読み、最後の fileset 検証で unreadable に倒す。
deserialize または in-memory validator に失敗した場合も pathname fallback は行わない。

recovery ledger は strict な append-only JSONL とする。各行は完全な JSON
object を canonical newline で終え、array、前後に余分な空白がある行、空行、
途中で切れた最終 record、terminal phase から始まる世代を受け付けない。各世代は
`RESTORE_PREPARED`、`RESTORE_REPLACED`、`RESTORE_COMMITTED` または契約で
許可した `RESTORE_ABORTED` edge の順に進む。generationは1から始まり、必ず1ずつ
増える。terminal generationの次に置けるのは`RESTORE_PREPARED`だけであり、
`RESTORE_ABORTED`の直前は`RESTORE_PREPARED`に限る。この規則は、単独で作る最初のgenerationにも、
terminal prefixの後に追加するgenerationにも同じように適用する。primary DB、SQLite の exact sidecar、marker、
ledger の basename は regular file に限定する。無関係な owner-only directory は
inventory に残すが、開かない。

## 明示的な recovery coordinator

`agent_team.recovery.RecoveryCoordinator` は、この child issue が所有する
recovery mutation の境界である。通常の `recover` は、正確な
`CLAIMED` または `FENCE_PENDING` の identity と
`now_ns >= lease_expires_ns` を要求する。private typed store transaction が
state と event を CAS で一体更新するため、同時実行で負けた caller は
conflict になる。provider effect の retry、close、execute は行わない。
期限後の plain な `FENCE_PENDING` には provider marker がないため、typed
store reclaim で未使用の次の attempt を割り当て、古い attempt と event 履歴を
保持する。proof を持つ `CLAIMED` は外部結果が不明なため
`UNKNOWN_EFFECT` に止める。

`force_recover` には、正確な operator identity、有限集合
`FORCE_REASON_CODES` に含まれる built-in の正確な文字列 reason、
`RecoveryAuthorizer` が返す authorization が必要である。authorization の
operation、operator、reason、audit reference も built-in の検証済み文字列として
すべて一致しなければならない。比較を上書きした値、boolean、欠落・暗黙 default、
自己申告の値は拒否する。epoch/token floor は store-issued の
`RecoveryFloorReservation` で進め、coordinator は token を計算しない。
claim と prepared effect の不確実性は force 後も保持する。既知の
`INTENT`、`RECEIPTED`、`COMPLETED` は typed rebase authority を使い、
`CLEANED` は不変のままにする。

`resolve_unknown` が provider に行うのは `status` query だけである。adapter は
完全な trusted `ProviderPort` の shape と、正確な runtime type の
`ProviderCapabilities` を持たなければならず、status だけの object は拒否する。
返された status も正確な `ProviderStatus` runtime type と全 field を手動検証する。
operation、effect、provider、owner、attempt、epoch、fencing token、fence proof が現在の
identity と完全一致し、strong consistency である場合だけ受理する。
`ABSENT` だけを `INTENT` に戻し、一致する `COMPLETED` は verified receipt
を保持した `RECEIPTED` にする。weak、old、timeout、WAL pending、identity
不一致は blocked のままにし、`execute` は呼ばない。

この query の前に、coordinator は store の read-only typed seam から現在の
global recovery epoch を読む。operation が既に stale なら provider call は
0 回で止める。query 後に epoch が変わる race は transaction の CAS が拒否する。

`RecoveryLayout` は exact な frozen/slotted value である。marker identity と
canonical な `recovery.ledger` basename を固定し、coordinator の全 public entry
point が state の inspect/mutation 前に再検証する。pending restore ledger を隠す
public setter はない。

`agent_team.recovery` の writer が所有する basename は固定の
`recovery.ledger` とする。初回作成には typed な
`RecoveryLedgerInitialization` authority が必要で、通常の `append()` は
欠損 ledger を作らない。`RECOVERY_LEDGER_VERSION=1` の record を
`RecoveryLedgerReader` と互換な strict JSONL として出力する。検証済みの
root descriptor を create/append/readback の間保持し、ledger の全 read/write open
に `O_NONBLOCK` と no-follow/close-on-exec/append の必要 flag を付ける。
FIFO や symlink への swap は block せず拒否する。`O_APPEND|O_NOFOLLOW`
で追記し、ledger と containing directory を fsync する。欠損、duplicate、
partial、version 不一致、sequence/generation/epoch/floor の逆行、空 ledger は
拒否し、write 後の bytes 全体も検証する。backup、restore、checkpoint、sidecar
cleanup、writer marker lifecycleは実装しない。これらは`agent_team.backup`、
`agent_team.restore`、`agent_team.wal`がそれぞれ所有する。

ownerを渡さないmutation APIも、通常のStore openと同じstartup/quiescence protocolに
参加する。初期化済みrootではWAL controllerのexclusiveなlifetime gate・marker spanを
使う。bootstrapまたは明示的にgateが欠けたrootでは、Storeと共通のroot startup lockを、
再検査、append、fsync、readbackが終わるまで保持する。owner-awareなrestore appendは、
保持中のquiescence ownerを再利用し、lockを取り直さない。read-only accessはsharedのままにする。
writer、Store、Doctor filesystem、WAL controller、backup、restoreは、closeが不確かな
descriptor、resource、sessionを観測済みidentityとともに保持し、次のI/Oより先にcleanupを
再試行する。再利用されたfdやidentity不明のfdを、番号だけでcloseしない。

commit response loss、rollback failure、temporary SQLite connectionのcleanup failure、
descriptor registryの上限到達は、いずれも成功ではなくcleanupの不確実性として扱う。
body errorをprimaryのまま保持し、すべてのcleanup ownerをopaqueなbest-effort composite
retry capabilityに束ねる。このretryは全memberを試し、成功したmemberを外し、失敗した
memberだけを残してidempotentに再試行する。registryが満杯でも現在のresourceを捨てない。
cleanup retryがSQL、phase append、primary replacement、provider operationを再実行することはない。

provider adapter は trusted composition root の依存である。full-shape の
悪意ある同一 process adapter はこの Python value boundary の外側だが、task data
や通常の caller が adapter を選択・注入する経路は持たない。

## stable writer marker と WAL sidecar controller

`agent_team.wal.WalSidecarController` は、exact な marker basename
`writer.marker` と、SQLite の `coordination.sqlite3-wal`、
`coordination.sqlite3-shm`、`coordination.sqlite3-journal` だけを所有する。
database、marker、sidecar、ledger、unknown entry、FIFO、symlink のいずれもない
完全に空の root だけが bootstrap できる。有効な
`CoordinationStore` は `O_CREAT|O_EXCL|O_NOFOLLOW`、owner-only の
mode `0600`、containing directory の `fsync` で `writer.marker` を一度だけ
作成する。store と read-only filesystem user は open から close まで marker の
shared lock を保持する。marker は unlink/recreate しないため、store と cleanup
cycle をまたいで path と device/inode が安定する。schema が不正な経路と
read-only doctor は marker を作成しない。database/marker の片方だけ、zero-byte または
truncated DB は SQLite open/schema initialization 前に fail closed し、既存 state を
再初期化しない。non-zero DB で schema が空の場合は、read-only SQLite schema inspection
直後、initialization 前に拒否する。

marker の内容は version 1 の canonical record とし、通常時は `CLEAN`、sidecar
削除中は `CLEANUP_PREPARED` とする。prepared または malformed marker があれば
通常の store open は fail closed し、doctor は operator review を要求する。
初期化済み DB で marker が欠落した場合も fail closed とし、作成できるのは最初の
empty-store initialization だけに限定する。

mutation の lock order は lifetime gate、次に writer marker とする。両方の
exclusive guard は `LOCK_NB` と有限 deadline を使い、取得できなければ typed busy
error/result にして fallback しない。guard を両方取得した後は、root、gate、marker、
database の検証済み descriptor を保持し、effect の前に type、owner、mode、link 数、
device、inode を再確認する。

backup/restore consumer は `hold_quiescence()` で opaque な
`QuiescenceSession` を取得できる。typed な `checkpoint`、`cleanup`、
`assert_identity`、`copy_database_to` は同じ exclusive guard を複数 phase にわたって
保持するため、`BackupRestore`はmarker/lifetime lockを複製しない。
`copy_database_to` は exact な checkpoint request を必須とし、controller 管理の
SQLite backup call を memory に一度だけ実行する。その serialized image を、
`O_CREAT|O_EXCL|O_NOFOLLOW` と mode `0600` で新規作成して held descriptor に書き込む。
source/target identity、target の exact sidecar、最後の target bytes を検証し、identity と
target の `sha256:` digest を返す。`SQLiteBackup`はbasenameをno-followでopenし、消費直前に
identity、size、digest を再検証する。result 自体は publish/restore authority ではない。既存の
known/unknown file は上書きしない。authorized な primary replacement 後は package-private
の database rebind で held descriptor を更新する。backup、restore、replacement policy
自体はこの module の責務に含めない。

checkpoint caller は、mode が `PASSIVE`、`FULL`、`RESTART`、`TRUNCATE` のいずれか
一つである exact な `CheckpointRequest` を渡す。`CheckpointResult` は SQLite が返す
`(busy, log, checkpointed)` を保持する。non-empty rollback journal または pre-existing
non-zero WAL は SQLite open 前に blocked にし、この preflight result の checkpoint tuple
は `None` とする。open 前 snapshot に WAL/journal がなかった場合は、standard library の
pathname connection で non-safe tuple を inode に bind できないため、serialized bytes が
一致しても recovery-required とする。safe tuple でも held bytes との serialize binding を
必須とする。canonical な pre-existing WAL は sidecar identity を検証した明示的 checkpoint
に限り SQLite の busy tuple を返せるが、cleanup と source-copy は SQLite open 前に拒否する。
WAL は parse/consume しない。既存の `CoordinationStore` も SQLite
connection を開く前に pending journal を拒否する。その他の `busy != 0`、
`log != checkpointed`、reader/writer active は typed blocked とする。cleanup は Python の `unlink` を行わず、`CLEANUP_PREPARED` を durable にしてから
SQLite の `journal_mode=DELETE` → `locking_mode=EXCLUSIVE` → `journal_mode=WAL` を
exact に確認する。`CLEANUP_PREPARED` 後に journal または non-zero WAL が現れた場合は
recovery-required とし、marker は prepared のままにする。実際の DELETE transition の
busy（SQLite が元の `wal` mode を返す場合を含む）だけは `CLEAN` に戻して blocked とし、
後続 transition・sidecar・identity・durability の不確実性は prepared のまま operator review
に渡す。
exact sidecar が消え、root directory を fsync し、exclusive connection を保持したまま
marker を `CLEAN` に戻してから close する。SQLite transition の直前には WAL/SHM の構造と
rollback journal の状態を再確認する。close 後に新しく作られた sidecar は後続 activity として
扱い、完了した cleanup が消費しない。最後の確認後に arbitrary filesystem writer が直接介入
する場合は SQLite lock protocol の保証範囲外であり、観測できた場合は保守的に扱うが、不可能
とは主張しない。provider、terminal、prompt、その他 unknown file は glob で探索・削除しない。

## version 1のbackup artifact

`agent_team.backup.SQLiteBackup`は、所有者だけが扱う既存のstate rootにbackupを作成し、
内容を検査する。version 1が使う最終ファイルは、callerが指定するdatabase basenameと、
そこから決まる`<name>.manifest`の2つだけである。入れ子のpath、外部archive directory、
retention、rotation、generation pointerは対象外とする。

`create()`は1つのquiescence sessionを保持し、
`copy_database_to(CheckpointRequest("TRUNCATE"), ...)`でprimaryをcheckpointしてcopyする。
sessionを閉じた後、Storeが所有するimage readerでcopyを検証する。manifestは、
canonicalなUTF-8 JSONと末尾のLF 1つで構成する。fieldは次の10個に固定する。

- version
- database basename
- Store schema version
- event schema version
- SQLite `user_version`
- integrity結果
- database size
- databaseのSHA-256 digest
- 取得時のrecovery epoch
- 取得時のfencing-token floor

duplicate、欠落、未知、非canonical、型不一致のfieldは拒否する。manifestの値を使って
recovery floorを更新することはない。

destination nameは1つのexactなbasenameに限る。path component、reserved name、restore candidate
namespaceである`.coordination.sqlite3.restore-`、wildcard文字（`*`、`?`、`[`、`]`）は、保持済み
resourceのretryやopenより前にfail-fastする。このprefixで始まるnameはrestoreが所有するため、
backup destinationには使えない。
`create()`は、この呼び出しで自分が公開した`BackupArtifact`だけを返す。返却前にfinal pair
inspectionを行い、公開したdatabaseとmanifestのidentity、canonicalなmanifest content、
databaseのsize/digest、captured floorが計画値と一致することを再確認する。見えているpathname
や以前のinspectionだけでは不十分であり、final manifest contentまたはidentityの不一致はerrorになる。

databaseとmanifestは別ファイルであり、POSIXには2つを同時公開する操作がない。database、
manifestの順に置換し、最後にcontaining directoryをfsyncする。置換間のcrashでは、
片側欠損や、DBとmanifestの一方だけが新しいpairが残り得る。`inspect()`が受理するのは、
basename、両ファイルのidentity、size、digest、schema、integrity、取得時のfloorが一致する
完全なpairだけである。古い片側へのfallback、新しい片側のrollback、orphan tempの
promotionは行わない。
unsafe type、link、owner/mode不一致、artifact sidecar、close uncertaintyはfail closedにする。
`create()`は、publicationまたはdirectory fsyncが不確実な場合も成功を返さない。後から行う
read-onlyの`inspect()`は現在のbytesとidentityを検証するが、過去のdirectory fsyncが
どのように完了したかまでは証明しない。
同様に、非協調なsame-UID processは、最後のprecondition確認後から`os.replace`までの間に
pathnameを差し替えられる。この最後のsyscall windowはversion 1の保証対象外である。
不一致を観測した場合はstateを保持し、成功したartifactとして報告しない。

## candidate-first restoreとdurable fencing

`agent_team.restore.BackupRestore`の高水準操作は`restore()`と`resume()`だけである。
callerは、直前に検査した`BackupArtifact`、opaqueなactor identifier、audit referenceを渡す。
resultはterminal phase、restore generation、source digestとcandidate digest、Store-issuedの
final `RecoveryFloor`を含む。また、destinationにだけ存在し、tombstoneとして残した
operation/effect identityも返す。path、descriptor、SQLite row、provider payload、
operationごとのtokenは含めない。

restoreは、lifetime gateとwriter markerをexclusiveで保持する`QuiescenceSession`を
1つ取得する。sessionから発行されたownerは、final readbackまで保持する。candidateを
作る前に、sourceと過去のcommitted tombstoneを照合する。これにより、古いbackupから
廃止済みのoperation IDやeffect keyが復活することを防ぐ。Store authorityはsourceとcurrent primaryを
読み、source、destination、ledger、全attemptのhigh-waterよりstrictに大きいepoch/token
floorを発行する。status更新は1つのin-memory `BEGIN IMMEDIATE` transactionで行う。
完全にシリアライズし、fsyncして検証したcandidateだけをdescriptor相対のprimary置換へ渡す。
restore timestampは、sourceとdestination双方のdurable clock high-water以上でなければならない。
古い値はclampせず、`ClockRollbackError`で拒否する。

10種類のoperation statusは外部の事実を変えない。

`INTENT`、`FENCE_PENDING`、`FENCE_RESERVATION_STARTED`、`CLAIMED`、
`EFFECT_PREPARED`、`UNKNOWN_EFFECT`、`UNKNOWN`、`RECEIPTED`、`COMPLETED`、
`CLEANED`がoperation stateのpolicyである。`RESTORE_INCOMPLETE`は別のrecovery
observationであり、sourceとして受け付けない。

- `INTENT`はattempt 0のまま、新しいepochへ進める。
- `FENCE_PENDING`、`FENCE_RESERVATION_STARTED`、`CLAIMED`、`EFFECT_PREPARED`、
  `UNKNOWN_EFFECT`、`UNKNOWN`はstatusと証拠を保持し、古いlease authorityだけをstaleにする。
  restore中にproviderの照会、実行、retry、reclaimは行わない。
- `RECEIPTED`はeffect、proof、owner、attemptを保持し、Store-issuedのexactなexpected
  epoch/tokenをattemptとreceiptへ同じtransactionで反映する。candidateとresumeの検証は、
  計画したtoken以外を拒否する。
- `COMPLETED`はterminal receiptを保持し、restore epoch/eventだけを追加する。
- `CLEANED`ではglobalの`RecoveryFloor`とstore全体のdurable clockはadvanceできるが、row、
  attempt、receipt、event、`updated_ns`はimmutableのままにする。
- `RESTORE_INCOMPLETE`を含むsourceは受理せず、operator reviewへ送る。

restoreはproviderの実行・status照会、自動retry、backend fallback、terminal resourceのcloseを
行わない。DDL、`STORE_SCHEMA`、`EVENT_SCHEMA_VERSION`、SQLite `user_version`は検証するが、
変更しない。

既存の9-field `recovery.ledger` version 1は変更しない。別のstrict append-onlyな
`recovery.tombstones` version 1には、generationごとのsource/old-primary/candidate digest、
destination-onlyのoperation/effect key、旧primaryのepoch、fencing-token、clock high-waterを
保存する。

通常のStore openは、lifetime gate、marker、database、SQLite connectionを作成またはopenする前に、
`recovery.ledger`と`recovery.tombstones`の履歴を検証する。shared lifetime gateを取得した後にも、
同じ履歴を再検証する。pending、partial、malformed、世代間不一致、
phase/digest/high-water不一致では通常openを拒否する。committed tombstoneと衝突する
`create_intent`も拒否する。

`ReadOnlyDoctor`も同じledger/tombstone pairを事前検査する。pendingまたはmalformedな
restore pairは両logを変更せず、observed stateを`RESTORE_INCOMPLETE`、safe actionを
`OPERATOR_REVIEW`として報告する。
tombstone履歴がなく、bareな`recovery.ledger`だけがmalformedな場合は、従来どおり
`UNREADABLE`とする。この場合もoperator reviewが必要である。

markerがcleanな場合、6つの不確実なoperation status、すなわち
`FENCE_PENDING`、`FENCE_RESERVATION_STARTED`、`CLAIMED`、`EFFECT_PREPARED`、
`UNKNOWN_EFFECT`、`UNKNOWN`は、`observed_state=UNKNOWN_EFFECT`、
`safe_action=OPERATOR_REVIEW`、低いconfidenceで報告する。このactionはoperatorへの
advisoryであり、coordinatorを呼び出す許可ではない。public observationだけでは
provider proof、expiry、現在のrecovery epochを確定できず、coordinatorもより狭い
exact preconditionしか受け付けない。そのためDoctorはこの6 statusについてproviderを
呼ばない。marker、pair、identityの異常がある場合は、さらに保守的なreview状態になる。

restore後のoperationでは、recovery epochと保持したlease epochが意図的に異なる場合がある。
Doctorがこの差を許可する根拠は、完全に検証したcanonicalなledger/tombstone履歴だけである。
committed recovery epochは現在のStore imageと一致し、現在のtoken high-waterはcommitted floor
以上でなければならない。さらに、Storeが所有するimage全体のhigh-water検査を通す。
callerが指定した診断用ledgerは、この例外を認可できない。abortだけの履歴、rowやtokenの
破損は`UNREADABLE`のままにする。

### stable restore-history binding

committed generationごとに、Storeはstableな`restore-history-binding` digestを計算する。
canonicalな入力は、latest committed logのstable field、すなわちgeneration、actor、
audit referenceのdigest、sourceとprevious primaryのdigest、previous recovery epoch・
token・clock high-water、final epoch/token floorである。これにcurrent generationの
tombstone batchと、すべてのcommitted tombstoneからなるactive identityの累積unionを加える。
candidateのrestore eventにはこのbindingを記録し、normal-open verifierは一致するprimary
restore eventをhistory anchorとして検証する。

`candidate_digest`は別のexact evidenceである。current generationのcandidate apply、primary
replacement、final result、resumeでは、expected candidate bytesを、replacement後はfull primary
imageをこのdigestで束縛する。関係する検証の間はbytesが変わらないことを前提とするため、
これらの経路ではdigestとidentityのexact比較を維持する。一方、stableなnormal-open binding
は意図的に`candidate_digest`を含めない。含めると、primaryに保存するevent refとのself-reference
になり、後続の正当なwriteでprimaryが変わるたびにhistory anchorが無効になるためである。
version 1のno-wire contractでは、すでにcommittedなhistoryの`candidate_digest`だけを一貫して
書き換えても、normal openや新しいrestoreで認証されず、必ず検出できるとは限らない。この改変は
 累積tombstone identity fenceを変えない。このケースまで保証するにはsignature、attestation、
 またはversioned durable anchorが必要である。

constructorはnormal stateをopenする前にrecovery pairをpreflightし、openしたimageに対する
binding検証を後続のPRAGMAやschema初期化より前に行う。`create_intent()`はshared lifetime gateの
中でpairとbindingを再検証する。新しいrestore generationも、candidate作成やrecovery record
追記の前に、現在のcommitted bindingを検証する。これによりopen後のhistory tamperを拒否し、
older backupが改ざんされたhistoryを新しいanchorとして取り込むことを防ぐ。mutableな
operation/status/token planはstable bindingに含めず、candidateとresumeでoperation、evidence、
receipt、expected tokenを別途exactに検証する。

latest generationが`RESTORE_ABORTED`/`ABORTED`の場合、normal-open stateは直前のcommitted
generationがあれば、そのhandleと累積unionを使う。直前のcommitted generationがなければstateは
emptyである。abortしたgenerationのidentityはactiveに含めない。
current batchと累積unionがどちらも空ならcollision anchorは不要である。ただしzero-event
restoreのprovenanceは、下記の保証境界に残る。currentまたは累積tombstone unionが空でない
zero-event restoreは、candidate transactionのcommit、ledger preparation、
`RESTORE_COMMITTED`より前にfail-fastする。

ledger recordとtombstone recordは別のappend-only fileであり、一つの原子的操作では公開されない。
同じgenerationで許可する組み合わせは次の6つだけである。途中の2組はresponse-loss状態を表す。

| Recovery ledger | Tombstone log | 意味 |
|---|---|---|
| `RESTORE_PREPARED` | `PREPARED` | 通常のprepared state |
| `RESTORE_PREPARED` | `ABORTED` | abort response-loss |
| `RESTORE_REPLACED` | `PREPARED` | 通常のreplaced state |
| `RESTORE_REPLACED` | `COMMITTED` | commit response-loss |
| `RESTORE_COMMITTED` | `COMMITTED` | terminal committed state |
| `RESTORE_ABORTED` | `ABORTED` | terminal aborted state |

これ以外の組み合わせ、片側欠損、不正なhistory prefix、high-water不一致は
recovery-requiredとする。

### pair単位のresume durability barrier

`RestoreLedger.read_for_resume()`はrecovery stateを最初に読むconsumerである。すでに保持して
いるquiescence ownerを使い、既存のlogだけをopenして、non-blocking lockを`tombstone`、次に
`ledger`の順で取得する。fsyncやmutationの前に、2つのbyte streamをstrictにparseし、pairを
classifyする。missing、partial、malformed、unsafe、mixed、generation skewのいずれかなら
fail-closedでreviewに止まり、log、primary、candidateを変更しない。

validなpairだけについて、各logをfsyncし、その後state root directoryをfsyncする。続いて同じ
locked descriptorからexactなbytesを読み直し、fileとrootのidentityおよびpair classificationを
再検証する。見えているbytes、JSON parseの成功、前のprocessが返した結果だけではdurabilityの
証明にならない。file/root fsync、readback、identity、unlock、closeのどれかが不確かな場合は、
durability proofもphaseや成功resultも返さない。barrierは欠けたlogを作らず、partial recordを
切り詰めず、どちらのfileも暗黙に修復しない。barrierが成功した後だけ、`resume()`は許可された
tombstone-first stateの不足recordを1行だけ追記し、そのproofのためにcandidate transactionや
primary replacementを再適用しない。

durable phaseの意味は次のとおりである。

- `RESTORE_PREPARED`: candidate transaction、image検証、tombstone evidenceがdurableである。
  primaryは、置換のdurabilityを明示的に証明できない限りoldとして扱う。
- `RESTORE_REPLACED`: descriptor-relative replace、directory fsync、descriptor rebind、
  primary image検証が完了している。
- `RESTORE_COMMITTED`: tombstoneとledgerのcommit recordがdurableである。高水準操作が成功を
  返すには、その後のartifact、primary、fileset、sidecar、ownerのfinal readbackも必要になる。
  final readbackが失敗してもterminal recordが残る場合がある。`resume()`はrecordだけで成功と
  判断せず、検証をやり直す。
- `RESTORE_ABORTED`: replaceが起きていないことをoperatorが明示的に証明した。
  高水準restore pathは自動abortしない。

`resume()`は、消えたcandidateを作り直したり、transactionを再適用したりしない。prepareは
tombstoneをledgerより先に公開するため、tombstone-firstのresponse-lossとして受理するのは、
ledgerがない初回generationと、完全なterminal prefix直後の次generationだけである。source、
old primary、candidate、旧epoch/token/clock high-water、committed identity set、candidate floorを
検証してから、不足したprepared ledger record 1行だけを追記する。それ以外の片側欠損や
generation skewはoperator reviewのままにする。`RESTORE_PREPARED`と`ABORTED` tombstoneの
組み合わせでは、old primaryを確認してから不足したabort ledger recordだけを追記し、candidateを
applyまたはreplaceしない。old primaryとexact candidateが残るprepared stateだけは、再検証後に
replaceできる。new primaryだけが残る
prepared stateでは、renameが完了したか、directory fsyncが永続化したかを後から判別できない。
この場合はoperator reviewに止める。replaced generationはfinal verificationとcommitだけを行い、committed
generationは検証済みno-opになる。mixed、missing、ambiguous、mismatch stateをrollback、
promote、silent repairしない。

## backupとrestoreの保証境界

検証済みの保証範囲は、Python標準`sqlite3`、default local POSIX VFS、所有者だけが扱う
local state root、協調する`CoordinationStore` client、deterministic fault barrier、
観測可能なpathname/inode raceである。POSIXにはportableなcompare-and-unlinkがないため、
最後の明示的なidentity確認後に、
非協調なsame-UID processがpathnameを差し替える動作は保証対象外とする。unknownまたは
不一致を観測したidentityは保持し、不確実性は保守的に報告する。同じUIDのprocessがprimary、
`transition_events`、両方のrecovery logを一貫した内容へ同時に書き換える場合、その改変は
検出できない。active tombstone unionが空のzero-event restoreにはdurableなprovenance anchorが
ない。bindingはSHA-256によるintegrity referenceであり、暗号学的なsignatureやattestationではない。
最終identity確認後の非協調なsyscall raceは、観測できる場合でも保証範囲に含めない。

custom/`nolock` VFS、network/distributed filesystem、実行したfsync contractを超える
power lossは対象外である。providerの実行・status照会、自動retry、backend fallback、
schema migrationもversion 1には含めない。

上記のcurrent-generationにおける`candidate_digest`のexact検証は、apply、replacement、
final result、resumeで引き続き必須である。ただしstableなnormal-openの保証は拡張しない。
committed historyの`candidate_digest`だけを書き換えるケースはversion 1の認証対象外であり、
累積tombstone fenceも変えない。

この変更でDDL、`STORE_SCHEMA`、`EVENT_SCHEMA_VERSION`、SQLite `user_version`は変えない。
9-fieldの`recovery.ledger` version 1と、13-fieldの`recovery.tombstones` version 1もwire
互換性を保つ。stable bindingは既存のrestore eventの`evidence_ref` fieldに保存する。
