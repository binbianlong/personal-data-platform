# GCP runtime

GCS Raw/preflight bucket、Cloud Run Jobs、Secret Manager、Scheduler、Cloud Logging/Monitoringを管理する。既定のiPhone構成は4つのJobを持つ。追加pipelineは独立したLoader・Reconciliation・Scheduler・heartbeatを持つ。Fitbitは`enable_fitbit_runtime=true`の場合だけ専用HTTP ServiceとCloud Tasksを追加し、定期補修は既存reconciliationを利用する。Cloud Run、GCS、Artifact Registryは`us-central1`に固定し、Schedulerの時刻解釈だけは`Asia/Tokyo`を使う。

## 初回構築

先に`infra/bootstrap`を再適用し、US Artifact Registryとstorage custom roleを作る。bootstrap outputのstate bucketをbackendへ渡す。既存state bucketのlocationは変更しない。

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
- `screen-time-loader`は毎時15分、`reconciliation`は毎日04:30と16:30に、どちらも`Asia/Tokyo`で起動する。
- `reconciliation`の完了metricが23.5時間届かない場合と、Jobが失敗した場合はCloud Monitoringから通知する。欠落監視を作成・更新した後は23.5時間以内にJobを1回成功または失敗まで完了させ、監視対象の時系列を初期化する。
- LoaderのTask timeoutは120分、MotherDuck上の排他期限は125分とする。前回実行が続いている間の定期起動は成功扱いでスキップし、次の定期起動で未処理Rawを再確認する。
- LoaderのTask自動リトライは無効（`max_retries = 0`）とし、失敗は失敗として記録する。未処理Rawは次の毎時起動で再処理する。異常終了で排他が残った場合は、排他期限が切れた後の定期起動で再開する。
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

## 西部の並行構成

`enable_west_runtime=true`で`us-west1`の別Raw/preflight bucket、毎時/日次の2つのJob、通知専用Service、Pub/Sub pull subscription、5つのsecret container、通常log用bucketを追加する。既存address・bucket・secret・実行系は維持する。`west_image_uri`は`us-west1`のdigest、MotherDuckは西部組織内の別production/preflight DBを指定する。Rawは新規の`age=90`に加え、Screen Timeコピーの`days_since_custom_time=90`で元の保持起点を維持し、soft deleteを無効にする。

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

`west_schedulers_enabled=false`を維持すると毎時・日次Schedulerは停止し、新しいalert policyも無効になる。毎時は毎時15分・50分、日次は04:10 Asia/Tokyo・100分、各Jobは1task/parallelism1である。CIの準備deployは西部production Job・preflight・dbtを自動実行しない。新旧imageの参照は別々に解決し、旧regionのimageをwestのfallbackとして受け入れない。

日次成功はすべての取得・commit・dbt・Screen Time監査が完了してから1回だけ通知する。専用の成功log metricは作らない。未完了・欠損の通知は`PDP_HEARTBEAT_CONFIG`内のdaily Healthchecksへ設定し、24時間の実行間隔と最大100分の実行時間を含め、Period 24時間＋Grace 24時間で48時間まで余裕を持たせる。native警報は2 Jobの失敗をまとめる1 policy、Pub/Subの24時間滞留1 policy、receiver ERROR log 1 policyとする。[Monitoringの制限](https://docs.cloud.google.com/monitoring/alerts/metric-absence)

`west_logging_enabled=true`は既存`_Default` sinkを西部log bucketへ切り替える。先に既存exclusionを読み取り、`west_logging_exclusions`へ同じfilterを設定する。`_Required`と旧bucket内の過去logは維持する。同一projectのlog bucketへのsinkは追加のwriter権限を必要としない。[Loggingの宛先設定](https://docs.cloud.google.com/logging/docs/export/configure_export_v2)

移行中は新旧secret version、Scheduler、image、bucketが並存し、定常無料枠の目標を超え得る。切替・復旧確認後に旧資産を範囲指定で整理する。backendは既存`TF_STATE_BUCKET`とprefixを維持し、西部state bucketへの移動はバックアップ・lock確認を含む独立した操作とする。
