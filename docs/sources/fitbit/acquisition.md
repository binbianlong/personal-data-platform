# Fitbit取得仕様

## Google Health API

OAuth refresh tokenからaccess tokenを得て、Google Health v4のreconcileを`google-wearables`指定で呼ぶ。
日次はTokyoの直近2完了日を1日・1種別の計10取得単位へ分割する。
環境変数と長期停止後の期間指定は[運用](operations.md)を参照する。

| data type | フィルターに使う値 |
|---|---|
| `steps` | 部分通知・日次・手動は`steps.interval.start_time`、全日通知は`steps.interval.civil_start_time` |
| `heart-rate` | `heart_rate.sample_time.physical_time` |
| `daily-resting-heart-rate` | `daily_resting_heart_rate.date` |
| `active-zone-minutes` | `active_zone_minutes.interval.start_time` |
| `sleep` | `sleep.interval.civil_end_time` |

日次指標・睡眠と歩数の全日通知は提供元のcivil date、それ以外はphysical timeで、
`>=`と`<`による半開区間の全ページを取得する。
クライアントは心拍を14日、その他を90日に分割するが、APIの最大期間を保証しない。
1取得は1,000ページ・250,000正規化行・20分まで。超過時は範囲を狭めて再実行する。

循環page token、範囲外の行、途中ページ失敗を完全な取得とみなさない。
空の完全取得は削除を意味し、失敗時は既存データを削除しない。
APIレスポンスは処理中のメモリだけで扱い、Rawとして保存しない。

## WebhookとPub/Sub

`POST /webhooks/fitbit`でAuthorizationの共有値とGoogle HealthのTink署名を検証する。
購読の検証は認証後に201、通常通知は全件検証・Pub/Sub発行後に204を返す。
部分的な発行失敗は503として再送を受ける。bodyは最大1 MiB、分割前の取得単位は最大1,000件。

歩数の物理区間はUTCの半開区間を取得し、小数秒をマイクロ秒へ外側に丸める。
物理区間のない全日通知は通知された現地暦日だけを取得する。
どちらもschema 2の`acquisition_mode="bounded_steps"`で、`windows[].time_basis`に`utc`または`civil`を指定する。
心拍の物理区間はUTC分境界へ外側に丸め、schema 1の`bounded_minutes`として取得する。
物理区間のない心拍や、物理区間あり・なしが混在する元通知は日単位の`legacy`取得を使う。
未完了分を含む心拍は保留し、通知範囲を完全に取得できた場合だけ置換する。

全種類をPub/Subへ保持し、毎時00分に非公開workerの`/notifications/fitbit/drain`を起動する。
通常の追加待機は0〜60分と処理時間で、失敗・lease競合時は次回以降へ持ち越す。通知の保持は7日。
receiver内では同じHTTP受信内の歩数を統合し、workerでは同じ取得モード・時刻基準・取得日内の
bounded歩数・心拍の重複と隣接範囲を統合する。日付指標とlegacy通知は完全一致する範囲だけをまとめる。
UTCとcivilの範囲は統合しない。

範囲ごとにtransactionを確定し、元通知の全範囲のCOMMITを確認してからACKする。
失敗・不正通知・取得保留は未ACKで残し、再配信時はAPIから再取得する。
COMMIT結果が不明なら接続を破棄して後続を止める。古い取得結果からの保護は
[範囲置換と再実行](data-model.md#範囲置換と再実行)を参照する。

## 参考仕様

- [Webhook](https://developers.google.com/health/webhooks)
- [reconcile API](https://developers.google.com/health/reference/rest/v4/users.dataTypes.dataPoints/reconcile)
- [日時フィルター](https://developers.google.com/health/filters)
- [データの統合方針](https://developers.google.com/health/data-management)
- [睡眠データ](https://developers.google.com/health/data-types/sleep)
