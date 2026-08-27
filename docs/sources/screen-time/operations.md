# 運用

## Collector state

Collectorのlocal stateは次のSQLite databaseへ保存する。

```text
~/Library/Application Support/personal-data-platform/collector.db
```

segment observationごとに疑似化scope、SHA-256、object key、状態を保持する。`pending`の間は再送に必要な
deterministic gzip bytesも保持し、B2 upload成功後に`uploaded`へ更新してpayload BLOBをnull化する。

complete scanが成功した場合だけ、scan完了時刻、device数、segment数、uploaded / skipped数を同じdatabaseの
singleton行へ保存する。device identifierやsegment pathの直接値は保存しない。

## 初回scanとwatch

初回はallowlist対象deviceの`App.InFocus/remote/<device_identifier>`に存在する全segmentを走査する。

```text
pdp screen-time collect --once   1回走査して終了
pdp screen-time collect --watch  完全走査を一定間隔で反復
```

`--watch`の間隔は`PDP_COLLECTOR_POLL_SECONDS`で指定し、defaultは300秒、最小は10秒である。各走査が全segmentを
再確認するため、過去eventを含むsegmentの後着更新もevent timestampのwatermarkで切り捨てない。LaunchAgentは
`--watch`をRunAtLoad / KeepAliveで起動し、実行バイナリへFull Disk Accessを付与する。

segmentはread前後のinode、size、mtimeを比較し、読込中に変わった場合は短いretry後に再読込する。安定しない
segmentを途中bytesのままuploadしない。

## Crash recovery

走査開始時にpending uploadを先に再送する。Collector credentialにはread / list権限がないため、B2の事前存在
確認へ依存しない。同じpending keyにはSQLiteへ保存した同じgzip bytesだけを送る。すべてのRaw uploadが成功
した後、deviceごとのcollector scan receiptを更新し、最後にlocal scan成功時刻をcommitする。

Macが停止またはofflineでもRawを捏造しない。LaunchAgent再起動後のcomplete scanとpending retryで回復する。

## 設定と診断

```text
pdp screen-time devices
pdp screen-time doctor
```

`devices`は`sync.db`の`platform = 2`だけを列挙し、疑似化`device_key`を表示する。raw device identifierは
設定へ保存しない。`PDP_SCREEN_TIME_DEVICE_ALLOWLIST`には取得対象の`device_key`を指定する。

`doctor`は変更を行わず、次を確認する。

- Biome `sync.db`へのread accessとplatform=2 device数
- allowlistとの一致
- App.InFocus remote directoryの存在
- SQLite state directoryのwrite可否
- Keychainまたは環境変数からのB2設定読込

B2への実write、Raw decode、MotherDuck接続は`doctor`では行わない。B2 writeは`collect --once`、cloud側は
`pdp preflight`でそれぞれ確認する。

## 異常判定

次の場合はnon-zeroで終了し、LaunchAgentまたはoperatorにretryを委ねる。

```text
Biome directoryまたはsync.dbを読めない
allowlist対象deviceを発見できない
segmentが安定して読めない
B2 Rawまたはscan receiptのuploadに失敗する
pending stateを復元できない
```

ReconciliationはB2のscan receiptが24時間以上更新されていない場合も失敗にする。新しいeventがないことだけを
障害とみなさず、complete scanの成功証跡を使用する。

Loader、通知、rebuildは[`Platform運用`](../../platform/operations.md)に従う。
