# Analytics契約

## Loader

LoaderはB2 Rawを型付きMotherDuck baseへ変換する。

```text
B2 object
  -> gzip展開
  -> 展開後bytesのSHA-256検証
  -> ccl-segb decode
  -> App.InFocus protobuf decode
  -> MotherDuck transaction
```

同じobject keyの`ops.ingestion_metadata.status`が`succeeded`ならdownloadを省略する。未処理または
`failed`のobjectだけを`(observed_at, object_key)`順に再試行する。1 object内のrecord、segment
observation、`succeeded`更新は同じtransactionでcommitする。1 recordでもdecodeできなければobject全体を
rollbackし、`failed`とerror種別を別transactionで保存する。

Loaderはsource横断JOIN、interval生成、日次集計を行わない。これらはdbt Viewで行う。

## Ops schema

### `ops.ingestion_metadata`

Raw objectごとの最新取込状態を保持する。

| column | 契約 |
|---|---|
| `object_key` | B2 object key。primary key |
| `device_key` / `source_stream` / `segment_key` | object keyから復元したscope |
| `observed_at` | UTC観測時刻 |
| `content_sha256` | 展開後Raw bytesのSHA-256 |
| `byte_size` | 展開後bytes数 |
| `status` | `loading` / `succeeded` / `failed` |
| `parser_version` / `record_count` | 成功時のdecoderとrecord数 |
| `started_at` / `completed_at` | 最新試行のUTC時刻 |
| `error_type` / `error_message` | 最新失敗。成功時はnull |
| `retry_count` | 同じobject keyの再試行回数 |

### `ops.schema_migration`

forward-only migrationの`migration_id`、ファイルSHA-256、`applied_at`を保持する。一度適用した
migrationのchecksumが変わっていた場合は停止し、既存migrationを書き換えない。

### `ops.job_lock`

LoaderとReconciliationの多重実行を防ぐ期限付きleaseである。`job_name`をprimary keyとし、`owner_id`と
`expires_at`を保持する。未期限切れleaseを持つ別ownerがいる場合は処理を開始しない。正常終了・失敗時は
自分のleaseだけを解放し、異常終了時は期限切れ後に次の実行が引き継ぐ。

### 実行記録

`ops.job_run`、`ops.reconciliation_run`、`ops.heartbeat`にJobの結果を記録する。外部heartbeat送信前の
途中状態を成功として記録しない。

## dbt

`base.screen_time_transition`、`base.screen_time_interval`、`marts.daily_screen_time`はdbt Viewである。
base dataの更新時には再materializeせず、query時点の最新baseを参照する。modelまたはschema定義をdeployした
場合だけ`dbt run`に続けて`dbt test`を実行する。

モデルの列、重複排除、interval品質、Asia/Tokyoの日境界は
[`Screen Timeデータモデル`](../sources/screen-time/data-model.md)を正本とする。

## ChatGPT

独自MCP serverは作らず、MotherDuck Remote MCPをOAuthで接続する。ChatGPT側ではread-onlyの`query`だけを
許可し、`query_rw`を無効にする。MotherDuckのdata accessはdatabase/share単位でも効くため、tool名の制限
だけをdata visibilityの境界とみなしてはならない。

接続手順、data scope、拒否テストは[`chatgpt-mcp.md`](chatgpt-mcp.md)に従う。
