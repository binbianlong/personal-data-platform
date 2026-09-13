# データモデル

## GCS Raw observation

Rawの保存単位はSEGB segmentの観測版である。既存v1を読み取り、新規Collectorはv2を保存する。

```text
raw/screen_time/v<1または2>/<device_key>/app-in-focus/<segment_key>/
  <observed_at>/<sha256>.segb.gz
```

| 要素 | 契約 |
|---|---|
| `device_key` | device identifierを疑似化した64文字のlowercase hex |
| `app-in-focus` | 初期coreで固定するstream識別子 |
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

JSONはUTF-8・key順・空白なしの決定的表現とし、最大1024 bytes。端末identifier・絶対path・親directory名は
追加しない。元SEGBをbyte-for-byteで復元できる。`device_key`と`segment_key`はv1と同じ計算式のため、
v2で得た元ファイル名を同一logical segmentのv1観測にも対応付けられる。control JSONはv1の既存keyを継続する。

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

同じ`device_key + app-in-focus + segment_key`をlogical scopeとする。直前にGCS保存を完了した観測と
SHA-256が同じ場合だけ新規保存をskipする。転送対象の`A -> B -> A`は3観測として保存する。
最新segmentの転送待ち中には観測版を作成しない。後続ファイルを確認した時点のbytesを保存対象にする。

`observed_at`を含む予定object keyとdeterministic gzip bytesをSQLiteへ先にcommitし、GCS upload成功後だけ
`uploaded`へ進める。再起動後のretryでも同じobject keyとgzip bytesを使う。

## Collector scan receipt

complete scanが成功したdeviceごとに、次のmutable control objectを更新する。

```text
raw/screen_time/v1/_control/collector/latest/<device_key>.json
```

本文は`schema_version`、`device_key`、UTCの`completed_at`、`segment_count`、`status=succeeded`だけを持つ。
RawのSystem of Recordではなく稼働確認用であり、端末identifier、path、Bundle IDは含めない。`segment_count`は
最新ファイルとして転送待ちの分も含む発見総数である。走査が正常に完了し、pendingと完成扱いの転送対象の
Raw uploadがすべて成功した後だけ更新する。完成待ちだけの場合も更新する。

今回発見できたallowlist対象deviceのreceiptを更新した後、次のmutable manifestを最後に更新する。

```text
raw/screen_time/v1/_control/collector/active.json
```

本文は`schema_version`、sort済みの`device_keys`、UTCの`completed_at`、`status=succeeded`だけを持つ。
Reconciliationはこのmanifestを最新のactive-device集合の正本として扱う。allowlistから外したdeviceのRawは
90日Lifecycleまで残り得るが、そのdeviceのreceipt更新は要求しない。

## `base.screen_time_segment_observation`

GCS objectごとに1行を保持する。

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
source_segment_name       v2で既知。v1はnull
segment_kind              events / tombstones。v1はnull
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
record_kind               event / deleted / crc_failure / tombstone
payload_length / record_timestamp_cocoa
target_segment_name / target_offset / target_length / target_event_timestamp / deletion_reason
```

`event_key`、前面状態、event時刻、Bundle IDは非イベント行ではnullを許容する。`record_count`はイベントだけ
でなく保存した全occurrence数。parser versionは`app-in-focus-v2`とする。

削除済みレコードは本文がゼロ埋めでもstate・offset・元bytesを保存する。CRC不一致は`crc_failure`として
元bytesを保存し、イベントを生成しない。CRC正常の未知payload形式や壊れたSEGB構造はobject全体を失敗させ、
部分的な成功として扱わない。元Rawが残る90日間は再解析できる。

### Tombstone

macOSの`BMTombstoneEvent`による合成データのdecode結果と実機の構造を照合した形式:

| protobuf field | 型 | 内容 |
|---|---|---|
| 1 | string | 対象segment名 |
| 2 | uint32 | 対象recordのmetadata offset |
| 3 | uint32 | 対象payload長 |
| 4 | uint32 | 削除理由: 1=TTL、2=UserInitiated |
| 5 | string | processName |
| 6 | double | 対象event timestamp（Cocoa秒） |
| 7 | string、省略可 | policyID |

private frameworkは形式検証だけに使い、本番パーサーはPythonで実装する。未知の削除理由は保持するが自動適用しない。
`base.screen_time_tombstone_match`で端末・stream・元segment名・metadata offset・payload長・元record時刻を照合する。
時刻の許容差は旧datetime列のmicrosecond丸め分の1 microsecondだけ。元ファイル名の対応が曖昧なsegmentは適用しない。

`base.screen_time_tombstone_status`は`user_deletion_applied`、`ttl_history_retained`、`unmatched`、
`unsupported_reason`と一致イベント数を公開する。`unmatched`には未到着・保存期間外・v1の元ファイル名不明も
含まれ、削除適用済みとは扱わない。後から対応する観測が届けばView上で再評価する。

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

1. 通常の候補はlogical segmentの最新観測から選び、同じrecord offsetの最後のmetadataを現在stateとする。
2. `event`かつ`WRITTEN`かつCRC不一致でないrecordだけを候補にする。
3. TTL tombstoneに完全照合できた過去観測の正常イベントを候補へ戻す。Apple側の期限切れだけでは既取得の履歴を消さない。
4. UserInitiated tombstoneに完全照合できた`event_key`は、別segmentの重複コピーも含めて候補から除外する。
5. 同一物理イベントの過去観測を重複計上せず、最後に同じ`event_key`を1件にまとめる。

これは集計からの除外であり、GCS Rawやbaseの証跡を物理削除する処理ではない。取得前に消えたpayloadは復元できない。

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
