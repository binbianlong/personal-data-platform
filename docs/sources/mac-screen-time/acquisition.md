# Mac Screen Timeの取得仕様

## 取得元

```text
~/Library/Biome/sync/sync.db
~/Library/Biome/streams/restricted/ScreenTime.AppUsage/local/
```

`sync.db`の`DevicePeer`で自身の行がちょうど1件あることを確認し、`PDP_MAC_SCREEN_TIME_DEVICE_KEY`の疑似化keyと照合する。
未設定ならこのsourceだけを休止扱いにする。raw device identifierを設定へ保存しない。
読取りにはFull Disk Accessが必要で、設定は[運用](operations.md)を参照する。

## 認証と疑似化secret

疑似化secretとMotherDuck専用tokenは[Keychainの専用項目](operations.md#設定と診断)から読む。
secretを変更すると過去のdevice / segment keyとの同一性を失うため、通常のcredential更新では変更しない。
iPhone・Macを分割するときは既存secretを両方の専用accountへ同じ値で複製する。

## SEGB container

SEGB segmentの元bytesと種別をenvelopeへ格納する。削除記録のsegmentは収集しない。
Collectorは最新segmentも読み、read前後のinode・size・mtimeが一致したbytesを検査する。
構造不整合や書込途中のCRC不一致があるsnapshotは保存・取り込みを見送り、次周期に再試行する。

未知protobuf fieldと削除済みrecordもRawに残す。既存のCRC不一致RawはLoaderで読めるが、
CRC正常の未知payload、壊れたSEGB構造、未知record stateは観測全体を取込失敗にし、既存の有効状態を維持する。
Rawの保持範囲は[データモデル](data-model.md#observation-semantics)を参照する。

## Payload

Bundle ID、アプリ開始・終了、Unix秒からtransitionを作る。Webサイト利用とmacOS設定画面の数値一致は対象外である。
sourceのdecoderは[sources/mac_screen_time](../../../src/personal_data_platform/sources/mac_screen_time/)にある。

## AppUsage診断command

```bash
pdp mac-screen-time inspect-mac
pdp mac-screen-time inspect-mac --directory <ScreenTime.AppUsage/localのpath>
```

既定対象は通常収集と同じ`~/Library/Biome/streams/restricted/ScreenTime.AppUsage/local`。
Mac専用decoderで完成扱いsegmentだけを解析し、最新は待機扱いにする。
JSONにsegment・record数、最初・最後のevent時刻、Bundle ID別のstart・end数を出す。
record数は一意イベント数や利用秒数ではない。読取り・解析失敗はnon-zeroで終了する。
継続保存せず、解析用一時ファイルも削除する。実行バイナリにはFull Disk Accessが必要である。
