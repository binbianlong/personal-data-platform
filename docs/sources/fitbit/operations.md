# Fitbit運用

Jobの実行・監視・停止、DB更新、共通資格情報は[Platform運用](../../platform/operations.md)を参照する。

## 環境変数

| 設定 | 用途 |
| --- | --- |
| `PDP_FITBIT_WEBHOOK_CONFIG` | `authorization`・`health_user_id`のJSON。receiverだけに渡す |
| `PDP_FITBIT_OAUTH_CONFIG` | `client_id`・`client_secret`・`refresh_token`・`health_user_id`のJSON。処理Jobに渡す |
| `PDP_FITBIT_PUBSUB_TOPIC` / `PDP_FITBIT_PUBSUB_SUBSCRIPTION` | receiverの発行先topic名 / workerが受理するsubscription名 |
| `PDP_FITBIT_PUBSUB_ENDPOINT` | `pubsub.us-west1.rep.googleapis.com` |
| `PDP_FITBIT_SUBJECT_KEY` | 安定した疑似subject key。receiverと処理Jobで一致させる |
| `PDP_FITBIT_PROCESSING_PAUSED` | `true`で取得処理を保留する。通常は`false` |

secretの保存とversion固定は[Platformセキュリティ](../../platform/security.md)に従う。

## 通知の診断ログ

Cloud Loggingの`pdp-west` bucketに30日保持する。通知の主要項目と処理結果だけを記録し、
ユーザーID、認証情報、通知JSON全体、健康値、未知フィールドは記録しない。

| event | 内容 |
| --- | --- |
| `fitbit_notification_received` | 元の区間ごとのUTC/civil開始・終了、種類、操作、`record_id`、受信時刻、区間番号 |
| `fitbit_notification_publish` | 分割後のキュー範囲、発行結果、`notification_id`、`pubsub_message_id` |
| `fitbit_push` | workerの実行結果、時刻、完了・失敗・保留の件数、実行ごとの`attempt_id` |

`delivery_id`はHTTP受信ごと、`source_notification_id`は元通知ごとに変わる。
発行ログの`source_notification_id`で元区間へ、`notification_id`と`pubsub_message_id`でworkerへ結び付ける。
`interval_index`は0始まりで、`interval_count`は元通知の区間数。存在しない元区間の表現やIDは`null`となる。
`notification_hash`は主要項目を正規化したSHA-256で、未知フィールドを含まない。
同じhashでも同じ区間への別更新の場合があるため、処理を省略する根拠にはしない。
同じPub/SubメッセージIDで`attempt_id`が異なる場合は再配信された実行を示す。
Pub/Sub発行の`unknown`は発行結果を確認できなかった状態であり、未発行と確定した意味ではない。

Logs Explorerで通知の元区間と発行を検索する例:

```text
resource.type="cloud_run_revision"
resource.labels.service_name="pdp-fitbit-west"
jsonPayload.event="fitbit_notification_received"
jsonPayload.summary.data_type="heart-rate"
```

種類別の同内容通知は`jsonPayload.summary.notification_hash="<hash>"`、
receiverからworkerまでの追跡は`jsonPayload.summary.notification_id="<id>"`で検索する。
再配信は`jsonPayload.summary.pubsub_message_id="<message-id>"`で検索し、実行IDと結果を比較する。

## CLIと再試行

取得・成功応答の条件は[取得仕様](acquisition.md#webhookとpubsub)、範囲置換は[データモデル](data-model.md)に従う。
古い成功だけで新通知をackせず、新しくAPI取得してcommitを確認する。workerの保留は503として再配信を受ける。

```bash
pdp fitbit sync --from YYYY-MM-DD --to YYYY-MM-DD
```

7日超の停止や広い期間の補修は明示的な`--from`/`--to`で行う。終了日は排他的で、`--resume-id`は提供しない。
途中終了時に出る最初の未完了日・種別から再実行し、日次の7日だけで過去の空白を埋めたと判断しない。
手動の物理時刻範囲を日全体へ拡大せず、睡眠・日次指標はproviderのcivil dateを使う。

## 停止と復旧

Schedulerの停止とlease確認は[Platform運用](../../platform/operations.md)に従う。receiverを止めなければ通知は7日保持される。
通常のAPI・DB失敗はPub/Subの再配信と日次の7日再照合で再試行する。
不正通知は成功応答せず、原因を修正するか、期間指定同期で補修する。
7日超の停止は空白期間を確認して上記の期間指定取得を行う。
本番DBを空にせず、`MOTHERDUCK_DATABASE`を独立したscratch DBへ向けて期間指定同期とdbtを実行し比較する。
Fitbitの復旧元はGoogle Health APIであり、Rawは保存しない。
