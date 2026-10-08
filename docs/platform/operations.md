# Platform運用

## 接続先

| 用途 | 設定 |
|---|---|
| GCP project / region | `health-data-pipeline-503813` / `us-west1` |
| Terraform state | `health-data-pipeline-503813-personal-data-platform-tfstate-west`、prefix `personal-data-platform/runtime` |
| Raw / Logging | `health-data-pipeline-503813-pdp-raw-west` / `pdp-west` |
| 毎時 / 日次Job | `fitbit-hourly-west` / `reconciliation-west` |
| MotherDuck production | `us-west-2`、`personal_data_platform`、owner `pdp_west_prod` |

MotherDuckはPulse/Pulse・read scaling 1、分析接続はrestricted read-only share `pdp_analytics`を使う。
分析アカウントの接続名も`personal_data_platform`にそろえ、view・macro内のDB参照を解決する。
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

通常runtimeとscratch DBは共通の現行スキーマを使い、SQLは`src/personal_data_platform/migrations/`を正本とする。

```bash
pdp migrate --database /private/path/scratch.duckdb
```

`--database`を省略すると`MOTHERDUCK_DATABASE`・`MOTHERDUCK_TOKEN`の接続先へ適用する。
各SQLとchecksum記録は同じtransactionで確定する。再実行は適用済みの一致を確認し、変更は新しいforward migrationへ追加する。
既存SQLや旧DBの台帳は変更しない。旧地域・旧取得方式のmigrationを持つDBには適用を拒否する。
空の移行先DBへ[Rebuild](#rebuild)し、FitbitをAPIから再取得してから接続先を切り替える。

現行履歴内の追加DDLは両Schedulerを止め、実行中Jobの終了を確認してから適用する。
日次全段階の成功を確認してSchedulerを再開する。

### 旧スキーマからの切替

1. 両Schedulerを停止し、実行中Jobと`loader` leaseの終了を確認する。receiverは通知を蓄積できる。
2. 旧DBを保持したまま、空の移行先DBを用意する。新imageを旧DBへ接続して稼働させない。
3. 下記Rebuildの手順でScreen Timeの両streamを移行先へ再生する。
4. `MOTHERDUCK_DATABASE`を移行先へ向け、`pdp fitbit sync --from <開始日> --to <終了日の翌日>`で必要なFitbit全期間を取得する。
   途中で止まった場合は出力された最初の未完了日から続ける。
5. `pdp dbt --source fitbit`で両sourceの集計・検証を実行し、Screen Timeのイベント・補助状態・集計を旧DBと比較する。
6. 新DBのread-only shareと分析用接続を設定し、処理JobのDB設定とimageを同時に切り替える。
   旧imageを新DBに向けるrollbackは行わず、戻す場合は旧DB・旧imageの組み合わせへ戻す。
7. 独立環境のpreflightと新DBの日次全段階を確認してSchedulerを再開する。旧DBの削除は切替確認後に行う。

Raw保持範囲だけで復元できないデータは再取得できる期間を確認する。
新DBでの再構築が終わるまで、旧DBのtableとmigration台帳を削除しない。

2026-10-07に`personal_data_platform_west`から`personal_data_platform`へ切り替えた。
runtime imageのソースは`5cd09e2`で、移行台帳は`001_initial.sql`の1件。
旧DB時点のScreen Time 80,386イベント・補助状態・日次集計の一致を確認してから、最新Raw 2件も反映した。
Fitbitは2026-09-29〜2026-10-07の5種別をAPIから取得し、Cloud Runの日次全段階・dbt 39件・外部成功pingを確認した。
分析接続では全10 viewの参照と書き込み拒否、preflightでは本番DB・shareへの接続拒否を確認した。
旧DBと`deployed-rollback`タグの旧imageを保持し、戻す場合は両方を組にして接続設定を戻す。
切替時の比較結果・image digest・Terraform state backupは`var/schema-cutover/2026-10-07/`に保存する。
自動Deployの再開前に、`main`へ新スキーマと切替後の接続設定を反映する。

## 収集・日次処理

毎時15分のJobは通知を集め、完全取得したRawとDBが確定した単位だけackする。
`pdp reconciliation`は日次04:10 Asia/Tokyoの処理全体を実行する。

1. 未取込Fitbit Rawを再生し、5種別のTokyo直近7完了日を再照合する。
2. dbt run/testを実行する。
3. Macが更新したScreen Time両streamの取込成功heartbeatを48時間の鮮度とDB relationで監査する。
4. 完了対象日と内部daily heartbeatを同じtransactionでcommitする。
5. Healthchecksへ外部成功pingを1回送る。

Screen Timeの収集・保存・取り込みはMacのCollectorが30分周期で実行する。日次Jobはstream heartbeatを更新しない。

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

本番DBを空にせず、保存中のRawを別の空scratch DBへ再生する。Screen TimeはMacのSQLiteを読むため、
Collectorを停止してからinventoryを取得し、再生完了後に再開する。Screen TimeにGCS設定・ADCは不要である。

FitbitのGCS RawをRebuildする場合だけ、Terraform output `rebuild_operator_service_account`のread-only SAを使う。

```bash
export GOOGLE_CLOUD_PROJECT="health-data-pipeline-503813"
export GCS_BUCKET="${GOOGLE_CLOUD_PROJECT}-pdp-raw-west"
export MOTHERDUCK_DATABASE="personal_data_platform"
export PDP_REBUILD_SERVICE_ACCOUNT_EMAIL="raw-rebuild-operator@${GOOGLE_CLOUD_PROJECT}.iam.gserviceaccount.com"
export CLOUDSDK_CONFIG="$HOME/Library/Application Support/personal-data-platform/gcloud-rebuild"
mkdir -p "$CLOUDSDK_CONFIG"
chmod 700 "$CLOUDSDK_CONFIG"
gcloud auth application-default login \
  --impersonate-service-account="$PDP_REBUILD_SERVICE_ACCOUNT_EMAIL"
export PDP_REBUILD_GOOGLE_APPLICATION_CREDENTIALS="$CLOUDSDK_CONFIG/application_default_credentials.json"
chmod 600 "$PDP_REBUILD_GOOGLE_APPLICATION_CREDENTIALS"
```

Fitbit用ADCはimpersonated形式、現在user所有、mode 0600、指定SA一致を検証する。

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
Screen TimeのDB復元はイベント・補助状態・台帳を同じ時点へ戻す。Rawは各元ファイルの最新版だけを保存するため、
更新前の再解析やRawだけからの全履歴復元を保証しない。[ローカルRawへの移行](../sources/screen-time/operations.md#gcsからの一度限りの移行)では既存DB履歴を保持する。
