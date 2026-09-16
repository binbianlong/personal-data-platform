# Analytics契約

## Loader

Loaderは選択した`source_id / stream`のGCS Rawを型付きMotherDuck baseへ変換する。
source adapterが対応する全schema versionを処理し、source・stream・schema版とkey由来のidentityの不一致は
取込前に拒否する。

```text
GCS object
  -> gzip展開
  -> 展開後bytesのSHA-256検証
  -> source adapterによるdecode
  -> 型付きDecodedBatch
  -> MotherDuck transaction（batchの書込と共通取込状態）
```

同じRaw identityとGCS generationの`ops.ingestion_metadata.status`が`succeeded`ならdownloadを省略する。同じ
keyが別generationで再作成された場合は未検証objectとして再取得する。未処理または`failed`のobjectだけを
`(observed_at, object_key)`順に再試行する。sourceがparser versionを指定する場合、利用可能なRawの旧parser成功分も再解析する。1 object内の型付きrecordと`succeeded`更新は同じtransactionで
commitする。batchはparser version、record数、型付きtableへの書込を提供し、transactionの開始・commit・rollbackは
Warehouseだけが行う。decodeまたは書込に失敗したobjectの分析行は確定せず、`failed`とerror種別を別transactionで
保存する。

iPhone Screen Timeではccl-segbとApp.InFocus protobufをdecodeした後、`ScreenTimeBatch.write()`が
MotherDuck内で対象segmentの補助状態、関連する削除照合と代表イベントを更新する。
`base.screen_time_event`は`event_key`ごとに1行で、分析項目・有効状態が変わる場合だけ更新する。
補助状態には原文payloadを含めない。既存のoccurrenceは証跡として保持する。
移行検査後、dbtは`base.screen_time_event`の有効行だけを読み、旧occurrenceの判定を再実行しない。
`SourceAdapter`と`DecodedBatch`の契約、共有Loader leaseはsource間で共通である。

補助状態・イベント・Rawの取込成功はWarehouseの1 transactionで確定する。commit前の失敗はrollbackし、
commit後の再実行は既存の成功判定でskipする。commitの応答が失われた場合やrollbackが失敗した場合は、
その接続での後続処理と失敗記録の書込を止める。再接続後、永続化済みの成功記録から再処理の要否を判断する。
保存場所と復旧は[`Screen Time運用`](../sources/screen-time/operations.md#イベント単位の保存への切り替え)に従う。

Loaderはsource横断JOIN、interval生成、日次集計を行わない。これらはdbt Viewで行う。

## Ops schema

### `ops.ingestion_metadata`

Raw objectごとの最新取込状態を保持する。

| column | 契約 |
|---|---|
| `object_key` | GCS object key。primary key |
| `source_id` / `source_stream` | 取込・監査・再構築の実行scope |
| `schema_version` | Rawの形式version。parser versionとは区別する |
| `subject_key` / `logical_key` | sourceが定義する対象と観測単位 |
| `device_key` / `segment_key` | 既存Screen Time writerとの移行互換用。新sourceではnull |
| `observed_at` | UTC観測時刻 |
| `storage_created_at` | GCS upload完了時刻。Lifecycle期限判定の正本 |
| `storage_generation` | listingとdownloadを結び付けるGCS object generation |
| `retention_expired_at` | sourceの保持期限以降のLifecycle削除をReconciliationが確認した時刻 |
| `content_sha256` | 展開後Raw bytesのSHA-256 |
| `byte_size` | 展開後bytes数 |
| `status` | `loading` / `succeeded` / `failed` |
| `parser_version` / `record_count` | 成功時のdecoderとrecord数 |
| `started_at` / `completed_at` | 最新試行のUTC時刻 |
| `error_type` / `error_message` | 最新失敗。成功時はnull |
| `retry_count` | 同じobject keyの再試行回数 |

`storage_created_at`が不明な欠損objectは安全側に失敗させる。`retention_expired_at`を持つ成功行は長期分析履歴と
監査証跡として残すが、日次のlive Raw照合対象から外す。状態と件数の読取には`source_id`と`source_stream`を
必ず指定し、他scopeの`failed`や古い成功行を混ぜない。

`003_source_ingestion.sql`は既存tableへ列を追加し、過去行を`screen_time`、schema v1として扱う。
旧writerが移行後に追加した行は共通scope列がnullになり得るため、読取は`COALESCE(subject_key, device_key)`と
`COALESCE(logical_key, segment_key)`で互換性を保つ。Screen Time adapterの`legacy_scope`は新writerからも旧scope列を
埋める。旧列と旧writer向けdefaultは残し、新sourceを有効にする前のruntime更新順序は
[`operations.md`](operations.md#更新時の互換性)に従う。

### Screen Timeの補助状態

| table | 保存する状態 |
|---|---|
| `ops.screen_time_segment` | device・stream・segmentごとの最新観測順位とv2の名前照合情報 |
| `ops.screen_time_record` | 内容と物理位置で一意なrecord、削除照合座標、代表選択に必要な正規化項目、最新順位・有効状態 |
| `ops.screen_time_tombstone` | 内容と物理位置で一意なtombstone、削除対象・理由、最新順位・解決状態 |
| `ops.screen_time_deletion_match` | tombstoneと一致する物理recordの組 |

同じ内容の再観測では既存行の順位情報を更新する。新しい物理recordや削除先が増えればindexは増えるが、
再観測回数に比例して補助行を追加しない。Raw単位の`ops.ingestion_metadata`は引き続き増加する。
元payloadや観測の全履歴を補助tableへ複製せず、通常取り込みの計算対象も関連するsegment・削除照合・イベントに限定する。

### `ops.schema_migration`

forward-only migrationの`migration_id`、ファイルSHA-256、`applied_at`を保持する。一度適用した
migrationのchecksumが変わっていた場合は停止し、既存migrationを書き換えない。
SQLの正本はPython package内の`src/personal_data_platform/migrations/`に置き、wheelにも同梱する。

### `ops.job_lock`

LoaderとReconciliationの多重実行を防ぐ期限付きleaseである。`job_name`をprimary keyとし、`owner_id`と
`expires_at`を保持する。未期限切れleaseを持つ別ownerがいる場合は処理を開始しない。正常終了・失敗時は
自分のleaseだけを解放し、異常終了時は期限切れ後に次の実行が引き継ぐ。

lease名はsourceごとに分けず、既存の`loader`と`reconciliation`を継続する。異なるsourceの同じroleも同時には
走らない。旧runtimeとの互換期間に別名leaseで同じdatabaseへの並行writerを増やさないためで、source数を増やす
場合はJobの所要時間とscheduleの重なりを確認する。

### 実行記録

`ops.job_run`、`ops.reconciliation_run`、`ops.heartbeat`にJobの結果を記録し、detailsにsourceとstreamを含める。
Reconciliationのheartbeatはadapterのmonitor名とscopeごとの外部URLを使う。iPhoneの既存monitor名は
`screen_time_reconciliation`を保持する。Collector receiptの集計はsource固有detailsに置き、既存の結果参照用属性でも
同じ集計値を返す。Collectorの概念を持たないsourceにreceiptを要求しない。

Reconciliationは監査に成功したら
`running`の監査記録を先に保存する。次にtransaction内でwarehouse heartbeatを更新し、外部heartbeat送信が
成功した後で成功auditを記録してcommitする。送信や更新に失敗した場合はrollbackし、失敗auditを記録する。

外部HTTP送信とDB commitはatomicではない。送信後の最終commit失敗や送信応答の喪失では、外部に成功pingが
届いていてもDBに成功が確定しない場合がある。復旧時は`run_id`と実行log、監査記録を照合する。

## dbt

`pdp dbt`は指定なしでは全modelの`dbt run`と`dbt test`を実行する。`--source`と`--stream`を指定した場合は
adapterのselectorを両commandへ渡す。iPhoneは`tag:screen_time_app_in_focus`を使い、source固有のmodelと
そのtestを選択する。Rebuildも選択scopeのselectorを使い、未再生sourceのbaseを前提とするmodelは選択しない。

`base.screen_time_transition`、`base.screen_time_interval`、`marts.daily_screen_time`はdbt Viewである。
base dataの更新時には再materializeせず、query時点の最新baseを参照する。初回構築、model / schema定義の
変更、明示した再実行時に`dbt run`に続けて`dbt test`を実行する。deploy時の実行条件は
[`operations.md`](operations.md)に従う。

モデルの列、重複排除、interval品質、Asia/Tokyoの日境界は
[`Screen Timeデータモデル`](../sources/screen-time/data-model.md)を正本とする。

## ChatGPT

独自MCP serverは作らず、MotherDuck Remote MCPをOAuthで接続する。ChatGPT側ではread-onlyの`query`だけを
許可し、`query_rw`を無効にする。MotherDuckのdata accessはdatabase/share単位でも効くため、tool名の制限
だけをdata visibilityの境界とみなしてはならない。

接続手順、data scope、拒否テストは[`chatgpt-mcp.md`](chatgpt-mcp.md)に従う。
