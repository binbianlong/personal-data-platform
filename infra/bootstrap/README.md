# GCP bootstrap

[runtime](../terraform/README.md)より先にstate・registry・GitHub WIFを作る。

```bash
terraform init -lockfile=readonly
terraform plan -var-file=<非公開tfvars> -out=bootstrap.tfplan
terraform apply bootstrap.tfplan
```

bootstrap自身のstateはローカルに保存してbackupする。runtime backendにはoutput `state_bucket_name`を指定し、既存state backupは保持する。
`artifact_repository`・plan/deploy WIF provider/SAのoutputをrepository variablesへ設定する。

registryは作成から1日超のimageを削除し、各imageの最新1版を保持する。
削除済みimageへrollbackする場合は再build・登録が必要。
