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

registryは`runtime`。作成から1日超のimageをtagの有無によらず削除し、各imageの最新1版を保持する。
1日以内の版は複数残る。削除と容量反映は定期処理で行われ、OCIの子manifestも親manifestの削除後に整理される。

`state_bucket_name`・`artifact_repository`・plan/deploy WIF provider/SAのoutputをrepository variablesへ設定する。
imageのfallbackは`GCP_RUNTIME_IMAGE_URI`。secret pinと一時停止は[runtime Terraform](../terraform/README.md)に従う。
現行digestが削除済みの場合は手動deployでimageを再buildする。
Terraformのstorage roleはbucket metadata/IAMだけで、object dataを読まない。
