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

初回およびIAM変更後は`pdp preflight`を専用B2 test prefixとMotherDuck test databaseへ接続して実行し、
次を確認する。

- B2 test objectのwrite / read / list / delete round trip
- MotherDuck test databaseでの一時table作成 / write / read / delete

B2ではupload応答の`VersionId`を使って作成したversionを削除する。`VersionId`を取得できない場合や削除に
失敗した場合は成功扱いにせず、検証prefixに残ったobject versionを確認する。

deploy workflowからCloud Run preflightが完了することで、WIF deploy、image取得、JobのSecret注入、外部通信も
合わせて確認する。preflight用B2 keyとMotherDuck tokenは本番用と分離し、production prefixまたはproduction
databaseへ書き込まない。

## Loader

`screen-time-loader` Cloud Run Jobを毎時15分に起動する。task数とparallelismはともに1とし、さらに
MotherDuckの期限付き`ops.job_lock`を取得して多重実行を防ぐ。

1. B2の対象prefixを全page listingする。
2. `ops.ingestion_metadata`で未成功objectを選ぶ。
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

1. B2 objectと`ops.ingestion_metadata`を照合し、未取込objectをLoader契約で再処理する。
2. 修復や並行Loaderが追加した取込済みkeyはB2を再確認してから、Raw欠損と判定する。
3. device別collector scan receiptの欠損・24時間超過と、未取込・Raw欠損・`failed` ingestionがないことを確認する。
4. 必須base / Viewの存在と各relationの代表`count(*)` queryを確認する。
5. 全監査項目と必要な再処理が成功した後、監査記録を保存し、Healthchecks.ioへ成功heartbeatを送る。

Jobの開始、retry開始、Loaderへの引き渡しだけでは成功heartbeatを送らない。監査結果を構築できた失敗では、
失敗object、欠損relation、stale receiptなどの構造化した結果を記録し、Jobをnon-zeroで終了する。
B2 listingやDB接続など結果構築前の失敗では監査行が残らない場合がある。この場合も成功heartbeatは送らず、
Jobの失敗とlogで原因を確認する。
DB更新と外部通知の順序、および配送後のcommit失敗に関する制約は[`analytics.md`](analytics.md)に従う。

## 監視

Cloud Logging / MonitoringとEmailで次を通知する。

- LoaderまたはReconciliation Jobの失敗
- decode失敗
- Collectorの成功scanが24時間以上ない状態

Collector停止はReconciliationのreceipt検査で検出する。Job自体が起動しない場合の検出には、Healthchecks.io側で
毎日04:30 Asia/Tokyoのschedule、許容するgrace period、通知先を別途設定する。この未着監視はTerraformでは
作成しないため、通知が届くことを運用開始前に確認する。

新しいsource eventがないことだけを障害とみなさない。scan完了、B2 listing、取込状態、query成功を
組み合わせて判定する。

## Rebuild

本番MotherDuck databaseを直接空にして再構築してはならない。

1. `pdp rebuild --dry-run`で対象prefixのobject数、device数、segment数、期間を表示する。
2. `pdp rebuild --target-db <scratch-db>`で空のscratch databaseを指定する。
   command内部でmigrationを適用し、B2の全pageをlistingして`(observed_at, object_key)`順に再生する。
   `ops.ingestion_metadata`とbaseの再構築後、同じscratch databaseへ`dbt run`と`dbt test`を実行する。
3. commandの成功後、productionと件数、stable key集合、代表martを手動で比較する。
4. 差分を確認した後、参照先を手動で切り替える。

target databaseがproductionと同一、既存tableを持つ、または環境識別が不明な場合は開始前に停止する。

## CLI

```text
pdp preflight
pdp loader
pdp dbt
pdp reconciliation
pdp rebuild --dry-run
pdp rebuild --target-db <scratch-db>
```
