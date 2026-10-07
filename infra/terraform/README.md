# Runtime Terraform

`us-west1`のreceiver・Pub/Sub・毎時/日次Job・Scheduler・監視・Logging・Raw/preflight bucketを管理する。
先に[bootstrap](../bootstrap/README.md)を適用し、`terraform.tfvars.example`から非公開入力を作る。

```bash
terraform init -backend-config="bucket=<西部state bucket>" -input=false
terraform fmt -check
terraform validate
terraform test
terraform plan -var-file=<非公開tfvars> -out=runtime.tfplan
terraform apply runtime.tfplan
```

`west_image_uri`には西部registryのdigest、MotherDuck production/preflightには独立DBを指定する。
backend bucketはGitHubの`TF_STATE_BUCKET`へ設定する。接続先・実行順・監視・復旧は[Platform運用](../../docs/platform/operations.md)に従う。
通常は両Schedulerとnative警報が有効。`west_schedulers_enabled=false`とGitHubの`PDP_WEST_SCHEDULERS_ENABLED=false`で一時停止し、Fitbit処理も延期する。
Logging変更時は既存_Defaultの除外を`west_logging_exclusions`と`PDP_WEST_LOGGING_EXCLUSIONS`へ引き継ぐ。

## Secretと資格情報

secretはglobal resource＋単一`us-west1` replica。Terraformはcontainerだけを作り、payloadは外部で登録する。
`west_secret_versions`は正の整数文字列へ固定し、`latest`・alias・未知のkeyを拒否する。
GitHubの`PDP_WEST_SECRET_VERSIONS`にも同じmapをJSONで設定する。`heartbeat_config`のpayloadは
`PDP_HEARTBEAT_CONFIG`へ渡す`{"daily":"https://..."}`形式で、HTTPS URLを使う。

| key | Secret Manager ID | 利用先 |
|---|---|---|
| `motherduck_token` | `pdp-west-motherduck-token` | hourly・daily |
| `motherduck_preflight_token`（省略可） | `pdp-west-motherduck-preflight-token` | 手動preflightのみ |
| `fitbit_oauth_config` | `pdp-west-fitbit-oauth-config` | hourly・daily |
| `fitbit_webhook_config` | `pdp-west-fitbit-webhook-config` | receiver |
| `heartbeat_config` | `pdp-west-heartbeat-config` | daily |

通常runtimeはpreflight以外の4 pinを必須とする。更新・rollbackはmapの番号を変更し、
切替完了前に参照中versionを無効化・破棄しない。秘密値をtfvars・state・GitHub variablesへ含めない。
JSON fieldは[Fitbit環境変数](../../docs/sources/fitbit/operations.md#環境変数)、権限境界は[セキュリティ](../../docs/platform/security.md)を参照する。
