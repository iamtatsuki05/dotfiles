---
name: database-dev
description: "Use when the user asks to design or modify database schemas, queries, indexes, migrations, transactions, EXPLAIN plans, N+1 issues, SQL/NoSQL modeling, or database performance. Read once per session. Do not use for application code that only calls an ORM without changing schema or queries, or for ad-hoc analysis SQL in notebooks."
---

# データベース開発スキル

スキーマ設計、クエリ最適化、migration、性能改善を、対象エンジンのバージョンと環境の制約に合わせて進めるための手順と判断基準。

この skill は session につき 1 回だけ読む。compaction 後は `checkpoint.md` の「適用中の skill と解決済みの実行形」を見て続け、SKILL.md を読み直さない。`$database-dev` で本文が注入済みの場合も再読しない。

## 着手前の確認

次の順に確認し、決めた内容を `checkpoint.md` に 1〜2 行で残す。repo が無く相談だけの依頼では設定ファイルを探さず、前提にしたバージョンや設定を回答に明記する。

1. エンジンとバージョン: PostgreSQL / MySQL / SQLite / MongoDB。`SELECT version()`(SQLite は `sqlite_version()`、MongoDB は `db.version()`)か接続設定、Docker Compose、CI の設定から特定する。バージョンで挙動が変わる項目(後述の migration、`pg_stat_statements` の列名)はここで確定する。
2. ORM と migration ツール: SQLAlchemy / Prisma / TypeORM / ActiveRecord、Alembic / Flyway / Knex / Prisma Migrate など。migration の生成・適用・rollback の実行形(例: `alembic upgrade head` / `alembic downgrade -1`)を既存の migration ファイルと `Makefile` / scripts から決める。
3. 既存スキーマ: スキーマ定義ファイル、直近の migration、対象テーブルの行数と主要な index。
4. 対象環境と制約: local / dev / staging / production、データ量、許容 lock 時間、バックアップと rollback の方針。

本番または共有環境に影響する migration、不可逆変更、長時間 lock、データ削除・大量更新は、実行前に対象、影響、戻し方を示してユーザー承認を取る。承認前に実行しない。

## 進め方

1. 性能問題は、まず本番相当のデータ量で `EXPLAIN (ANALYZE, BUFFERS)`(MySQL 8.0.18+ は `EXPLAIN ANALYZE`)を取り、推測でなく実測から原因を絞る。`ANALYZE` は文を実際に実行するので、UPDATE / DELETE は `BEGIN; ... ROLLBACK;` で包み、本番接続では承認済みの場合だけ取る。
2. スキーマ変更は migration ファイルとして書き、up と down(または戻し手順)を対にする。1 migration は 1 目的に絞り、複数テーブル横断の変更は段階に分ける。
3. 変更に対応するテストを先に用意する: migration の適用と rollback をテスト DB で通す、クエリの結果と `EXPLAIN` の scan 種別を検証する、ORM なら N+1 が消えたことを発行クエリ数で確認する。
4. 互換のための alias 列、二重書き込み、default 値補完は、expand-contract の計画と削除予定がある場合だけ追加する。実際に書きそうになった時点でだけ `compatibility-safety` を読む。

## 規約と判断基準

プロジェクトに規約や既存パターンがあればそれに従う。無い場合の既定:

スキーマ:

- 3NF までを目標にし、性能要件による非正規化は理由を migration か ADR に残す。各正規形の変換例、階層データや時系列データのモデリングは [references/normalization.md](references/normalization.md) を見る。
- 制約(NOT NULL、UNIQUE、FK、CHECK)はアプリ層任せにせず DB 層で守る。FK の `ON DELETE` は業務上の親子関係で選び、無条件に CASCADE にしない。多対多は中間テーブル + 複合主キー。
- `updated_at` の自動更新は ORM / アプリ層かトリガーで行う。PostgreSQL のトリガー実装は [references/engine-specific.md](references/engine-specific.md)。

| 用途 | PostgreSQL | MySQL | 注意点 |
|------|------------|-------|--------|
| 主キー | UUID / BIGSERIAL | BINARY(16) / BIGINT AUTO_INCREMENT | UUID は分散環境向き |
| 日時 | TIMESTAMPTZ | DATETIME(6) | MySQL の DATETIME は tz を持たないので UTC で保存しアプリ側で変換する |
| 金額 | DECIMAL(p,s) | DECIMAL(p,s) | 浮動小数点は使わない |
| JSON | JSONB | JSON | PostgreSQL は JSONB |
| 列挙 | VARCHAR + CHECK | ENUM | ENUM は変更が難しい |

index:

- 対象は WHERE、JOIN 条件、ORDER BY / GROUP BY の列。複合 index は左端からしか使われないので、等価条件を左、範囲条件を右に置く。
- 特定条件の行だけ検索するなら部分 index、SELECT 列まで含めるならカバリング(`INCLUDE`)。書き込み頻度とのトレードオフを見る。
- 列に関数を適用した条件(`LOWER(email) = ...`)には B-tree が効かない。関数 index を作るか、正規化して保存する。`LIKE '%語%'` の中間一致は `CREATE EXTENSION pg_trgm` + `USING gin(col gin_trgm_ops)`。
- GIN / GiST / BRIN の使い分けと実例は [references/query-optimization.md](references/query-optimization.md) の「インデックス詳細」。

クエリと `EXPLAIN`:

- 大きなテーブルの Seq Scan、内側が大きい Nested Loop、ディスクに落ちる Sort、推定行数と実行行数の乖離(乖離が大きければ `ANALYZE` で統計を更新)を見る。ノード別の読み方、JOIN・サブクエリ・ページネーションの最適化は同ファイル。
- N+1 はループ内クエリの発火が典型。JOIN か集計サブクエリで 1 クエリにまとめ、ORM では eager loading を設定する。

トランザクション:

- 既定の分離レベルは PostgreSQL が READ COMMITTED、MySQL(InnoDB)が REPEATABLE READ。REPEATABLE READ での phantom read は、MySQL では locking read や更新後の再読で起こり得るが、PostgreSQL では起きない。エンジンをまたぐ移植ではここを確認する。
- 複数行・複数テーブルを lock する処理は常に同じ順序で取得し、`lock_timeout`(PostgreSQL。MySQL は `innodb_lock_wait_timeout`)を設定する。トランザクションは短く保ち、外部 API 呼び出しを中に含めない。`SKIP LOCKED` によるキュー処理は同ファイルの「ロック最適化」。

migration の安全性(SQL の実例は [references/migrations.md](references/migrations.md)):

- 列追加(default なし)は即時に完了する。default ありは PostgreSQL 11+ の非 volatile な default に限り即時で、`gen_random_uuid()` などの volatile な default や古いバージョンでは全行書き換えになる。MySQL はバージョンと `ALGORITHM` 指定で挙動が変わるので対象バージョンの docs を確認する。
- 大きなテーブルへの index 追加は PostgreSQL では `CREATE INDEX CONCURRENTLY`(トランザクション外で実行)、MySQL では `ALGORITHM=INPLACE, LOCK=NONE` を指定して書き込み lock を避ける。
- 列削除はアプリの参照削除 → NOT NULL 解除 → 期間を置いて削除の順。列名変更・型変更は直接の `RENAME` / `ALTER TYPE` を避け、expand-contract(新列追加 → コピーと二重書き込み → 参照切替 → 旧列削除)で進める。

NoSQL(MongoDB):

- 1 対少で親と常に一緒に読み書きするデータは埋め込み、独立してアクセス・更新するデータは参照。index は RDB と同様に複合(左端から)、部分、ユニーク、TTL、テキストを使い分ける。実例は [references/engine-specific.md](references/engine-specific.md) の MongoDB 節。

監視:

- PostgreSQL は `pg_stat_statements`(13+ は `mean_exec_time` / `total_exec_time`、12 以前は `mean_time` / `total_time`)でスロークエリ、`pg_stat_user_tables` で Seq Scan の多いテーブル、`pg_stat_user_indexes` で未使用 index を見る。MySQL はスロークエリログと `performance_schema`。監視 SQL は同ファイルの各エンジン節。

## 完了前の確認

- テスト DB で migration の適用と rollback を通した(実行形は着手前に決めたもの)。
- 変更したクエリの `EXPLAIN (ANALYZE, BUFFERS)` を本番相当のデータ量で(書き込み系は `ROLLBACK` で包んで)取り、意図した scan 種別と index が使われている。
- スキーマに必要な制約と index がある。N+1 が発行クエリ数で消えている。
- 本番・共有環境に触れる操作は承認済みで、dry-run か staging での結果がある。実行できない検証は、理由と残るリスクを報告に回す。

## 報告に含めること

- 変更したテーブル・index・クエリと目的。対象エンジンとバージョン。
- 互換性への影響、想定 lock 時間、データ量、rollback 手順、段階的に適用する場合はその順序。
- 実行した検証(migration 適用 / rollback、`EXPLAIN` の要約、テスト件数)と未実行の検証。
- 承認が必要な操作とその状態。
- PR description を書く段階になったら `eng-practices` を読む。それ以外の場面では読まない。
