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
| `fitbit_notification_publish` | 統合・分割後のキュー範囲、元通知IDの対応、発行結果、`notification_id`、`pubsub_message_id` |
| `fitbit_push` | workerの実行結果、時刻、完了・失敗・保留の件数、実行ごとの`attempt_id` |
| `fitbit_acquisition` | API要求範囲、完了した実取得範囲、取得行数、INSERT投入行数、範囲置換の結果 |

`delivery_id`はHTTP受信ごと、`source_notification_id`は元通知ごとに変わる。
発行ログの`source_notification_id`で元区間へ、`notification_id`と`pubsub_message_id`でworkerへ結び付ける。
`source_notification_ids`は、その取得範囲に重なる元通知IDの一覧となる。
同じHTTP内の歩数通知を統合し、元通知が複数になる場合は単数の`source_notification_id`を`null`とする。
元通知の区間・操作・削除対象IDは統合前の受信ログに残る。
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
統合した発行ログは`jsonPayload.summary.source_notification_ids="<source-id>"`で元通知から検索できる。
再配信は`jsonPayload.summary.pubsub_message_id="<message-id>"`で検索し、実行IDと結果を比較する。

取得結果は`notification_id`と`attempt_id`でworker実行へ結び付ける。日次・手動取得では両IDが`null`となる。
`requested_start/end`はAPIへ渡す範囲、`acquired_start/end`は検証済みの取得範囲である。
心拍は要求範囲の開始を分境界に切り上げ、終了を取得時点の完了済み分まで縮めるため、両範囲が異なる場合がある。
物理心拍通知の`acquisition_mode="bounded_minutes"`では、receiverが元区間を含むよう分境界へ外側に丸める。
workerはその範囲を全て取得できた場合だけ置換・ackするため、成功時の要求・取得範囲は一致する。
未完了分の保留は`reason="notified_utc_minute_incomplete"`で確認できる。
`acquisition_mode="legacy"`は従来の日単位通知処理で、日次・手動の取得ログでは`null`となる。
歩数の`acquisition_mode="bounded_steps"`は、`time_basis="utc"`なら通知の物理区間、
`time_basis="civil"`なら通知の現地暦日を取得する。civilの開始・終了は内部の日付cursorであり、
実際のUTC時刻を意味しない。発行ログの`summary.time_basis`にも同じ基準を記録する。
`fetched_rows`は正規化後の取得行数で、心拍は分集計行、睡眠は親とstage/wake子行を含む。
`submitted_rows`はINSERTへ渡した行数であり、変更・削除行数や通信バイト数ではない。追加のDB照会で数えない。
`accepted_ranges`は古い取得結果から保護された範囲を除いた書き込み対象、`protected_records`は保護した主レコード数となる。

| outcome | 意味 |
| --- | --- |
| `range_replaced` | 対象範囲を置換。投入0行でも既存データの削除を伴う場合がある |
| `unchanged` | 正規化後の内容が同じためデータ行の置換を省略。coverageは更新 |
| `stale` | 全範囲がより新しい取得で保護され、writerによる変更を省略 |

`succeeded`はCOMMIT確認後のみ記録する。未完了の取得や途中の書き込み失敗で不明な件数は`null`で、
正常に取得した空の結果の0件と区別する。COMMIT/ROLLBACK結果不明は`unknown`となり、
再配信後にAPIから再取得する。workerの集計が失敗でも、先にCOMMITできた範囲は取得ログの成功として残る。

```text
resource.type="cloud_run_revision"
resource.labels.service_name="pdp-fitbit-worker-west"
jsonPayload.event="fitbit_acquisition"
jsonPayload.summary.notification_id="<id>"
```

通知の元区間と要求範囲の広がり、`unchanged`の頻度、種類別の投入行数を比較する。
同じ通知メタデータが繰り返されても、API取得結果が同じとは限らない。

通知範囲に限定した心拍の取得結果を検索する例:

```text
resource.type="cloud_run_revision"
resource.labels.service_name="pdp-fitbit-worker-west"
jsonPayload.event="fitbit_acquisition"
jsonPayload.summary.acquisition_mode="bounded_minutes"
```

歩数の通知範囲と差分投入を検索する例:

```text
resource.type="cloud_run_revision"
resource.labels.service_name="pdp-fitbit-worker-west"
jsonPayload.event="fitbit_acquisition"
jsonPayload.summary.acquisition_mode="bounded_steps"
```

`time_basis`と要求範囲を元区間へ照合し、`fetched_rows`と`submitted_rows`を比較する。
同内容の全日通知が再度届いた場合もAPI取得は行い、一致する行の再投入を省略する。

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

歩数のschema 2通知は`acquisition_mode="bounded_steps"`と`windows[].time_basis`を持つ。
`utc`は物理時刻、`civil`は現地暦日をUTC midnightで表した内部cursorとなる。
暦日歩数は`steps.interval.civil_start_time`で取得し、保存行のUTC時刻は変えない。
同じcoverageテーブルの`records-v1`と`records-civil-v1`で双方の取得順位を保護する。
暦日取得を使用する処理と日次Jobは、この両方を認識するwriterを使用する。
schema 2を読み取れない旧workerへ戻すと、キュー内の新通知を処理できない。
互換workerはschema 2の暦日をUTCの保守的包絡へ変換し、従来の日単位処理で再試行できる。

反映はworker、`reconciliation-west`の日次Job、receiverの順にimage digestを固定して行う。
receiver更新前に、旧writerを使う日次Jobの実行が完了していることを確認する。
rollbackはreceiverを先に旧digestへ戻し、workerをschema 2対応の互換imageへ戻す。
日次Jobにも両方のcoverageを認識するwriterを維持する。新しい暦日coverageが存在する間は、
schema 1だけの旧worker・旧Jobへ戻さない。
心拍のschema 1通知と既存のlegacy通知も受理できる。
各反映でコミット、image digest、対象revisionを記録する。未pushのimage反映は次の通常Deployで上書きされ得る。


## 15分バッチの確認

`fitbit_delivery_mode=pull`ではCloud Scheduler `pdp-fitbit-batch-west`が既存workerの
`POST /notifications/fitbit/drain`をOIDC認証で起動する。日次補完の対象範囲は変更しない。
切替と切り戻しは[Runtime Terraform](../../../infra/terraform/README.md#fitbitの配信切替)を参照する。

`fitbit_batch`のsummaryは`received`（pullした件数）、`grouped`（統合後の取得単位）、
`completed`・`failed`・`deferred`、`acked`（ACK応答を確認した元メッセージ数）、
`session_opens`、`elapsed_seconds`を持つ。`failed`は不正通知・通信・後始末の失敗も含み、単位件数だけを意味しない。
元通知の`fitbit_push`は`batch_id`と`group_id`／`group_ids`を持ち、
取得ログの`notification_id`へ対応する。`attempt_id`はバッチ実行ごとに変わる。
COMMIT後のACKが不明なら`retry`となるため、`completed`と`acked`は一致しない場合がある。
ACK ID、通知JSON全体、健康値、資格情報は記録しない。

Logs Explorerでは次を既存の`pdp-west`ログビューで検索する。

```text
resource.type="cloud_run_revision"
resource.labels.service_name="pdp-fitbit-worker-west"
jsonPayload.event="fitbit_batch"
```

```text
resource.labels.service_name="pdp-fitbit-worker-west"
(jsonPayload.summary.batch_id="<batch_id>" OR jsonPayload.summary.attempt_id="<batch_id>")
```

費用比較は切替前後各72時間のinternet送信bytesと課金対象実行時間を取得し、
元通知件数・取得件数・投入行数・COMMITまでの遅延と併記する。変更前のrevision別にも分ける。
Cloud Run・Scheduler等の費用を含めて評価し、SQL呼び出し削減率を転送量削減率とは扱わない。
