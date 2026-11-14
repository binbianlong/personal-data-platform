# 分析クエリ例

## 日次Screen Time

| 対象 | 利用区間 | 日次合計 |
|---|---|---|
| iPhone | `marts.iphone_screen_time_interval` | `marts.daily_iphone_screen_time_total` |
| Mac | `marts.mac_screen_time_interval` | `marts.daily_mac_screen_time_total` |

日次合計はJST日につき1行。同じOSの端末間重複を和集合にし、iPhoneとMacは別々に集計する。
観測と推定が重なれば観測を優先し、推定で追加された分だけをinferred_secondsに含める。

```sql
SELECT activity_date, observed_seconds, inferred_seconds,
       total_seconds / 60.0 AS total_minutes, unresolved_event_count
FROM marts.daily_iphone_screen_time_total
WHERE activity_date BETWEEN
    CAST(current_timestamp AT TIME ZONE 'Asia/Tokyo' AS DATE) - 6
    AND CAST(current_timestamp AT TIME ZONE 'Asia/Tokyo' AS DATE)
ORDER BY activity_date DESC;
```

当日は途中経過。未解決イベントだけの日の秒数NULLと、記録も区間もない日の行の不在を、利用0と同一視しない。
baseのBundle IDに表示名を付ける場合は、`base.app`へOSとBundle IDでLEFT JOINする。未登録でも元記録を落とさない。

## 健康・位置との組合せ

歩数・AZMには`marts.daily_steps`、`marts.daily_active_zone`を使う。主睡眠の開始・終了時刻と実睡眠・覚醒分数は`marts.daily_sleep`、段階別の内訳は`marts.daily_sleep_detail`を参照する。
睡眠の帰属日は親睡眠の終了JST日、`base.health_daily.source_date`は提供元の日付であり、旅行時などには一致しない。
睡眠詳細はkind/category別の値で、種類をまたいで実睡眠時間として足さない。
位置は`base.location_point`、運動は`base.exercise_session`、分心拍は`base.heart_rate_minute`を直接参照する。
日次と細かい記録をJOINするときは、先に比較する日付・粒度を揃えて行の増殖を防ぐ。

ビューの定義と単位は[データモデル](../platform/data-model.md)を参照する。
