# Runtime Terraform

`us-west1`のreceiver・Pub/Sub・worker・日次Job・Scheduler・監視・Logging・Raw/preflight bucketを管理する。
先に[bootstrap](../bootstrap/README.md)を適用し、`terraform.tfvars.example`から非公開入力を作る。

```bash
terraform init -backend-config="bucket=<state bucket>" -input=false
terraform fmt -check
terraform validate
terraform test
terraform plan -var-file=<非公開tfvars> -out=runtime.tfplan
terraform apply runtime.tfplan
```

`runtime_image_uri`にはruntime registryのdigest、MotherDuck production/preflightには独立DBを指定する。
backend bucketはGitHubの`TF_STATE_BUCKET`へ設定する。
GitHub設定は地域名を含まない変数を優先し、未設定なら既存の`GCP_WEST_*`・`PDP_WEST_*`も参照する。
registryは`GCP_RUNTIME_ARTIFACT_REPOSITORY`、image fallbackは`GCP_RUNTIME_IMAGE_URI`を使う。
ローカルtfvarsは`terraform.tfvars.example`の入力名に合わせる。既存のcloud resource名とbackendのbucket/prefixは維持する。接続先・実行順・監視・復旧は[Platform運用](../../docs/platform/operations.md)に従う。
通常はFitbitの毎時batch・OwnTracksのpush配信・日次Schedulerとnative警報が有効。`schedulers_enabled=false`とGitHubの`PDP_SCHEDULERS_ENABLED=false`で一時停止し、通知をqueueに保持してFitbit・OwnTracks処理を延期する。
Logging変更時は既存_Defaultの除外を`logging_exclusions`と`PDP_LOGGING_EXCLUSIONS`へ引き継ぐ。

## Fitbitのbatch処理

Fitbit通知は既存subscriptionに全種類を保持し、`pdp-fitbit-batch-west`が60分ごと（毎時00分）に非公開workerの`/notifications/fitbit/drain`をOIDC認証で呼ぶ。
subscriptionの7日保持・600秒ack deadline、workerの540秒timeout・最大1instance・同時処理1は維持する。未ack通知の警報はFitbitが75分超、OwnTracksが15分超の状態が5分続いた場合に発報する。
`schedulers_enabled=false`でbatch Schedulerも停止する。通知は引き続きqueueに保持する。

## Secretと資格情報

secretはglobal resource＋単一`us-west1` replica。Terraformはcontainerだけを作り、payloadは外部で登録する。
`secret_versions`は正の整数文字列へ固定し、`latest`・alias・未知のkeyを拒否する。
GitHubの`PDP_SECRET_VERSIONS`にも同じmapをJSONで設定する。`heartbeat_config`のpayloadは
`PDP_HEARTBEAT_CONFIG`へ渡す`{"daily":"https://..."}`形式で、HTTPS URLを使う。

| key | Secret Manager ID | 利用先 |
|---|---|---|
| `motherduck_token` | `pdp-west-motherduck-token` | worker・daily |
| `motherduck_preflight_token`（省略可） | `pdp-west-motherduck-preflight-token` | 手動preflightのみ |
| `fitbit_oauth_config` | `pdp-west-fitbit-oauth-config` | worker・daily |
| `fitbit_webhook_config` | `pdp-west-fitbit-webhook-config` | receiver |
| `heartbeat_config` | `pdp-west-heartbeat-config` | daily |
| `owntracks_config` | `pdp-west-owntracks-config` | OwnTracks receiver |

Fitbit batch・OwnTracks pushのOIDCには既存Scheduler SAを使う。workerは非公開で最大1instance・同時処理1、通知がなければ0instanceとなる。
通常runtimeはpreflight以外の5 pinを必須とする。更新・rollbackはmapの番号を変更し、
切替完了前に参照中versionを無効化・破棄しない。秘密値をtfvars・state・GitHub variablesへ含めない。
JSON fieldは[Fitbit環境変数](../../docs/sources/fitbit/operations.md#環境変数)、権限境界は[セキュリティ](../../docs/platform/security.md)を参照する。

OwnTracksの初回secretは`google_secret_manager_secret.runtime["owntracks_config"]`を先に適用してcontainerを作り、payloadを登録後に全体を適用する。
