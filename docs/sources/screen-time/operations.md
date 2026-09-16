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

`--watch`の確認間隔は`PDP_COLLECTOR_POLL_SECONDS`で指定し、defaultは300秒、最小は10秒である。
5分ごとの確認でRawを必ず保存するわけではない。端末・親directoryごとに数値のファイル名を数値順で比較し、
より大きい名前の後続ファイルが存在するsegmentだけを転送対象とする。通常directoryと`tombstone`を含む
各子directoryは独立して判定する。同じ数値の名前が複数ある場合、それだけでは後続とみなさない。

各directoryの最新segmentは内容が変わっても転送待ちにする。時間経過による強制転送はなく、最新データの
反映には数日以上かかる場合がある。初回・再起動・`--once`でも同じ条件を使い、最新以外の未送信segmentを
回収する。数値でない名前は完成判定できないため、走査をエラーにする。

完成扱いのsegmentも毎回再確認し、内容が直前の送信済み版と同じなら保存を省略する。後着segmentや過去の
segmentへの修正はevent timestampのwatermarkで切り捨てず、内容が変わっていれば新しい観測版を保存する。

実行結果のJSONは`devices`、`segments`、`uploaded`、`skipped`、`retried`に加えて、最新ファイルとして待機した
件数`deferred`を出力する。`segments`とscan receiptの`segment_count`は待機分を含む発見総数、`skipped`は
完成扱いの転送対象のうち内容が同じだった件数である。待機分しかなくても正常走査ならreceiptとmanifestを
更新する。`deferred`は実行結果だけに追加し、既存SQLiteとcontrol objectの形式は変更しない。

segmentはread前後のinode、size、mtimeを比較し、読込中に変わった場合は短いretry後に再読込する。安定しない
segmentを途中bytesのままuploadしない。

## Crash recovery

走査開始時に、現在のallowlistから削除済みのdeviceも含めてpending uploadを先に再送する。Collector credentialには
read / list権限がないため、GCSの事前存在確認へ依存しない。同じpending keyにはSQLiteへ保存した同じgzip bytesだけを
送る。既存pendingは最新ファイルの待機条件より優先し、後続ファイルが消えていても再送する。pendingと今回の
転送対象すべてのRaw uploadが成功した後、deviceごとのcollector scan receipt、active-device manifestの順に更新し、
最後にlocal scan成功時刻をcommitする。manifestはfull allowlistを持つため、一部のallowlist対象deviceが未発見なら
そのdeviceのreceipt欠損をReconciliationが検出する。

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
残っていても監査対象外となり、旧Rawはuploadから90日のLifecycle期限まで保持される。

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
走査中のdirectoryの権限エラーや消失、または数値でないsegment名
転送対象segmentが安定して読めない
GCS Raw、scan receipt、またはactive-device manifestのuploadに失敗する
pending stateを復元できない
```

Reconciliationはactive-device manifestまたはmanifest内deviceのscan receiptが24時間以上更新されていない場合も
失敗にする。新しいeventがないことだけを障害とみなさず、complete scanの成功証跡を使用する。

Loader、通知、rebuildは[`Platform運用`](../../platform/operations.md)に従う。


## 他streamとの共存

このCollectorのstate DB、active-device manifestとreceiptはiPhoneの`app-in-focus`取得専用である。
Mac自身のScreen Timeを別Collectorで取得する場合は、stateとcontrol objectを別に所有する。
別Collectorから同じmanifestを上書きしたり、iPhoneのscan成功で別streamの稼働を証明したりしない。

Loader・監査・再構築は`--source screen_time --stream app-in-focus`でこのstreamを明示できる。
共通runtimeの更新順序は[`Platform運用`](../../platform/operations.md#更新時の互換性)に従う。

## Parser v2とRaw v2への移行

1. GCSのv1/v2両prefixの90日Lifecycle、Collector create権限、Loader/Reconciliationのprefix設定をTerraformで揃える。
2. 新runtimeへ更新し、migration `004_screen_time_tombstones.sql`を適用する。既存行を保持し、nullable列・削除情報を追加する。
3. Loaderは利用可能なRawのparser versionが古い場合も再解析する。同一objectの置換と取込成功更新は1 transactionで行う。
4. 現行runtimeでは後述のイベント単位の保存への切り替えも完了し、dbtで集計Viewを更新する。
   Reconciliationで分析用relationの存在を確認する。
5. Mac Collectorを新コードで起動する。既存pendingは元のv1/v2 keyとbytesで先に再送し、新規観測はv2で保存する。

Collectorの5分確認・後続ファイル待ち条件は継続する。v1からv2への切替時は、完成扱いの既存segmentも照合情報を
伴うv2として一度保存し直す。その後の同内容はskipする。v2を理解しない旧Loaderと同時運用しない。
Terraform apply、クラウド再解析、Mac Collector起動はローカルのテストとは別に実施する。

v1は元segment名を持たないため、同じlogical segmentのv2が未取得ならファイル間の削除照合ができない。
新方式では`ops.screen_time_tombstone.resolution`の`unmatched`と`unsupported_reason`を確認する。
旧`base.screen_time_tombstone_status` Viewは廃止済み。古いRawが期限切れの場合、
新parserで再解析できる範囲は現在残っているRawに限られる。既に失われた利用内容は作らない。
期限切れの履歴保持は、取得済みの正常イベントと照合可能なTTL tombstoneがある場合に限る。

## イベント単位の保存への切り替え

1. Loader/ReconciliationのSchedulerを止め、実行中のLoader・Reconciliation・dbt Jobの終了を確認する。
   以降は旧writerと新writerを混在させない。
2. MotherDuckをバックアップし、Loader・Reconciliation・dbtを新runtimeへ揃える。
   `006_screen_time_ingestion.sql`が未適用なら、旧履歴から最小の補助状態と代表イベントを初期化する。
   旧occurrence・segment observation行は保持する。各migrationとledgerは同じtransactionで確定する。
3. `pdp dbt --source screen_time --stream app-in-focus`を実行する。
   dbt実行前に`007_screen_time_analysis_entry.sql`まで適用され、下記の移行検査に成功した場合だけ
   分析の入口が`base.screen_time_event`の有効行に切り替わる。旧判定Viewは削除され、dbtでも再作成しない。
4. 新Loaderを1回実行する。既存成功Rawはparserが同じならskipし、旧parserの保持中Rawは再解析する。
5. 日別利用時間、イベント一意性、`ops.screen_time_tombstone.resolution`、Loaderログの
   `affected events`と`tombstones`件数を確認してSchedulerを再開する。

分析tableは`event_key`ごとに1行、補助状態はsegment・物理record・tombstone・削除照合の組ごとに保持する。
同じ内容を繰り返し観測しても補助行数は増えない。新イベント・物理位置・Raw単位の取込記録は増える。
旧occurrenceの容量は減らさない。

### 分析入口の一本化に伴う移行検査

`006`適用済みの環境も同じ停止・バックアップ手順で`007`を適用する。
検査はmigration適用時に一度だけ旧履歴と補助状態を全体走査する。通常の分析クエリには含めない。

- 旧segment観測以上に新しい観測が補助状態に存在すること。
- 旧観測の各物理位置の最終metadataから移行対象となるevent・tombstoneが補助状態に存在すること。
- 取り込みSQLで再計算した削除照合が保存済みの照合と一致すること。
- 取り込みSQLで再計算した代表イベントと、保存済みの分析項目・有効状態が一致すること。
  同内容の再観測で更新しないprovenanceは比較せず、parser訂正で元recordを失った無効イベントは許容する。

失敗時は`Screen Time cutover blocked:`で停止し、`007`のView変更とledgerをrollbackする。
停止理由に対応する旧履歴・補助状態・イベントを調べ、バックアップとの比較や保持中Rawの再解析で修復してから
同じcommandを再実行する。ledgerを手で適用済みにしたり、旧tableを削除して検査を回避しない。
Rawは90日保持のため、期限切れの履歴はバックアップまたは保存済みの旧行からの修復が必要になる。

検査成功時は入口Viewを置換してから旧Viewを削除するため、既存の下流Viewも引き続き参照できる。
旧tableの保存と分析結果への採用は独立しており、以後の削除診断は
`ops.screen_time_tombstone.resolution`と`ops.screen_time_deletion_match`で行う。

### 停止後の再実行

`ScreenTimeBatch.write()`が補助状態とイベントを更新し、WarehouseがRawの取込成功記録とまとめてcommitする。

- commit前の失敗：補助状態・イベント・成功記録を全てrollbackする。同じRawを再実行できる。
- commit後の再実行：Raw identity・generation・parser versionに一致する成功記録があればskipする。
- commit結果が不明な接続障害：後続Raw、失敗記録、Job終了記録、lease解放の書込を止めて接続を閉じる。
  lease期限切れ後に新接続で起動し、永続化済みの成功記録から再処理の要否を判断する。

復元はMotherDuckのイベント・補助状態・取込記録を同じ時点のバックアップから行う。
Rawの保持は90日のため、Rawだけから期限切れの履歴を完全に復元することは保証できない。
新方式で書き込み後は旧runtimeに戻さず、修正版runtimeで再開する。

scratch rebuildは別の空MotherDuck databaseへ、GCSに残るRawだけを同じ取り込み経路で処理する。
本番の状態は参照しない。中断したscratchを本番へ切り替えず、再構築後にdbtと監査を確認する。
