# 運用

## Collector state

```text
~/Library/Application Support/personal-data-platform/collector.db
```

疑似化scope・SHA-256・object key・状態をSQLiteへ保存し、`pending`では再送用gzip bytesを保持する。
upload成功後は`uploaded`へ更新してpayload BLOBをnull化する。device identifierやsegment pathの直接値は保存しない。
complete scan成功時だけ完了時刻・device/segment数・uploaded/skipped数をsingleton行へ記録する。
これは最後に成功したstreamの記録であり、stream別の稼働判定には[receiptとmanifest](data-model.md#collector-scan-receipt)を使う。

## 初回scanとwatch

```text
pdp screen-time collect --once   1回走査して終了
pdp screen-time collect --watch  完全走査を一定間隔で反復
```

初回から全segmentを走査する。`--watch`間隔は`PDP_COLLECTOR_POLL_SECONDS`で、default 1800秒、最小10秒。
完成判定は[取得仕様](acquisition.md#segb-container)に従う。同じ数値の名前だけでは後続とみなさず、数値以外は走査エラーとする。
最新segmentは内容変化・時間経過・初回・再起動・`--once`でも強制転送しない。反映には数日以上かかる場合がある。
完成扱いsegmentは毎回再確認し、直前の送信済み版と同内容ならskipする。後着や修正をevent時刻のwatermarkで捨てない。
read前後のinode・size・mtimeが変われば短いretryで再読込し、安定しないsegmentを途中bytesで送信しない。

JSON結果は`devices`、`segments`、`uploaded`、`skipped`、`retried`、待機件数`deferred`を持つ。
`segments`は待機分を含む発見総数、`skipped`は完成扱いの同内容件数。`deferred`はcontrolへ含めない。
Rawの確認とlocal scan記録は毎scan続け、controlの公開間隔とは分ける。

## Crash recovery

streamの走査開始時に、allowlistから外したdeviceも含めてpendingを先に再送する。Collector credentialはwrite-onlyで
read/list権限を持たず、存在確認に依存しない。SQLiteの同じkey・gzip bytesを使い、後続が消えていても待機条件より再送を優先する。
すべてのRaw upload後にreceipt、manifest、local scan成功記録の順で更新する。allowlistの一部が未発見なら監査がreceipt欠損を検出する。
片方のstreamが失敗しても他方を試行し、原因をstderrへ出してcommand全体はnon-zeroで終了する。
未設定streamは空manifestで休止を示す。Raw・旧receiptは保持し、休止中はreceipt更新を要求しない。
Mac停止・offline後はLaunchAgent再起動のcomplete scanとpending retryで回復する。

## 設定と診断

```text
pdp screen-time devices
pdp screen-time doctor
```

疑似化secretを登録後に`devices`でiPhoneとローカルMacの`device_key`・platformを確認する。
iPhoneは`PDP_SCREEN_TIME_DEVICE_ALLOWLIST`、Macは`PDP_SCREEN_TIME_MAC_DEVICE_KEY`へ表示されたkeyを設定する。
Mac keyが未設定ならMac行がなくてもwarningを出してiPhoneの表示を続ける。一部のiPhoneが未発見の場合はallowlistと結果を照合する。

`doctor`は変更を行わず、次を確認する。

- Biome `sync.db`へのread accessとplatform=2 device数
- allowlistとの一致
- App.InFocus remote directoryの存在
- Macを有効化した場合はdevice keyとの一致とScreenTime.AppUsage local directoryの存在
- SQLite state directoryのwrite可否
- GCS設定とimpersonated ADCの種類、所有者、mode、target Service Account

実writeは`collect --once`、cloud側は`pdp preflight`で確認する。`doctor`はGCS write・Raw decode・MotherDuck接続を行わない。
初回成功から1時間以上経過した定期実行でも、access tokenが対話なしで自動更新されることを確認する。

## LaunchAgent

専用Collector Service Accountをimpersonateできるuserで、project専用ADCを作成する。

```bash
export GOOGLE_CLOUD_PROJECT="<project-id>"
export GCS_BUCKET="${GOOGLE_CLOUD_PROJECT}-pdp-raw-west"
export PDP_COLLECTOR_SERVICE_ACCOUNT_EMAIL="screen-time-collector@<project-id>.iam.gserviceaccount.com"
export CLOUDSDK_CONFIG="$HOME/Library/Application Support/personal-data-platform/gcloud"
mkdir -p "$CLOUDSDK_CONFIG"
chmod 700 "$CLOUDSDK_CONFIG"
gcloud auth application-default login \
  --impersonate-service-account="$PDP_COLLECTOR_SERVICE_ACCOUNT_EMAIL"
export GOOGLE_APPLICATION_CREDENTIALS="$CLOUDSDK_CONFIG/application_default_credentials.json"
chmod 600 "$GOOGLE_APPLICATION_CREDENTIALS"
export PDP_SCREEN_TIME_DEVICE_ALLOWLIST="<device_key>[,<device_key>...]"
export PDP_SCREEN_TIME_MAC_DEVICE_KEY="<mac_device_key>"  # Macも収集する場合
export PDP_COLLECTOR_POLL_SECONDS="1800"
```

`CLOUDSDK_CONFIG`はADC作成時だけ使う。plistはproject・bucket・target Service Account・ADC pathを持ち、ADC本文やsecretを含めない。
Python processがSecurity.framework経由でKeychainから疑似化secretを読む。`doctor`と`collect --once`成功後にplistを生成する。
read-only rebuildにはこのCollector ADCを使わず、[`Platform運用`](../../platform/operations.md#rebuild)の
別Service Accountと別ADC directoryを使う。

端末を外す場合はallowlistまたはMac keyを外し、`collect --once`を成功させる。manifestから外れた時点で監査対象外となる。
0台なら空manifestで休止を示し、旧Rawはuploadから90日保持する。watchの環境変数変更はLaunchAgent再生成・再起動で反映する。

```bash
collector_plist="$HOME/Library/LaunchAgents/com.personal-data-platform.screen-time-collector.plist"
pdp screen-time launch-agent \
  --output "$collector_plist" \
  --project-root "$(pwd)" \
  --python-executable "$(pwd)/.venv/bin/python"
plutil -lint "$collector_plist"
plutil -extract ProgramArguments.0 raw -o - "$collector_plist"
```

表示されたPython executableへ「システム設定 > プライバシーとセキュリティ > フルディスクアクセス」で権限を付与する。
Terminalの権限だけではLaunchAgentから読めない。symlinkの`.venv/bin/python`は実体の`python3.14`として表示される場合がある。
Python更新・仮想環境再作成後は読取りを再確認する。付与後に登録し、状態とlogを確認する。

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

生成時のmodeはplist `0600`、log directory `0700`。bootstrapは自動実行しない。

## 異常判定

次の場合はnon-zeroで終了し、LaunchAgentまたはoperatorにretryを委ねる。

```text
Biome directoryまたはsync.dbを読めない
allowlist対象deviceを1台も発見できない
走査中のdirectoryの権限エラーや消失、または数値でないsegment名
転送対象segmentが安定して読めない
GCS Raw、scan receipt、またはactive-device manifestのuploadに失敗する
pending stateを復元できない
```

監査はmanifestまたは対象receiptが48時間以上古い場合、Raw/receiptがあるのにmanifestがない場合に失敗する。
空manifestは休止中、未設定でRaw・receipt・manifestがないMacは初回有効化前として扱う。新eventの有無だけで障害を判定しない。

Loader、通知、rebuildは[`Platform運用`](../../platform/operations.md)に従う。

## iPhoneとMacの共存

同じCollector process・SQLiteを使い、pendingとsegment scopeはstreamで分離する。
Loader・再構築は`--source screen_time --all-streams`で両方を処理する。
単一streamのLoader・再構築は`--source screen_time --stream app-in-focus`または`app-usage`を使う。

## 初回セットアップとRaw形式

DBの準備と定期実行の有効化は[`DBの初期化と更新`](../../platform/operations.md#dbの初期化と更新)に従う。
Collectorは新規観測をv2で保存し、既存pendingは元のv1/v2 keyとbytesで再送する。
parser versionが古いRawは再解析する。v1保存済みの完成segmentも照合情報を伴うv2として一度保存し直し、以後の同内容はskipする。
v1は元segment名がなく、同じlogical segmentのv2が未取得ならファイル間の削除照合ができない。
`ops.screen_time_tombstone.resolution`の`unmatched`と`unsupported_reason`を確認する。
再解析は残存Rawに限られ、期限切れの履歴保持には取得済み正常イベントと照合可能なTTL tombstoneが必要となる。

## イベントと補助状態の保存

保存形式・イベント選択は[データモデル](data-model.md)を参照する。Loader後は日別利用時間・イベント一意性、
ログの`affected events`/`tombstones`件数を確認する。削除診断には`ops.screen_time_tombstone.resolution`と`ops.screen_time_deletion_match`を使う。

### 停止後の再実行

補助状態・イベント・Raw取込成功記録は同じtransactionで更新する。再実行・不明commitの処理は[共通永続化契約](../../platform/architecture.md#loaderと永続化)に従う。
バックアップ復元は3種類の状態を同じ時点へ戻す。Rawの90日保持を超えた履歴をRawだけで完全復元することは保証できない。
scratch rebuildは本番状態を参照せず別の空DBで行い、dbtと監査の確認前や中断状態のまま本番へ切り替えない。
