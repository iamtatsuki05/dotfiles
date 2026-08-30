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
  変更しない。これらはrecovery-required markerとして保持し、`UNKNOWN_EFFECT`への遷移は
  trustedな`RecoveryStoreTx.mark_prepared_unknown`による明示操作だけに限定する
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
stable writer marker と recovery ledger の basename は、後続の marker/ledger
実装が所有するため、呼び出し側が明示的に渡す。doctor がファイル名を暗黙に
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
許可した `RESTORE_ABORTED` edge の順に進み、terminal の後は次の世代の
`RESTORE_PREPARED` だけを許可する。primary DB、SQLite の exact sidecar、marker、
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
cleanup、writer marker lifecycle は実装せず、#55/#56 の contract に残す。

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
保持するため、後続の restore 実装が marker/lifetime lock を複製しない。
`copy_database_to` は exact な checkpoint request を必須とし、controller 管理の
SQLite backup call を memory に一度だけ実行する。その serialized image を、
`O_CREAT|O_EXCL|O_NOFOLLOW` と mode `0600` で新規作成して held descriptor に書き込む。
source/target identity、target の exact sidecar、最後の target bytes を検証し、identity と
target の `sha256:` digest を返す。#56 consumer は basename を no-follow で open し、消費直前に
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
