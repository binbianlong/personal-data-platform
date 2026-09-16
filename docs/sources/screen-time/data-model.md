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

## `base.screen_time_event`

移行済みの旧履歴と新規取り込みを含む、分析の唯一の入口となるtable。
`event_key`をprimary keyとし、同じイベントは1行だけ保持する。
列は後述の`base.screen_time_transition`に`is_active`と`loaded_at`を加えたもの。
`original_payload`や観測版ごとのrecord本文は保存しない。無効化も行を増やさず`is_active=false`へ更新する。

同じイベントの再観測だけでは更新しない。分析項目、parser version、物理コピー数、有効状態が変わる場合だけ
更新し、provenance列はその更新で採用した代表recordを指す。`observed_at`は全再観測の最新日時ではない。
Rawごとの取込時刻・件数は`ops.ingestion_metadata`で確認する。

`ops.screen_time_segment`は`(observed_at, object_key)`順の最新snapshotだけを保持する。
古いRawの遅着や再解析は最新snapshotを巻き戻さず、物理recordとtombstoneの照合情報を補完・修正する。
`ops.screen_time_record`は内容digestと物理位置で重複排除し、順位情報を同じ行で更新する。
代表選択に必要な正規化項目を持つが、元payloadは保存しない。同じ内容の再観測では補助行数が増えない。

TTLで必要な過去recordの照合情報と正規化項目はMotherDuckに残し、ユーザー削除は同じevent_keyを持つ
別segmentのコピーにも適用する。削除照合はdevice・stream・一意なsegment名・metadata位置・payload長・
record時刻で行い、物理位置が再利用されても異なる時刻のイベントを削除しない。
v1で不明だったsegment名は同じlogical segmentのv2から補完する。名前が矛盾するsegmentは照合しない。

通常取り込みの代表イベント再計算は、追加・更新・無効化・最新状態の交代に関係するrecordの
変更前後のevent_keyと、削除照合の変更前後で効果が変わるevent_keyに限定する。
観測日時・object_keyの更新も代表順位に影響するため対象に含める。更新が不成立になる古い観測や、
同じ削除照合・削除理由の再観測だけでは再計算対象を増やさない。
対象event_keyの別segmentのコピーは引き続き比較する。これは再計算対象の制限であり、
DBの物理走査量が履歴量によらず一定になることを保証するものではない。

## 既存データとの互換性

以下のsegment observationとrecord occurrenceは切り替え前の保存形式で、新方式では書き込まない。
`006_screen_time_ingestion.sql`が旧履歴をSQLで集約し、最新segment・物理record・tombstoneと代表イベントを
初期化する。旧行と元payloadは変更せず保持する。migration ledgerにより再実行で重複しない。
旧checkpoint writerを利用したDBでは、`pdp screen-time migrate-checkpoint --checkpoint PATH`で
format-1 SQLite checkpointの観測差分を同じtransaction内で補助状態へ移す。
checkpointはpayloadを持たないため、旧履歴と正規化内容が一致するrecordは既存の物理IDを再利用し、
それ以外には`checkpoint:`で始まる正規化内容のIDを付ける。元payloadを捏造・復元することはない。
このIDには物理位置も保持する。同じ位置・時刻・長さ・解析内容を持つ新しいRawを取得した場合は、
checkpoint由来の版を無効化し、通常の物理IDへ引き継ぐ。以後のparser訂正で旧削除情報だけが残ることを防ぐ。
後続Rawの物理コピーとの重複は、通常と同じsegment・offset・event key単位で除外する。
削除前の重複数が残る無効イベントは0へ補正し、削除状態自体は通常取り込みと同じresolverで検証する。

`007_screen_time_analysis_entry.sql`は旧観測・物理recordの移行漏れ、削除照合、代表イベントの分析項目と
有効状態を検査する。削除照合と代表選択の検証には取り込みと同じSQL macroを使い、不整合があれば停止する。
検査成功後、分析Viewを`base.screen_time_event`だけの参照に切り替え、旧判定Viewを削除する。
旧tableは証跡として保存するが、dbt・通常クエリ・必須relation監視からは参照しない。

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
取り込み時に端末・stream・元segment名・metadata offset・payload長・元record時刻を照合し、
`ops.screen_time_deletion_match`へ保存する。
時刻の許容差は旧datetime列のmicrosecond丸め分の1 microsecondだけ。元ファイル名の対応が曖昧なsegmentは適用しない。

`ops.screen_time_tombstone.resolution`で`user_deletion_applied`、`ttl_history_retained`、`unmatched`、
`unsupported_reason`、再解析で無効になった`invalidated`を確認する。`unmatched`には未到着・保存期間外・
v1の元ファイル名不明も含まれ、削除適用済みとは扱わない。照合はLoaderで再評価する。
旧`base.screen_time_tombstone_match`・`base.screen_time_tombstone_status` Viewは廃止する。

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

`base.screen_time_event`の`is_active=true`だけを公開するdbt Viewである。
旧履歴へのfallbackや最新観測の選択、削除照合、重複排除は行わない。
`base.screen_time_legacy_transition`は廃止し、interval生成・日別集計は引き続きこのViewを参照する。

取り込み側が次のイベント選択規則を適用し、結果を`base.screen_time_event`に保存する。

1. 通常の候補はlogical segmentの最新観測から選び、同じrecord offsetの最後のmetadataを現在stateとする。
2. `event`かつ`WRITTEN`かつCRC不一致でないrecordだけを候補にする。
3. TTL tombstoneに完全照合できた過去観測の正常イベントを候補へ戻す。Apple側の期限切れだけでは既取得の履歴を消さない。
4. UserInitiated tombstoneに完全照合できた`event_key`は、別segmentの重複コピーも含めて候補から除外する。
5. 同一物理イベントの過去観測を重複計上せず、最後に同じ`event_key`を1件にまとめる。

これは集計からの除外であり、GCS Rawや既存baseの証跡を物理削除する処理ではない。取得前に消えたpayloadは復元できない。

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
