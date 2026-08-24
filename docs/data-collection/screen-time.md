# iPhone・Mac Screen Timeデータ取得

## 1. 取得元

### iPhone

デバイス情報:

```text
~/Library/Biome/sync/sync.db
```

アプリ利用イベント:

```text
~/Library/Biome/streams/restricted/App.InFocus/remote/<device_identifier>/
```

`sync.db`の`DevicePeer`から`platform = 2`のdeviceを列挙し、対応する`remote/<device_identifier>`を読む。

### Mac

アプリ利用イベント:

```text
~/Library/Biome/streams/restricted/ScreenTime.AppUsage/local/
```

これらのpathを読むCollectorプロセスにはFull Disk Accessが必要である。開発時のTerminalではなく、本番で実際に起動するCollectorを権限主体にする。

## 2. ファイル形式

各directoryにはSEGB segmentが保存されている。

```text
SEGB segment
└─ record
   ├─ record metadata
   │  ├─ data offset
   │  ├─ state
   │  └─ creation time
   └─ protobuf payload
```

SEGB containerは`ccl-segb`互換decoderで読む。各recordについて次を保持する。

```text
segment filename
record data offset
record metadata
original protobuf bytes
```

protobufのdecode結果だけでなく元bytesも保持する。macOS更新でschemaが変わった場合に再decodeできるようにするためである。

## 3. iPhone `App.InFocus` payload

| Field | 型 | 名前 | 内容 |
|---:|---|---|---|
| 1 | string | `transition_reason` | 遷移理由 |
| 2 | uint32 | `kind` | event種別 |
| 3 | uint32 | `in_foreground` | `1=前面開始`、`0=前面終了` |
| 4 | double | `cf_absolute_time` | 2001-01-01基準の秒数 |
| 6 | string | `bundle_id` | アプリBundle ID |
| 9 | string | `app_version` | アプリversion |
| 10 | string | `app_build` | build number |
| 13 | uint32 | `platform_flag` | source/platformフラグ |

時刻変換:

```text
event_at_unix = cf_absolute_time + 978307200
```

取得できるデータ:

```text
device identifier
Bundle ID
前面開始・終了
event発生時刻
遷移理由
アプリversion
build number
platform flag
```

表示用アプリ名はpayloadに含まれない場合がある。Bundle IDと表示名の対応は取得処理とは別に管理する。

## 4. Mac `ScreenTime.AppUsage` payload

このstreamの公式schemaは公開されていない。確認済みのwire formatは次のとおり。

| Field | wire type | 型 | 内容 |
|---:|---:|---|---|
| 1 | 0 | varint | `1=利用開始`、`0=利用終了` |
| 2 | 1 | double | Unix timestamp |
| 3 | 2 | string | Bundle ID |
| 5 | 0 | varint | platformフラグ |

field 2はUnix timestampである。iPhoneの`cf_absolute_time`用offsetを加算しない。

取得できるデータ:

```text
Bundle ID
利用開始・終了
event発生時刻
platform flag
```

## 5. Collectorが保存するevent

iPhoneとMacを次の共通形式へ正規化する。

```text
screen_time_transition
- event_key
- device_identifier
- platform
- source_stream
- bundle_id
- event_at
- state
- transition_reason
- app_version
- app_build
- platform_flag_raw
- segment_filename
- record_offset
- observed_at
- original_payload
- parser_version
```

Macに存在しない`transition_reason`、`app_version`、`app_build`はnullとする。

`event_key`は次をcanonical化してhash化する。

```text
device_identifier
source_stream
bundle_id
event timestampのraw値
state
payload discriminator
```

segment filenameとoffsetは追跡情報として保存するが、同じeventが別segmentに現れる可能性を考慮し、それだけで重複判定しない。

## 6. アプリ利用時間の定義

生データに利用時間は入っていない。開始eventと終了eventの差から算出する。

```text
利用時間 = 終了event時刻 - 開始event時刻
```

### iPhone

```text
in_foreground = 1 → 開始
in_foreground = 0 → 終了
```

この値は「アプリが前面として記録された経過時間」であり、キー入力やタッチ操作を継続していた時間ではない。

### Mac

```text
field 1 = 1 → 開始
field 1 = 0 → 終了
```

この値は`ScreenTime.AppUsage`が記録した利用区間である。Appleの画面表示と完全に一致することは保証しない。

## 7. 欠損・不整合event

区間生成時は次の状態を区別する。

```text
start + 同じappのend   → complete
start + 次のapp start  → inferred_end_from_next_start
startのみ              → missing_end
endのみ                → missing_start
end < start            → invalid_timestamp
同一eventが複数存在    → duplicate
```

推定した終了時刻を、実際のend eventと同じ品質として扱わない。`quality`を付けて保存する。

```text
screen_time_interval
- interval_key
- device_identifier
- platform
- bundle_id
- started_at
- ended_at
- duration_seconds
- source_stream
- quality
```

## 8. 定期取得

通常処理:

```text
FSEventsでsegment作成・更新を検知
→ 新しいrecordをdecode
→ transitionを保存
→ watermarkを更新
```

保持するwatermark:

```text
device identifier
stream
segment filename
最後に処理したrecord offset
最後に処理したevent timestamp
```

iCloud同期により過去時刻のeventが後から届く可能性があるため、watermark以降だけを永久に読む方式にはしない。

補正処理:

- 毎日、存在する全segmentのhashを確認する。
- hashが変わったsegmentを先頭から再decodeする。
- `event_key`で重複排除する。
- 未完了intervalを後から到着したend eventで更新する。

実測では約4週間分のsegmentが残っていた。Appleの保証値ではないため、変更検知に加えて日次再走査を行う。

## 9. エラーとして扱う状態

```text
Biome directoryを読めない
sync.dbを開けない
SEGB containerをdecodeできない
protobuf payloadをdecodeできない
未知fieldまたはdecode失敗が急増する
24時間scanが成功しない
```

新しいeventがないことだけでは、端末未使用と障害を区別できない。directoryへのアクセス、scan完了、decode結果を使って稼働判定する。

