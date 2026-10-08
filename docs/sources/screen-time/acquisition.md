# 取得仕様

## 取得元

```text
~/Library/Biome/sync/sync.db
~/Library/Biome/streams/restricted/App.InFocus/remote/<device_identifier>/
~/Library/Biome/streams/restricted/ScreenTime.AppUsage/local/
```

`sync.db`の`DevicePeer`から`platform = 2`のdeviceを列挙し、対応する`remote/<device_identifier>`を読む。
Macは`platform = 3 AND me = 1`の行がちょうど1件あることを確認して同じsecretで疑似化する。
`PDP_SCREEN_TIME_MAC_DEVICE_KEY`がそのkeyと一致する場合だけ`ScreenTime.AppUsage/local`を読む。

取得対象は`PDP_SCREEN_TIME_DEVICE_ALLOWLIST`のiPhoneと`PDP_SCREEN_TIME_MAC_DEVICE_KEY`のMacで、未設定streamは休止扱いとする。
許可したiPhoneが1台も見つからなければ失敗し、一部だけ未発見なら発見済み端末を別々の`device_key`で収集する。
疑似化keyの確認と設定は[運用](operations.md#設定と診断)に従い、raw device identifierを設定へ保存しない。

読み取るCollectorバイナリにはFull Disk Accessが必要である。LaunchAgentへの付与は[運用](operations.md#launchagent)を参照する。

## 認証と疑似化secret

疑似化secretはmacOS Keychain service `personal-data-platform`、account `screen-time-pseudonym-key-hex`から読む。
`PDP_PSEUDONYM_KEY_HEX`を設定した場合は環境変数を優先する。32 bytes以上のhex値を使う。
secretの変更は過去のdevice / segment keyとの同一性を失うため、通常のcredential更新では変更しない。

MotherDuck writer tokenはKeychain service `personal-data-platform`、account `screen-time-motherduck-token`から読む。
一時overrideは`PDP_SCREEN_TIME_MOTHERDUCK_TOKEN`。接続先は`MOTHERDUCK_DATABASE`で指定し、CollectorにGCS認証は不要である。

## SEGB container

SEGB segmentはrecord metadata（data offset・state・creation time）とprotobuf payloadを持つ。
MIT Licenseの`ccl-segb`互換decoderをcommit `23c3f7d3d969a79627b738ba0a2486c31d675753`へ固定し、parser versionとともに記録する。

Collectorは最新segmentも毎回読み取る。read前後のinode・size・mtimeが一致したbytesを既存decoderで検査し、
構造不整合・書込途中のCRC不一致があるsnapshotは保存・取り込みを見送る。次の30分周期に再試行する。

元bytesとsegment名・種別をv2 envelopeへ格納する。envelope全体のSHA-256と`mtime=0`のgzipを使う。
object keyと疑似化keyは[データモデル](data-model.md)に従う。

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

```text
event_at_unix = cf_absolute_time + 978307200
```

表示用アプリ名はpayloadに含まれない場合がある。Bundle IDと表示名の対応は取得処理とは別に管理する。

未知protobuf fieldはRawに保持する。削除済みrecordは元bytesとメタデータを保持する。
既存のCRC不一致RawはLoaderの互換処理を維持するが、Collectorは新しいCRC不一致snapshotを保存しない。
CRC正常の通常イベント・tombstoneの既知fieldやSEGB構造をdecodeできなければ、観測全体を取込失敗とする。

SEGB v2のtrailerはdecode前にrecord stateを検査する。未知stateは既存の有効状態を維持して観測全体を失敗させる。
`state=0`かつ`end_offset=0`の未使用slotと、既知の空record `state=4`は許容する。

## Mac `ScreenTime.AppUsage` payload

時刻はUnix秒で、`event_at`と2001-01-01基準の`cf_absolute_time`へ変換して保存する。

| Field | 型 | 内容 |
|---:|---|---|
| 1 | uint32 | `1=アプリ開始`、`0=終了` |
| 2 | double | Unix秒のevent時刻 |
| 3 | string | Bundle ID |
| 5 | uint32、省略可 | 意味が未確定のfield。正規化せずRawに保持 |

`app-usage-v1`をparser versionとして記録する。削除済みrecord、CRC不一致、tombstone、
最新segmentの検査はiPhoneと同じ処理を使う。Webサイト利用とmacOS設定画面の数値一致は対象外である。

### App.InFocus診断command

```bash
pdp screen-time inspect-mac
pdp screen-time inspect-mac --directory <App.InFocus/localのpath>
```

既定対象は`~/Library/Biome/streams/restricted/App.InFocus/local`。完成扱いsegmentだけを安定読込し、
最新は`deferred_segment_count`へ数えて解析しない。読取り・解析失敗は相対pathを示してnon-zeroで終了する。
JSONには発見・解析・待機segment数、各stateのrecord数、最初・最後のUTC ISO 8601 event時刻（未検出は`null`）、
Bundle ID順の`apps`別start・end・合計record数を出す。record数は一意イベント数や利用秒数ではない。
元payload・端末identifier・アプリ表示名を出力せず、継続保存せず解析用一時ファイルも削除する。
実行バイナリにはFull Disk Accessが必要。`devices`と`doctor`はpayloadを解析しない。
