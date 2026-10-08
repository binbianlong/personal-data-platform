# Platform運用

## 接続先

| 用途 | 設定 |
|---|---|
| GCP project / region | `health-data-pipeline-503813` / `us-west1` |
| Terraform state | `health-data-pipeline-503813-personal-data-platform-tfstate-west`、prefix `personal-data-platform/runtime` |
| 保持済みRaw bucket / Logging | `health-data-pipeline-503813-pdp-raw-west` / `pdp-west` |
| receiver / worker | `pdp-fitbit-west` / `pdp-fitbit-worker-west` |
| 日次Job | `reconciliation-west` |
| MotherDuck production | `us-west-2`、`personal_data_platform`、owner `pdp_west_prod` |

MotherDuckはPulse/Pulse・read scaling 1、分析接続はrestricted read-only share `pdp_analytics`を使う。
分析アカウントの接続名も`personal_data_platform`にそろえ、view・macro内のDB参照を解決する。
preflightは独立したbucket・DB・tokenで手動実行し、本番DB/shareを付与しない。

## Deploy

[bootstrap](../../infra/bootstrap/README.md)でstate・registry・WIFを作り、[runtime Terraform](../../infra/terraform/README.md)で通常資産を管理する。
secret payloadはTerraform外で登録し、数値versionに固定する。常設preflight/dbt Jobは作らない。
通常はpush配信・日次Scheduler・監視が有効で、`PDP_WEST_SCHEDULERS_ENABLED=false`で一時停止する。
停止時はsubscriptionをpullへ戻して通知を保持し、workerも処理を保留する。

CIは現行Job・receiverのimageをdeployedタグで保護してからcandidate digestへ更新する。
source・dbt・依存・Dockerfileに変更がないpushでは現行日次Jobのdigestを再利用する。
無効な比較元やJob一覧取得失敗はdeployを止める。アプリdeploy失敗後の再反映には手動deployを使う。
手動`run_daily`で日次全体を実行できる。rollbackには直接取込・push受付に対応したimageを使う。

## DBの初期化と更新

通常runtimeとscratch DBは共通の現行スキーマを使い、SQLは`src/personal_data_platform/migrations/`を正本とする。

```bash
pdp migrate --database /private/path/scratch.duckdb
```

`--database`を省略すると`MOTHERDUCK_DATABASE`・`MOTHERDUCK_TOKEN`の接続先へ適用する。
各SQLとchecksum記録は同じtransactionで確定する。再実行は適用済みの一致を確認し、変更は新しいforward migrationへ追加する。
適用済みSQLや台帳は変更しない。checksumの不一致や未対応のスキーマ履歴があるDBへの適用は拒否する。
再構築には空のscratch DBを使い、[Rebuild](#rebuild)の保持範囲を確認する。

追加DDLはCollector・push配信・日次Schedulerを止め、実行中Jobと`loader` leaseの終了を確認してから適用する。
Collectorは起動後にスキーマを初期化し、成功後の周期では省略する。DDL・コード・接続設定の変更後は再起動し、
取込成功と日次全段階の成功を確認して通常運用を再開する。

## 収集・日次処理

通知受信時にPub/Subが非公開workerを呼び、完全取得した範囲のDB commit後に204を返す。
既存martsはViewであり、取込ごとにdbtを実行しなくても次のqueryへ反映される。
`pdp reconciliation`は日次04:10 Asia/Tokyoの処理全体を実行する。

1. APIから5種別のTokyo直近7完了日を再照合する。
2. dbt run/testを実行する。
3. Macが更新したScreen Time両streamの取込成功heartbeatを48時間の鮮度とDB relationで監査する。
4. 完了対象日と内部daily heartbeatを同じtransactionでcommitする。
5. Healthchecksへ外部成功pingを1回送る。

Screen Timeの収集・保存・取り込みはMacのCollectorが30分周期で実行する。日次Jobはstream heartbeatを更新しない。

workerは最小0・最大1instance、同時処理1。処理予算480秒、サービスtimeout540秒、lease600秒である。
日次Jobは1task/parallelism1、100分の予算、125分のleaseを使う。どちらも単一`loader` leaseで直列化する。
競合はINFOで延期し、成功pingを送らない。dbt・手動取得・Loaderも同じleaseを使う。

## 監視とLogging

native警報は日次Job失敗、15分を超えたPub/Sub最古未ackが5分継続、receiver/worker ERRORの3件。
workerの成功ログは通知受信時刻・commit時刻・反映までの秒数を含み、Rawやtokenは含めない。
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
3. 固定inventoryのgenerationで再生し、dbt検証後に本番と件数・key・代表martを比較する。途中でgenerationが消えた場合は失敗させる。
4. 差分確認後に参照先を切り替える。本番と同じDB、既存tableのあるDB、partial history未承認の再構築は拒否する。

## 停止と復旧

push配信と日次Schedulerを止め、実行中worker/Jobとleaseを確認する。
workerの異常終了で残ったleaseは10分、日次・手動処理は125分の期限後に再試行する。
7日を超えたFitbitの空白は[Fitbitの期間指定補修](../sources/fitbit/operations.md)で補う。
DB成功と外部pingが一致しない場合はrun_id・log・監査を照合する。外部送信失敗で先に確定した取込・期限切れ記録は巻き戻らない。

Screen TimeのDB復元はイベント・補助状態・台帳を同じ時点へ戻す。Rawは各元ファイルの最新版だけを保存するため、
更新前の再解析やRawだけからの全履歴復元を保証しない。復元元のDB・backupは検証が終わるまで保持する。

## Fitbitのpush切替と既存Raw整理

1. 旧毎時・日次Schedulerを停止し、実行中Jobとleaseの終了を確認する。
2. 旧毎時JobのTerraform `deletion_protection`を解除してから撤去する。
3. 非公開workerとIAMを作り、既存subscriptionを名前・未ack通知を維持してpushへ変更する。
4. 実通知、日次全体、scratchへの期間指定再取得を検証する。
5. 旧・現行Raw bucketの`raw/fitbit/`配下を全version対象で削除する。別のRawコピーは作らない。
6. 日次Schedulerを再開し、Rawが再作成されないことを確認する。

bucket本体、Terraform state、preflight、Screen Timeはこの整理の対象に含めない。
