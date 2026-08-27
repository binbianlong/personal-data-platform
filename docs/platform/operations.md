# Platform運用

## Provisioningとdeploy

最初にbootstrap Terraformで、GCS Terraform state、Artifact Registry、GitHub OIDC / Workload Identity
Federation、plan用・deploy用Service Accountを作る。その後にruntime TerraformでSecret Manager、
Cloud Run Jobs、Cloud Scheduler、Logging / Monitoringを作る。

runtimeはcommit SHAに対応するimmutable image digestを参照する。mutable tagだけをdeploy入力にしない。

初回およびIAM変更後は`pdp preflight`を専用B2 test prefixとMotherDuck test databaseへ接続して実行し、
次を確認する。

- B2 test objectのwrite / read / list / delete round trip
- MotherDuck test databaseでの一時table作成 / write / read / delete

deploy workflowからCloud Run preflightが完了することで、WIF deploy、image取得、JobのSecret注入、外部通信も
合わせて確認する。preflight用B2 keyとMotherDuck tokenは本番用と分離し、production prefixまたはproduction
databaseへ書き込まない。

## Loader

`screen-time-loader` Cloud Run Jobを毎時15分に起動する。task数とparallelismはともに1とし、さらに
MotherDuckの期限付き`ops.job_lock`を取得して多重実行を防ぐ。

1. B2の対象prefixを全page listingする。
2. `ops.ingestion_metadata`で未成功objectを選ぶ。
3. `(observed_at, object_key)`の昇順に処理する。
4. [`analytics.md`](analytics.md)のtransaction契約でbaseと取込状態を更新する。
5. 1件でも未処理の失敗が残ればJobをnon-zeroで終了する。

poison objectは失敗として記録するが、自動削除や上書きを行わない。修正したdecoderをdeployした後に同じ
objectを再試行できるようにする。

## dbt

`pdp dbt`はmodel / schema定義のdeploy時にだけ実行し、`dbt run`に続けて`dbt test`を行う。
通常のLoaderとReconciliationは、martsがViewである間はdbtを起動しない。

## Reconciliation

`pdp reconciliation`を毎日04:30 Asia/Tokyoに実行する。

1. B2 control prefixにあるdevice別collector scan receiptが24時間以内であることを確認する。
2. B2 objectと`ops.ingestion_metadata`を照合し、未取込objectをLoader契約で再処理する。
3. `failed` ingestionが監査対象期間に残っていないことを確認する。
4. 必須base / Viewの存在と各relationの代表`count(*)` queryを確認する。
5. 全stageと必要な再処理が成功した後だけHealthchecks.ioへ成功heartbeatを送る。

Jobの開始、retry開始、Loaderへの引き渡しだけでは成功heartbeatを送らない。いずれかのstageが失敗したら
失敗object、欠損relation、stale receiptなどの構造化した結果を記録し、Jobをnon-zeroで終了する。

## 監視

Cloud Logging / MonitoringとEmailで次を通知する。

- LoaderまたはReconciliation Jobの失敗
- decode失敗
- Collectorの成功scanが24時間以上ない状態
- Healthchecks.io heartbeatのschedule + grace period超過

新しいsource eventがないことだけを障害とみなさない。scan完了、B2 listing、取込状態、query成功を
組み合わせて判定する。

## Rebuild

本番MotherDuck databaseを直接空にして再構築してはならない。

1. `pdp rebuild --dry-run`で対象prefixのobject数、device数、segment数、期間を表示する。
2. `pdp rebuild --target-db <scratch-db>`で空のscratch databaseを指定する。
3. forward-only migrationを適用する。
4. B2を全page listingし、`(observed_at, object_key)`順に全objectを再生する。
5. `ops.ingestion_metadata`とbaseを再構築する。
6. dbt modelをdeployし、`dbt test`を実行する。
7. productionと件数、stable key集合、代表martを手動で比較する。
8. 差分を確認した後、参照先を手動で切り替える。

target databaseがproductionと同一、既存tableを持つ、または環境識別が不明な場合は開始前に停止する。

## CLI

```text
pdp preflight
pdp loader
pdp dbt
pdp reconciliation
pdp rebuild --dry-run
pdp rebuild --target-db <scratch-db>
```
