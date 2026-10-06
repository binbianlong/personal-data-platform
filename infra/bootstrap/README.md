# GCP bootstrap

runtimeより先にstate・`us-west1` registry・GitHub WIF・storage roleを作る。
plan/deployは別identityで、repository・workflow・event/mainへ制限し、SA keyを発行しない。

```bash
terraform init -lockfile=readonly
terraform fmt -check
terraform validate
terraform test
terraform plan -var-file=<非公開tfvars> -out=bootstrap.tfplan
terraform apply bootstrap.tfplan
```

stateはpublic access prevention・uniform access・versioning・prevent_destroyで保護する。
bootstrap自身のstateはローカルに保存してbackupする。runtime backendには`state_bucket_name_west`を指定し、既存state backupは保持する。

registryは`runtime_west`。30日超のimageを削除し、直近5版とdeployedタグを保持する。
CIは現行Job・receiver・指定rollback・candidate digestをapply前に保護する。rollbackにもRaw v3対応imageを使う。

`state_bucket_name_west`・`artifact_repository`・plan/deploy WIF provider/SAのoutputをrepository variablesへ設定する。
西部imageのfallbackは`GCP_WEST_RUNTIME_IMAGE_URI`。secret pinと運用フラグは[runtime Terraform](../terraform/README.md)に従う。
Terraformのstorage roleはbucket metadata/IAMだけで、object dataを読まない。
