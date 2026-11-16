# GCP bootstrap

[runtime](../terraform/README.md)より先にstate・registry・GitHub WIFを作る。

```bash
terraform init -lockfile=readonly
terraform plan -var-file=<非公開tfvars> -out=bootstrap.tfplan
terraform apply bootstrap.tfplan
```

bootstrap自身のstateはローカルに保存してbackupする。runtime backendにはoutput `state_bucket_name`を指定し、既存state backupは保持する。
`artifact_repository`・plan/deploy WIF provider/SAのoutputをrepository variablesへ設定する。

registryは作成から1日超のimageを削除し、最新1版と `deployed-` で始まるタグの付いた版を保持する。
初回はbootstrapを適用してから[Deploy](../../docs/platform/operations.md#deploy)を使う。
Deployは稼働中の版と新しい版を保護してから切り替え、完了後に使わなくなった版の保護を外す。
直前のリリースは `deployed-rollback` で確認できる。削除済みの版に戻す場合は再build・登録が必要。
