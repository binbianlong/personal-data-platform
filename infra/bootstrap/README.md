# GCP bootstrap

runtime Terraformより先に適用し、state bucket、`us-central1`のArtifact Registry、GitHub Actions用Workload Identity Federation、runtime storage用custom roleを作成する。planとdeployは別のpool・provider・Service Accountを使う。plan providerは対象repository、`.github/workflows/terraform-plan.yml`、許可したeventへ、deploy providerは対象repository、`main`、`.github/workflows/terraform-deploy.yml`へ制限する。Service Account keyは作成しない。

## 適用

Project IAMを設定できる管理者identityで実行する。既存repositoryへcleanup policyを初適用する前に、
後述の「Imageの保持と旧repositoryの廃止」に従って現行imageの保持タグを確認する。

```bash
cp terraform.tfvars.example terraform.tfvars
terraform init -lockfile=readonly
terraform fmt -check
terraform validate
terraform test
terraform plan -out=bootstrap.tfplan
terraform apply bootstrap.tfplan
```

state bucketは`prevent_destroy`、public access prevention、uniform bucket-level access、versioningを有効にしている。既存stateを移動しないため、`state_bucket_location`のdefaultは従来どおり`ASIA`である。GCS RawやArtifact Registryの`us-central1`移行とは分けて扱い、既存bucketのlocationを変更しない。bootstrap自身のstateは、このbucketを作成する循環を避けるためローカルに残る。暗号化した安全な場所へバックアップし、Gitには追加しない。

以前の`google_artifact_registry_repository.runtime`がstateにある場合、最初のplanはそのAsia repositoryを「削除せず管理対象から外す」と表示し、`google_artifact_registry_repository.runtime_us`を`us-central1`へ作成する。旧repositoryとimageは自動削除しない。planに旧repositoryのdestroyが出る場合はapplyしない。

custom roleは次の境界だけを持つ。

- Collector Raw creator: object createのみ。
- Collector control writer: object create/deleteのみ。runtime側のIAM conditionでlatest receipt prefixと固定
  active-device manifest keyへ限定する。
- preflight object operator: 専用bucket内のcreate/get/list/deleteのみ。
- runtime bucket manager: bucket metadataとIAMの管理のみ。object read/write権限は持たない。
- runtime bucket reader: Terraform planでbucket metadataとIAM policyをrefreshする`storage.buckets.get`と
  `storage.buckets.getIamPolicy`だけ。

runtime deploy identityへ付与するbucket manager roleは、runtime Terraformがbucket、Lifecycle、authoritative IAM policyを管理するために使う。plan identityはbucket readerでbucket metadataとIAM policyを読む。このcustom roleにobject read / listは含めず、project-level Security Reviewerも付与しない。runtime bucketのobject accessはruntime側のauthoritative IAM policyに含めない。

apply後、outputを次のGitHub Actions Repository Variablesへ登録する。

| Output | Repository Variable |
| --- | --- |
| `state_bucket_name` | `TF_STATE_BUCKET` |
| `plan_workload_identity_provider` | `GCP_PLAN_WIF_PROVIDER` |
| `plan_service_account` | `GCP_PLAN_SERVICE_ACCOUNT` |
| `deploy_workload_identity_provider` | `GCP_DEPLOY_WIF_PROVIDER` |
| `deploy_service_account` | `GCP_DEPLOY_SERVICE_ACCOUNT` |
| `artifact_repository` | `GCP_ARTIFACT_REPOSITORY` |

併せて`GCP_PROJECT_ID`、`GCP_IMAGE_NAME`、初回planのfallbackとなるdigest URIを表す`GCP_RUNTIME_IMAGE_URI`、通知先の`GCP_ALERT_EMAIL`、Mac operatorを表す`GCP_COLLECTOR_IMPERSONATOR_MEMBER`をRepository Variablesへ登録する。最後の値は`user:operator@example.com`または`group:operators@example.com`形式にし、write-only Collectorとread-only Rebuildの2つの専用Service Accountをimpersonateできるprincipalになる。workflowとTerraformのregionは`us-central1`へ固定しているため、`GCP_REGION`は使わない。いずれもcredentialではなく、secret payloadは登録しない。runtime構築後のplanは、現在`screen-time-loader`へ適用済みのdigest URIをGoogle Cloudから読み取って使用する。

対応するWIF providerのRepository Variableが未設定の間、Terraform Plan / Deployのjobはskipされる。今回の移行では、先にbootstrapを再適用してUS repositoryとcustom roleを作成し、その後runtime planへ進む。Deployは登録後、対象pathを変更するmainへのpushでも起動する。

## Imageの保持と旧repositoryの廃止

`us-central1`のrepositoryは、作成から30日を超えたimageをSHAタグの有無にかかわらず削除する。
各image packageの最新5世代と、`deployed-`接頭辞のタグが付いたimageは保持する。
KEEPはDELETEより優先され、実際の削除はArtifact Registryの非同期処理で行われる。
[cleanup policyの仕様](https://cloud.google.com/artifact-registry/docs/repositories/cleanup-policy)を参照する。

既存repositoryへ初めて適用する場合は、先に保持タグを付けるdeploy workflowを反映し、
現在の全Jobのimageに`deployed-job-<Job名>`が付いていることを確認する。手動で付ける場合も、
Jobごとの実際のdigestを読み取り、`gcloud artifacts docker tags add`で同じタグを付ける。
その後bootstrapをplanし、repositoryのcleanup policy以外に意図しない変更がないことを確認してapplyする。
runtimeのdeploy workflowはbootstrapの変更をapplyしない。

旧Asia repositoryの削除は手動で行う。対象project・location・repository IDを明示し、
Cloud Run Jobs、Servicesの有効なrevision、実行中Job、その他の利用元やbuild/deploy設定、
GitHub Variablesのfallback imageが旧repositoryを参照していないことを確認する。
旧imageを使ったrollbackが不要で、us-central1の代替imageを利用できることを確認できた場合だけ削除する。

```bash
gcloud artifacts repositories delete "<old-repository-id>" \
  --project="<project-id>" --location="<old-asia-location>"
```

削除後は対象locationのrepository一覧で不在を確認する。`removed`ブロックは以前のstateからの移行用に残す。
