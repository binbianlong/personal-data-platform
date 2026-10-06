# 西部リージョン移行記録

2026-10-06更新。最小構成の実装と、西部環境での移行・復元試験を完了した。定期処理は停止状態であり、provider URL、Mac collector、分析/MCPの最終切替は未実施である。[設計書](../superpowers/specs/2026-10-05-pubsub-gcs-migration-design.md)と[移行計画](../superpowers/plans/2026-10-05-pubsub-gcs-migration-plan.md)に従い、最終差分を反映してから定期処理を開始する。

## 接続先と準備状態

GCP projectは`health-data-pipeline-503813`。地域を選べる資産は`us-west1`、MotherDuckは`us-west-2`を使用する。

| 用途 | 移行先・現在の状態 |
| --- | --- |
| Terraform state | `health-data-pipeline-503813-personal-data-platform-tfstate-west`。prefix `personal-data-platform/runtime`と旧resource IDを維持 |
| Registry | `us-west1`の`personal-data-platform` |
| Raw | `health-data-pipeline-503813-pdp-raw-west`、90日、soft-delete 0、Screen Timeは元の保持起点を維持 |
| 手動preflight | 独立bucket/DB/token。常設Jobなし、tokenのSecret Manager containerは未使用・payloadなし |
| 通知 | `pdp-fitbit-west` topic、`pdp-fitbit-west-pull` subscription。us-west1、enforceInTransit、保持7日、期限切れなし |
| Receiver | `pdp-fitbit-west`。処理APIやDB資格情報を持たず、認証後に日付×種別の通知を発行 |
| Job/Scheduler | `fitbit-hourly-west`と`reconciliation-west`。共通runtime SA。Schedulerは毎時15分と日次04:10 JST、両方停止中 |
| Logging | `pdp-west`、30日保持。通常logのroutingは切替時に変更 |
| MotherDuck production | `personal_data_platform_west`、owner `pdp_west_prod`、Pulse |
| MotherDuck preflight | `personal_data_platform_west_preflight`、owner `pdp_west_preflight`、Pulse。本番DBへのATTACH拒否を確認 |
| 欠損監視 | Healthchecks `pdp-daily` 1件、POSTのみ、Period 24h＋Grace 24h、自分のemail integration |

Secret Managerの通常参照はproduction MotherDuck、OAuth、Webhook、日次pingの4 payloadで、すべて数値version `1`。管理用APIキーをruntimeへ渡さない。値とstate、健康データはGit対象外のprivate artifactへ保存している。

GitHubの`TF_STATE_BUCKET`は西部へ変更済み。remoteのTerraform/workflowが西部構成に未対応のため、Terraform Plan/Deployは停止中。対応revisionの反映とplan確認後に再開する。旧state bucketとrollback用backupは保持する。

## Releaseと確認結果

Raw v3対応releaseはcommit `d34d841`。imageは`us-west1-docker.pkg.dev/health-data-pipeline-503813/personal-data-platform/personal-data-platform:deployed-minimal-d34d841`、digest `sha256:7e73844d4a36a41482d699c05eb95e51f804f1cd83f3c8d4bafaa8c0436bb181`。linux/amd64のRaw v3・west 5 migration・CLI smokeを確認した。Raw v2時点のimage `66a090…`をRaw v3のrollbackへ使わない。

- Python 794件、Ruff check/format、strict mypy、wheel buildを確認。Terraform bootstrap/runtime/GitHub rootのvalidate/testは2/28/1件PASS。
- 西部production/preflightに001〜005を適用。適用済み001〜004 checksumを維持し、空の通知・attempt・bundle・cursor等10表だけ撤去。旧東京DBのmigration台帳とSQLは変更していない。
- source exportとtarget importを別processで実行。初回Screen Time snapshotの9表243,898行を本番DBへ取り込み、全値・Raw generation・保持起点を照合した。active lease 0件。
- Screen Time Raw 53 object、圧縮6,787,430 bytesをコピーし、圧縮/展開後hashと保持起点を照合。control 4 object、727 bytesはbackupのみで、西部公開はcollector切替時に行う。
- 独立preflightのGCS・DDL/DML roundtripと、本番DBへの接続拒否を確認。検証資格情報を本番へ転用していない。
- 2026-10-04 Tokyoの1完了日・5種別を実API取得し、Raw v3を1 object保存。歩数112行、active zone 3行、分心拍1,418行、安静時心拍1行、睡眠1行・stage 23行・wake 22行。dbt run/test 39件PASS。
- 新しい空のローカルDBへ同じScreen Time snapshotと保存Rawを復元。全業務値digestと39 dbt testが一致し、active lease 0件。移行用export/importとRaw再生の復元経路を確認した。
- Healthchecksの短い試験周期でup→grace→down→upを確認し、24h＋24hへ復元した。native警報は制御した失敗・ERROR log・滞留で発火を確認。Job失敗とreceiver ERRORは復旧済み、滞留の復旧は実Jobで確認中。メール受信箱への到達は未確認。

Pub/Subの初回pullが空でも収集期限内に再試行し、実行期限を越えない回帰テストを追加した。Webhookの検証応答は現行Google Health APIの201に合わせた。停止状態でのdeployは保護bucketを置換せず、新2 Job以外の常設Jobを追加していない。

## 最終切替と保全

旧Schedulerは停止中。旧provider URLとMac collectorは旧bucketへの送信を続けているため、初回snapshotを最終差分として扱わない。旧writerの終了とlease 0を確認し、collector停止後にRaw・warehouse・SQLiteを再backupする。その後、別processのexport/import、9表の全値・期間・generation照合を行う。

旧URLを一時的に西部Pub/Subへ発行するreceiverへ更新し、provider URL、西部control、collector、restricted read-only分析share、MCPを順に切り替える。毎時のcommit後ack、日次の全段階・両stream監査・1 heartbeatを確認してSchedulerと通常Loggingを開始する。

旧Fitbitの削除対象は旧DBのFitbit scope、`raw/fitbit/v1/`、`receipts/fitbit/v1/`、旧queueだけ。Screen Time、共通台帳、新Pub/Sub、新Raw、旧組織を削除対象に含めない。共有secretは実際の使用元を照合してから整理する。

初回backupと最終backup、SQLite integrity_check、Raw manifest、復元proof、apply/monitoring結果は`var/west-migration/2026-10-05/`に保存する。secretや健康データをこの記録へ転記しない。

## MotherDuck向け通信の料金と移行前計測

2026-10-05にCloud Billing Catalog APIでCloud Run専用のインターネット転送SKUを確認した。米国から東京への大陸間通信は最初から$0.12/GiB（SKU `DBAA-7594-B9FA`）、北米内の通信は月1GiBまで無料、その後は$0.105/GiB（SKU `DDFD-AE42-E219`）。いずれも最初の有料帯のUSD単価であり、北米内でも同じOregonだから無制限に無料とは扱わない。[Cloud Run料金](https://cloud.google.com/run/pricing)、[大陸間転送SKU](https://cloud.google.com/skus?currency=USD&filter=DBAA-7594-B9FA)、[北米内転送SKU](https://cloud.google.com/skus?currency=USD&filter=DDFD-AE42-E219)

同projectの`run.googleapis.com/container/network/sent_bytes_count`を`kind=internet`で集計した。JobとServiceを含むCloud Run全体の監視値であり、MotherDuckだけの通信量や請求明細ではない。

| 計測期間（JST） | 外向きインターネット送信 | 全量を東京向けと仮定した概算 |
| --- | ---: | ---: |
| 2026-09-01 00:00〜2026-10-01 00:00 | 418,676,667 bytes（0.390 GiB） | 約$0.047 |
| 2026-10-01 00:00〜2026-10-05 20:56 | 144,666,580 bytes（0.135 GiB） | 約$0.016 |

9月は旧構成の計測で、新Fitbitの定常処理を含まない。10月は停止中の処理と検証実行を含むため、通常月の通信量へ外挿しない。監視値からの概算を実際の課金額として記録せず、運用再開後の送信量と請求SKUを同じ期間で照合する。MotherDuck自体の保存・計算無料枠に収まっていても、Cloud Runから東京へ送る料金は別に発生し得る。
