# GCP runtime

GCS Raw/preflight bucket、Cloud Run Jobs、Secret Manager、Scheduler、Cloud Logging/Monitoringを管理する。既定のiPhone構成は4つのJobと3つのsecret resourceを持つ。追加source / streamは独立したLoader・Reconciliation・Scheduler・heartbeatを持つ。HTTP Service、Cloud Tasks、空のwebhook/fetch runtimeは作成しない。Cloud Run、GCS、Artifact Registryは`us-central1`に固定し、Schedulerの時刻解釈だけは`Asia/Tokyo`を使う。

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

対象は`terraform output -json secret_ids`で確認する。既定の対象は本番MotherDuck token、preflight MotherDuck token、iPhoneのHealthchecks URLの3つで、追加pipelineには専用heartbeat URLのcontainerも作る。すべてにversionを登録してから通常のapplyまたは`Terraform Deploy` workflowを実行する。Email notification channelは適用後に届く確認メールで有効化する。

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
- Raw bucket IAM policyとpreflight bucket IAM policyはauthoritative管理する。手動で追加したbucket bindingは次回applyで削除される。
- Mac CollectorはRaw segmentのcreateとlatest receipt・active-device manifestのcreate/deleteだけを持ち、read/listは持たない。`collector_impersonator_member`は専用Collector Service Accountとread-only Rebuild Service AccountだけをToken Creatorとしてimpersonateできる。
- Loader、Reconciliation、Rebuild Service AccountはRaw bucketのobject Viewerを持つ。preflight Jobはpreflight bucketだけのcreate/get/list/deleteを持ち、dbt JobはGCS権限を持たない。
- ローカルrebuildは`rebuild_operator_service_account` outputをtargetにした別ADCを使い、Collector ADCを上書きしない。

## 実行契約

- imageはtagではなく`@sha256:`付きdigestだけを受け付ける。
- `platform-preflight`は隔離したGCS bucketとMotherDuck test databaseを使い、deployごとにworkflowから実行する。
- `screen-time-loader`は毎時15分、`reconciliation`は毎日04:30に、どちらも`Asia/Tokyo`で起動する。
- `dbt-runner`はSchedulerから起動せず、初回構築、dbt定義・SQL migration変更時、または`run_dbt=true`を指定したdeploy時に実行する。初回はapply前のTerraform planでdbt Jobの新規作成を検出し、applyと隔離preflightが成功した後にmodelを作成する。Job再作成も同じ扱いとする。
- 各Jobは専用Service Accountを持ち、必要なSecretだけを参照する。
- deploy identityの`actAs`は、Terraformが作成するJob用Service AccountとScheduler用Service Accountだけへ付与する。

手動実行例:

```bash
gcloud run jobs execute platform-preflight --region=us-central1 --wait
gcloud run jobs execute screen-time-loader --region=us-central1 --wait
gcloud run jobs execute dbt-runner --region=us-central1 --wait
gcloud run jobs execute reconciliation --region=us-central1 --wait
```


## Source / streamの追加

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
