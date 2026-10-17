# MotherDuck本番設定

本番DB `personal_data_platform`（TRANSIENT・履歴保持0日）、restricted・discoverable・automaticの
share `pdp_analytics`、user `hamaberyu`へのREAD権限を管理する。
テーブル・Viewはmigrationとdbtが管理する。計算設定・アカウント・token・検証DBは管理しない。
DBとshareは`prevent_destroy`で保護する。

## 検証

mock providerによる検証にはMotherDuck・GCPの認証情報は不要である。

```bash
terraform -chdir=infra/motherduck init -backend=false -input=false -lockfile=readonly
terraform -chdir=infra/motherduck fmt -check
terraform -chdir=infra/motherduck validate
terraform -chdir=infra/motherduck test
```

## 認証とstate

SQL操作はowner `pdp_west_prod`の既存writer tokenを使う。macOS Keychainから環境変数へ渡し、
tokenをソース・tfvars・ログへ保存しない。管理者tokenは不要である。

```bash
export MOTHERDUCK_TOKEN="$(security find-generic-password \
  -s personal-data-platform -a screen-time-motherduck-token -w)"
```

GCS backendは既存のApplication Default Credentialsを使う。stateは既存bucketの
`personal-data-platform/motherduck` prefixへ保存し、runtime stateと分離する。

```bash
terraform -chdir=infra/motherduck init -input=false -lockfile=readonly \
  -backend-config="bucket=health-data-pipeline-503813-personal-data-platform-tfstate-west"
```

## 初回import

既存DB・share・grantをstateへ取り込む。importでデータやshare URLを変更しない。

```bash
terraform -chdir=infra/motherduck import motherduck_database.production personal_data_platform
terraform -chdir=infra/motherduck import motherduck_share.analytics pdp_analytics
terraform -chdir=infra/motherduck import motherduck_share_grant.analytics_reader pdp_analytics/user/hamaberyu
```

続けて通常のplan・applyを行い、追加・変更・削除がすべて0件であることを確認する。
置換・更新・削除の差分が出た場合はapplyせず、設定と実態の差を確認する。
DB UUID・share URL・公開設定と全付与先を照合し、分析userからmartを参照できることも確認する。

## 通常のplan・apply

```bash
terraform -chdir=infra/motherduck plan -out=motherduck.tfplan
terraform -chdir=infra/motherduck apply motherduck.tfplan
terraform -chdir=infra/motherduck plan -detailed-exitcode
unset MOTHERDUCK_TOKEN
```

最後のplanの終了コードは、差分なしが0、エラーが1、差分ありが2となる。
state・保存したplanはGitへ追加しない。share grantリソースは指定した1件を管理し、管理外のgrantは自動削除しない。
CIは構文と契約だけを検証し、MotherDuckへのplan・applyは手動で行う。
