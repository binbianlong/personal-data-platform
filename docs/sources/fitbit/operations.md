# Fitbit運用

Jobの実行・監視・停止、DB更新、共通資格情報は[Platform運用](../../platform/operations.md)を参照する。

## 環境変数

| 設定 | 用途 |
| --- | --- |
| `PDP_FITBIT_WEBHOOK_CONFIG` | `authorization`・`health_user_id`のJSON。receiverだけに渡す |
| `PDP_FITBIT_OAUTH_CONFIG` | `client_id`・`client_secret`・`refresh_token`・`health_user_id`のJSON。処理Jobに渡す |
| `PDP_FITBIT_PUBSUB_TOPIC` / `PDP_FITBIT_PUBSUB_SUBSCRIPTION` | receiverの発行先topic名 / workerがpullするsubscription名 |
| `PDP_FITBIT_PUBSUB_ENDPOINT` | `pubsub.us-west1.rep.googleapis.com` |
| `PDP_FITBIT_SUBJECT_KEY` | 安定した疑似subject key。receiverと処理Jobで一致させる |
| `PDP_FITBIT_PROCESSING_PAUSED` | `true`で取得処理を保留する。通常は`false` |

secretの保存とversion固定は[Platformセキュリティ](../../platform/security.md)に従う。

## 通知の診断ログ

Cloud Loggingの`pdp-west` bucketに30日保持する。通知JSON全体、健康値、ユーザーID、認証情報は記録しない。

| event | 用途 |
| --- | --- |
| `fitbit_notification_received` | 統合前の通知区間・操作・削除対象ID |
| `fitbit_notification_publish` | キューへ発行した範囲と元通知との対応 |
| `fitbit_batch` | バッチ全体の受信・統合・完了・失敗・保留・ACK件数 |
| `fitbit_notification_processed` | 元通知ごとの処理結果 |
| `fitbit_acquisition` | API要求範囲・取得範囲・取得行数・投入行数・範囲置換の結果 |

Logs Explorerで元通知を検索する例:

```text
resource.type="cloud_run_revision"
resource.labels.service_name="pdp-fitbit-west"
jsonPayload.event="fitbit_notification_received"
jsonPayload.summary.data_type="heart-rate"
```

発行ログの`source_notification_ids`で元通知へ、`notification_id`でworkerの取得へ、
`pubsub_message_id`で再配信へたどる。`attempt_id`は実行ごとに変わる。
同じ`notification_hash`でも別の更新の場合があるため、処理を省略する根拠にはしない。

```text
resource.type="cloud_run_revision"
resource.labels.service_name="pdp-fitbit-worker-west"
jsonPayload.event="fitbit_acquisition"
jsonPayload.summary.notification_id="<id>"
```

`requested_start/end`と`acquired_start/end`は要求範囲と検証済み取得範囲を表す。
`bounded_minutes`と`bounded_steps`の成功時は両範囲が一致する。
心拍の未完了分は`reason="notified_utc_minute_incomplete"`で確認する。
歩数の`time_basis="civil"`の範囲は日付cursorであり、実際のUTC時刻を意味しない。

`fetched_rows`は正規化後の行数で、心拍は分集計、睡眠は親と子の合計となる。
`submitted_rows`はINSERT/MERGEへ渡した行数であり、変更・削除行数や通信バイト数ではない。
`range_replaced`は空取得による削除も含み、`unchanged`はデータ行の置換を省略してcoverageだけを更新した状態、
`stale`は全範囲がより新しい取得で保護された状態を示す。
`succeeded`はCOMMIT確認後だけ記録する。未確定の件数は`null`で、正常な空取得の0件と区別する。
発行やCOMMITの`unknown`は結果不明を表し、未実行とは限らない。

## CLIと再試行

```bash
pdp fitbit sync --from YYYY-MM-DD --to YYYY-MM-DD
```

終了日は排他的。直近2完了日より前の空白は期間を指定して補修する。
途中終了時に出る最初の未完了日・種別から再実行する。
手動同期は`ops.job_run`に履歴を保存せず、標準出力のJSON・終了コードと取得ログで確認する。
物理時刻の範囲を日全体へ拡大せず、睡眠・日次指標はproviderのcivil dateを使う。
通知の再試行条件は[取得仕様](acquisition.md#webhookとpubsub)を参照する。

## 停止と復旧

Schedulerの停止とlease確認は[Platform運用](../../platform/operations.md)に従う。receiverを止めなければ通知は7日保持される。
通常のAPI・DB失敗は次回バッチと日次再照合で再試行する。不正通知は原因を修正し、必要な期間を手動同期する。
本番DBを空にせず、独立したscratch DBへ期間指定同期とdbtを実行して比較する。
Fitbitの復旧元はGoogle Health APIであり、Rawは保存しない。

schema 2の歩数通知と暦日coverageを使う間は、worker・日次Jobとも`records-v1`と`records-civil-v1`を認識するwriterが必要となる。
反映はworker、日次Job、receiverの順とし、receiver更新前に旧writerのJobが終了していることを確認する。
rollbackではreceiverを先に戻す。キュー内のschema 2通知を読めないworkerや、暦日coverageを認識しないJobへ戻さない。
schema 1の心拍・legacy通知は引き続き受理する。

## 指標追加の導入

1. [共通のDB更新手順](../../platform/operations.md#dbの初期化と更新)に従い、Collector・各取込・日次処理を停止し、実行中Jobとloader leaseの終了を確認する。既存checkoutへ追加migrationを配置する前にCollectorを停止する。
2. migration `011_fitbit_health_metrics.sql`を適用し、Fitbit worker・OwnTracks worker・日次Jobを同じ新しいimageへ更新する。Collectorのcheckoutも更新し、`just sync`を実行する。全取込runtimeが適用済み011を認識し、Fitbit writerが16種類を読める状態を確認した後でreceiverを更新する。
3. OAuthの`googlehealth.activity_and_fitness.readonly`・`googlehealth.health_metrics_and_measurements.readonly`・`googlehealth.sleep.readonly`を確認する。運動だけ`google-sources`を使う。
4. Google Healthの既存subscriptionの`dataTypes`へ、既存5種類を残して次の10種類を追加する。`daily-vo2-max`は通知対象に追加しない。
5. dbtのFitbitモデルとテストを反映し、Collector・各取込・日次処理を再開する。過去分の一括同期は行わず、通知と直近2完了日の再照合を確認する。

追加する通知型:

```text
daily-heart-rate-variability
heart-rate-variability
daily-respiratory-rate
respiratory-rate-sleep-summary
daily-oxygen-saturation
daily-sleep-temperature-derivations
exercise
sedentary-period
distance
time-in-heart-rate-zone
```

取得ログの種別・範囲・成功、追加テーブルの件数、`daily_fitbit_health`と睡眠・運動ビューを確認する。
端末や元データによって返却されない項目もあるため、正常な空取得とAPIエラーを区別する。
日次VO2 maxは次の日次再照合で確認し、必要な場合だけ直近範囲を型指定で同期する。

rollbackではGoogle Healthの追加10型の購読を外してからreceiverを戻す。
キューに追加型が残る間は、新しいworker・日次Jobを維持して処理を完了する。
migrationは削除・巻き戻しせず、適用済み011を認識するコードを使う。

## 60分バッチの確認

Cloud Scheduler `pdp-fitbit-batch-west`がworkerの`POST /notifications/fitbit/drain`を毎時00分にOIDC認証で起動する。
Fitbitの滞留警報は90分超が5分継続した場合に発報する。設定は[Runtime Terraform](../../../infra/terraform/README.md)を参照する。

```text
resource.type="cloud_run_revision"
resource.labels.service_name="pdp-fitbit-worker-west"
jsonPayload.event="fitbit_batch"
```

`batch_id`または`attempt_id`で同じ実行のログを検索する。
COMMIT後のACKが不明なら再試行するため、`completed`と`acked`は一致しない場合がある。
`failed`には不正通知・通信・後始末の失敗も含まれ、取得単位数だけを意味しない。
pullの空応答はbacklogがないことの保証ではない。

費用を評価するときは送信bytes・課金対象実行時間に、元通知件数・取得件数・投入行数・COMMITまでの遅延を併記する。
SQL呼び出し削減率を転送量や課金額の削減率として扱わない。
