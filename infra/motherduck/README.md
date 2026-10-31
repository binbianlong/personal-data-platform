# MotherDuck本番設定

本番DB `personal_data_platform`、分析share `pdp_analytics`、user `hamaberyu`へのREAD権限を管理する。
share全体を個人の分析アカウントへ公開するため、base・opsも参照できる。
DBのsnapshot履歴は保持しない。復旧できる範囲は[Rebuild](../../docs/platform/operations.md#rebuild)を参照する。

## 認証とstate

SQL操作はowner `pdp_west_prod`のwriter token、GCS backendは既存のApplication Default Credentialsを使う。
stateはruntimeと別の`personal-data-platform/motherduck` prefixへ保存する。

```bash
export MOTHERDUCK_TOKEN="$(security find-generic-password \
  -s personal-data-platform -a iphone-screen-time-motherduck-token -w)"
terraform -chdir=infra/motherduck init -input=false -lockfile=readonly \
  -backend-config="bucket=health-data-pipeline-503813-personal-data-platform-tfstate-west"
```

## 通常のplan・apply

```bash
terraform -chdir=infra/motherduck plan -out=motherduck.tfplan
terraform -chdir=infra/motherduck apply motherduck.tfplan
terraform -chdir=infra/motherduck plan -detailed-exitcode
unset MOTHERDUCK_TOKEN
```

意図しないDB・shareの置換や削除があればapplyしない。share URL・公開範囲・付与先も確認する。
share grantは指定した1件だけを管理し、管理外のgrantは自動削除しない。
state・保存したplan・tokenをGitやログへ残さない。CIはmock providerの検証だけで、適用は手動で行う。
