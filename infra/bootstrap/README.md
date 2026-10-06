# GCP bootstrap

runtimeより先にstate・us-west1 registry・GitHub WIF・専用storage roleを作る。
plan/deployは別identityで、repository・workflow・event/mainへ制限する。Service Account keyは作らない。

```bash
terraform init -lockfile=readonly
terraform fmt -check
terraform validate
terraform test
terraform plan -var-file=<非公開tfvars> -out=bootstrap.tfplan
terraform apply bootstrap.tfplan
```

stateはpublic access prevention、uniform access、versioning、prevent_destroyを使う。
既存ASIA state bucketを保全し、西部runtime backendにはstate_bucket_name_westを指定する。
bootstrap自身のstateはローカルに保存し、安全な場所へbackupする。

通常registryはruntime_westだけ。us-central1の旧repositoryは、旧Job/Serviceの参照を外し、
Raw v3対応releaseと復元を確認してから撤去する。旧imageをRaw v3のrollbackへ使わない。
西部registryは30日超のimageを削除し、直近5版とdeployed-タグを保持する。
CIは現行Job・receiver・指定rollback・candidateのdigestをapply前に保護する。

state_bucket_name_west、artifact_repository、plan/deploy WIF providerとSAのoutputを
repository variablesへ設定する。西部imageのfallbackはGCP_WEST_RUNTIME_IMAGE_URI、
数値secret pinsはPDP_WEST_SECRET_VERSIONSを使う。
PDP_WEST_ENABLED、PDP_WEST_SCHEDULERS_ENABLED、PDP_WEST_LOGGING_ENABLEDを実環境と揃える。
secret payloadをGitHub variables・tfvars・stateへ保存しない。

CollectorはRaw createだけ、controlは限定prefixのcreate/deleteだけ。
手動preflightは専用bucketのcreate/get/list/delete、rebuildはRaw read-only。
Terraform用storage roleはbucket metadata/IAMだけで、object dataを読まない。
