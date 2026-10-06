# Fitbit取得仕様

## Google Health API

OAuth refresh tokenからaccess tokenを得て、Google Health v4のreconcileを呼ぶ。
データソース指定は`google-wearables`。必要な環境変数は[運用](operations.md#環境変数)を参照する。

Webhook通知と日次の直近7完了日をTokyoの1日・1種別へ分割して取得する。
長期停止や広い期間には明示的な期間指定を使い、端末の同期checkpointは保存しない。

| data type | フィルターに使う値 | 置換cursor |
|---|---|---|
| `steps` | `steps.interval.start_time` | 区間開始のUTC時刻 |
| `heart-rate` | `heart_rate.sample_time.physical_time` | サンプルのUTC時刻 |
| `daily-resting-heart-rate` | `daily_resting_heart_rate.date` | 提供元の日付 |
| `active-zone-minutes` | `active_zone_minutes.interval.start_time` | 区間開始のUTC時刻 |
| `sleep` | `sleep.interval.civil_end_time` | 提供元の睡眠終了日付 |

日次・睡眠はcivil date、それ以外はphysical timeとしてクエリを構築する。
同じフィールドに対する`>=`と`<`を使い、半開区間の全ページを取得してから結果を返す。
通常のpage sizeは10,000、睡眠は25。クライアント側で心拍は14日、その他は90日に分割する。
これは安全な取得単位であり、reconcileの最大期間を保証するものではない。
1取得は1,000ページ・250,000正規化行・20分まで。上限超過は失敗として扱い、範囲を狭めて再実行する。

401/403・OAuth失敗、429、通信/5xx、データ不正を別例外にする。レスポンス本文やtokenはエラーに含めない。
重複・循環page token、範囲外の行、途中ページ失敗を完全な取得とみなさない。
空の完全取得は削除を意味する。失敗時は既存データを削除しない。

公式REST型とガイドの応答例に差があるため、point IDは`dataPointName`/`name`、
睡眠の主睡眠フラグは`mainSleep`/`main`の両方を受理する。同時に存在して矛盾する場合は停止する。

## WebhookとPub/Sub

`POST /webhooks/fitbit`でAuthorizationの共有値とGoogle HealthのTink署名を検証する。
署名鍵は公式keysetから取得してcacheし、署名不一致時には一度だけ鍵を再取得する。
購読の検証は認証後に201、通常通知は全件検証・Pub/Sub発行後に204を返す。
部分的な発行失敗は503として再送を受ける。再配信は範囲更新と取得順位で安全に処理する。

1 MiBまでのbodyを、1日・1種別の最大1,000単位へ分割する。
毎時Jobは最大500通知を120秒まで集め、50分の実行予算内で処理する。
初回pullが空でも収集期限までは再試行する。Raw保存・DB commitが確認できた単位だけackし、
API失敗・未処理・commit不明の単位は再配信に任せる。
通知の保持は7日。処理停止中もreceiverは発行を続け、7日を超える空白は期間指定で補う。

## 参考仕様

- [Webhook](https://developers.google.com/health/webhooks)
- [reconcile API](https://developers.google.com/health/reference/rest/v4/users.dataTypes.dataPoints/reconcile)
- [日時フィルター](https://developers.google.com/health/filters)
- [データの統合方針](https://developers.google.com/health/data-management)
- [睡眠データ](https://developers.google.com/health/data-types/sleep)
