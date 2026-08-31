# データモデル

## B2 Raw observation

Rawの保存単位はSEGB segmentの観測版である。object keyを次に固定する。

```text
raw/screen_time/v1/<device_key>/app-in-focus/<segment_key>/
  <observed_at>/<sha256>.segb.gz
```

| 要素 | 契約 |
|---|---|
| `device_key` | device identifierを疑似化した64文字のlowercase hex |
| `app-in-focus` | 初期coreで固定するstream識別子 |
| `segment_key` | deviceとsegment相対pathを疑似化した64文字のlowercase hex |
| `observed_at` | 取得完了UTC時刻。`YYYYMMDDTHHMMSSffffffZ` |
| `sha256` | gzip前のSEGB segment bytesに対するlowercase SHA-256 hex |

gzip本文を展開すると、観測時点のSEGB segment bytesとbyte-for-byteで一致しなければならない。

## 疑似化key

疑似化secretはmacOS Keychainから読み、HMAC-SHA-256のkeyとして使う。domain separatorと入力を次に
固定する。`||`はbytesの連結、`\0`は1 byteのNUL、文字列はUTF-8を表す。

```text
device_key = HMAC-SHA256(
  secret,
  "screen-time/device/v1\0" || UTF8(device_identifier)
)

segment_key = HMAC-SHA256(
  secret,
  "screen-time/segment/v1\0"
  || UTF8(device_identifier)
  || "\0App.InFocus\0"
  || UTF8(segment_relative_posix_path)
)
```

`segment_relative_posix_path`は`remote/<device_identifier>/`からの相対POSIX pathである。疑似化secretの
変更は過去との同一性を失うため、通常のcredential rotationでは行わない。

## Observation semantics

同じ`device_key + app-in-focus + segment_key`をlogical scopeとする。直前にB2保存を完了した観測と
SHA-256が同じ場合だけ新規保存をskipする。`A -> B -> A`は3観測として保存する。

`observed_at`を含む予定object keyとdeterministic gzip bytesをSQLiteへ先にcommitし、B2 upload成功後だけ
`uploaded`へ進める。再起動後のretryでも同じobject keyとgzip bytesを使う。

## Collector scan receipt

complete scanが成功したdeviceごとに、次のmutable control objectを更新する。

```text
raw/screen_time/v1/_control/collector/latest/<device_key>.json
```

本文は`schema_version`、`device_key`、UTCの`completed_at`、`segment_count`、`status=succeeded`だけを持つ。
RawのSystem of Recordではなく稼働確認用であり、端末identifier、path、Bundle IDは含めない。全segmentの
Raw uploadが成功した後だけ更新する。

## `base.screen_time_segment_observation`

B2 objectごとに1行を保持する。

```text
object_key                 primary key
device_key
source_stream              "app-in-focus"
segment_key
observed_at                UTC
content_sha256
byte_size                  gzip展開後
record_count
parser_version
loaded_at                  UTC
```

## `base.screen_time_record_occurrence`

成功decodeしたsegment observation内の各SEGB recordを、観測版ごとのoccurrenceとして保持する。

```text
object_key + record_metadata_offset primary key
event_key
device_key
source_stream
segment_key / segment_sha256 / observed_at
segment_filename / record_offset / record_metadata_offset
record_state / segment_record_timestamp / crc_passed
transition_reason / kind / in_foreground
cf_absolute_time / event_at / bundle_id
app_version / app_build / platform_flag
unknown_field_count
original_payload           protobuf bytes
parser_version / loaded_at
```

SEGBまたは既知fieldを安全にdecodeできないobjectはtransaction全体をrollbackして
`ops.ingestion_metadata.status=failed`にする。CRC failureはoccurrenceへ記録し、成功decodeしたobject内でも
dbt Viewのtransition候補から除外する。元segment bytesはB2に残るため、decoder更新後に再試行できる。

## `event_key`

decode済みtransitionの同一性を表す。次のcanonical bytesのSHA-256とする。

```text
"screen-time/event/v1\0"
|| uint32be(length(device_key)) || UTF8(device_key)
|| uint32be(length("app-in-focus")) || UTF8("app-in-focus")
|| uint32be(length(bundle_id)) || UTF8(bundle_id)
|| IEEE-754 binary64 big-endian(cf_absolute_time)
|| uint32be(in_foreground)
|| uint32be(kind)
```

`kind`がpayloadにない場合は`0xffffffff`をsentinelにする。segmentやrecord offsetはprovenanceであり、同じ
eventが別segmentに現れるため`event_key`へ含めない。

## `base.screen_time_transition`

現在の各segmentとlogical eventを選ぶdbt Viewである。

1. `device_key + source_stream + segment_key`ごとに最新の`(observed_at, object_key)`を選ぶ。
2. 同じ`object_key + record_offset`では最大の`record_metadata_offset`を現在stateとして選ぶ。
3. 現在stateが`WRITTEN`かつCRC failureでないrecordを選び、後続`DELETED`があるrecordを除外する。
4. 同じ`event_key`を1件へまとめ、除外したoccurrence数を`duplicate_occurrence_count`に保持する。

```text
event_key / device_key / platform / source_stream
bundle_id / event_at / state
transition_reason / kind / app_version / app_build / platform_flag
object_key / segment_key / segment_filename / record_offset / observed_at
parser_version / unknown_field_count / duplicate_occurrence_count
```

## `base.screen_time_interval`

transitionを`device_key + source_stream`内で`(event_at, event_key)`順に評価するdbt Viewである。

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

有効なintervalをAsia/Tokyoの日境界で分割し、日・device・Bundle ID単位に集計するdbt Viewである。

```text
activity_date / device_key / platform / bundle_id
complete_seconds / inferred_seconds / total_seconds
complete_interval_parts / inferred_interval_parts
```

`complete_seconds`と`inferred_seconds`は分離し、欠損qualityは加算しない。表示用アプリ名は初期coreでは
解決せず、Bundle IDを公開値とする。
