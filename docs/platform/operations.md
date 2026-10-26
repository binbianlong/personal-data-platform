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
分析アカウントの接続名も`personal_data_platform`にそろえ、view・macro内のDB参照を解決する。
preflightは独立したbucket・DB・tokenで手動実行し、本番DB/shareを付与しない。
アプリのMotherDuck接続とdbtのprod接続は`attach_mode=single`で指定DBだけをattachする。
workspaceの他DBを自動attachせず、ローカルDuckDBへの接続方法は変更しない。

## Deploy

[bootstrap](../../infra/bootstrap/README.md)でstate・registry・WIFを作り、[runtime Terraform](../../infra/terraform/README.md)で通常資産を管理する。
secret payloadはTerraform外で登録し、数値versionに固定する。
一時停止はGitHub変数`PDP_SCHEDULERS_ENABLED=false`を設定してDeployを反映するか、手動Terraformで`schedulers_enabled=false`を反映する。
Fitbitの毎時バッチ・OwnTracksのpush配信・日次Scheduler・native警報が停止し、workerも処理を保留する。receiverからの通知はqueueに保持する。

[Deploy workflow](../../.github/workflows/terraform-deploy.yml)はimage digestを固定してruntimeを更新する。
アプリdeploy失敗後の再反映には手動deploy、日次全体の実行には手動入力`run_daily`を使う。
rollbackには[Fitbitの通知・coverage互換性](../sources/fitbit/operations.md#停止と復旧)と現行DBスキーマに対応したimageを使い、削除済みなら再build・登録する。
workflowが停止中の場合はローカルで`linux/amd64` imageを登録し、digestを指定してTerraformを反映する。

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
MacではCollectorを停止してからcheckoutを更新し、`just sync`で依存を同期して再起動する。
Cloud Runのdeploy、次の日次Job、Screen Timeの取込成功heartbeatを確認する。復旧には[Deploy](#deploy)のrollback手順を使う。

## DBの初期化と更新

通常runtimeとscratch DBは共通の現行スキーマを使い、SQLは`src/personal_data_platform/migrations/`を正本とする。

```bash
pdp migrate --database /private/path/scratch.duckdb
```

`--database`を省略すると`MOTHERDUCK_DATABASE`・`MOTHERDUCK_TOKEN`の接続先へ適用する。
各SQLとchecksum記録は同じtransactionで確定する。適用済みSQLや台帳は変更せず、新しいforward migrationを追加する。
再実行時にchecksumを照合し、不一致や未対応のスキーマ履歴があるDBへの適用は拒否する。
未適用SQLは共通の`loader` leaseを使うため、取込が実行中なら終了後に再試行する。
再構築には空のscratch DBを使い、[Rebuild](#rebuild)の保持範囲を確認する。

追加DDLはCollector・Fitbitバッチ・OwnTracksのpush配信・日次Schedulerを止め、実行中Jobと`loader` leaseの終了を確認してから適用する。
MacのCollectorはeditable installのcheckoutを参照するため、そのcheckoutへmigrationを追加する前に停止する。
Collectorは起動後にスキーマを初期化し、成功後の周期では省略する。DDL・コード・接続設定の変更後は再起動し、
取込成功と日次全段階の成功を確認して通常運用を再開する。

## 収集・日次処理

Fitbit通知はPub/Subへ蓄積し、毎時00分に非公開workerがpullしてDB commit後にACKする。
OwnTracksのGCS保存完了通知はPub/Subから非公開workerへpushし、DB commit後に204を返す。
既存martsはViewであり、取込ごとにdbtを実行しなくても次のqueryへ反映される。
`pdp reconciliation`は日次04:10 Asia/Tokyoの処理全体を実行する。

1. Fitbit APIから5種別のTokyo直近2完了日（計10取得単位）を再照合する。
2. dbt run/testを実行する。
3. OwnTracksの無期限Rawと台帳を照合し、未取込分を補修する。
4. Macが更新したScreen Time両streamの取込成功heartbeatを48時間の鮮度とDB relationで監査する。
5. 完了対象日と照合結果を`ops.job_run`へcommitする。
6. Healthchecksへ外部成功pingを1回送る。

Screen Timeの収集・保存・取り込みはMacのCollectorが30分周期で実行する。日次Jobはstream heartbeatを更新しない。
日次成功時に`ops.job_run`の7日を超えた履歴を整理する。
欠測判定に使う最新の日次成功記録は7日を超えても保持する。

workerは最小0・最大1instance、同時処理1。処理予算480秒、サービスtimeout540秒。
Fitbit workerのleaseは600秒、共通Loaderを使うOwnTracks workerのleaseは125分である。
日次Jobは1task/parallelism1、100分の予算、125分のleaseを使う。全処理を単一`loader` leaseで直列化する。
競合はINFOで延期し、成功pingを送らない。dbt・手動取得・Loaderも同じleaseを使う。

## 監視とLogging

native警報は日次Job失敗、Pub/Sub滞留、receiver/worker ERRORの3件。
Pub/Sub滞留はFitbitが75分超、OwnTracksが15分超の最古未ackが5分継続した場合に発報する。
Fitbit workerの成功ログは通知受信時刻・commit時刻・反映までの秒数を含み、Rawやtokenは含めない。
OwnTracksは`base.owntracks_location`の`received_at`と`loaded_at`から保存後の取込時間を確認する。
Healthchecksはdaily 1件、Period 24h＋Grace 24h。新しいeventがないことだけを障害とみなさない。
通常logは_Defaultから`pdp-west` bucketへ1経路で送り、30日保持する。_Requiredと既存backupは維持する。

```bash
gcloud run jobs execute reconciliation-west --project=<project-id> --region=us-west1 --wait
gcloud logging read 'resource.type="cloud_run_job"' --project=<project-id> \
  --location=us-west1 --bucket=pdp-west --view=_AllLogs --limit=50
```

## Rebuild

本番DBを空にせず、保存中のRawを別の空scratch DBへ再生する。Screen TimeはMacのSQLiteを読むため、
Collectorを停止してからinventoryを取得し、再生完了後に再開する。Screen TimeにGCS設定・ADCは不要である。

FitbitはRawを持たない。別のscratch DBへ`pdp fitbit sync --from ... --to ...`で再取得し、
`pdp dbt --source fitbit`と本番との比較で復旧を検証する。Screen TimeのRaw再生は次の手順で行う。

1. `pdp rebuild --source screen_time --all-streams --dry-run`で保持範囲とinventoryを確認する。
2. writer tokenを安全なsecret sourceから`MOTHERDUCK_TOKEN`へ渡し、`pdp rebuild --source screen_time --all-streams --target-db <scratch-db> --allow-partial-history`を実行する。
3. 固定inventoryから再生し、dbt検証後に本番と件数・key・代表martを比較する。途中で対象Rawが消えた場合は失敗させる。
4. 差分確認後に参照先を切り替える。本番と同じDB、既存tableのあるDB、partial history未承認の再構築は拒否する。

OwnTracksは無期限のGCS Rawを専用read-only ADCで読む。`pdp rebuild --source owntracks --dry-run`でinventoryを確認し、`pdp rebuild --source owntracks --target-db <scratch-db> --allow-partial-history`で再生する。

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

障害時は、対象の処理に対応する次の入口から確認する。

| 対象 | 最初に確認する情報 | 復旧判断 |
|---|---|---|
| 日次処理 | `ops.job_run`の最新日次行の`status`・`details.failed_stage` | 失敗段階のログを確認し、原因解消後に日次Jobを再実行する |
| Fitbitの通常取込 | `fitbit_batch`ログの`failed`・`deferred`・`acked`とPub/Sub滞留 | 未ACKは次回バッチで再試行する。7日の保持期限を越えた欠測は期間指定同期する |
| OwnTracks・Screen TimeのRaw取込 | `ops.ingestion_metadata`の失敗行 | Rawを残して原因を修正し、Loaderを再実行する |
| Macの収集停止 | `ops.heartbeat`のScreen Time両streamの成功時刻 | MacのCollectorを確認する。日次Jobの成功でMacの成功時刻は更新されない |

日次の照合結果は同じ行の`details.owntracks`、古いFitbit欠測は`details.manual_repair_required`に含む。
LoaderはRaw単位の台帳とログを使い、別のジョブ履歴は作らない。
Fitbitの手動同期もJSON出力と終了コードで確認し、`ops.job_run`は日次結果の確認に使う。
`ops.job_lock`は排他制御、Fitbitのcoverageと削除記録は取得順位の保護に使う内部状態であり、成功・失敗の判定元には使わない。
競合が続く場合だけleaseの所有者と有効期限を確認し、実行中処理の終了を待つ。

```sql
SELECT run_id, status, started_at, completed_at, details
FROM ops.job_run
WHERE job_name IN ('daily-reconciliation', 'daily-reconciliation-west')
ORDER BY started_at DESC
LIMIT 1;
```

Fitbitバッチ・OwnTracksのpush配信・日次Schedulerを止め、実行中worker/Jobとleaseを確認する。
異常終了で残ったleaseはFitbit workerが10分、OwnTracks worker・日次・手動処理が125分の期限後に再試行する。
日次補完の直近2完了日より前のFitbitの空白は[Fitbitの期間指定補修](../sources/fitbit/operations.md)で補う。
外部ping失敗は日次履歴の`failed_stage="heartbeat"`に記録する。先に確定した取込記録は巻き戻らない。

Screen TimeのDB復元はイベント・補助状態・台帳を同じ時点へ戻す。Rawは各元ファイルの最新版だけを保存するため、
更新前の再解析やRawだけからの全履歴復元を保証しない。復元元のDB・backupは検証が終わるまで保持する。
