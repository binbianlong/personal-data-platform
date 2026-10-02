# GCP runtime

GCS Raw/preflight bucket、Cloud Run Jobs、Secret Manager、Scheduler、Cloud Logging/Monitoringを管理する。既定のiPhone構成は4つのJobを持つ。追加pipelineは独立したLoader・Reconciliation・Scheduler・heartbeatを持つ。Fitbitは`enable_fitbit_runtime=true`の場合だけ専用HTTP ServiceとCloud Tasksを追加し、定期補修は既存reconciliationを利用する。Cloud Run、GCS、Artifact Registryは`us-central1`に固定し、Schedulerの時刻解釈だけは`Asia/Tokyo`を使う。

## 初回構築

先に`infra/bootstrap`を再適用し、US Artifact Registryとstorage custom roleを作る。bootstrap outputのstate bucketをbackendへ渡す。
既存backendを移す場合は[bootstrapのstate移行手順](../bootstrap/README.md#既存stateの米国への移行)で
`init -migrate-state`を使い、既存stateを引き継ぐ。新bucketへの`-reconfigure`だけで移行を代用しない。

```bash
terraform init -backend-config="bucket=$TF_STATE_BUCKET" -lockfile=readonly
terraform fmt -check
terraform validate
terraform test
```

Secret payloadをTerraformへ渡すとstateへ残るため、Terraformはsecret containerだけを作る。初回はcontainerを先に適用する。

```bash
terraform apply -target=google_secret_manager_secret.runtime
```

その後、各値を標準入力からSecret Managerへ登録する。値はcommand line、tfvars、GitHub Actions logsへ含めない。

```bash
printf '%s' "$SECRET_VALUE" | gcloud secrets versions add SECRET_ID --data-file=-
```

対象は`terraform output -json secret_ids`で確認する。既定のJobが使うのは本番MotherDuck tokenとpreflight MotherDuck tokenである。旧`healthchecks-ping-url` containerはversionを登録せず、iPhoneのJobでは参照しない。追加pipelineには専用heartbeat URLのcontainerも作る。Jobが参照するsecretにversionを登録してから通常のapplyまたは`Terraform Deploy` workflowを実行する。Email notification channelは適用後に有効状態とalert policyへの紐付けを確認し、通知の到達は実際の発報で確認する。

## B2からのstate移行

以前Terraform管理していた6つのB2 Secret Manager containerは、`moved`と`removed { destroy = false }`でstateから外す。最初のruntime planで次を確認する。

- B2 secretは`will no longer be managed ... but will not be destroyed`と表示される。
- Cloud Run JobからB2環境変数とB2 secret accessが削除される。
- B2 secret container自体のdestroyは表示されない。

条件を満たさないplanはapplyしない。残したB2 secret、credential、bucketはGCSの実運用受入後に別作業で失効・削除する。このTerraform applyはB2のリモートデータを削除しない。

## GCS保持と権限

- `${project_id}-pdp-raw`: `STANDARD`、flat namespace、UBLA、public access prevention、`force_destroy=false`、`prevent_destroy=true`。
- Raw Lifecycleは`raw/screen_time/v1/`または`raw/screen_time/v2/`配下の`.segb.gz`だけを作成から90日でDeleteする。90日間はStandardのまま保持し、Coldline / Archiveへ遷移しない。Soft Deleteは0秒、Object VersioningとAutoclassは無効で、削除後は復元できない。Lifecycle実行は非同期で、90日ちょうどの削除を保証するprovider SLAはない。
- device別receipt JSONと`_control/collector/active.json` manifestはsuffix条件に合わないため90日削除の対象外で、最新objectを上書きする。
- `${project_id}-pdp-preflight`: 本番と別の`STANDARD` bucket。Soft Deleteは0秒で、`test/preflight/`の孤立objectを1日で削除する。
- Raw bucket IAM policyとpreflight bucket IAM policyはauthoritative管理する。運用者にはbucket単位のStorage Adminを明示的に付与し、手動で追加したその他のbucket bindingは次回applyで削除される。
- Mac CollectorはRaw segmentのcreateとlatest receipt・active-device manifestのcreate/deleteだけを持ち、read/listは持たない。`collector_impersonator_member`は専用Collector Service Accountとread-only Rebuild Service AccountだけをToken Creatorとしてimpersonateできる。
- Loader、Reconciliation、Rebuild Service AccountはRaw bucketのobject Viewerを持つ。preflight Jobはpreflight bucketだけのcreate/get/list/deleteを持ち、dbt JobはGCS権限を持たない。
- ローカルrebuildは`rebuild_operator_service_account` outputをtargetにした別ADCを使い、Collector ADCを上書きしない。

## 実行契約

- imageはtagではなく`@sha256:`付きdigestだけを受け付ける。
- `platform-preflight`は隔離したGCS bucketとMotherDuck test databaseを使い、deployごとにworkflowから実行する。
- `reconciliation`は毎日04:30 `Asia/Tokyo`に1回起動し、両Screen Time streamの取込・修復・監査と、有効な場合のFitbit補修を実行する。
  `screen-time-loader`は手動実行用に残し、そのSchedulerとScheduler用invoker bindingだけを削除する。既存tfvarsの`loader_schedule`は削除する。追加pipelineのLoader scheduleは維持する。
- `reconciliation`の完了数が直近48時間で0の場合と、Jobが失敗した場合はCloud Monitoringから通知する。
  48時間のPromQL監視はGoogleの長期間query機能（Preview）を使い、5分ごとに評価する。通常のmetric-absenceの最大23.5時間では日次運転を監視できない。
  対象のqueryが当該projectで評価できること、完了metric、通知先、発報と回復を適用後に確認する。初回から完了metricがない場合も通知対象になる。
- Fitbitの完全な補修巡回が48時間を超えて成功していない場合は、現在の補修logにある経過秒数で検出する。日次測定のため最終成功から約72時間まで検知が遅れることがある。巡回成功時の0秒の測定で回復を確認する。
- LoaderのTask timeoutは120分、MotherDuck上の排他期限は125分とする。前回Loaderが続いている間の手動実行は成功扱いでスキップし、次の日次修復で未処理Rawを再確認する。ReconciliationのTask timeoutは150分、排他期限は155分とする。
- LoaderのTask自動リトライは無効（`max_retries = 0`）とし、失敗は失敗として記録する。未処理Rawは翌日のReconciliationの修復または手動Loaderで再処理する。異常終了で排他が残った場合は、排他期限が切れた後に再開する。
- `dbt-runner`はSchedulerから起動せず、初回構築、dbt定義・SQL migration変更時、または`run_dbt=true`を指定したdeploy時に実行する。初回はapply前のTerraform planでdbt Jobの新規作成を検出し、applyと隔離preflightが成功した後にmodelを作成する。Job再作成も同じ扱いとする。
- 各Jobは専用Service Accountを持ち、必要なSecretだけを参照する。
- deploy identityの`actAs`は、Terraformが作成するJob/Scheduler用SAと、有効化時のFitbit Service/Task用SAへ限定する。

手動実行例:

```bash
gcloud run jobs execute platform-preflight --region=us-central1 --wait
gcloud run jobs execute screen-time-loader --region=us-central1 --wait
gcloud run jobs execute dbt-runner --region=us-central1 --wait
gcloud run jobs execute reconciliation --region=us-central1 --wait
```


## Source / streamの追加

Fitbitは`additional_ingestion_pipelines`へ重複登録せず、`enable_fitbit_runtime`、`fitbit_subject_key`、
`fitbit_secret_ids`を設定する。`fitbit_processing_paused=true`が既定。
隔離検証・費用確認・購読登録の条件は[`Fitbit運用`](../../docs/sources/fitbit/operations.md)を参照する。

GitHub ActionsのPlanとDeployは、次のrepository variablesを同じTerraform入力として使う。
未設定ではFitbitを作成せず、処理を停止する。秘密値は登録せず、既存Secret Manager IDだけを渡す。

| Repository variable | Terraform入力 | 未設定時 |
|---|---|---|
| `PDP_FITBIT_ENABLED` | `enable_fitbit_runtime` | `false` |
| `PDP_FITBIT_PROCESSING_PAUSED` | `fitbit_processing_paused` | `true` |
| `PDP_FITBIT_SUBJECT_KEY` | `fitbit_subject_key` | 空文字 |
| `PDP_FITBIT_SECRET_IDS` | `fitbit_secret_ids` | `{}`。環境変数名からSecret Manager IDへのJSON object |

OAuthは`PDP_FITBIT_OAUTH_CREDENTIALS`の1つのJSON Secretへ集約し、`PDP_FITBIT_HEALTH_USER_ID`、
`PDP_FITBIT_WEBHOOK_AUTHORIZATION`と合わせて3つのIDを渡す。
移行中はOAuthを3つの個別Secretで渡す従来の5-ID形式も使用できる。
JSON対応imageのdeployと旧versionを廃止する順序は[OAuth Secretの集約](../../docs/sources/fitbit/operations.md#oauth-secretの集約)を参照する。

稼働後もこれらを維持する。`PDP_FITBIT_ENABLED=false`は受信Serviceやqueueの削除計画になるため、
運用停止には`PDP_FITBIT_PROCESSING_PAUSED=true`を使い、Planを確認してからDeployする。

`sources.tf`の既定pipelineは`screen_time / app-in-focus`である。`additional_ingestion_pipelines`へ実装済みの
source / streamを追加すると、共通imageを使うLoaderとReconciliation、それぞれのService Account・Scheduler・
監視と、専用heartbeat secretが作られる。iPhoneの既存resource addressと名前、preflight、dbt-runnerは維持する。

各entryには次を指定する。secretの値は含めない。

| 設定 | 内容 |
|---|---|
| map key | リソース名に使う16文字以内の小文字・数字・underscoreの識別名 |
| `source_id` / `stream` | runtime registryに登録済みの実行単位 |
| `raw_prefixes` / `raw_suffixes` | adapterが対応する全Raw版の保存領域・gzip拡張子 |
| `retention_days` / `lifecycle_grace_days` | Raw保持日数とLifecycle遅延の監査猶予 |
| `loader_schedule` / `reconciliation_schedule` | scopeごとのcron。時刻解釈は既存のtimezone設定 |
| `raw_creator_members` | そのRaw領域へcreateのみを許可する取得用identity |

Rawのprefix・suffix・保持日数はadapterと一致させる。Jobへ渡した設定との不一致はruntimeがcloud接続前に拒否する。
同じsource / streamの重複登録、領域が重なるRawに対する異なる保持期限は拒否する。Raw bucketのViewerは各取り込みJobと
Rebuildへ付与するため、sourceごとの処理分離はbucket内の完全なアクセス分離ではない。

取得用identityの作成・認証と、mutableなcontrol objectの更新権限はsourceの取得実装に合わせて定義する。
自動で追加されるのはRawのcreate権限であり、iPhone用のmanifest更新権限を他streamへ流用しない。
追加heartbeat secretへURLのversionを登録し、外部monitorのscheduleと通知先も設定する。

追加sourceの前に、既存runtimeの更新と旧実行の終了確認を完了させる。手順は
[`Platform運用`](../../docs/platform/operations.md#更新時の互換性)、adapterの追加は
[`アーキテクチャ`](../../docs/platform/architecture.md#sourcestream追加手順)を参照する。
