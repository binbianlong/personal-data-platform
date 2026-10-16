# Runtime Terraform

`us-west1`のreceiver・Pub/Sub・worker・日次Job・Scheduler・監視・Logging・Raw/preflight bucketを管理する。
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
通常はpush配信・日次Schedulerとnative警報が有効。`west_schedulers_enabled=false`とGitHubの`PDP_WEST_SCHEDULERS_ENABLED=false`で一時停止し、subscriptionをpullへ戻して通知を保持し、Fitbit・OwnTracks処理も延期する。
Logging変更時は既存_Defaultの除外を`west_logging_exclusions`と`PDP_WEST_LOGGING_EXCLUSIONS`へ引き継ぐ。

## Fitbitの配信切替

`fitbit_delivery_mode`は`push`（既定）または`pull`。GitHub Deployも`PDP_FITBIT_DELIVERY_MODE`を参照し、未設定時は`push`を使う。
`pull`では既存subscriptionのpush設定だけを外し、`pdp-fitbit-batch-west`が30分ごと（毎時00・30分）に非公開workerの`/notifications/fitbit/drain`をOIDC認証で呼ぶ。通知は全種類を同じqueueに保持する。
subscriptionの7日保持・600秒ack deadline、workerの540秒timeout・最大1instance・同時処理1は維持する。未ack通知の警報はFitbitが45分超、OwnTracksが15分超の状態が5分続いた場合に発報する。

切替は`west_schedulers_enabled`を変更せず、次の順で行う。

1. `push`のまま新Schedulerを停止状態で作成し、drain endpointを持つworker imageとsubscriptionのsubscriber権限を先に適用する。
2. workerの準備完了後、`fitbit_delivery_mode=pull`で`google_pubsub_subscription.west`だけをtargetにplan/applyし、既存subscriptionを再作成せずpullへ変える。新Schedulerは停止を維持する。
3. Scheduler SAでdrain endpointを手動実行し、処理とackを確認する。その後、同じ`pull`設定で新Schedulerだけを再開する。imageの共有変数によってreceiverや日次Jobが更新されないことをplanで確認する。GitHubの`PDP_FITBIT_DELIVERY_MODE`にも`pull`を保持する。

rollbackは新Schedulerだけを停止し、実行中drainの終了を待ってから`fitbit_delivery_mode=push`へ戻す。日次SchedulerやOwnTracksは停止しない。

## Secretと資格情報

secretはglobal resource＋単一`us-west1` replica。Terraformはcontainerだけを作り、payloadは外部で登録する。
`west_secret_versions`は正の整数文字列へ固定し、`latest`・alias・未知のkeyを拒否する。
GitHubの`PDP_WEST_SECRET_VERSIONS`にも同じmapをJSONで設定する。`heartbeat_config`のpayloadは
`PDP_HEARTBEAT_CONFIG`へ渡す`{"daily":"https://..."}`形式で、HTTPS URLを使う。

| key | Secret Manager ID | 利用先 |
|---|---|---|
| `motherduck_token` | `pdp-west-motherduck-token` | worker・daily |
| `motherduck_preflight_token`（省略可） | `pdp-west-motherduck-preflight-token` | 手動preflightのみ |
| `fitbit_oauth_config` | `pdp-west-fitbit-oauth-config` | worker・daily |
| `fitbit_webhook_config` | `pdp-west-fitbit-webhook-config` | receiver |
| `heartbeat_config` | `pdp-west-heartbeat-config` | daily |
| `owntracks_config` | `pdp-west-owntracks-config` | OwnTracks receiver |

pushのOIDCには既存Scheduler SAを使う。workerは非公開で最大1instance・同時処理1、通知がなければ0instanceとなる。
通常runtimeはpreflight以外の5 pinを必須とする。更新・rollbackはmapの番号を変更し、
切替完了前に参照中versionを無効化・破棄しない。秘密値をtfvars・state・GitHub variablesへ含めない。
JSON fieldは[Fitbit環境変数](../../docs/sources/fitbit/operations.md#環境変数)、権限境界は[セキュリティ](../../docs/platform/security.md)を参照する。

OwnTracksの初回secretは`google_secret_manager_secret.west["owntracks_config"]`を先に適用してcontainerを作り、payloadを登録後に全体を適用する。
