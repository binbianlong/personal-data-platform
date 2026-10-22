# OwnTracksデータモデル

## Rawと分析行

Rawには`tst`、`lat`、`lon`、`acc`、`vel`、`motionactivities`のうち受信したフィールドを保存する。電池残量・充電状態・通信・地域情報など他のフィールドは保存しない。Rawは無期限に保持する。

```text
raw/owntracks/v1/<device_key>/location/YYYY/MM/DD/YYYYMMDDThhmmssZ_<sha256>.json.gz
```

`device_key`は64桁の疑似化key、日付と時刻は`tst`のUTC表現、hashは正規化JSONのSHA-256である。同じ端末・時刻・内容の再送は同じobject keyになる。Rawのgenerationとhashを確認してから、共通Loaderが`base.owntracks_location`へ書き込む。

| 列 | 内容 |
|---|---|
| `event_key` / `device_key` | 端末と内容hashから作るイベントkey / 疑似化端末key |
| `event_at` / `latitude` / `longitude` | 測位時刻と緯度経度 |
| `accuracy_m` / `velocity_kmh` | 受信時に存在した場合の位置精度・速度 |
| `object_key` / `storage_generation` | 取込元Rawの識別子 |
| `received_at` / `loaded_at` / `parser_version` | GCS保存時刻・DB取込時刻・parser版 |

`marts.owntracks_location_time_series`は分析行にTokyo時刻`event_at_jst`と日付`activity_date`を加えるViewである。測位点を日次に集約しない。
