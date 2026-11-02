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
`range_replaced`は正常な空取得による削除も含み、`unchanged`は保存済みの実値が同じで書換えを省いた状態を示す。
取得順位や削除IDの台帳は持たず、再試行ではAPIから取得し直す。
`succeeded`はCOMMIT確認後だけ記録する。未確定の件数は`null`で、正常な空取得の0件と区別する。
発行やCOMMITの`unknown`は結果不明を表し、未実行とは限らない。

## CLIと再試行

```bash
pdp fitbit sync --from YYYY-MM-DD --to YYYY-MM-DD
```

終了日は排他的。直近2完了日より前の空白は期間を指定して補修する。
途中終了時に出る最初の未完了日・種別から再実行する。
手動同期・日次とも実行結果はJSONログで確認する。DBにjob履歴や最終成功日checkpointは保存しない。
物理時刻の範囲を日全体へ拡大せず、睡眠・日次指標はproviderのcivil dateを使う。
通知の再試行条件は[取得仕様](acquisition.md#webhookとpubsub)を参照する。

## 停止と復旧

Schedulerの停止とlease確認は[Platform運用](../../platform/operations.md)に従う。receiverを止めなければ通知は7日保持される。
通常のAPI・DB失敗は次回バッチと日次再照合で再試行する。不正通知は原因を修正し、必要な期間を手動同期する。
本番DBを空にせず、独立したscratch DBへ期間指定同期とdbtを実行して比較する。
Fitbitの復旧元はGoogle Health APIであり、Rawは保存しない。

歩数通知のUTC/civilと要求範囲に対応したworker・日次Jobを同じコードで稼働させる。
新schemaへ切り替えたDBに旧imageを戻さない。通知の保持期間を超える停止や直近2完了日より前の欠落は、期間指定で補修する。
APIの日次記録が別日へ訂正された場合、旧日付を再取得するまで旧値が残ることがある。

## 指標と購読の更新

全16取得型は`DATA_TYPES`、通知対象15型は`WEBHOOK_TYPES`を正本とする。日次VO2 maxは通知対象ではなく日次再照合で取得する。
OAuth scopeと既存subscriptionを確認し、workerが新型に対応してから購読・receiverを変更する。
運動だけgoogle-sources、他の型はgoogle-wearablesを使う。未知分類は保存し、正常な空取得とAPIエラーを区別する。

base変更は追加のforward migrationと対応するwriterを配備し、[DB更新手順](../../platform/operations.md#dbの初期化と更新)で停止・検証・再開する。
既存の履歴を埋める一括再取得は自動では行わず、必要な型・期間だけ同期する。

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
