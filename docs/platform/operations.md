# Platform運用

## Provisioningとdeploy

最初にbootstrap Terraformで、GCS Terraform state、Artifact Registry、GitHub OIDC / Workload Identity
Federation、plan用・deploy用Service Accountを作る。その後にruntime TerraformでSecret Manager、
Cloud Run Jobs、Cloud Scheduler、Logging / Monitoringを作る。

具体的な準備は[`bootstrap`](../../infra/bootstrap/)と[`runtime`](../../infra/terraform/)の手順に従う。
Secret Managerのsecret resourceを先に作成し、secret versionを登録してからJobsをdeployする。
必要なrepository variablesも事前に設定する。`GCP_PLAN_WIF_PROVIDER`未設定時はTerraform Plan、
`GCP_DEPLOY_WIF_PROVIDER`未設定時は
Terraform DeployのJobをskipする。これらを設定した後の実行成功を、コードのCI成功とは別に確認する。

runtimeはcommit SHAに対応するimmutable image digestを参照する。mutable tagだけをdeploy入力にしない。

初回およびIAM変更後は`pdp preflight`を専用GCS preflight bucketとMotherDuck test databaseへ接続して実行し、
次を確認する。

- GCS test objectのwrite / read / list / generation指定delete round trip
- MotherDuck test databaseでの一時table作成 / write / read / delete

GCSではupload応答のgenerationを使って作成したobjectだけをread / deleteする。generationを取得できない場合や
cleanupに失敗した場合は成功扱いにせず、preflight bucketの`test/preflight/`を確認する。

deploy workflowからCloud Run preflightが完了することで、WIF deploy、image取得、JobのSecret注入、外部通信も
合わせて確認する。preflight Service AccountとMotherDuck tokenは本番用と分離し、production bucketまたは
production databaseへ書き込まない。

## Loader

source / streamごとにLoader Jobを持つ。既存のiPhoneは`screen-time-loader`を毎時15分に起動する。
各Jobのtask数とparallelismは1とし、さらにMotherDuckの期限付き`loader` leaseを取得する。
異なるsourceのLoaderもこのleaseを共有するため、scheduleは所要時間を踏まえてずらす。

1. 選択source / streamの対応schema版のprefixを全page listingする。
2. `ops.ingestion_metadata`で、同じGCS generationの取込に成功していないobjectを選ぶ。
3. `(observed_at, object_key)`の昇順に処理する。
4. [`analytics.md`](analytics.md)のtransaction契約でbaseと取込状態を更新する。
5. 1件でも未処理の失敗が残ればJobをnon-zeroで終了する。

poison objectは失敗として記録するが、自動削除や上書きを行わない。修正したdecoderをdeployした後に同じ
objectを再試行できるようにする。

## dbt

`pdp dbt`は`dbt run`に続けて`dbt test`を行う。deploy workflowはapplyと隔離preflightの成功後、次のいずれかの
場合に`dbt-runner`を実行する。

- Terraform planがdbt Jobの新規作成または再作成を含む。
- push差分に`dbt/`または`src/personal_data_platform/migrations/`の変更がある。
- 手動実行で`run_dbt=true`を指定した。

初回判定はTerraform plan上のJob作成によるもので、MotherDuck内のViewの有無を調べるものではない。
通常のLoaderとReconciliationは、martsがViewである間はdbtを起動しない。日常のbase更新にはdbt再実行は不要である。

## Reconciliation

監査もsource / streamごとのJobとして実行する。既存iPhoneの`reconciliation`は毎日04:30 Asia/Tokyoに起動する。
`reconciliation` leaseは同じroleの全sourceで共有し、Loaderとは別leaseである。

1. 選択source / streamのGCS objectとactiveな取込状態だけを照合し、未取込objectを同じadapterで再処理する。
2. 修復や並行Loaderが追加した取込済みkeyはGCSを再確認してから、Raw欠損と判定する。
3. 取込成功済みobjectの欠損をGCS作成時刻とsourceの保持期限で分類する。期限前は失敗、期限以降は予定された期限切れとする。
4. 対象scopeの`failed` / `loading` / 作成時刻不明の欠損と、保持期限にgrace日数を加えた時点で残るRawを失敗にする。
5. adapterの取得状態監査を実行する。iPhoneではmanifestとreceiptの欠損・24時間超過を確認する。
   manifestから外れたdeviceの残存Rawや古いreceiptはactive Collectorの異常に数えない。他sourceへ同じcontrol形式を要求しない。
6. 未取込・`failed` ingestionがないことを確認する。
7. 必須base / Viewの存在と各relationの代表`count(*)` queryを確認する。
8. 対象scopeの全監査項目と再処理が成功した後、監査記録を保存し、そのscope専用URLへ成功heartbeatを送る。

Jobの開始、retry開始、Loaderへの引き渡しだけでは成功heartbeatを送らない。監査結果を構築できた失敗では、
失敗object、欠損relation、stale receiptなどの構造化した結果を記録し、Jobをnon-zeroで終了する。
GCS listingやDB接続など結果構築前の失敗では監査行が残らない場合がある。この場合も成功heartbeatは送らず、
Jobの失敗とlogで原因を確認する。
DB更新と外部通知の順序、および配送後のcommit失敗に関する制約は[`analytics.md`](analytics.md)に従う。

## 監視

Cloud Logging / MonitoringとEmailで次を通知する。

- LoaderまたはReconciliation Jobの失敗
- decode失敗
- Collectorの成功scanが24時間以上ない状態
- 期限前のRaw欠損、未取込Rawの期限切れ、93日を超えたLifecycle未削除

93日判定はLifecycleの遅延を検知するこのprojectの運用SLOであり、GCSが90日ちょうどの削除時刻を保証するものではない。

Collector停止はReconciliationのreceipt検査で検出する。Job自体が起動しない場合の検出には、Healthchecks.io側で
毎日04:30 Asia/Tokyoのschedule、許容するgrace period、通知先を別途設定する。この未着監視はTerraformでは
作成しないため、通知が届くことを運用開始前に確認する。

新しいsource eventがないことだけを障害とみなさない。scan完了、GCS listing、取込状態、query成功を
組み合わせて判定する。

## Rebuild

本番MotherDuck databaseを直接空にして再構築してはならない。

GCS Rawはsourceの保持期限で永久削除されるため、全期間のrebuildは保証しない。iPhoneの保持期限は90日である。

Collectorのwrite-only ADCとは別に、Terraform output
`rebuild_operator_service_account`のread-only Service AccountをimpersonateするADCを作る。Collector用の
`CLOUDSDK_CONFIG`やADC fileを上書きしない。

```bash
export GOOGLE_CLOUD_PROJECT="<project-id>"
export GCS_BUCKET="${GOOGLE_CLOUD_PROJECT}-pdp-raw"
export MOTHERDUCK_DATABASE="<production-database>"
# non-dry-runではMOTHERDUCK_TOKENも安全なsecret sourceから環境へ渡す
export PDP_REBUILD_SERVICE_ACCOUNT_EMAIL="raw-rebuild-operator@${GOOGLE_CLOUD_PROJECT}.iam.gserviceaccount.com"
export CLOUDSDK_CONFIG="$HOME/Library/Application Support/personal-data-platform/gcloud-rebuild"
mkdir -p "$CLOUDSDK_CONFIG"
chmod 700 "$CLOUDSDK_CONFIG"
gcloud auth application-default login \
  --impersonate-service-account="$PDP_REBUILD_SERVICE_ACCOUNT_EMAIL"
export PDP_REBUILD_GOOGLE_APPLICATION_CREDENTIALS="$CLOUDSDK_CONFIG/application_default_credentials.json"
chmod 600 "$PDP_REBUILD_GOOGLE_APPLICATION_CREDENTIALS"
```

`pdp rebuild`はこのADCがimpersonated Service Account形式、現在user所有、mode `0600`、指定target一致であることを
確認し、実行中だけ`GOOGLE_APPLICATION_CREDENTIALS`として使う。CollectorのADCは変更しない。

1. `pdp rebuild --dry-run`でsource / stream、object数、subject数、scope数、GCS作成期間、保持日数、
   `full_history_rebuild_guaranteed=false`を表示する。iPhoneは従来のdevice数・segment数も表示する。
2. `pdp rebuild --target-db <scratch-db> --allow-partial-history`で空のscratch databaseを指定する。
   command内部でmigrationを適用し、GCSに現在残る全pageを1回だけlistingしてinventoryを固定する。各objectは
   選択source / streamのinventoryに記録したgenerationを指定し、`(observed_at, object_key)`順に再生する。途中でそのgenerationが
   Lifecycle削除された場合は別generationへ読み替えず失敗する。
   `ops.ingestion_metadata`とbaseの再構築後、同じscratch databaseへadapterのselectorで選択した`dbt run`と`dbt test`を実行する。
3. commandの成功後、productionと件数、stable key集合、代表martを手動で比較する。
4. 差分を確認した後、参照先を手動で切り替える。

target databaseがproductionと同一、既存tableを持つ、環境識別が不明、または
`--allow-partial-history`がない場合は開始前に停止する。MotherDuckの90日より古い履歴を失った場合、GCSからは
復元できない。

## 更新時の互換性

`003_source_ingestion.sql`は既存のiPhone履歴を残す追加migrationである。旧列を読めることと、旧runtimeが
新sourceを監査できることは別の条件である。旧Reconciliationは全sourceの取込状態をiPhoneのRawと照合するため、
新sourceを先に有効化してはならない。

1. iPhoneのRawキー、疑似化キー、Collectorのstate DB、LaunchAgent labelとCLIを維持したまま、対応runtimeを用意する。
2. 更新時は既存Loader・Reconciliationの定期実行を一時停止し、実行中Jobが終了したことを確認する。
   CollectorのRaw保存は継続できる。migrationは競合するJobがない状態で一度適用する。
3. 新runtimeを既存の全Jobへ反映し、隔離preflightとdbtを実行する。iPhoneの手動Loader・監査が成功したら
   定期実行を再開する。旧Jobの再試行が残っていないことも確認する。
4. 新sourceの取得処理、必要なsecret version、control権限と外部monitorを用意する。
   新sourceを登録したruntimeが動作している状態で、追加pipelineと取得処理を有効にする。
5. 新scopeの初回Raw、型付きbase、監査と停止検出を確認する。新sourceを有効にした後は、全件監査を行う
   旧runtimeへのrollbackは行わず、新scopeの取得・定期実行を止めて対応runtimeで修復する。

旧scope列と旧writer向けdefaultの削除は、別migrationとして扱う。

## CLI

`loader`、`reconciliation`、`rebuild`はsource / stream未指定なら既存iPhoneを選ぶ。
sourceのみ指定できるのは、そのsourceの登録streamが一つの場合だけである。streamだけの指定や未登録の組合せは拒否する。
`pdp dbt`は未指定時には全modelを実行する。

```text
pdp loader --source screen_time --stream app-in-focus
pdp reconciliation --source screen_time --stream app-in-focus
pdp rebuild --source screen_time --stream app-in-focus --dry-run
pdp dbt --source screen_time --stream app-in-focus
```


```text
pdp preflight
pdp loader
pdp dbt
pdp reconciliation
pdp rebuild --dry-run
pdp rebuild --target-db <scratch-db> --allow-partial-history
```
