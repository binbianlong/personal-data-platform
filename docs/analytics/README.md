# Analytics

データソースを横断するmartとmetricを管理する。

扱う内容:

- source横断JOINとtimezone統一
- 日次集約と分析用mart
- 複数sourceを比較・統合するmetric
- metricの単位、計算方法、欠損時の意味

source単独で完結する派生値と品質定義は[`sources/`](../sources/)を正本とする。

## 日次Screen Timeの参照

アプリ別には`marts.daily_screen_time`、端末別の合計には`marts.daily_screen_time_total`を使う。
日付・品質・欠損の意味は[`Screen Timeデータモデル`](../sources/screen-time/data-model.md)に従う。
以下は日本時間の今日を含む直近7日を取得する例で、当日の値は途中経過を含む。

### アプリ別の使用時間

```sql
SELECT
    activity_date,
    device_key,
    platform,
    bundle_id,
    complete_seconds,
    inferred_seconds,
    total_seconds / 60.0 AS total_minutes
FROM marts.daily_screen_time
WHERE activity_date BETWEEN
    CAST(current_timestamp AT TIME ZONE 'Asia/Tokyo' AS DATE) - 6
    AND CAST(current_timestamp AT TIME ZONE 'Asia/Tokyo' AS DATE)
ORDER BY activity_date DESC, device_key, platform, total_seconds DESC, bundle_id;
```

### 端末別の総使用時間

```sql
SELECT
    activity_date,
    device_key,
    platform,
    complete_seconds,
    inferred_seconds,
    total_seconds / 60.0 AS total_minutes
FROM marts.daily_screen_time_total
WHERE activity_date BETWEEN
    CAST(current_timestamp AT TIME ZONE 'Asia/Tokyo' AS DATE) - 6
    AND CAST(current_timestamp AT TIME ZONE 'Asia/Tokyo' AS DATE)
ORDER BY activity_date DESC, device_key, platform;
```
