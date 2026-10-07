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
bootstrap自身のstateはローカルに保存してbackupする。runtime backendにはoutput `state_bucket_name`を指定し、既存state backupは保持する。

registryは`runtime_west`。30日超のimageを削除し、直近5版とdeployedタグを保持する。
CIは現行Job・receiver・指定rollback・candidate digestをapply前に保護する。rollbackにもRaw v3対応imageを使う。

`state_bucket_name`・`artifact_repository`・plan/deploy WIF provider/SAのoutputをrepository variablesへ設定する。
imageのfallbackは`GCP_WEST_RUNTIME_IMAGE_URI`。secret pinと一時停止は[runtime Terraform](../terraform/README.md)に従う。
Terraformのstorage roleはbucket metadata/IAMだけで、object dataを読まない。
