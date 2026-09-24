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
push差分が`Dockerfile`、`.dockerignore`、`pyproject.toml`、`src/`、`dbt/`を含まない場合は、
`screen-time-loader`へ現在deploy済みのdigestを再利用し、build/pushを省略する。初回構築、比較元のないpush、
手動実行ではbuildする。Job一覧の取得失敗、無効な比較元、再利用対象のdigest不正はdeployを停止する。
アプリ変更のdeployが失敗した後にinfraだけを更新しても、そのアプリ変更は再buildされない。
失敗したアプリ変更を反映するときはTerraform Deployを手動実行する。

apply前に、各Jobの現行digestへ`deployed-job-<Job名>`、次のdigestへ`deployed-candidate`タグを付ける。
cleanupの保持対象となるタグであり、Terraformには引き続きdigestを渡す。現行Jobの保持タグを先に更新し、
途中失敗で複数digestが稼働していても保護してからcandidateタグを移す。タグ付け失敗時はapplyしない。
手動でJobのimageを変更するときも、変更先へ`deployed-`接頭辞の保持タグを先に付ける。
Jobを廃止した後の`deployed-job-<Job名>`タグは、参照がないことを確認して手動で除去する。

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
2. `ops.ingestion_metadata`で、同じGCS generationの取込に成功していないobjectを選ぶ。sourceがparser versionを指定する場合は旧parser成功分も対象にする。
3. `(observed_at, object_key)`の昇順に処理する。
4. [`analytics.md`](analytics.md)のtransaction契約でbaseと取込状態を更新する。
5. 1件でも未処理の失敗が残ればJobをnon-zeroで終了する。

LoaderのCloud Run Task自動リトライは無効（`max_retries = 0`）とし、失敗した実行を成功扱いにせず、
未処理Rawの再処理は次の毎時起動に任せる。異常終了でleaseが残った場合は、取得から125分の期限が
切れた後の定期起動で再開する。期限内の定期起動は処理せず成功扱いでスキップする。

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
   sourceがparser versionを指定する場合は旧parserの成功記録も未取込扱いにし、修復後も現在のversionを確認する。
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

## DBの初期化と更新

初回は空のMotherDuck databaseを指定する。`pdp loader`、`pdp reconciliation`、`pdp dbt`は処理開始時に
package内のSQLを順番に適用する。`001_initial.sql`で運用table、Screen Timeのイベント・補助状態・
分析入口Viewを作成し、`ops.schema_migration`へchecksumと適用日時を記録する。
再実行では適用済みSQLをskipし、checksumが変わっていれば停止する。

1. Schedulerを停止した状態で接続先とsecretを設定し、隔離preflightを成功させる。
2. `pdp dbt --source screen_time --stream app-in-focus`で初期スキーマと分析Viewを作成・検証する。
3. CollectorによるRaw保存を確認してLoaderを手動実行し、Reconciliationで取込と分析relationを確認する。
4. 初回の取込・監査が成功した後に定期実行を有効にする。

既存DBの自動変換や削除は行わない。異なる初期SQLを適用した開発用DBは引き継がず、別の空DBを用意する。
適用履歴を手で変更してchecksum検証を回避しない。

今後のスキーマ変更は`002`以降のforward migrationで追加し、適用済みSQLは書き換えない。
更新時はLoader・Reconciliationの定期実行を止め、実行中のLoader・Reconciliation・dbt Jobが終了してから
対応runtimeを全Jobへ反映する。migration、dbt、手動Loader・監査の成功を確認して定期実行を再開する。
新sourceは対応runtime、取得権限・secret・monitorを揃えてから有効にする。

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
