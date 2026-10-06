# Platform運用

通常構成はus-west1のWebhook receiver・Pub/Sub・毎時Job・日次Jobと、us-west-2のMotherDuck。
接続先・確認結果は[西部移行記録](west-migration-2026-10-05.md)を参照する。

## Deploy

bootstrapでstate・registry・WIFを作り、[runtime Terraform](../../infra/terraform/README.md)で通常資産を管理する。
通常payloadはMotherDuck・OAuth・Webhook・日次heartbeatの4件を数値versionへ固定する。
初回・IAM変更時のpreflightは独立bucket/DB/tokenで手動実行し、本番資格情報を流用しない。
常設preflight/dbt Jobは設けない。migrationsはwest profileでforward-onlyに適用し、適用済みchecksumを変更しない。

CIは西部の各Job・receiverのdigestをdeployedタグで保護してから、同じ西部candidate digestへ更新する。
source・dbt・依存・Dockerfileに変更がないpushでは現行reconciliation-westのdigestを再利用する。
無効な比較元やJob一覧取得失敗はdeployを止める。アプリdeploy失敗後のinfraだけのpushは
アプリを再buildしないため、失敗した変更を反映する場合は手動deployを使う。
GitHubのPDP_WEST_SCHEDULERS_ENABLED/PDP_WEST_LOGGING_ENABLEDで切替状態を維持する。
手動run_dailyではcombined daily Jobを実行できる。

## 収集・日次処理

毎時15分のfitbit-hourly-westは通知を集め、完全取得したRawとDBが確定してからackする。
日次04:10 JSTのreconciliation-westは次の順で進む。

1. Screen TimeのiPhone・Mac Rawをgeneration指定で取り込む。
2. 未取込Fitbit Rawを再生し、5種別のTokyo直近7完了日を再照合する。
3. dbt run/testで分析relationを検証する。
4. Screen Time両streamのRaw・取込状態・relation・48時間のcollector鮮度を監査する。
5. 完了対象日と内部daily/両stream heartbeatを同じtransactionへ保存する。
6. commit後にHealthchecksへ1 pingを送る。

2 Jobは1task/parallelism1、共通125分leaseを使う。毎時50分・日次100分の期限を越えない。
lease競合はINFOで延期し、成功heartbeatを送らない。毒性Rawは削除せず修正decoderで再試行する。
異常終了で残ったleaseは期限後に再試行する。7日を超えたFitbitの空白は手動の期間指定で補う。

## 監視とLogging

native警報は2 Jobをまとめた失敗、24時間を超えたPub/Sub最古未ack、receiver ERRORの3件。
Healthchecksはdaily 1件、Period 24h＋Grace 24h、全段階成功時だけPOSTする。
内部のstream監査・成功記録を残し、新しいeventがないことだけを障害とみなさない。
Rawの保持起点から93日を超える未削除は監査失敗とする。GCSの90日ちょうどの削除を保証するものではない。

通常logは_Default sinkからus-west1のpdp-west bucketへ1経路で送る。保持30日。
Required audit logと既存backup bucketは維持する。custom bucketのlogは、そのviewを指定して読む。

```bash
gcloud run jobs execute fitbit-hourly-west --project=<project-id> --region=us-west1 --wait
gcloud run jobs execute reconciliation-west --project=<project-id> --region=us-west1 --wait
gcloud logging read 'resource.type="cloud_run_job"' --project=<project-id> \
  --location=us-west1 --bucket=pdp-west --view=_AllLogs --limit=50
```

## Rebuild

本番MotherDuck databaseを直接空にして再構築してはならない。

GCS Rawはsourceの保持期限で永久削除されるため、全期間のrebuildは保証しない。iPhoneの保持期限は90日である。

Collectorのwrite-only ADCとは別に、Terraform output
`rebuild_operator_service_account`のread-only Service AccountをimpersonateするADCを作る。Collector用の
`CLOUDSDK_CONFIG`やADC fileを上書きしない。

```bash
export GOOGLE_CLOUD_PROJECT="<project-id>"
export GCS_BUCKET="${GOOGLE_CLOUD_PROJECT}-pdp-raw-west"
export MOTHERDUCK_DATABASE="<production-database>"
# non-dry-runではMOTHERDUCK_TOKENも安全なsecret sourceから環境へ渡す
export PDP_REBUILD_SERVICE_ACCOUNT_EMAIL="raw-rebuild-operator@${GOOGLE_CLOUD_PROJECT}.iam.gserviceaccount.com"
export CLOUDSDK_CONFIG="$HOME/Library/Application Support/personal-data-platform/gcloud-rebuild"
mkdir -p "$CLOUDSDK_CONFIG"
chmod 700 "$CLOUDSDK_CONFIG"
gcloud auth application-default login \
  --impersonate-service-account="$PDP_REBUILD_SERVICE_ACCOUNT_EMAIL"
export PDP_REBUILD_GOOGLE_APPLICATION_CREDENTIALS="$CLOUDSDK_CONFIG/application_default_credentials.json"
chmod 600 "$PDP_REBUILD_GOOGLE_APPLICATION_CREDENTIALS"
```

`pdp rebuild`はこのADCがimpersonated Service Account形式、現在user所有、mode `0600`、指定target一致であることを
確認し、実行中だけ`GOOGLE_APPLICATION_CREDENTIALS`として使う。CollectorのADCは変更しない。

1. `pdp rebuild --source screen_time --all-streams --dry-run`でstreamごとのobject数、subject数、scope数、GCS作成期間、保持日数、
   `full_history_rebuild_guaranteed=false`を表示する。iPhoneは従来のdevice数・segment数も表示する。
2. `pdp rebuild --source screen_time --all-streams --target-db <scratch-db> --allow-partial-history`で空のscratch databaseを指定する。
   command内部でmigrationを適用し、GCSに現在残る全pageを1回だけlistingしてinventoryを固定する。各objectは
   選択source / streamのinventoryに記録したgenerationを指定し、`(observed_at, object_key)`順に再生する。途中でそのgenerationが
   Lifecycle削除された場合は別generationへ読み替えず失敗する。
   両streamの`ops.ingestion_metadata`とbaseの再構築後、同じscratch databaseへ共通selectorで選択した`dbt run`と`dbt test`を1回実行する。
3. commandの成功後、productionと件数、stable key集合、代表martを手動で比較する。
4. 差分を確認した後、参照先を手動で切り替える。

target databaseがproductionと同一、既存tableを持つ、環境識別が不明、または
`--allow-partial-history`がない場合は開始前に停止する。MotherDuckの90日より古い履歴を失った場合、GCSからは
復元できない。

## 分析・変更・復旧

本番DB ownerから分析アカウントへrestricted read-onlyの自動更新shareを付与する。
preflightへ本番shareを付与しない。MCPは西部接続とpdp_analytics_westを使う。

schema変更では両Schedulerを止め、Jobの終了を確認し、runtime・forward migration・dbtを反映する。
日次全段階の成功後にSchedulerを再開する。Raw v3にはv3対応imageを使い、旧decoderへ戻さない。
旧組織・Screen Time・共通台帳・Raw/state backupを通常のFitbit整理へ巻き込まない。
