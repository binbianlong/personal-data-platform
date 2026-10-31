# iPhone Screen Timeのデータモデル

## Local Raw observation

Rawは専用SQLite `collector.db`のgzip BLOBとして保存する。CollectorとLoaderは同じ保存形式を使う。
keyと保存metadataのgenerationを取込台帳との照合に使う。

```text
raw/iphone_screen_time/v2/<device_key>/app-in-focus/<segment_key>/<observed_at>/<sha256>.segb.gz
```

`observed_at`は取得完了UTC時刻。hashはgzip前のenvelope全体に対するSHA-256である。
Raw v2 envelopeはsegment種別と元SEGBを保持し、元bytesを復元できる。端末identifierや絶対pathは保存しない。
分割移行ではobject keyのsource部分だけを変更し、payload、hash、event key、疑似化のHMAC domainを維持する。

## 疑似化key

`device_key`はdevice identifier、`segment_key`はdevice・stream・segment相対pathをHMAC-SHA-256で疑似化する。
secretの扱いは[取得仕様](acquisition.md#認証と疑似化secret)を参照する。

## Observation semantics

`device_key + stream + segment_key`をlogical scopeとする。直前の保存済み観測とSHA-256が同じ場合だけskipし、`A→B→A`は3回の観測として取り込む。
Rawを先にSQLiteへcommitし、MotherDuck台帳の成功確認後だけ`uploaded`へ進める。この状態名はwarehouse取込済みを表す。
各scopeの最新成功版と全pendingを残し、古い成功版を整理する。元ファイルがBiomeから消えても最新保存版は無期限に残す。
更新前Rawの再解析やRawだけからの全履歴復元は保証しない。

## Collector scan

専用SQLiteの`collector_stream_scan`にstream・device別の最終scan時刻と有効なdevice集合を保存する。
集合が空なら明示的な休止状態とし、外したdeviceの更新は要求しない。他方のsourceのscan・休止状態は更新しない。
監視条件は[運用](operations.md#crash-recoveryと監視)を参照する。

## `base.iphone_screen_time_event`

同じイベントは1行とし、無効化は`is_active=false`で分析行を残す。
元ファイルの最新snapshotから有効な物理recordを選び、同じイベントの別segmentのコピーも照合する。
更新で消えたイベントを集計から外すが、取得前に消えたpayloadは復元できない。Appleの削除済みの印は照合しない。

`ops.iphone_screen_time_segment`は`(observed_at, object_key)`順の最新snapshot、`ops.iphone_screen_time_record`は現行の有効recordを保持する。
古いRawの再解析で現行状態を巻き戻さない。分析項目・parser version・物理コピー数・有効状態が変わった場合だけ分析行を更新するため、
`observed_at`は全再観測の最新日時ではない。現在保存しているRawの取込時刻・件数は`ops.ingestion_metadata`で確認する。

## Recordの取り込み

`ops.ingestion_metadata`のrecord数は削除済み・CRC不一致を含む。削除記録のRawは取込対象から外す。
削除済み・CRC不一致のrecordは現行候補から外し、不要になった物理recordはイベント更新と同じtransactionで削除する。
保存中の最新版Rawは再解析できるが、整理済みの旧版Rawと成功台帳は残さない。

## `event_key`

端末・stream・Bundle ID・元の時刻・開始/終了・kindから同一イベントを識別する。
同じイベントが別segmentに現れるため、segmentやrecord offsetはkeyに含めない。

## `base.iphone_screen_time_transition` / `base.iphone_screen_time_interval`

transitionは`base.iphone_screen_time_event`の`is_active=true`だけを公開する。
intervalはtransitionを`device_key + source_stream`内でevent時刻順に対応付ける。
同時刻ならendをstartより先に扱い、次のstartと同時刻の観測済みendを区間の終点に使う。

| 入力 | `quality` |
|---|---|
| startの後に同じappのend | `complete` |
| 対応endより先に次のapp start | `inferred_end_from_next_start` |
| 対応endがないstart | `missing_end` |
| 対応startとして使われないend | `missing_start` |

重複コピーの存在は`has_duplicate_source`で保持し、区間の対応付け品質と区別する。

## `marts.daily_iphone_screen_time` / `marts.daily_iphone_screen_time_total`

有効なintervalをAsia/Tokyoの日境界で分割し、日・端末・Bundle IDごとに集計する。
`complete_seconds`は対応endがある秒数、`inferred_seconds`は次のstartから終了を推定した秒数、`total_seconds`は両方の合計となる。
表示名は解決せず、Bundle IDを公開値とする。`_total`はアプリ別Viewを日・端末ごとに合計する。
異なる端末の利用時間は合算しない。

両日次Viewは小数秒を保持し、`*_interval_parts`は日境界で分割した片数であってアプリ起動回数ではない。
終了が午前0時なら翌日のゼロ秒片を作らず、`missing_start`・`missing_end`・ゼロ秒区間を加算しない。
対象がない日・端末・アプリは行を作らないため、不在だけでは利用ゼロと記録不足を区別できない。当日は途中経過である。
訂正・無効化は即時反映し、dbt実行は初回作成・定義変更時に必要となる。

クエリ例とFitbitとの横断分析は[分析クエリ](../../analytics/queries.md)を参照する。
