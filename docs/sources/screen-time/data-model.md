# データモデル

## Local Raw observation

RawはSQLite `collector.db`のgzip BLOBとして保存する。Collectorはv2を保存し、Loaderはv1/v2を読む。
下記keyと保存metadataのgenerationを取込台帳との照合に使う。

```text
raw/screen_time/v<1または2>/<device_key>/<app-in-focusまたはapp-usage>/<segment_key>/
  <observed_at>/<sha256>.segb.gz
```

| 要素 | 契約 |
|---|---|
| `device_key` | device identifierを疑似化した64文字のlowercase hex |
| `app-in-focus` / `app-usage` | iPhone / Macのstream識別子。platformはそれぞれ`ios` / `macos` |
| `segment_key` | deviceとsegment相対pathを疑似化した64文字のlowercase hex |
| `observed_at` | 取得完了UTC時刻。`YYYYMMDDTHHMMSSffffffZ` |
| `sha256` | gzip前のRaw本文に対するlowercase SHA-256 hex。v1はSEGB、v2はenvelope全体 |

v1のgzip本文は元SEGB bytesそのものである。v2のgzip本文は次のbinary envelopeとする。

```text
6 bytes: ASCII PDPST + 0x02
4 bytes: big-endian unsigned JSON metadata length
JSON: {"segment_kind":"events"または"tombstones","source_segment_name":"数値のファイル名"}
残り: 元SEGB bytes（変更なし）
```

JSONはUTF-8・key順・空白なし、最大1024 bytesで、端末identifier・絶対path・親directory名を含めない。
元SEGBはbyte-for-byteで復元できる。v1/v2は同じ疑似化keyを使う。

## 疑似化key

HMAC-SHA-256のkeyには[疑似化secret](acquisition.md#認証と疑似化secret)を使う。
`||`はbytesの連結、`\0`は1 byteのNUL、文字列はUTF-8を表す。

```text
device_key = HMAC-SHA256(
  secret,
  "screen-time/device/v1\0" || UTF8(device_identifier)
)

segment_key = HMAC-SHA256(
  secret,
  "screen-time/segment/v1\0"
  || UTF8(device_identifier)
  || "\0" || UTF8(source_stream_name) || "\0"
  || UTF8(segment_relative_posix_path)
)
```

`source_stream_name`はiPhoneで`App.InFocus`、Macで`ScreenTime.AppUsage`。
`segment_relative_posix_path`は各streamのdevice directoryからの相対POSIX pathである。

## Observation semantics

`device_key + stream + segment_key`をlogical scopeとする。直前の保存済み観測とSHA-256が同じ場合だけskipし、`A→B→A`は3回の観測として取り込む。
最新segmentも検査済みbytesを観測版にする。予定keyと決定的gzipをSQLiteへ先にcommitし、MotherDuck台帳の
成功確認後だけ`uploaded`へ進める。各logical scopeの最新成功版と全pendingを残し、古い成功版を整理する。
元ファイルがBiomeから消えても最新保存版は無期限に残す。更新前Rawの再解析やRawだけからの全履歴復元は保証しない。

## Collector scan

complete scanごとに、SQLiteの`collector_stream_scan`へstream別の最終scan時刻、有効なdevice集合、deviceごとの最終scan時刻を保存する。
有効なdevice集合が空なら明示的な休止状態とする。集合から外したdeviceの更新は要求せず、保存済みRawは残す。
streamと有効なdeviceのscan時刻を48時間の鮮度で監査し、片方のstreamで他方の稼働を証明しない。
MotherDuckの既存stream別heartbeatは、全streamの取込成功・pending解消・ローカル監査成功後にMacが更新する。
Cloud Runはその成功時刻と`scan_completed_at`が48時間以内であることを確認し、自身では更新しない。

## `base.screen_time_event`

分析入口tableで、`event_key`がprimary key。同じイベントは1行とし、無効化は`is_active=false`へ更新する。
列は後述のtransitionに`is_active`と`loaded_at`を加えたもので、`original_payload`や観測ごとの本文を保存しない。
アプリversion・build・遷移理由・platformフラグと元ファイル内の位置は、分析行へ保存しない。
現行の物理recordの診断・選択に必要な情報は`ops.screen_time_record`に保持する。
分析項目・parser version・物理コピー数・有効状態が変わった場合だけ更新し、provenanceは採用した代表recordを指す。
`observed_at`は全再観測の最新日時ではない。現在保存しているRawの取込時刻・件数は`ops.ingestion_metadata`で確認する。

`ops.screen_time_segment`は`(observed_at, object_key)`順の最新snapshotを保持する。古いRawやその再解析で現行状態を巻き戻さない。
`ops.screen_time_record`は現行かつ有効な物理recordだけを保持する。新しいsnapshotとの差分で追加・訂正・削除し、
元payloadや非現行recordの照合情報は保存しない。代表recordの順位と物理コピー数は現行recordから求める。

Appleの削除済みの印は照合しない。代表イベントの再計算はrecordや順位が変わる`event_key`に限定し、
別segmentの現行コピーも比較する。分析用の非現行行は`base.screen_time_event`に残す。

## Recordの取り込み

`ops.ingestion_metadata`にはparser versionと全record数（削除済み・CRC不一致・tombstoneを含む）を保存する。
parser versionはiPhone `app-in-focus-v2`、Mac `app-usage-v1`。削除済み・CRC不一致の位置は現行候補から外す。
CRC正常の未知payloadや壊れたSEGB構造はobject全体を失敗させ、保存中の最新版Rawは再解析できる。

削除済みの印を含むRawも取込成否とrecord数は記録するが、印の照合状態はMotherDuckに作らない。
現行Rawの整理を確認したscopeでは、旧版の取込成功台帳も削除する。

## `event_key`

decode済みtransitionの同一性を表し、次のcanonical bytesのSHA-256とする。

```text
"screen-time/event/v1\0"
|| uint32be(length(device_key)) || UTF8(device_key)
|| uint32be(length(stream)) || UTF8(stream)
|| uint32be(length(bundle_id)) || UTF8(bundle_id)
|| IEEE-754 binary64 big-endian(cf_absolute_time)
|| uint32be(in_foreground)
|| uint32be(kind)
```

`kind`がpayloadにない場合は`0xffffffff`をsentinelにする。segmentやrecord offsetはprovenanceであり、同じ
eventが別segmentに現れるため`event_key`へ含めない。

## `base.screen_time_transition`

`base.screen_time_event`の`is_active=true`だけを公開するdbt Viewで、interval・日別集計の入力となる。
取り込み側が次の選択結果をevent tableへ保存する。

1. 通常の候補はlogical segmentの最新観測から選び、同じrecord offsetの最後のmetadataを現在stateとする。
2. `event`かつ`WRITTEN`かつCRC不一致でないrecordだけを候補にする。
3. 同一物理イベントを重複計上せず、最後に同じ`event_key`を1件にまとめる。

元ファイルの更新で消えたイベントは`is_active=false`にして集計から外し、分析行を残す。
不要になった物理recordは同じ取込transactionで削除する。取得前に消えたpayloadは復元できない。

```text
event_key / device_key / platform / source_stream
bundle_id / event_at / state
kind / object_key / segment_key / observed_at
parser_version / unknown_field_count / duplicate_occurrence_count
```

## `base.screen_time_interval`

transitionを`device_key + source_stream`内でevent時刻順に評価するdbt Viewである。
同時刻ならendをstartより先に扱い、次のstartと同時刻の観測済みendを区間の終点に使う。

| 入力 | `quality` |
|---|---|
| startの後に同じappのend | `complete` |
| startの後、対応endより先に次のapp start | `inferred_end_from_next_start` |
| 対応endがないstart | `missing_end` |
| 対応startとして使われないend | `missing_start` |

負のdurationは生成せずdbt testで拒否する。重複occurrenceの存在は`has_duplicate_source`で別に保持し、
pairing品質と混同しない。

```text
interval_key / device_key / platform / bundle_id
started_at / ended_at / duration_seconds
source_stream / quality / has_duplicate_source
start_event_key / end_event_key
```

## `marts.daily_screen_time`

有効なintervalをAsia/Tokyoの日境界で分割し、`activity_date / device_key / platform / bundle_id`ごとに
1行を返すdbt Viewである。

```text
activity_date / device_key / platform / bundle_id
complete_seconds / inferred_seconds / total_seconds
complete_interval_parts / inferred_interval_parts
```

表示名は解決せず、Bundle IDを公開値とする。

## `marts.daily_screen_time_total`

アプリ別Viewの`bundle_id`以外の列を持ち、`activity_date / device_key / platform`ごとに合計した1行を返す。
異なる端末の利用時間は合算しない。

両日次Viewの指標は次の意味を持つ。

| column | 意味 |
|---|---|
| `activity_date` | Asia/Tokyoの暦日。午前0時を日境界とする |
| `device_key` / `platform` | 利用した端末の疑似化keyとplatform |
| `complete_seconds` | 対応するstartとendがある区間の利用秒数 |
| `inferred_seconds` | 次のapp startを終了時刻とした区間の利用秒数 |
| `total_seconds` | 確定分と推定分を含む合計秒数 |
| `complete_interval_parts` | 当日に属する確定区間の分割片数 |
| `inferred_interval_parts` | 当日に属する推定区間の分割片数 |

小数秒を保持し、日境界をまたぐ区間は秒数・分割片数を各日に分配する。分割片数はアプリ起動回数ではない。
終了が午前0時なら翌日のゼロ秒片を作らず、`missing_start`・`missing_end`・ゼロ秒区間を加算しない。
対象がない日・端末・アプリは行を作らないため、不在だけでは利用ゼロと記録不足を区別できない。当日は途中経過である。
両Viewは訂正・無効化を即時反映し、dbt実行は初回作成・定義変更時に必要となる。Reconciliationは存在と参照可否を確認する。

クエリ例は[日次Screen Timeの参照](../../analytics/queries.md#日次screen-timeの参照)を参照する。
