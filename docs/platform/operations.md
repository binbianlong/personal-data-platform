# Platform運用

## 接続先

| 用途 | 設定 |
|---|---|
| GCP project / region | `health-data-pipeline-503813` / `us-west1` |
| Terraform state | `health-data-pipeline-503813-personal-data-platform-tfstate-west`、prefix `personal-data-platform/runtime` |
| Raw / Logging | `health-data-pipeline-503813-pdp-raw-west` / `pdp-west` |
| 毎時 / 日次Job | `fitbit-hourly-west` / `reconciliation-west` |
| MotherDuck production | `us-west-2`、`personal_data_platform_west`、owner `pdp_west_prod` |

MotherDuckはPulse/Pulse・read scaling 1、分析接続はrestricted read-only share `pdp_analytics_west`を使う。
preflightは独立したbucket・DB・tokenで手動実行し、本番DB/shareを付与しない。

## Deploy

[bootstrap](../../infra/bootstrap/README.md)でstate・registry・WIFを作り、[runtime Terraform](../../infra/terraform/README.md)で通常資産を管理する。
secret payloadはTerraform外で登録し、数値versionに固定する。常設preflight/dbt Jobは作らない。
通常はScheduler・監視が有効で、`PDP_WEST_SCHEDULERS_ENABLED=false`で一時停止する。

CIは現行Job・receiverのimageをdeployedタグで保護してからcandidate digestへ更新する。
source・dbt・依存・Dockerfileに変更がないpushでは現行日次Jobのdigestを再利用する。
無効な比較元やJob一覧取得失敗はdeployを止める。アプリdeploy失敗後の再反映には手動deployを使う。
手動`run_daily`で日次全体を実行できる。rollbackにもRaw v3対応imageを使う。

## DBの初期化と更新

通常runtimeとscratch DBは`PDP_SCHEMA_PROFILE=west`を使い、SQLは`src/personal_data_platform/migrations/west/`を正本とする。

```bash
pdp fitbit migrate --database /private/path/west-scratch.duckdb --profile west
```

`--database`を省略すると`MOTHERDUCK_DATABASE`・`MOTHERDUCK_TOKEN`の接続先へ適用する。
各SQLとchecksum記録は同じtransactionで確定する。再実行は適用済みの一致を確認し、変更は新しいforward migrationへ追加する。
既存SQLや旧DBの台帳は変更しない。

スキーマ更新は両Schedulerを止め、実行中Jobの終了を確認してからruntime・migration・dbtを反映する。
独立環境のpreflightと、対象DBの日次全段階の成功を確認してSchedulerを再開する。

## 収集・日次処理

毎時15分のJobは通知を集め、完全取得したRawとDBが確定した単位だけackする。
`pdp reconciliation`は日次04:10 Asia/Tokyoの処理全体を実行する。

1. Screen Time両streamのRawをgeneration指定で取り込む。
2. 未取込Fitbit Rawを再生し、5種別のTokyo直近7完了日を再照合する。
3. dbt run/testを実行する。
4. Screen Time両streamのRaw・台帳・relation・48時間のcollector鮮度を監査する。
5. 完了対象日と内部daily/両stream heartbeatを同じtransactionでcommitする。
6. Healthchecksへ外部成功pingを1回送る。

各Jobは1task/parallelism1。毎時50分・日次100分の予算で、単一`loader` leaseを125分保持する。
競合はINFOで延期し、成功pingを送らない。dbt・手動取得・Loaderも同じleaseを使う。

## 監視とLogging

native警報はJob失敗、24時間を超えたPub/Sub最古未ack、receiver ERRORの3件。
Healthchecksはdaily 1件、Period 24h＋Grace 24h。新しいeventがないことだけを障害とみなさない。
通常logは_Defaultから西部`pdp-west` bucketへ1経路で送り、30日保持する。_Requiredと既存backupは維持する。

```bash
gcloud run jobs execute fitbit-hourly-west --project=<project-id> --region=us-west1 --wait
gcloud run jobs execute reconciliation-west --project=<project-id> --region=us-west1 --wait
gcloud logging read 'resource.type="cloud_run_job"' --project=<project-id> \
  --location=us-west1 --bucket=pdp-west --view=_AllLogs --limit=50
```

## Rebuild

本番DBを空にせず、現在GCSに残るRawを別の空scratch DBへ再生する。
Collectorのwrite-only ADCを再利用せず、Terraform output `rebuild_operator_service_account`のread-only SAを使う。

```bash
export GOOGLE_CLOUD_PROJECT="health-data-pipeline-503813"
export GCS_BUCKET="${GOOGLE_CLOUD_PROJECT}-pdp-raw-west"
export MOTHERDUCK_DATABASE="personal_data_platform_west"
export PDP_REBUILD_SERVICE_ACCOUNT_EMAIL="raw-rebuild-operator@${GOOGLE_CLOUD_PROJECT}.iam.gserviceaccount.com"
export CLOUDSDK_CONFIG="$HOME/Library/Application Support/personal-data-platform/gcloud-rebuild"
mkdir -p "$CLOUDSDK_CONFIG"
chmod 700 "$CLOUDSDK_CONFIG"
gcloud auth application-default login \
  --impersonate-service-account="$PDP_REBUILD_SERVICE_ACCOUNT_EMAIL"
export PDP_REBUILD_GOOGLE_APPLICATION_CREDENTIALS="$CLOUDSDK_CONFIG/application_default_credentials.json"
chmod 600 "$PDP_REBUILD_GOOGLE_APPLICATION_CREDENTIALS"
```

ADCはimpersonated形式、現在user所有、mode 0600、指定SA一致を検証する。CollectorのADCは変更しない。

1. `pdp rebuild --source screen_time --all-streams --dry-run`で保持範囲とinventoryを確認する。
2. writer tokenを安全なsecret sourceから`MOTHERDUCK_TOKEN`へ渡し、`pdp rebuild --source screen_time --all-streams --target-db <scratch-db> --allow-partial-history`を実行する。
3. 固定inventoryのgenerationで再生し、dbt検証後に本番と件数・key・代表martを比較する。途中でgenerationが消えた場合は失敗させる。
4. 差分確認後に参照先を切り替える。本番と同じDB、既存tableのあるDB、partial history未承認の再構築は拒否する。

## 停止と復旧

両Schedulerを止め、実行中Jobとleaseを確認する。異常終了で残ったleaseは125分の期限後に再試行する。
毒性Rawは削除せず、decoder修正後に同じgenerationを再処理する。
7日を超えたFitbitの空白は[Fitbitの期間指定補修](../sources/fitbit/operations.md)で補う。
DB成功と外部pingが一致しない場合はrun_id・log・監査を照合する。外部送信失敗で先に確定した取込・期限切れ記録は巻き戻らない。

復旧用backupは`var/west-migration/2026-10-05/`。旧Raw bucket・state backupは通常deployで削除しない。
Screen TimeのDB復元はイベント・補助状態・台帳を同じ時点へ戻し、Rawだけで90日より古い履歴を復元できるとはみなさない。
