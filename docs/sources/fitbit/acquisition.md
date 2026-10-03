# Fitbit取得仕様

## Google Health API

OAuth refresh tokenからaccess tokenを得て、Google Health v4のreconcileを呼ぶ。
データソース指定は`google-wearables`。必要な環境変数は[運用](operations.md#環境変数)を参照する。

## 日次取得

既存のScreen Time Reconciliation Jobで毎朝04:30 Asia/Tokyoに実行する。
初回と通常実行はTokyoの直近7完了日を5種別すべて取得し、今日の未完了分は翌日以降に取得する。
同じ日の成功済み処理を再実行しても追加取得しない。失敗した処理は同じ日でも再開できる。
同期時刻が変わらなくても直近7日を毎日再照合し、遅れて到着したデータや修正を拾う。

`pairedDevices.list`を全ページ取得し、`deviceType=TRACKER`の`lastSyncTime`が最も新しい端末を選ぶ。
機種名を固定せず、時刻はナノ秒まで解釈する。同期時刻の進展は照合範囲を広げる契機であり、
データが既にAPIへ到着した証明ではない。端末や同期時刻がない、権限不足の場合はcheckpointを進めない。
以前の完了日以降の欠落と、同期時刻が進んだ際の前回同期日以降も補完する。
7日分ずつ処理し、日数上限90日に達した場合は`deferred`として次回へ継続する。
途中の完了範囲はDBへ残し、全範囲が完了したときだけ当日の成功と同期時刻を確定する。

歩数とアクティブゾーン時間は分境界と既存区間の重なりへ取得範囲を広げる。
同一の完全取得範囲・元API項目を含む内容hash・未解決の保存予定を確認して、変更のないRawを省く。
変更のある複数種別・日付のSnapshotを1つのgzip Rawにまとめ、保存直後のbytesを共通Loaderへ渡す。
通常の日次取得ではGCSの一覧取得、受付記録、checkpoint JSON、Rawの再ダウンロードを行わない。
障害復旧と手動監査・再構築では必要なGCS読み取りを行う。

## APIの完全取得

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

## 参考仕様

- [reconcile API](https://developers.google.com/health/reference/rest/v4/users.dataTypes.dataPoints/reconcile)
- [pairedDevices.list](https://developers.google.com/health/reference/rest/v4/users.pairedDevices/list)
- [日時フィルター](https://developers.google.com/health/filters)
- [データの統合方針](https://developers.google.com/health/data-management)
- [睡眠データ](https://developers.google.com/health/data-types/sleep)
