# 分析クエリ例

## 日次Screen Timeの参照

| 対象 | アプリ別 | 端末別の合計 |
|---|---|---|
| iPhone | `marts.daily_iphone_screen_time` | `marts.daily_iphone_screen_time_total` |
| Mac | `marts.daily_mac_screen_time` | `marts.daily_mac_screen_time_total` |

旧`marts.daily_screen_time` / `_total`と`base.screen_time_event` / `_transition` / `_interval`は分割後には存在しない。
参照するsourceに応じて上表と`base.iphone_screen_time_*` / `base.mac_screen_time_*`へ切り替える。
日付・品質・欠損の意味は[iPhone](../sources/iphone-screen-time/data-model.md)・[Mac](../sources/mac-screen-time/data-model.md)のデータモデルに従う。
以下は日本時間の今日を含む直近7日のiPhoneアプリ別使用時間を取得する例で、当日の値は途中経過を含む。

```sql
SELECT
    activity_date,
    device_key,
    bundle_id,
    complete_seconds,
    inferred_seconds,
    total_seconds / 60.0 AS total_minutes
FROM marts.daily_iphone_screen_time
WHERE activity_date BETWEEN
    CAST(current_timestamp AT TIME ZONE 'Asia/Tokyo' AS DATE) - 6
    AND CAST(current_timestamp AT TIME ZONE 'Asia/Tokyo' AS DATE)
ORDER BY activity_date DESC, device_key, total_seconds DESC, bundle_id;
```

Macには`marts.daily_mac_screen_time`を使う。端末別合計はそれぞれ`_total`を使い、`bundle_id`を選択・並び順から外す。
異なる端末の時間を合算しない。区間のない日の不在をゼロに置き換えない。

## Fitbit睡眠との横断分析

睡眠開始前2時間の端末別使用時間は`marts.fitbit_sleep_iphone_screen_time`と
`marts.fitbit_sleep_mac_screen_time`を使う。旧`marts.fitbit_sleep_screen_time`は存在しない。
重複区間・欠測・日付・端末分離の定義は[Fitbitデータモデル](../sources/fitbit/data-model.md#分析ビュー)に従う。

横断モデルはトップレベルの`cross_source`に置き、source単独のdbt selectorには含めない。
単独実行は`pdp dbt --source iphone_screen_time` / `pdp dbt --source mac_screen_time`を使う。
横断モデルの更新・検証は全体の`pdp dbt`で行う。
