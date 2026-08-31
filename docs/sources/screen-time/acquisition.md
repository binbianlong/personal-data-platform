# 取得仕様

## 取得元

デバイス情報:

```text
~/Library/Biome/sync/sync.db
```

アプリ利用イベント:

```text
~/Library/Biome/streams/restricted/App.InFocus/remote/<device_identifier>/
```

`sync.db`の`DevicePeer`から`platform = 2`のdeviceを列挙し、対応する`remote/<device_identifier>`を読む。

`pdp screen-time devices`は発見したiPhoneの疑似化`device_key`を表示する。取得対象は環境変数
`PDP_SCREEN_TIME_DEVICE_ALLOWLIST`へカンマ区切りで指定した`device_key`だけとし、raw device identifierを
設定へ保存しない。allowlistが空、または許可した端末を1台も`DevicePeer`に発見できない場合は設定エラーと
する。一部だけ未発見の場合は発見済み端末を収集するため、`devices`の結果とallowlistを照合して対象端末の
不足を確認する。複数iPhoneは別々の`device_key`として処理する。

これらのpathを読むCollectorプロセスにはFull Disk Accessが必要である。開発時のTerminalではなく、
本番で実際に起動するLaunchAgentの実行バイナリを権限主体にする。

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

SEGB containerはMIT Licenseの`ccl-segb`互換decoderで読む。再現性のため、decoderは次のcommitへ
固定し、parser versionと一緒に記録する。

```text
23c3f7d3d969a79627b738ba0a2486c31d675753
```

Collectorはsegment単位で元bytesを読み、圧縮前bytesのSHA-256を計算する。B2へ保存する本文は元bytesを
そのままgzipしたもので、gzip headerの`mtime`は`0`に固定する。object keyと疑似化keyは
[`data-model.md`](data-model.md)に従う。

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

未知protobuf fieldはRaw bytesに保持し、既知fieldのdecodeを妨げない限り収集を継続する。SEGBまたは
既知fieldをdecodeできない場合は、そのsegment observationをAnalyticsへ成功取込した扱いにしない。
