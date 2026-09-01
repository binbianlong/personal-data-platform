# GCP runtime

4つのCloud Run Jobs、GCS Raw/preflight bucket、3つのactive Secret Manager resource、loader/reconciliationのScheduler、Cloud Logging/MonitoringのEmail通知を管理する。HTTP Service、Cloud Tasks、空のwebhook/fetch runtimeは作成しない。Cloud Run、GCS、Artifact Registryは`us-central1`に固定し、Schedulerの時刻解釈だけは`Asia/Tokyo`を使う。

## 初回構築

先に`infra/bootstrap`を再適用し、US Artifact Registryとstorage custom roleを作る。bootstrap outputのstate bucketをbackendへ渡す。既存state bucketのlocationは変更しない。

```bash
terraform init -backend-config="bucket=$TF_STATE_BUCKET" -lockfile=readonly
terraform fmt -check
terraform validate
terraform test
```

Secret payloadをTerraformへ渡すとstateへ残るため、Terraformはsecret containerだけを作る。初回はcontainerを先に適用する。

```bash
terraform apply -target=google_secret_manager_secret.runtime
```

その後、各値を標準入力からSecret Managerへ登録する。値はcommand line、tfvars、GitHub Actions logsへ含めない。

```bash
printf '%s' "$SECRET_VALUE" | gcloud secrets versions add SECRET_ID --data-file=-
```

対象は`terraform output -json secret_ids`で確認する。対象は本番MotherDuck token、preflight MotherDuck token、Healthchecks URLの3つだけである。すべてにversionを登録してから通常のapplyまたは`Terraform Deploy` workflowを実行する。Email notification channelは適用後に届く確認メールで有効化する。

## B2からのstate移行

以前Terraform管理していた6つのB2 Secret Manager containerは、`moved`と`removed { destroy = false }`でstateから外す。最初のruntime planで次を確認する。

- B2 secretは`will no longer be managed ... but will not be destroyed`と表示される。
- Cloud Run JobからB2環境変数とB2 secret accessが削除される。
- B2 secret container自体のdestroyは表示されない。

条件を満たさないplanはapplyしない。残したB2 secret、credential、bucketはGCSの実運用受入後に別作業で失効・削除する。このTerraform applyはB2のリモートデータを削除しない。

## GCS保持と権限

- `${project_id}-pdp-raw`: `STANDARD`、flat namespace、UBLA、public access prevention、`force_destroy=false`、`prevent_destroy=true`。
- Raw Lifecycleは`raw/screen_time/v1/`かつ`.segb.gz`だけを作成から60日でDeleteする。60日間はStandardのまま保持し、Coldline / Archiveへ遷移しない。Soft Deleteは0秒、Object VersioningとAutoclassは無効で、削除後は復元できない。Lifecycle実行は非同期で、60日ちょうどの削除を保証するprovider SLAはない。
- device別receipt JSONと`_control/collector/active.json` manifestはsuffix条件に合わないため60日削除の対象外で、最新objectを上書きする。
- `${project_id}-pdp-preflight`: 本番と別の`STANDARD` bucket。Soft Deleteは0秒で、`test/preflight/`の孤立objectを1日で削除する。
- Raw bucket IAM policyとpreflight bucket IAM policyはauthoritative管理する。手動で追加したbucket bindingは次回applyで削除される。
- Mac CollectorはRaw segmentのcreateとlatest receipt・active-device manifestのcreate/deleteだけを持ち、read/listは持たない。`collector_impersonator_member`は専用Collector Service Accountとread-only Rebuild Service AccountだけをToken Creatorとしてimpersonateできる。
- Loader、Reconciliation、Rebuild Service AccountはRaw bucketのobject Viewerを持つ。preflight Jobはpreflight bucketだけのcreate/get/list/deleteを持ち、dbt JobはGCS権限を持たない。
- ローカルrebuildは`rebuild_operator_service_account` outputをtargetにした別ADCを使い、Collector ADCを上書きしない。

## 実行契約

- imageはtagではなく`@sha256:`付きdigestだけを受け付ける。
- `platform-preflight`は隔離したGCS bucketとMotherDuck test databaseを使い、deployごとにworkflowから実行する。
- `screen-time-loader`は毎時15分、`reconciliation`は毎日04:30に、どちらも`Asia/Tokyo`で起動する。
- `dbt-runner`はSchedulerから起動せず、初回構築、dbt定義・SQL migration変更時、または`run_dbt=true`を指定したdeploy時に実行する。初回はapply前のTerraform planでdbt Jobの新規作成を検出し、applyと隔離preflightが成功した後にmodelを作成する。Job再作成も同じ扱いとする。
- 各Jobは専用Service Accountを持ち、必要なSecretだけを参照する。
- deploy identityの`actAs`は、Terraformが作成するJob用Service AccountとScheduler用Service Accountだけへ付与する。

手動実行例:

```bash
gcloud run jobs execute platform-preflight --region=us-central1 --wait
gcloud run jobs execute screen-time-loader --region=us-central1 --wait
gcloud run jobs execute dbt-runner --region=us-central1 --wait
gcloud run jobs execute reconciliation --region=us-central1 --wait
```
