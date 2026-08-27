# GitHub repository ruleset

default branchへの変更をPull Request経由に限定し、最新のdefault branchを取り込んだ状態でGitHub Actionsの`CI` jobが成功することを必須にする。

## 検証

認証情報を使わずに構文、provider schema、rulesetの契約を検証できる。

```bash
terraform init -backend=false
terraform fmt -check
terraform validate
terraform test
```

## 適用

workflowを含むbranchでPull Requestを作成し、`CI` jobが一度成功した後に適用する。対象repositoryのAdministration権限を持つfine-grained personal access tokenを環境変数で渡す。

```bash
export GITHUB_TOKEN="..."
terraform init
terraform plan -out=ruleset.tfplan
terraform apply ruleset.tfplan
```

適用後はrulesetがactiveであることをGitHub APIから確認する。

```bash
gh api repos/binbianlong/personal-data-platform/rulesets \
  --jq '.[] | {name, enforcement}'
```

tokenはtfvarsやTerraform stateへ保存しない。CIは管理tokenを受け取らず、rulesetの検証だけを行う。

backendを指定していないため、stateは適用した環境のローカルファイルに保存される。stateをGitへ追加せず、安全に保管する。
