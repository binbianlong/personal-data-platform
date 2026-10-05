# 西部リージョン移行記録

2026-10-06更新。初回backup、Terraform backend移行、西部GCP資産の一部作成、Screen Time Rawコピー、西部MotherDuckの独立DB作成まで完了した。本番の接続先切替は未実施である。定常構成はWebhook＋Pub/Sub＋毎時/日次の2 Jobへ改訂し、追加deployと切替は最小構成の実装・検証後に行う。構成は[設計書](../superpowers/specs/2026-10-05-pubsub-gcs-migration-design.md)、作業順序と受入条件は[移行計画](../superpowers/plans/2026-10-05-pubsub-gcs-migration-plan.md)を参照する。

## 接続先と適用済み資産

GCP projectは`health-data-pipeline-503813`。地域を選べる新資産は`us-west1`に作成した。

| 用途 | 移行元 | 移行先・現在の状態 |
| --- | --- | --- |
| Terraform state | `health-data-pipeline-503813-personal-data-platform-tfstate`（ASIA） | `health-data-pipeline-503813-personal-data-platform-tfstate-west`（US-WEST1）へ移行済み |
| Artifact Registry | `us-central1`の`personal-data-platform` | `us-west1`に同名repositoryを作成し、imageを登録済み |
| production Raw | `health-data-pipeline-503813-pdp-raw`（US-CENTRAL1） | `health-data-pipeline-503813-pdp-raw-west`（US-WEST1）へ初回53 objectをコピー済み |
| preflight Raw | `health-data-pipeline-503813-pdp-preflight` | `health-data-pipeline-503813-pdp-preflight-west`を作成済み |
| 通知 | Cloud Tasks `pdp-fitbit` | Pub/Sub topic `pdp-fitbit-west`、subscription `pdp-fitbit-west-pull`を作成済み。providerの送信先は旧URLのまま |
| 受信Service | `pdp-fitbit`（us-central1） | `pdp-fitbit-west`（us-west1）をdeploy済み。認証付き検証リクエストは200、認証なしは401 |
| Logging | 既存の`_Default` | `pdp-west` bucketを30日保持で作成済み。通常logのroutingは未変更 |
| MotherDuck | `ap-northeast-1`をSDKで確認済み | `us-west-2`の新組織にproduction/preflight DBと専用service accountsを作成済み。データimport・通常writer・Remote MCPは未切替 |
| 欠損監視 | 既存の内部Job/stream監査 | Healthchecksのaccountとproject管理APIキーを準備済み。日次1件への設定・runtimeのpingは未実施 |

bootstrapは6 create、runtimeは依存資産11 createと受信Service関連6 createを段階適用した。いずれも既存資産の変更・削除は0件。新Jobと新Schedulerは未作成であり、新側のDB writerは動いていない。

stateのprefixは`personal-data-platform/runtime`、lineageは`90a02072-fcdc-db7b-1e9b-d4da9aa3ec64`を維持した。backendのコピーでserialは23から24へ進み、段階適用後は27。旧83 resource IDがすべて同じであることを再照合した。旧state bucketと初回state backupは保持している。

GitHubの`TF_STATE_BUCKET`は西部へ変更済み。Terraform Plan/Deploy workflowは停止中である。remoteのTerraform/workflowが西部構成に未対応のため、対応するrevisionの反映と安全なplan確認を終えてから再開する。bootstrapのlocal stateとruntime remote stateは別々に保全した。

登録imageは`us-west1-docker.pkg.dev/health-data-pipeline-503813/personal-data-platform/personal-data-platform:deployed-west-467a82b`。digestは`sha256:66a090090771d8ac96ee1066f1a222f0b2b9633b9595bdacde6edc0f700c47b5`、linux/amd64で約180 MiB。`deployed-`タグでcleanupから保護している。旧設計のv2 Raw・分心拍・Screen Time保持起点を扱うreleaseであり、最小構成で使うRaw v3には対応しない。切替とrollbackには、最小構成の新imageを検証して使う。

Pub/Subは保存先`["us-west1"]`、`enforceInTransit=true`、subscription保持604800秒、自動期限切れなしを実設定で確認した。受信側のendpointは`pubsub.us-west1.rep.googleapis.com`を指定している。

Secret Managerは単一US-WEST1 replicaの新IDを5個作成した。`pdp-west-fitbit-oauth-config`と`pdp-west-fitbit-webhook-config`は数値version `1`を登録済みで、受信ServiceにはWebhook用だけを注入した。MotherDuck production/preflight tokenとheartbeat configの3個はまだ空である。secret payloadはstateとGitに保存しない。

## MotherDuckと欠損監視の準備

西部組織の登録を完了し、以下を別々のowner資格情報で作成した。2026-10-06にSDKで各DBのregionを`us-west-2`と確認した。

| 用途 | Database | Owner service account |
| --- | --- | --- |
| 本番 | `personal_data_platform_west` | `pdp_west_prod` |
| 手動preflight | `personal_data_platform_west_preflight` | `pdp_west_preflight` |

両service accountの読み書きinstanceはPulse、read-scalingはPulse・flock 1に設定した。各DBにwest migration 001〜004を適用済みであり、再照合ではmigration台帳4行以外の`base`/`ops`行数はそれぞれ0件だった。productionとpreflightを互いのtokenでATTACHできないことも確認した。旧東京DBへのmigration適用・データ削除は行っていない。

移行用の1日tokenと、production/preflightそれぞれのtokenは非公開ファイルへ保存した。通常runtimeのSecret Managerへは未登録である。MotherDuck接続は資格情報ごとに別processで行い、1 process内での異なるtokenの切替に依存しない。分析/MCP用のrestricted read-only shareは未作成であり、本番ownerのtokenを分析用途へ流用しない。

Healthchecksはproject管理APIキーを非公開ファイルへ保存した。2026-10-06のAPI確認では既定の`My First Check`が`new`、ping 0件であり、設定変更・試験通知は未実施である。最小構成ではこれを日次1チェックへ変更し、Period 24時間＋Grace 24時間とする。管理キーをCloud Runへ渡さず、日次の成功ping URLだけをruntime secretへ保存する。

未実装のwest migration 005で、通知・attempt・bundle・cursor等の10表を撤去する。適用済み001〜004のSQL/checksumと、Screen Time・共通表・coverageは維持する。西部のFitbit v2 Rawはまだ存在せず、Raw v3への切替で本番データのformat変換は必要ない。

## Backupと照合

private artifactはGit対象外の`var/west-migration/2026-10-05/`に保存した。state、SQLite、control、Raw、DuckDB snapshotの内容はこの記録に含めない。

- Screen Time Rawは53 object、圧縮6,787,430 bytes。圧縮/展開後hash、size、元の保持起点、西部の新generationをすべて照合した。移行先の`Custom-Time`は元の保持起点を維持し、soft-deleteは0、90日のLifecycleを設定した。
- controlは4 object、727 bytesをbackupした。西部への初回control公開はcollector切替時に行う。
- PC collectorのSQLiteとBiome sync DBは別々にbackupし、`integrity_check`を確認した。
- MotherDuckは読み取り専用接続でScreen Timeの9 tableをsnapshotへexportした。初回snapshotの全値digestと、west baselineを適用したローカル復元先の全値digest・Raw参照を照合した。
- 旧DBの30 relationと適用済みmigration 4件をinventoryし、既存SQLのchecksumがすべて台帳と一致することを確認した。旧DBのmigration台帳は変更していない。

| Table | 初回snapshotの行数 |
| --- | ---: |
| `base.screen_time_event` | 67,229 |
| `ops.screen_time_segment` | 20 |
| `ops.screen_time_record` | 67,229 |
| `ops.screen_time_tombstone` | 109,051 |
| `ops.screen_time_deletion_match` | 0 |
| `ops.ingestion_metadata` | 39 |
| `ops.reconciliation_run` | 29 |
| `ops.heartbeat` | 2 |
| `ops.job_run` | 299 |
| 合計 | 243,898 |

全243,898行のローカル復元は成功した。Fitbitの旧table/行、migration台帳、leaseはimport対象に含めず、復元先のFitbit行とactive leaseは0件だった。これはローカル復元の証拠であり、西部MotherDuckへのimport、手動preflight、実API取得、切替後rollbackの成功を示すものではない。

collectorは旧bucketへ送信を続けているため、このbackupは最終差分ではない。旧writerとcollectorを止めた後にRawとwarehouseを再exportし、最終manifestのgenerationを照合する。

## Warehouse移行scriptの実行条件

大量の行は1,000行単位のparameterized INSERTで転送する。移行用Python環境にはoptional依存を追加する。

```bash
uv venv --python 3.13 var/west-migration/python
uv pip install --python var/west-migration/python/bin/python -e '.[migration]'
```

`pandas`はDuckDBのPython値変換時に発生する繰り返しのimport探索を避けるための移行用依存であり、通常runtimeには追加しない。今回のローカル計測ではsource CLI exportが11.25秒、baseline importと全件照合が24.73秒だった。西部MotherDuckへの通信を含む所要時間は未測定である。

cloud sourceは通常、検証済みread-scaling tokenを使う。現アカウントではread-scaling token作成が403となったため、旧Scheduler・旧Job・手動writerの停止とactive lease 0件を確認して、次の明示的な読み取り専用exportを使用した。

```bash
var/west-migration/python/bin/python scripts/migrate_west_warehouse.py --export \
  --source-db personal_data_platform --source-writers-stopped \
  --snapshot var/west-migration/2026-10-05/screen-time-source-verified.duckdb
```

`SOURCE_MOTHERDUCK_TOKEN`は実行環境から渡す。`--source-writers-stopped`はwriterを停止する操作ではない。停止確認済みの`.rw`接続を`read_only=True`で開くための選択肢であり、active leaseが残れば拒否する。import側の共通lease・期限・全table transactionと、失敗時のrollbackは維持している。

上記scriptの変更時点ではpytest 779件、Ruff check/format、strict mypy 59 source filesがPASS。実sourceからのCLI exportと全件ローカル復元も確認した。これは最小構成の改修前の結果である。

source exportとtarget importは別processで実行する。既存の`--final-delta`は異なるMotherDuck tokenを同じprocessで開くため、最小構成の移行では分離実行へ修正するか廃止する。cloudの最終差分転送がこの引数で成功したとは記録しない。

## 残る切替と費用確認

2026-10-06の再確認でも旧Scheduler 2個は停止中、西部Jobは0件だった。準備時の停止確認では実行中の旧JobとCloud Tasks残件も0件である。旧受信Serviceはprocessing pausedを維持し、旧Fitbitデータ・Raw・receiptの削除はまだ行っていない。provider URL、Mac collector、分析/MCPは旧接続先のままである。

次は通知の単位分割、Raw v3と共通Loaderへの改修、005適用、2 Job/1外部heartbeatへの整理を行う。新releaseで手動preflight・実cloud import・限定5種別の取得・dbt・両stream監査・復元を確認し、本番tokenと日次heartbeatの数値versionを登録して新2 Jobを停止状態でdeployする。preflight tokenの空containerを埋めるためだけにversionや常設Jobを追加しない。その後に最終差分、旧URLからのPub/Sub発行、collector/分析/MCPの切替、2 scheduleの有効化、Logging routing、警報の発火/復旧を確認する。

2026-10-05のGCP準備時点ではactive secret versionは新旧併存で9個だった。新imageだけで約180 MiB、コピーRawは約6.8 MBであり、これだけでは定常無料枠の目標達成を判断できない。コピー/検証のGET・LIST・書込操作、転送、旧資産、MotherDuck容量/CUhを含む総額は未確定である。受入とrollbackの確認後に旧資産を整理し、切替後7日・30日の実測を記録する。

## MotherDuck向け通信の料金と移行前計測

2026-10-05にCloud Billing Catalog APIでCloud Run専用のインターネット転送SKUを確認した。米国から東京への大陸間通信は最初から$0.12/GiB（SKU `DBAA-7594-B9FA`）、北米内の通信は月1GiBまで無料、その後は$0.105/GiB（SKU `DDFD-AE42-E219`）。いずれも最初の有料帯のUSD単価であり、北米内でも同じOregonだから無制限に無料とは扱わない。[Cloud Run料金](https://cloud.google.com/run/pricing)、[大陸間転送SKU](https://cloud.google.com/skus?currency=USD&filter=DBAA-7594-B9FA)、[北米内転送SKU](https://cloud.google.com/skus?currency=USD&filter=DDFD-AE42-E219)

同projectの`run.googleapis.com/container/network/sent_bytes_count`を`kind=internet`で集計した。JobとServiceを含むCloud Run全体の監視値であり、MotherDuckだけの通信量や請求明細ではない。

| 計測期間（JST） | 外向きインターネット送信 | 全量を東京向けと仮定した概算 |
| --- | ---: | ---: |
| 2026-09-01 00:00〜2026-10-01 00:00 | 418,676,667 bytes（0.390 GiB） | 約$0.047 |
| 2026-10-01 00:00〜2026-10-05 20:56 | 144,666,580 bytes（0.135 GiB） | 約$0.016 |

9月は旧構成の計測で、新Fitbitの定常処理を含まない。10月は停止中の処理と検証実行を含むため、通常月の通信量へ外挿しない。監視値からの概算を実際の課金額として記録せず、運用再開後の送信量と請求SKUを同じ期間で照合する。MotherDuck自体の保存・計算無料枠に収まっていても、Cloud Runから東京へ送る料金は別に発生し得る。
