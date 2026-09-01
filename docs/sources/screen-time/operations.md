# 運用

## Collector state

Collectorのlocal stateは次のSQLite databaseへ保存する。

```text
~/Library/Application Support/personal-data-platform/collector.db
```

segment observationごとに疑似化scope、SHA-256、object key、状態を保持する。`pending`の間は再送に必要な
deterministic gzip bytesも保持し、GCS upload成功後に`uploaded`へ更新してpayload BLOBをnull化する。

complete scanが成功した場合だけ、scan完了時刻、device数、segment数、uploaded / skipped数を同じdatabaseの
singleton行へ保存する。device identifierやsegment pathの直接値は保存しない。

## 初回scanとwatch

初回はallowlist対象deviceの`App.InFocus/remote/<device_identifier>`に存在する全segmentを走査する。

```text
pdp screen-time collect --once   1回走査して終了
pdp screen-time collect --watch  完全走査を一定間隔で反復
```

`--watch`の間隔は`PDP_COLLECTOR_POLL_SECONDS`で指定し、defaultは300秒、最小は10秒である。各走査が全segmentを
再確認するため、過去eventを含むsegmentの後着更新もevent timestampのwatermarkで切り捨てない。

segmentはread前後のinode、size、mtimeを比較し、読込中に変わった場合は短いretry後に再読込する。安定しない
segmentを途中bytesのままuploadしない。

## Crash recovery

走査開始時にpending uploadを先に再送する。Collector credentialにはread / list権限がないため、GCSの事前存在
確認へ依存しない。同じpending keyにはSQLiteへ保存した同じgzip bytesだけを送る。すべてのRaw uploadが成功
した後、deviceごとのcollector scan receipt、active-device manifestの順に更新し、最後にlocal scan成功時刻を
commitする。manifestはfull allowlistを持つため、一部のallowlist対象deviceが未発見ならそのdeviceのreceipt欠損を
Reconciliationが検出する。

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
- GCS設定とimpersonated ADCの種類、所有者、mode、target Service Account

GCSへの実write、Raw decode、MotherDuck接続は`doctor`では行わない。GCS writeは`collect --once`、cloud側は
`pdp preflight`でそれぞれ確認する。access tokenの自動更新は、初回成功から1時間以上経過した定期実行でも
対話なしで成功することを確認する。

## LaunchAgent

専用Collector Service Accountをimpersonateできるuserで、project専用ADCを作成する。

```bash
export GOOGLE_CLOUD_PROJECT="<project-id>"
export GCS_BUCKET="<project-id>-pdp-raw"
export PDP_COLLECTOR_SERVICE_ACCOUNT_EMAIL="screen-time-collector@<project-id>.iam.gserviceaccount.com"
export CLOUDSDK_CONFIG="$HOME/Library/Application Support/personal-data-platform/gcloud"
mkdir -p "$CLOUDSDK_CONFIG"
chmod 700 "$CLOUDSDK_CONFIG"
gcloud auth application-default login \
  --impersonate-service-account="$PDP_COLLECTOR_SERVICE_ACCOUNT_EMAIL"
export GOOGLE_APPLICATION_CREDENTIALS="$CLOUDSDK_CONFIG/application_default_credentials.json"
chmod 600 "$GOOGLE_APPLICATION_CREDENTIALS"
export PDP_SCREEN_TIME_DEVICE_ALLOWLIST="<device_key>[,<device_key>...]"
export PDP_COLLECTOR_POLL_SECONDS="300"
```

`CLOUDSDK_CONFIG`はADC作成時だけ使う。plistにはGCS project、bucket、target Service Account、ADC pathを保存し、
ADC本文や疑似化secretは保存しない。疑似化secretはmacOS Keychain service
`personal-data-platform`から実行時に読む。`pdp screen-time doctor`と`collect --once`を成功させてからplistを生成する。
read-only rebuildにはこのCollector ADCを使わず、[`Platform運用`](../../platform/operations.md#rebuild)の
別Service Accountと別ADC directoryを使う。

端末を運用対象から外す場合は`PDP_SCREEN_TIME_DEVICE_ALLOWLIST`からdevice keyを削除し、残る対象deviceで
`collect --once`を成功させる。最後に更新されたmanifestから外れた時点で正式なdecommissionとなる。旧receiptは
残っていても監査対象外となり、旧Rawはuploadから60日のLifecycle期限まで保持される。

```bash
collector_plist="$HOME/Library/LaunchAgents/com.personal-data-platform.screen-time-collector.plist"
pdp screen-time launch-agent \
  --output "$collector_plist" \
  --project-root "$(pwd)" \
  --python-executable "$(pwd)/.venv/bin/python"
plutil -lint "$collector_plist"
plutil -extract ProgramArguments.0 raw -o - "$collector_plist"
```

最後のcommandが表示したPython executableへ、macOSの「システム設定 > プライバシーとセキュリティ >
フルディスクアクセス」でFull Disk Accessを付与する。Terminalへの付与だけではLaunchAgentの権限にならない。
付与後に登録し、service状態とlogを確認する。

```bash
launchctl bootstrap "gui/$(id -u)" "$collector_plist"
launchctl print "gui/$(id -u)/com.personal-data-platform.screen-time-collector"
tail -F "$HOME/Library/Logs/personal-data-platform/screen-time-collector.stdout.log" \
  "$HOME/Library/Logs/personal-data-platform/screen-time-collector.stderr.log"
```

設定、Python executable、project pathを変更する場合は、先に停止してplistを再生成し、検証後に再登録する。

```bash
launchctl bootout "gui/$(id -u)" "$collector_plist"
```

ADCが失効またはrevokeされた場合も先にLaunchAgentを停止し、同じ`CLOUDSDK_CONFIG`と
`--impersonate-service-account`で`gcloud auth application-default login`を再実行する。`doctor`と
`collect --once`が成功してから再登録し、globalのgcloud configurationやuser ADCへfallbackしない。

生成commandはplistをmode `0600`、log directoryをmode `0700`で作成する。`launchctl bootstrap`は自動実行
しないため、Full Disk AccessとKeychain、`collect --once`の確認前にCollectorが起動することはない。

## 異常判定

次の場合はnon-zeroで終了し、LaunchAgentまたはoperatorにretryを委ねる。

```text
Biome directoryまたはsync.dbを読めない
allowlist対象deviceを1台も発見できない
segmentが安定して読めない
GCS Raw、scan receipt、またはactive-device manifestのuploadに失敗する
pending stateを復元できない
```

Reconciliationはactive-device manifestまたはmanifest内deviceのscan receiptが24時間以上更新されていない場合も
失敗にする。新しいeventがないことだけを障害とみなさず、complete scanの成功証跡を使用する。

Loader、通知、rebuildは[`Platform運用`](../../platform/operations.md)に従う。
