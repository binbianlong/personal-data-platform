# Platform運用

## 接続先

| 用途 | 設定 |
|---|---|
| GCP project / region | `health-data-pipeline-503813` / `us-west1` |
| Terraform state | `health-data-pipeline-503813-personal-data-platform-tfstate-west`、prefix `personal-data-platform/runtime` |
| Raw bucket / Logging | `health-data-pipeline-503813-pdp-raw-west` / `pdp-west` |
| Fitbit receiver / worker | `pdp-fitbit-west` / `pdp-fitbit-worker-west` |
| OwnTracks receiver / worker | `pdp-owntracks-west` / `pdp-owntracks-worker-west` |
| 日次Job | `reconciliation-west` |
| MotherDuck production | `us-west-2`、`personal_data_platform`、owner `pdp_west_prod` |

MotherDuckはPulse/Pulse・read scaling 1、分析接続はrestricted read-only share `pdp_analytics`を使う。
分析アカウントの接続名も`personal_data_platform`にそろえる。martsの参照はschema修飾とし、特定のcatalogに依存させない。
preflightは独立したbucket・DB・tokenで手動実行し、本番DB/shareを付与しない。
アプリのMotherDuck接続とdbtのprod接続は`attach_mode=single`で指定DBだけをattachする。
workspaceの他DBを自動attachせず、ローカルDuckDBへの接続方法は変更しない。

## Deploy

[bootstrap](../../infra/bootstrap/README.md)でstate・registry・WIFを作り、[runtime Terraform](../../infra/terraform/README.md)で通常資産を管理する。
secret payloadはTerraform外で登録し、数値versionに固定する。
一時停止はGitHub変数`PDP_SCHEDULERS_ENABLED=false`を設定してDeployを反映するか、手動Terraformで`schedulers_enabled=false`を反映する。
Fitbitの30分バッチ・OwnTracksのpush配信・日次Scheduler・native警報が停止し、workerも処理を保留する。receiverからの通知はqueueに保持する。

[Deploy workflow](../../.github/workflows/terraform-deploy.yml)はimage digestを固定してruntimeを更新する。
本番DBに対して最後に反映できた `dbt/` の内容が変わったときは、更新した日次Jobを `dbt` 引数で一度実行し、ビューを作成・テストする。
反映した版は成功後にstate bucketの `personal-data-platform/deploy/dbt-version` に記録する。記録がない初回も実行する。
再反映には手動deployを使い、ビューの強制再適用は `run_dbt`、日次全体の実行は `run_daily` を選ぶ。
rollbackには[Fitbitの通知・取得方式の互換性](../sources/fitbit/operations.md#停止と復旧)と現行DBスキーマに対応したimageを使い、削除済みなら再build・登録する。
workflowが停止中の場合はローカルで`linux/amd64` imageを登録し、digestを指定してTerraformを反映する。
手動切替も、登録前に稼働中のimage、切替前に新しいdigestを保護する。
`GCP_PROJECT_ID`・`GCP_REGION`・`ARTIFACT_REPOSITORY`・`IMAGE_NAME` を設定し、次のコマンドを使う。

```bash
python3 .github/scripts/runtime-image-tags.py protect
# imageをbuild・pushしてdigestを確認した後
python3 .github/scripts/runtime-image-tags.py protect "$image_digest_uri"
```

全runtimeを切り替えた手動デプロイは、確認後に同じスクリプトを `finish "$image_digest_uri"` で実行する。
一部のサービスだけ切り替えた場合、保護の整理は全runtimeの次回Deployに任せる。
手動デプロイやrollbackでビュー定義も切り替える場合は、state bucketの反映記録を削除してから、
切り替えた `reconciliation-west` を `gcloud run jobs execute reconciliation-west --args=dbt --wait` で実行する。
次回のDeployはビュー定義を再適用して記録し直す。

## 依存関係の更新

更新対象・schedule・group・承認条件は[Renovate設定](../../renovate.json)を正本とする。
通常更新は四半期ごと、既知の脆弱性修正は随時対応し、すべて差分とCIを確認して手動マージする。
`uv.lock`全体更新は間接依存も対象となり、個別依存のDashboard承認や14日待機で対象を絞らない。
commitに固定した`ccl-segb`の参照先も個別確認する。

GitHubのDependency graphとDependabot alertsを有効にし、PR作成はRenovateへ一本化する。
`pep621` managerはlockfileにしかない間接依存のalertから修正PRを作れないため、
修正versionを確認して`uv lock --upgrade-package <package>`で更新する。
上位依存の制約で更新できない場合はその依存も確認する。alert対象外のOS package等はこの経路では検知できない。

`uv.lock`の変更は開発用依存だけでも本番imageの再build・deployを起動するため、deploy結果を確認できる日にマージする。
Macの稼働用checkoutは`~/Library/Application Support/personal-data-platform/runtime`とし、検証済みcommitのdetached HEADで稼働する。
Collectorを停止してからそのcheckoutを更新し、`uv sync --locked --no-dev`で依存を同期して再起動する。
Cloud Runのdeploy、次の日次Job、Screen TimeのCollector実行ログを確認する。復旧には[Deploy](#deploy)のrollback手順を使う。

## DBの初期化と更新

通常runtimeとscratch DBは共通の現行スキーマを使い、SQLは`src/personal_data_platform/migrations/`を正本とする。

```bash
pdp migrate --database /private/path/scratch.duckdb
```

`--database`を省略すると`MOTHERDUCK_DATABASE`・`MOTHERDUCK_TOKEN`の接続先へ適用する。
各SQLとchecksum記録は同じtransactionで確定する。適用済みSQLや台帳は変更せず、新しいforward migrationを追加する。
再実行時にchecksumを照合し、不一致や未対応のスキーマ履歴があるDBへの適用は拒否する。
通常の取込とdbtは空のDBを初期化できる。既存DBに未適用SQLがあると開始時の照合で停止する。
再構築には空のscratch DBを使い、[Rebuild](#rebuild)の保持範囲を確認する。

既存DBのスキーマを更新するときは、両Collector・Fitbitバッチ・OwnTracksのpush配信・日次Schedulerを止める。手動同期やdbtを含め実行中の処理が終了してから、新しい版で`pdp migrate`を実行する。
MacのCollectorはeditable installのcheckoutを参照するため、そのcheckoutへmigrationを追加する前に停止する。
適用済みmigrationを認識しない旧コードはDB接続時のschema照合で失敗するため、再開前に全Cloud Run runtimeのimageとCollectorのcheckout・依存を更新する。
Collectorは起動後にスキーマを初期化し、成功後の周期では省略する。DDL・コード・接続設定の変更後は再起動し、
取込成功と日次全段階の成功を確認して通常運用を再開する。

## 収集・日次処理

Fitbit通知はPub/Subへ蓄積し、毎時00分・30分に非公開workerがpullしてDB commit後にACKする。
OwnTracksのGCS保存完了通知はPub/Subから非公開workerへpushし、DB commit後に204を返す。
既存martsはViewであり、取込ごとにdbtを実行しなくても次のqueryへ反映される。
`pdp reconciliation`は日次04:10 Asia/Tokyoの処理全体を実行する。

1. Fitbit APIから16種別のTokyo直近2完了日を再照合する。
2. OwnTracksの無期限Rawとbase IDを照合し、未取込分を補修する。
3. iPhone・Macの必須relationが存在しqueryできることを検査する。Macの稼働鮮度の監査ではない。
4. 歩数ヒートマップを生成し、Healthchecksへ外部成功pingを1回送る。
5. 成功・失敗・延期と各段階の結果をJSONログへ残す。

Fitbitの取得やOwnTracksの補修で失敗結果が返っても、DB接続が利用できれば残りのソースを検査する。
失敗があれば歩数ヒートマップの更新と成功pingを省き、結果を残してジョブを失敗で終了する。
DB接続が使えなくなった場合は後続処理を止める。

Screen Timeの収集・保存・取り込みはMacの独立した2つのCollectorが各30分周期で実行する。
DBのjob履歴・source heartbeat・最終成功日checkpointは持たない。日次は同じ日でも再実行できる。
通常補修より古いFitbit欠落は、過去の実行ログを確認して期間指定で補修する。

workerは最小0・最大1instance、同時処理1。処理予算480秒、サービスtimeout540秒。
Fitbitのleaseは通知workerが600秒、日次補修と手動同期が125分で、処理終了時に解放する。
日次Jobは1task/parallelism1、全体の予算は100分。Fitbit補修が終わるとleaseを解放してOwnTracksの補修に進む。
別のFitbit処理がleaseを保持していればFitbit補修だけを延期し、OwnTracksの補修とScreen Timeの検査を実行する。他に失敗がなければジョブの結果は `deferred` となり、歩数ヒートマップの更新と成功pingを省く。

## 監視とLogging

native警報は日次Job失敗、Pub/Sub滞留、receiver/worker ERRORの3件。
Pub/Sub滞留はFitbitが90分超、OwnTracksが15分超の最古未ackが5分継続した場合に発報する。
Fitbit workerの成功ログは通知受信時刻・commit時刻・反映までの秒数を含み、Rawやtokenは含めない。
OwnTracksはworkerログとPub/Sub滞留を確認する。baseには受信・取り込みの管理時刻を保存しない。
Healthchecksはdaily 1件、Period 24h＋Grace 24h。新しいeventがないことだけを障害とみなさない。
通常logは_Defaultから`pdp-west` bucketへ1経路で送り、30日保持する。_Requiredは維持する。
日次の`event="reconciliation"`は`running`とterminalの`succeeded` / `failed` / `deferred`を残す。
`summary`にrun_id・対象日・開始/終了・失敗段階と取得済み結果を含み、通常のLOG_LEVELを下げても必須履歴は残る。

```bash
gcloud run jobs execute reconciliation-west --project=<project-id> --region=us-west1 --wait
gcloud logging read 'resource.type="cloud_run_job"' --project=<project-id> \
  --location=us-west1 --bucket=pdp-west --view=_AllLogs --limit=50
```

## Rebuild

OwnTracksは保存済みGCS Rawを専用read-only ADCで読み、空のscratch DBへ再生できる。

```bash
pdp rebuild --source owntracks --dry-run
pdp rebuild --source owntracks --target-db <scratch-db> --allow-partial-history
```

本番と同名・既存データのあるtargetへの再生は拒否する。固定したgenerationのRawを使い、検証後に参照先を切り替える。
parserの修正は`pdp loader --source owntracks --prefix raw/owntracks/v1/... --reprocess`で明示する。

FitbitはRawを持たない。scratch DBへ`pdp fitbit sync --from ... --to ...`でAPI取得し直し、dbtとbaseの値を検証する。
Screen Timeのpendingファイルは送信後に削除するため、全履歴を再構築する入力ではない。
Screen Timeの全履歴移行では、canonical baseと各SQLiteの参照情報を引き継ぐ。Rawだけを再生するrebuildは実行しない。
保持したRaw・APIで再取得できる範囲・既存baseで、復元可能なデータは異なる。

## 処理別ネットワーク計測

Fitbit workerと日次Jobは`PDP_NETWORK_DIAGNOSTICS=true`で、一実行につき一つの`network_profile` JSONログを出す。
通常は無効とし、調査対象のruntimeだけで有効にする。終了時は環境変数を削除するか`false`へ戻す。
SQL・引数・健康値・認証情報は保存しない。

```text
jsonPayload.event="network_profile"
jsonPayload.summary.workload="fitbit_batch"
jsonPayload.summary.valid=true
```

日次は`workload="daily"`で検索する。Fitbitの`correlation_id`は`batch_id`に対応する。
`summary.sent_bytes`と`received_bytes`は`/proc/net/dev`の非loopbackインターフェースの増分で、Internet課金量そのものではない。
Google向け・バックグラウンド通信を含み、startupとHTTP応答後の通信は含まれない。
Cloud Monitoringの`run.googleapis.com/container/network/sent_bytes_count`（`kind=internet`と`google`）と集計範囲を合わせて比較し、受信量をegressへ加算しない。

`phases`は排他的な計測区間で、`/`区切りの親子を重複計上しない。非同期通信は後続区間や`unattributed`へ入る場合がある。
`calls`は区間数でSQL件数ではなく、SQL回数の減少率をegress削減率として扱わない。
カウンタが読めない・巻き戻る・interfaceが変わる場合は`valid=false`、送受信量は`null`となる。正常な0 byteと区別する。
計測不能でも取込処理の成功・失敗は変えない。

## 停止と復旧

障害時は次の情報を確認する。

| 対象 | 確認する情報 | 復旧判断 |
|---|---|---|
| 日次処理 | reconciliationログのstatusとsummary.failed_stage | 原因を修正し、同じ日次Jobを再実行する |
| Fitbit | fitbit_batchログとPub/Sub滞留 | 未ACKは再試行。古い欠落は期間指定でAPI再取得する |
| OwnTracks | workerログ・Pub/Sub滞留・Raw/base ID差分 | Rawを残し、欠落修復または明示reprocessを行う |
| iPhone / Mac Collector | Macの各Collector stdout/stderrとSQLiteのpending | 同じbytesを保持し再送する。Mac停止をDBイベント時刻から判定しない |

Fitbitの競合が続く場合は`ops.job_lock`の所有者と有効期限を確認する。別ownerや稼働中のleaseを削除しない。

```sql
SELECT job_name, owner_id, expires_at FROM ops.job_lock;
```

Fitbitバッチ・OwnTracksのpush配信・日次SchedulerとMacの両Collectorを停止し、実行中worker/Jobの終了を確認する。
Fitbitの異常終了でleaseが残った場合は期限後に再試行する。外部ping失敗はログの`failed_stage="heartbeat"`に残り、先に確定したデータは巻き戻らない。
