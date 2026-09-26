# 取得仕様

## 取得元

デバイス情報:

```text
~/Library/Biome/sync/sync.db
```

アプリ利用イベント:

```text
~/Library/Biome/streams/restricted/App.InFocus/remote/<device_identifier>/
~/Library/Biome/streams/restricted/ScreenTime.AppUsage/local/
```

`sync.db`の`DevicePeer`から`platform = 2`のdeviceを列挙し、対応する`remote/<device_identifier>`を読む。
Macは`platform = 3 AND me = 1`の行がちょうど1件あることを確認して同じsecretで疑似化する。
`PDP_SCREEN_TIME_MAC_DEVICE_KEY`がそのkeyと一致する場合だけ`ScreenTime.AppUsage/local`を読む。

`pdp screen-time devices`は発見したiPhoneとローカルMacの疑似化`device_key`を表示する。Mac行が
見つからなくてもMac keyが未設定ならiPhoneの表示を続ける。iPhoneの取得対象は環境変数
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

Collectorは同じ端末・親directoryに数値名がより大きいsegmentが存在する場合、その前のsegmentを完成扱いに
する。通常directoryと`tombstone`を含む各子directoryは別々に判定する。これはOSの完成通知ではなく、後続
ファイルの存在を使う運用上の判定である。最新segmentは後続ができるまで転送せず、数日以上待つ場合もある。

転送対象のsegment単位で元bytesを読み、元segment名・種別とともにv2 envelopeへ格納する。
envelope全体のSHA-256を計算してgzipし、gzip headerの`mtime`は`0`に固定する。object keyと疑似化keyは
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

未知protobuf fieldはRaw bytesに保持する。削除済み・CRC不一致はイベント本文を要求せず、元bytesとメタデータを
保持する。CRC正常の通常イベントやtombstoneの既知field、またはSEGB構造をdecodeできない場合は、
そのsegment observationをAnalyticsへ成功取込した扱いにしない。

SEGB v2のtrailerはdecoderへ渡す前にrecord stateを検査する。未知のstateが1件でもあれば
観測全体を失敗させ、直前の正常観測と既存イベントの有効状態を維持する。`state=0`かつ
`end_offset=0`の未使用slotと、既知の空レコード`state=4`は引き続き許容する。

## Mac `ScreenTime.AppUsage` payload

継続収集するMacの`ScreenTime.AppUsage` payloadは次のfieldを使う。時刻はUnix秒で、
既存のイベント形式には`event_at`と2001-01-01基準の`cf_absolute_time`へ変換して保存する。

| Field | 型 | 内容 |
|---:|---|---|
| 1 | uint32 | `1=アプリ開始`、`0=終了` |
| 2 | double | Unix秒のevent時刻 |
| 3 | string | Bundle ID |
| 5 | uint32、省略可 | 意味が未確定のfield。正規化せずRawに保持 |

`app-usage-v1`をparser versionとして記録する。削除済みrecord、CRC不一致、tombstone、
最新segmentの待機はiPhoneと同じ処理を使う。Webサイト利用とmacOS設定画面の数値一致は対象外である。

### App.InFocus診断command

```bash
pdp screen-time inspect-mac
pdp screen-time inspect-mac --directory <App.InFocus/localのpath>
```

既定の対象は`~/Library/Biome/streams/restricted/App.InFocus/local`。iPhone Collectorと同じく、親directory
ごとに数値名の後続ファイルがあるsegmentだけを安定読込して解析する。最新segmentは`deferred_segment_count`
へ数え、内容は解析しない。読取りや解析が失敗した場合はsegmentの相対pathを示してnon-zeroで終了する。

結果はJSONで、`segment_count`、`checked_segment_count`、`deferred_segment_count`、event・start・end・
deleted・CRC不一致・tombstoneのrecord件数、最初と最後のevent時刻を出す。`apps`には有効なevent recordの
Bundle IDとstart・end・合計のrecord件数をBundle ID順で出す。時刻はUTC ISO 8601形式で、eventがなければ
`null`になる。record件数は一意なイベント数や利用秒数を表さない。表示用アプリ名はpayloadにないため補完
しない。元payloadや端末identifierは出力せず、取得データの継続保存も行わない。既存parserが使う一時ファイルは
解析後に削除する。

Biome directoryを読む実行バイナリにはFull Disk Accessが必要。`devices`と`doctor`はevent payloadを
解析しないため、アプリ別の出力は持たない。
