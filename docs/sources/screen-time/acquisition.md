# 取得仕様

## 取得元

```text
~/Library/Biome/sync/sync.db
~/Library/Biome/streams/restricted/App.InFocus/remote/<device_identifier>/
~/Library/Biome/streams/restricted/ScreenTime.AppUsage/local/
```

`sync.db`の`DevicePeer`からiPhoneを列挙し、Macは自身の行がちょうど1件あることを確認する。
対象は`PDP_SCREEN_TIME_DEVICE_ALLOWLIST`のiPhoneと`PDP_SCREEN_TIME_MAC_DEVICE_KEY`のMacで、未設定streamは休止扱いとする。
許可したiPhoneが1台も見つからなければ失敗し、一部だけ未発見なら発見済み端末を収集する。
疑似化keyの確認と設定は[運用](operations.md#設定と診断)に従い、raw device identifierを設定へ保存しない。
読取りにはFull Disk Accessが必要で、LaunchAgentへの付与は[運用](operations.md#launchagent)を参照する。

## 認証と疑似化secret

疑似化secretとMotherDuck専用tokenは[Keychainの専用項目](operations.md#設定と診断)から読む。
secretを変更すると過去のdevice / segment keyとの同一性を失うため、通常のcredential更新では変更しない。

## SEGB container

SEGB segmentの元bytesとsegment名・種別をenvelopeへ格納する。
Collectorは最新segmentも読み、read前後のinode・size・mtimeが一致したbytesを検査する。
構造不整合や書込途中のCRC不一致があるsnapshotは保存・取り込みを見送り、次周期に再試行する。

未知protobuf fieldと削除済みrecordもRawに残す。既存のCRC不一致RawはLoaderで読めるが、
CRC正常の未知payload、壊れたSEGB構造、未知record stateは観測全体を取込失敗にし、既存の有効状態を維持する。
Rawの保持範囲は[データモデル](data-model.md#observation-semantics)を参照する。

## iPhone `App.InFocus` payload

Bundle ID、前面開始・終了、2001-01-01基準の秒数からtransitionを作る。
表示用アプリ名は取得せず、Bundle IDと表示名の対応は別に管理する。
protobufのfield定義は[payloads.py](../../../src/personal_data_platform/sources/screen_time/payloads.py)にある。

## Mac `ScreenTime.AppUsage` payload

Bundle ID、アプリ開始・終了、Unix秒からtransitionを作る。
Webサイト利用とmacOS設定画面の数値一致は対象外である。

### App.InFocus診断command

```bash
pdp screen-time inspect-mac
pdp screen-time inspect-mac --directory <App.InFocus/localのpath>
```

既定対象は`~/Library/Biome/streams/restricted/App.InFocus/local`。
通常収集の`ScreenTime.AppUsage`とは別の診断で、完成扱いsegmentだけを解析し、最新は待機扱いにする。
JSONにsegment・record数、最初・最後のevent時刻、Bundle ID別のstart・end数を出す。
record数は一意イベント数や利用秒数ではない。読取り・解析失敗はnon-zeroで終了する。
継続保存せず、解析用一時ファイルも削除する。実行バイナリにはFull Disk Accessが必要である。
