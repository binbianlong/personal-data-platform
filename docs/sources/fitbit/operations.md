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
