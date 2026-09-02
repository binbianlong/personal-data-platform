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

`screen-time-loader` Cloud Run Jobを毎時15分に起動する。task数とparallelismはともに1とし、さらに
MotherDuckの期限付き`ops.job_lock`を取得して多重実行を防ぐ。

1. GCSの対象prefixを全page listingする。
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

`pdp reconciliation`を毎日04:30 Asia/Tokyoに実行する。

1. GCS objectとactiveな`ops.ingestion_metadata`を照合し、未取込objectをLoader契約で再処理する。
2. 修復や並行Loaderが追加した取込済みkeyはGCSを再確認してから、Raw欠損と判定する。
3. 取込成功済みobjectの欠損をGCS作成時刻で分類する。90日前は失敗、90日以降は予定された期限切れとする。
4. `failed` / `loading` / 作成時刻不明の欠損と、93日を超えて残るRawを失敗にする。
5. 最新active-device manifestの欠損・24時間超過と、manifest内deviceのscan receipt欠損・24時間超過を確認する。
   manifestから外れたdeviceの残存Rawや古いreceiptはactive Collectorの異常に数えない。
6. 未取込・`failed` ingestionがないことを確認する。
7. 必須base / Viewの存在と各relationの代表`count(*)` queryを確認する。
8. 全監査項目と必要な再処理が成功した後、監査記録を保存し、Healthchecks.ioへ成功heartbeatを送る。

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

GCS Rawはuploadから90日で永久削除されるため、全期間のrebuildは保証しない。

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

1. `pdp rebuild --dry-run`で対象prefixのobject数、device数、segment数、GCS作成期間、
   `retention_days=90`、`full_history_rebuild_guaranteed=false`を表示する。
2. `pdp rebuild --target-db <scratch-db> --allow-partial-history`で空のscratch databaseを指定する。
   command内部でmigrationを適用し、GCSに現在残る全pageを1回だけlistingしてinventoryを固定する。各objectは
   inventoryに記録したgenerationを指定し、`(observed_at, object_key)`順に再生する。途中でそのgenerationが
   Lifecycle削除された場合は別generationへ読み替えず失敗する。
   `ops.ingestion_metadata`とbaseの再構築後、同じscratch databaseへ`dbt run`と`dbt test`を実行する。
3. commandの成功後、productionと件数、stable key集合、代表martを手動で比較する。
4. 差分を確認した後、参照先を手動で切り替える。

target databaseがproductionと同一、既存tableを持つ、環境識別が不明、または
`--allow-partial-history`がない場合は開始前に停止する。MotherDuckの90日より古い履歴を失った場合、GCSからは
復元できない。

## CLI

```text
pdp preflight
pdp loader
pdp dbt
pdp reconciliation
pdp rebuild --dry-run
pdp rebuild --target-db <scratch-db> --allow-partial-history
```
