# 取得仕様

## iPhone

デバイス情報:

```text
~/Library/Biome/sync/sync.db
```

アプリ利用イベント:

```text
~/Library/Biome/streams/restricted/App.InFocus/remote/<device_identifier>/
```

`sync.db`の`DevicePeer`から`platform = 2`のdeviceを列挙し、対応する`remote/<device_identifier>`を読む。

## Mac

アプリ利用イベント:

```text
~/Library/Biome/streams/restricted/ScreenTime.AppUsage/local/
```

これらのpathを読むCollectorプロセスにはFull Disk Accessが必要である。開発時のTerminalではなく、本番で実際に起動するCollectorを権限主体にする。

## SEGB container

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

SEGB containerは`ccl-segb`互換decoderで読む。

## iPhone `App.InFocus` payload

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

## Mac `ScreenTime.AppUsage` payload

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
