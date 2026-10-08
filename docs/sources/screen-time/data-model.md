# データモデル

## Local Raw observation

Rawは既存SQLite `collector.db`のgzip BLOBとして保存する。新規Collectorはv2を保存し、Loaderはv1/v2を読む。
下記key形式は従来と共通で、移行したRawは元のkey・generation・保持起点を引き継ぐ。

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
元SEGBはbyte-for-byteで復元できる。v1/v2は同じ疑似化keyを使い、v2の元ファイル名を同一logical segmentのv1にも対応付けられる。

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

## Collector scan receipt

全対象のローカル保存が成功したcomplete scanで、deviceごとのreceiptをSQLiteへ保存する。
成功から24時間後か保存先・端末設定の変更時に更新する。下記keyのcontrol本文も同じSQLiteに保持する。

```text
raw/screen_time/v1/_control/collector/latest/<device_key>.json
raw/screen_time/v1/_control/collector/app-usage/latest/<mac_device_key>.json
```

本文は`schema_version`、`device_key`、UTCの`completed_at`、待機分を含む`segment_count`、`status=succeeded`のみ。
稼働確認用であり、端末identifier・path・Bundle IDを含めない。receiptの後にmutable manifestを更新する。

```text
raw/screen_time/v1/_control/collector/active.json
raw/screen_time/v1/_control/collector/app-usage/active.json
```

本文は`schema_version`、sort済みの`device_keys`、UTCの`completed_at`、`status=succeeded`のみ。
manifestは全allowlistを持つactive-device集合の正本で、空集合は明示的な休止状態である。
manifestから外したdeviceのreceipt更新は要求しない。保存済み最新Rawは残す。streamごとに独立し、片方のreceiptで他方の稼働を証明しない。
MotherDuckの既存stream別heartbeatは、全streamの取込成功・pending解消・ローカル監査成功後にMacが更新する。
Cloud Runはその成功時刻と`scan_completed_at`が48時間以内であることを確認し、自身では更新しない。

## `base.screen_time_event`

分析入口tableで、`event_key`がprimary key。同じイベントは1行とし、無効化は`is_active=false`へ更新する。
列は後述のtransitionに`is_active`と`loaded_at`を加えたもので、`original_payload`や観測ごとの本文を保存しない。
分析項目・parser version・物理コピー数・有効状態が変わった場合だけ更新し、provenanceは採用した代表recordを指す。
`observed_at`は全再観測の最新日時ではない。Rawごとの取込時刻・件数は`ops.ingestion_metadata`で確認する。

`ops.screen_time_segment`は`(observed_at, object_key)`順の最新snapshotを保持する。古いRawは巻き戻さず照合情報を補完・修正する。
`ops.screen_time_record`は内容digestと物理位置で重複排除し、順位と代表選択用の正規化項目を保持する。
元payloadは保存せず、同内容の再観測では補助行数が増えない。

TTLで必要な過去recordの照合情報・正規化項目はMotherDuckに残し、ユーザー削除は同じevent_keyの別segmentコピーにも適用する。
v1で不明のsegment名は同じlogical segmentのv2から補完する。`source_segment_names`は重複を除いたsort済み非NULL配列で、未観測は空配列。
複数名を持つsegmentは`name_ambiguous=true`として照合対象外にし、その候補名も全体の一意性判定へ含める。
名前追加時は全候補名のtombstoneを再照合する。`source_segment_name`は辞書順最小値で、照合には単独で使わない。
代表イベントの再計算はrecord・順位・削除効果が変わるevent_keyに限定し、別segmentのコピーも比較する。

## Recordの取り込み

`ops.ingestion_metadata`にはparser versionと全record数（削除済み・CRC不一致・tombstoneを含む）を保存する。
parser versionはiPhone `app-in-focus-v2`、Mac `app-usage-v1`。削除済み・CRC不一致は同じ物理位置の既存recordを無効化する。
CRC正常の未知payloadや壊れたSEGB構造はobject全体を失敗させ、保存中の最新版Rawは再解析できる。

### Tombstone

| protobuf field | 型 | 内容 |
|---|---|---|
| 1 | string | 対象segment名 |
| 2 | uint32 | 対象recordのmetadata offset |
| 3 | uint32 | 対象payload長 |
| 4 | uint32 | 削除理由: 1=TTL、2=UserInitiated |
| 5 | string | processName |
| 6 | double | 対象event timestamp（Cocoa秒） |
| 7 | string、省略可 | policyID |

未知の削除理由は保持して自動適用しない。端末・stream・一意な元segment名・metadata offset・payload長・元record時刻を照合し、
`ops.screen_time_deletion_match`へ保存する。時刻は1 microsecondの許容差とし、物理位置が再利用されても異なる時刻のイベントを削除しない。

`ops.screen_time_tombstone.resolution`は`user_deletion_applied`、`ttl_history_retained`、`unmatched`、`unsupported_reason`、
再解析で無効になった`invalidated`を持つ。`unmatched`は未到着・保存期間外・v1の名前不明も含み、適用済みではない。Loaderで再評価する。

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
3. TTL tombstoneに完全照合できた過去観測の正常イベントを候補へ戻す。Apple側の期限切れだけでは既取得の履歴を消さない。
4. UserInitiated tombstoneに完全照合できた`event_key`は、別segmentの重複コピーも含めて候補から除外する。
5. 同一物理イベントの過去観測を重複計上せず、最後に同じ`event_key`を1件にまとめる。

無効化は集計からの除外で、Raw・イベント・補助状態を物理削除しない。取得前に消えたpayloadは復元できない。

```text
event_key / device_key / platform / source_stream
bundle_id / event_at / state
transition_reason / kind / app_version / app_build / platform_flag
object_key / segment_key / segment_filename / record_offset / observed_at
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

クエリ例は[`日次Screen Timeの参照`](../../analytics/README.md#日次screen-timeの参照)を参照する。
