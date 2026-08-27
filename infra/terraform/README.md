# GCP runtime

4つのCloud Run Jobs、Secret Manager resource、loader/reconciliationのScheduler、Cloud Logging/MonitoringのEmail通知を管理する。HTTP Service、Cloud Tasks、空のwebhook/fetch runtimeは作成しない。

## 初回構築

bootstrap outputのbucketをbackendへ渡す。

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

対象は`terraform output -json secret_ids`で確認する。すべてにversionを登録してから通常のapplyまたは`Terraform Deploy` workflowを実行する。preflight用B2 keyとMotherDuck tokenは本番用とは分離する。Email notification channelは適用後に届く確認メールで有効化する。

## 実行契約

- imageはtagではなく`@sha256:`付きdigestだけを受け付ける。
- `platform-preflight`は隔離したB2 prefixとMotherDuck test databaseを使い、deployごとにworkflowから実行する。
- `screen-time-loader`は毎時15分、`reconciliation`は毎日04:30に、どちらも`Asia/Tokyo`で起動する。
- `dbt-runner`はSchedulerから起動せず、dbt定義変更時または明示したdeploy時だけ実行する。
- 各Jobは専用Service Accountを持ち、必要なSecretだけを参照する。
- deploy identityの`actAs`は、Terraformが作成するJob用Service AccountとScheduler用Service Accountだけへ付与する。

手動実行例:

```bash
gcloud run jobs execute platform-preflight --region=asia-northeast1 --wait
gcloud run jobs execute screen-time-loader --region=asia-northeast1 --wait
gcloud run jobs execute dbt-runner --region=asia-northeast1 --wait
gcloud run jobs execute reconciliation --region=asia-northeast1 --wait
```
