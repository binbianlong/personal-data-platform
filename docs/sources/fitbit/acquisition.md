# Fitbit取得仕様

## Google Health API

OAuth refresh tokenからaccess tokenを得て、Google Health v4のreconcileを呼ぶ。
データソース指定は`google-wearables`。必要な環境変数は[運用](operations.md#環境変数)を参照する。

既存のReconciliation Jobは`pairedDevices.list`を全ページ取得し、`deviceType=TRACKER`の
`lastSyncTime`が最も新しい端末を選ぶ。機種名を固定せず、時刻はナノ秒まで解釈する。
同期時刻の進展は対象期間をAPIで照合する契機であり、データが既にAPIへ到着した証明ではない。
端末がない、同期時刻がない、権限不足の場合はcheckpointを進めず、取得失敗として報告する。
初回の端末同期ではTokyoの直近7完了日を対象とする。その後は前回成功した同期日から新しい同期日までを対象とし、
7日超の空白も自動で補完する。対象はTokyoの1日・1種別の受付に分け、1回の補修で最大90日分を発行する。
全対象の完了後にのみ端末同期checkpointを進める。同日再同期でも`lastSyncTime`の進展は扱う。
週1回、直近7完了日を5種別すべて照合し、週単位の一意受付で重複作成を防ぐ。
初回取得と同じ週には週次照合を重ねない。追加のSchedulerは使わない。

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

## Webhookとタスク

`POST /webhooks/fitbit`でAuthorizationの共有値とGoogle HealthのTink署名を検証する。
署名鍵は公式の公開keysetから取得してcacheし、署名不一致時に再取得する。
購読登録用の検証要求は共有値を検証して200を返し、通常通知は対象health user IDと5 data typeを検証する。
認証不正は401、不正payloadは400、永続化またはキュー登録失敗は503。

同一通知内の同種・重複範囲を統合し、小さなGCS受付記録を先に保存する。
Cloud Tasksへの登録成功後に204を返す。キューに登録できなくても受付記録は残る。
受信処理はワーカーと並行して応答でき、リクエスト本文は1 MiBまで。

`POST /internal/tasks/fitbit`はGoogle署名のOIDC ID tokenを検証する。
issuer、audience、タスク用サービスアカウントemail、`email_verified=true`が一致する必要がある。
Cloud Run Service自体の公開設定を内部処理の認証として扱わない。

ワーカーは1日・1種別ずつ取得する。歩数とアクティブゾーン時間は分境界と既存区間の重なりへ範囲を広げる。
取得後、同じ完全な範囲の現在内容と一致し未解決のRaw保存予定がなければ、Rawを増やさず受付を完了する。
保存が必要ならDBにRaw保存予定を先に確定し、GCSへcreate-onlyで書く。keyとgenerationを受付記録へ確定し、
そのRawだけを共有Loaderへ渡す。停止後の再試行では受付・保存予定・GCS・取込台帳を照合する。
複数区間の続きは次のタスクへ渡す。通常通知の処理でGCS全件listingやmigrationを実行しない。
停止中もWebhook受付は続けるが、ワーカーのAPI取得と定期受付の作成はしない。

## 参考仕様

- [Webhook](https://developers.google.com/health/webhooks)
- [reconcile API](https://developers.google.com/health/reference/rest/v4/users.dataTypes.dataPoints/reconcile)
- [pairedDevices.list](https://developers.google.com/health/reference/rest/v4/users.pairedDevices/list)
- [日時フィルター](https://developers.google.com/health/filters)
- [データの統合方針](https://developers.google.com/health/data-management)
- [睡眠データ](https://developers.google.com/health/data-types/sleep)
