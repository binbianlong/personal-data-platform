# GCP bootstrap

runtime Terraformより先に一度だけ適用し、state bucket、Artifact Registry、GitHub Actions用Workload Identity Federationを作成する。planとdeployは別のpool・provider・Service Accountを使う。plan providerは対象repository、`.github/workflows/terraform-plan.yml`、許可したeventへ、deploy providerは対象repository、`main`、`.github/workflows/terraform-deploy.yml`へ制限する。Service Account keyは作成しない。

## 適用

Project IAMを設定できる管理者identityで実行する。

```bash
cp terraform.tfvars.example terraform.tfvars
terraform init -lockfile=readonly
terraform fmt -check
terraform validate
terraform test
terraform plan -out=bootstrap.tfplan
terraform apply bootstrap.tfplan
```

state bucketは`prevent_destroy`、public access prevention、uniform bucket-level access、versioningを有効にしている。bootstrap自身のstateは、このbucketを作成する循環を避けるためローカルに残る。暗号化した安全な場所へバックアップし、Gitには追加しない。

apply後、outputを次のGitHub Actions Repository Variablesへ登録する。

| Output | Repository Variable |
| --- | --- |
| `state_bucket_name` | `TF_STATE_BUCKET` |
| `plan_workload_identity_provider` | `GCP_PLAN_WIF_PROVIDER` |
| `plan_service_account` | `GCP_PLAN_SERVICE_ACCOUNT` |
| `deploy_workload_identity_provider` | `GCP_DEPLOY_WIF_PROVIDER` |
| `deploy_service_account` | `GCP_DEPLOY_SERVICE_ACCOUNT` |
| `artifact_repository` | `GCP_ARTIFACT_REPOSITORY` |

併せて`GCP_PROJECT_ID`、`GCP_REGION`、`GCP_IMAGE_NAME`、初回planのfallbackとなるdigest URIを表す`GCP_RUNTIME_IMAGE_URI`、通知先の`GCP_ALERT_EMAIL`をRepository Variablesへ登録する。いずれもcredentialではなく、secret payloadは登録しない。runtime構築後のplanは、現在`screen-time-loader`へ適用済みのdigest URIをGoogle Cloudから読み取って使用する。

対応するWIF providerのRepository Variableが未設定の間、Terraform Plan / Deployのjobはskipされる。bootstrapとruntimeの初回準備を完了してから登録する。Deployは登録後、対象pathを変更するmainへのpushでも起動する。
