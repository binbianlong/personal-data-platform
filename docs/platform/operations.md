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
secret payloadはTerraform外で登録し、数値versionに固定する。常設preflight/dbt Jobは作らない。
通常はpush配信・日次Scheduler・監視が有効で、`PDP_WEST_SCHEDULERS_ENABLED=false`で一時停止する。
停止時はsubscriptionをpullへ戻して通知を保持し、workerも処理を保留する。

CIはimageをbuild・登録し、そのdigestを指定してruntimeを更新する。
source・dbt・依存・Dockerfileに変更がないpushでは現行日次Jobのdigestを再利用する。
無効な比較元やJob一覧取得失敗はdeployを止める。アプリdeploy失敗後の再反映には手動deployを使う。
手動`run_daily`で日次全体を実行できる。rollbackには直接取込・push受付に対応したimageを使い、削除済みなら再build・登録する。
Deploy workflowが停止中の場合は、ローカルで`linux/amd64` imageを登録し、digestを指定してTerraformを手動反映する。

## DBの初期化と更新

通常runtimeとscratch DBは共通の現行スキーマを使い、SQLは`src/personal_data_platform/migrations/`を正本とする。

```bash
pdp migrate --database /private/path/scratch.duckdb
```

`--database`を省略すると`MOTHERDUCK_DATABASE`・`MOTHERDUCK_TOKEN`の接続先へ適用する。
各SQLとchecksum記録は同じtransactionで確定する。再実行は適用済みの一致を確認し、変更は新しいforward migrationへ追加する。
全SQLが適用済みの場合は、台帳の構造・ID・checksumの確認だけで終了し、DDLは実行しない。
適用済みSQLや台帳は変更しない。checksumの不一致や未対応のスキーマ履歴があるDBへの適用は拒否する。
再構築には空のscratch DBを使い、[Rebuild](#rebuild)の保持範囲を確認する。

追加DDLはCollector・push配信・日次Schedulerを止め、実行中Jobと`loader` leaseの終了を確認してから適用する。
MacのCollectorはeditable installのcheckoutを参照するため、そのcheckoutへmigrationを追加する前に停止する。
Collectorは起動後にスキーマを初期化し、成功後の周期では省略する。DDL・コード・接続設定の変更後は再起動し、
取込成功と日次全段階の成功を確認して通常運用を再開する。

## 収集・日次処理

Fitbit通知とOwnTracksのGCS保存完了通知はPub/Subから非公開workerへ配信し、DB commit後に204を返す。
既存martsはViewであり、取込ごとにdbtを実行しなくても次のqueryへ反映される。
`pdp reconciliation`は日次04:10 Asia/Tokyoの処理全体を実行する。

1. Fitbit APIから5種別のTokyo直近2完了日（計10取得単位）を再照合する。
2. dbt run/testを実行する。
3. OwnTracksの無期限Rawと台帳を照合し、未取込分を補修する。
4. Macが更新したScreen Time両streamの取込成功heartbeatを48時間の鮮度とDB relationで監査する。
5. 完了対象日と内部daily heartbeatを同じtransactionでcommitする。
6. Healthchecksへ外部成功pingを1回送る。

Screen Timeの収集・保存・取り込みはMacのCollectorが30分周期で実行する。日次Jobはstream heartbeatを更新しない。
日次成功時に`ops.job_run`と`ops.reconciliation_run`の7日を超えた履歴を整理する。
欠測判定に使う最新の日次成功記録は7日を超えても保持する。

workerは最小0・最大1instance、同時処理1。処理予算480秒、サービスtimeout540秒。
Fitbit workerのleaseは600秒、共通Loaderを使うOwnTracks workerのleaseは125分である。
日次Jobは1task/parallelism1、100分の予算、125分のleaseを使う。全処理を単一`loader` leaseで直列化する。
競合はINFOで延期し、成功pingを送らない。dbt・手動取得・Loaderも同じleaseを使う。
FitbitはAPIの各HTTP要求前、書き込み開始前、COMMIT前にDB上のleaseを確認する。
実行予算だけの確認はローカルで行い、同じAPI要求でleaseを二重照会しない。
取得完了後の予算判定、所有権・期限不足による延期、書き込み途中のROLLBACKは維持する。

## 監視とLogging

native警報は日次Job失敗、Pub/Sub滞留、receiver/worker ERRORの3件。
Pub/Sub滞留はFitbitが75分超、OwnTracksが15分超の最古未ackが5分継続した場合に発報する。
Fitbit workerの成功ログは通知受信時刻・commit時刻・反映までの秒数を含み、Rawやtokenは含めない。
OwnTracksは`base.owntracks_location`の`received_at`と`loaded_at`から保存後の取込時間を確認する。
Healthchecksはdaily 1件、Period 24h＋Grace 24h。新しいeventがないことだけを障害とみなさない。
通常logは_Defaultから西部`pdp-west` bucketへ1経路で送り、30日保持する。_Requiredと既存backupは維持する。

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

Fitbit workerと日次Jobは`PDP_NETWORK_DIAGNOSTICS=true`で、一実行につき一つの
`network_profile` JSONログを出す。通常は無効とし、調査時に対象のruntimeだけで有効にする。
停止するには環境変数を削除するか`false`へ戻す。権限・DBスキーマ・取得範囲は変更しない。

`summary.sent_bytes`と`received_bytes`は`/proc/net/dev`の非loopbackインターフェースの増分。
`phases`はDB接続、スキーマ確認、ロック確認・更新、coverage読取・更新、削除履歴、
既存データ比較、データ書込、BEGIN/COMMIT/ROLLBACK、API、Pub/Subを区別する。
日次は`daily_fitbit`、`daily_dbt/dbt_run`、`daily_dbt/dbt_test`、
`daily_owntracks`、`daily_screen_time`の段階も記録する。`/`で区切られた入れ子は
排他的な計測区間で、親子の通信量を重複計上しない。`calls`は計測区間数でSQL件数ではない。
Fitbitの`correlation_id`は既存の`batch_id`へ対応する。

これはInternet課金量そのものではない。Google向け通信やバックグラウンド通信を含み、
非同期送信は後続の区間・`unattributed`へ入る場合がある。実行前のstartupとHTTP応答後の
通信も含まれないため、Cloud Monitoringの`run.googleapis.com/container/network/sent_bytes_count`
（`kind=internet`と`google`）と集計範囲を合わせて比較する。受信量をegressへ加算しない。
SQL・引数・健康値・認証情報は保存しない。カウンタが読めない・巻き戻る・interfaceが変わる場合は
`valid=false`、送受信量は`null`とし、取り込み処理の成功・失敗は変えない。

```text
jsonPayload.event="network_profile"
jsonPayload.summary.workload="fitbit_batch"
jsonPayload.summary.valid=true
```

日次は`workload="daily"`で検索する。計測の合計と各phaseを比較し、正常な0 byteと
計測不能の`null`を区別する。SQL回数の減少率をegress削減率として扱わない。

## 停止と復旧

push配信と日次Schedulerを止め、実行中worker/Jobとleaseを確認する。
異常終了で残ったleaseはFitbit workerが10分、OwnTracks worker・日次・手動処理が125分の期限後に再試行する。
日次補完の直近2完了日より前のFitbitの空白は[Fitbitの期間指定補修](../sources/fitbit/operations.md)で補う。
DB成功と外部pingが一致しない場合はrun_id・log・監査を照合する。外部送信失敗で先に確定した取込・期限切れ記録は巻き戻らない。

Screen TimeのDB復元はイベント・補助状態・台帳を同じ時点へ戻す。Rawは各元ファイルの最新版だけを保存するため、
更新前の再解析やRawだけからの全履歴復元を保証しない。復元元のDB・backupは検証が終わるまで保持する。
