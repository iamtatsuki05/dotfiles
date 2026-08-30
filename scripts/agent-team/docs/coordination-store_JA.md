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
| cleanup | connection close、WAL checkpoint、WAL/SHM確認 | known tempの分類、file/directory fsync、backup管理 |
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
`O_RDONLY|O_NOFOLLOW|O_CLOEXEC|O_NONBLOCK` で開き、存在する lifetime gate を shared lock で保持して
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
