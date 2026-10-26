# GitHub repository ruleset

Pull Requestと`CI`成功をdefault branchのマージ条件にする。
workflowを含むPRで`CI`が一度成功した後、repositoryのAdministration権限を持つfine-grained tokenで適用する。

```bash
export GITHUB_TOKEN="..."
terraform init
terraform plan -out=ruleset.tfplan
terraform apply ruleset.tfplan
gh api repos/binbianlong/personal-data-platform/rulesets \
  --jq '.[] | {name, enforcement}'
unset GITHUB_TOKEN
```

stateはローカルに保存される。state・保存したplan・tokenをGitへ追加しない。
CIでは認証情報を使わず、`terraform validate`とmock providerの`terraform test`だけを行う。
