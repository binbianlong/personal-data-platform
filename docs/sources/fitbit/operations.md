# Fitbit運用

通常のJob・監視・停止手順は[Platform運用](../../platform/operations.md)、切替時の結果は
[西部移行記録](../../platform/west-migration-2026-10-05.md)を参照する。

## 西部の最小構成

受信Service、Pub/Sub topic/subscription、毎時取得Job、日次Jobを使う。

## 環境変数

| 設定 | 用途 |
| --- | --- |
| `PDP_FITBIT_WEBHOOK_CONFIG` | `authorization`・`health_user_id`のJSON。receiverだけに渡す |
| `PDP_FITBIT_OAUTH_CONFIG` | `client_id`・`client_secret`・`refresh_token`・`health_user_id`のJSON。処理Jobに渡す |
| `PDP_FITBIT_PUBSUB_TOPIC` / `PDP_FITBIT_PUBSUB_SUBSCRIPTION` | 完全なPub/Sub resource名 |
| `PDP_FITBIT_PUBSUB_ENDPOINT` | `pubsub.us-west1.rep.googleapis.com` |
| `PDP_HEARTBEAT_CONFIG` | `daily`だけを持つHTTPS成功ping URLのJSON |
| `PDP_FITBIT_SUBJECT_KEY` | 安定した疑似subject key。receiverと処理Jobで一致させる |
| `PDP_FITBIT_PROCESSING_PAUSED` | `true`で取得処理を保留する。通常は`false` |
| `PDP_SCHEMA_PROFILE` | `west`。旧DBへ適用しない |

処理Jobには`GOOGLE_CLOUD_PROJECT`、`GCS_BUCKET`、`MOTHERDUCK_DATABASE`、`MOTHERDUCK_TOKEN`も必要である。
通常のsecret container・数値pinと手動preflightの資格情報は[Runtime Terraform](../../../infra/terraform/README.md#secretと資格情報)を参照する。

## CLIと再試行

通知は認証・全件検証後に日付×種別へ分割する。1リクエスト1,000単位まで、Pub/Subの各メッセージは1日以内。毎時は最大500メッセージを120秒まで集めて取得し、RawとDBが確定した単位だけackする。未完了単位は再配信し、期限/leaseによる保留だけではJob失敗にしない。

Fitbit Rawは`raw/fitbit/v3/`の直接gzip JSON配列で、1 object最大16 MiB。完全取得の境界だけで分割し、各objectを単独で再生できる。保存済みRawを共通Loaderで先に再試行し、新API取得が同じならRawを増やさずcoverageの取得時刻を更新する。古い成功だけで新通知をackしない。通知・attempt・bundle・cursorの永続台帳は持たない。

```bash
pdp fitbit migrate --database /private/path/west-scratch.duckdb --profile west
pdp fitbit ingest-notifications --max-messages 500 --collect-seconds 120 --timeout-seconds 3000
pdp fitbit sync --from 2026-09-28 --to 2026-10-05
pdp reconciliation
```

日次の順序・成功記録・監視は[Platform運用](../../platform/operations.md#収集日次処理)に従う。

7日より長い停止や広い期間の補修は、明示的な`--from`/`--to`で行う。`--resume-id`は提供しない。途中終了時は最初の未完了日・種別を出力し、その範囲から再実行する。日次の7日だけで過去の空白を埋めたと記録しない。手動の物理時刻範囲を日全体へ拡大しない。睡眠・日次指標はproviderのcivil dateを使う。

心拍は完全なUTC分の平均・最小・最大を保存し、sample countはNULL、欠測を0で埋めない。旧秒心拍をコピーしない。Screen Timeの履歴・control・削除保護を維持する。

DBの初期化・更新は[Platform運用](../../platform/operations.md#dbの初期化と更新)に従う。
完了した移行のデータ保全と旧Fitbit整理は[西部移行記録](../../platform/west-migration-2026-10-05.md)に残す。

## 停止と復旧

Schedulerを停止し、実行中Jobの完了と共有leaseを確認する。receiverを止めなければ通知は7日保持される。
通常のAPI・DB失敗は次の毎時取得、日次のRaw再生と7日再照合で再試行する。
毒性Rawは削除せず、decoder修正後に同じgenerationを再処理する。
leaseが残った異常終了は125分の期限後に再開する。

7日超の停止は空白期間を確認して`pdp fitbit sync --from <日付> --to <終了日>`を実行する。
終了は排他的で、途中失敗時には最初の未完了日・種別を出力する。
本番DBを空にせず、Raw再生は独立したscratch DBで比較する。90日より古いRawの復元は保証しない。

毎時・日次のCloud Run実行と構造化logを確認し、日次成功時には両Screen Time監査、
`ops.job_run`の完了対象日、内部3 heartbeatと外部1 pingを確認する。
共有lease競合はINFOで延期し、外部pingを送らない。

## 使用量

通常の請求・監視値でGCS容量/Class A/B、Pub/Sub滞留・再配信、Cloud Run実行時間と送信量、
MotherDuck容量/CUhを7日・30日で確認する。初回移行と検証実行を定常月へ外挿しない。
北米内のMotherDuck通信にもCloud Runの月1 GiBを超えた転送料があり、リージョン移行だけで無料を保証しない。
