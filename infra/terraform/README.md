# Runtime Terraform

通常構成はus-west1の通知専用receiver、Pub/Sub、毎時/日次の2 Job・2 Scheduler、3 native警報、
30日Logging bucket。MotherDuckはus-west-2の本番DBと独立した手動preflight DBを使う。
Screen Timeの旧bucketは書き込みを停止した復旧用backupとして保持する。

bootstrapを先に適用し、runtimeは西部state bucketをbackendへ指定する。
secret containerだけをTerraformで作成し、4つの通常payloadはTerraform外で登録して数値versionへ固定する。
秘密値をtfvarsやstateへ含めない。`terraform.tfvars.example`を非公開入力の雛形とする。

```bash
terraform init -backend-config="bucket=<西部state bucket>" -input=false
terraform fmt -check
terraform validate
terraform test
terraform plan -var-file=<非公開tfvars> -out=runtime.tfplan
terraform apply runtime.tfplan
```

`PDP_WEST_SCHEDULERS_ENABLED`と`PDP_WEST_LOGGING_ENABLED`をrepository variablesで管理し、
切替後のdeployで停止状態へ戻さない。`PDP_WEST_LOGGING_EXCLUSIONS`は既存_Defaultの除外を引き継ぐ。
通常のデプロイ、DB更新、停止・復旧手順は[Platform運用](../../docs/platform/operations.md)を参照する。

## Runtime構成

通常は`enable_west_runtime=true`とし、`us-west1`のRaw/preflight bucket、毎時/日次Job、通知専用Service、Pub/Sub pull subscription、5つのsecret container、通常log用bucketを管理する。Screen Timeの旧Rawとstate backupは保持する。`west_image_uri`は`us-west1`のdigest、MotherDuckは西部組織内の別production/preflight DBを指定する。Rawは新規の`age=90`に加え、Screen Timeコピーの`days_since_custom_time=90`で元の保持起点を維持し、soft deleteを無効にする。

## Secretと資格情報

新secretはglobal secret＋単一`us-west1` replicaで、payloadはTerraformへ渡さない。secret containerを先に作り、payload/versionの登録はTerraform外で行う。受信Serviceと各Jobの参照先は、秘密値を含まない`west_secret_versions` mapで数値versionへ固定する。`enable_west_runtime=false`の準備構成では既定の`{}`を使い、有効化するときは通常runtimeが使う下記4つのkeyを指定する。未使用のpreflight keyは省略できる。値は`"1"`などの正の整数文字列で、`latest`や独自alias、未知のkeyは拒否する。

| `west_secret_versions` key | Secret Manager ID | 利用先 |
|---|---|---|
| `motherduck_token` | `pdp-west-motherduck-token` | hourly・daily |
| `motherduck_preflight_token`（省略可） | `pdp-west-motherduck-preflight-token` | 手動試験用。通常runtimeは参照しない |
| `fitbit_oauth_config` | `pdp-west-fitbit-oauth-config` | hourly・daily |
| `fitbit_webhook_config` | `pdp-west-fitbit-webhook-config` | 受信Service |
| `heartbeat_config` | `pdp-west-heartbeat-config` | daily |

Plan/Deploy workflowではrepository variable `PDP_WEST_SECRET_VERSIONS`に、同じmapをJSON objectとして設定する（未設定時は`{}`）。例: `{"motherduck_token":"1","fitbit_oauth_config":"1","fitbit_webhook_config":"1","heartbeat_config":"1"}`。有効化したworkflowは、すべての数値pinが揃うまでcloud操作へ進まない。container作成だけを先に行う場合も有効化時のmap検証は適用されるため、登録予定のversion番号を指定してsecret containerをtarget適用し、version登録後に実際の番号へ合わせて通常のPlanを確認する。更新・rollbackではこのmapの番号を変更し、参照しているversionを無効化・破棄する前に切替完了を確認する。

OAuth JSONは`client_id/client_secret/refresh_token/health_user_id`、Webhook JSONは`authorization/health_user_id`を持つ。受信ServiceにはWebhook secretとtopic publisherだけを渡す。hourly JobはOAuth・MotherDuckとsubscription subscriber、日次JobはOAuth・MotherDuck・heartbeatを使う。runtimeは共通のService Accountを使い、receiverは分離する。preflightは専用bucket/token/DBで手動実行し、dbtは日次処理内で実行する。常設のpreflight/dbt Jobは作らない。

`west_schedulers_enabled=false`を維持すると毎時・日次Schedulerは停止し、新しいalert policyも無効になる。毎時は毎時15分・50分、日次は04:10 Asia/Tokyo・100分、各Jobは1task/parallelism1である。CIは独立preflight/dbt Jobを作らない。手動のrun_dailyで日次処理を実行できる。imageは西部の現行digestか西部buildから解決する。

日次成功はすべての取得・commit・dbt・Screen Time監査が完了してから1回だけ通知する。専用の成功log metricは作らない。未完了・欠損の通知は`PDP_HEARTBEAT_CONFIG`内のdaily Healthchecksへ設定し、24時間の実行間隔と最大100分の実行時間を含め、Period 24時間＋Grace 24時間で48時間まで余裕を持たせる。native警報は2 Jobの失敗をまとめる1 policy、Pub/Subの24時間滞留1 policy、receiver ERROR log 1 policyとする。[Monitoringの制限](https://docs.cloud.google.com/monitoring/alerts/metric-absence)

`west_logging_enabled=true`は既存`_Default` sinkを西部log bucketへ切り替える。先に既存exclusionを読み取り、`west_logging_exclusions`へ同じfilterを設定する。`_Required`と旧bucket内の過去logは維持する。同一projectのlog bucketへのsinkは追加のwriter権限を必要としない。[Loggingの宛先設定](https://docs.cloud.google.com/logging/docs/export/configure_export_v2)

backendは西部state bucketを`TF_STATE_BUCKET`へ指定し、prefix `personal-data-platform/runtime`を使う。
stateの移動は通常deployから分け、バックアップ・lock確認を行う。完了した切替と旧資産整理の結果は
[西部移行記録](../../docs/platform/west-migration-2026-10-05.md)に残す。
