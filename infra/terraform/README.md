# Runtime Terraform

[bootstrap](../bootstrap/README.md)の適用後、`terraform.tfvars.example`から非公開入力を作る。
接続先・Deploy・停止・復旧は[Platform運用](../../docs/platform/operations.md)を参照する。

```bash
terraform init -backend-config="bucket=<state bucket>" -input=false
terraform plan -var-file=<非公開tfvars> -out=runtime.tfplan
terraform apply runtime.tfplan
```

backend bucketはGitHubの`TF_STATE_BUCKET`へ設定する。既存のresource名とbackendのbucket/prefixは維持する。
registryは`GCP_RUNTIME_ARTIFACT_REPOSITORY`、image fallbackは`GCP_RUNTIME_IMAGE_URI`を使う。
Logging変更時は既存_Defaultの除外を`logging_exclusions`と`PDP_LOGGING_EXCLUSIONS`へ引き継ぐ。

## Secretと資格情報

TerraformはSecret Managerのcontainerだけを作り、payloadは外部で登録する。
`secret_versions`とGitHubの`PDP_SECRET_VERSIONS`に同じmapを設定し、versionを正の整数文字列へ固定する。
`heartbeat_config`のpayloadは`{"daily":"https://..."}`形式で、HTTPS URLを使う。

| key | Secret Manager ID | 利用先 |
|---|---|---|
| `motherduck_token` | `pdp-west-motherduck-token` | worker・daily |
| `motherduck_preflight_token`（省略可） | `pdp-west-motherduck-preflight-token` | 手動preflightのみ |
| `fitbit_oauth_config` | `pdp-west-fitbit-oauth-config` | worker・daily |
| `fitbit_webhook_config` | `pdp-west-fitbit-webhook-config` | receiver |
| `heartbeat_config` | `pdp-west-heartbeat-config` | daily |
| `owntracks_config` | `pdp-west-owntracks-config` | OwnTracks receiver |

更新・rollbackはmapの番号を変更し、切替完了前に参照中versionを無効化・破棄しない。
秘密値をtfvars・state・GitHub variablesへ含めない。JSON fieldは[Fitbit環境変数](../../docs/sources/fitbit/operations.md#環境変数)、権限境界は[セキュリティ](../../docs/platform/security.md)を参照する。

OwnTracksの初回secretは`google_secret_manager_secret.runtime["owntracks_config"]`を先に適用してcontainerを作り、payloadを登録後に全体を適用する。
