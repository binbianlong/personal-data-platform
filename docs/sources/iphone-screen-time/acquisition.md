# iPhone Screen Timeの取得仕様

## 取得元

```text
~/Library/Biome/sync/sync.db
~/Library/Biome/streams/restricted/App.InFocus/remote/
```

`sync.db`の`DevicePeer`からiPhoneを列挙し、`PDP_IPHONE_SCREEN_TIME_DEVICE_ALLOWLIST`の疑似化keyに一致する端末を収集する。
許可したiPhoneが1台も見つからなければ失敗し、一部だけ未発見なら発見済み端末を収集する。
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

Bundle ID、前面開始・終了、2001-01-01基準の秒数からtransitionを作る。
表示用アプリ名は取得せず、Bundle IDと表示名の対応は別に管理する。
sourceのdecoderは[sources/iphone_screen_time](../../../src/personal_data_platform/sources/iphone_screen_time/)にある。
