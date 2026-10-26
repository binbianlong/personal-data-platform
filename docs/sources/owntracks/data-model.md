# OwnTracksデータモデル

## Rawと分析行

Rawには`tst`、`lat`、`lon`、`acc`、`vel`、`motionactivities`のうち受信したフィールドだけを保存し、無期限に保持する。

```text
raw/owntracks/v1/<device_key>/location/YYYY/MM/DD/YYYYMMDDThhmmssZ_<sha256>.json.gz
```

日付・時刻は`tst`のUTC表現で、同じ端末・時刻・内容の再送は同じobject keyになる。
`base.owntracks_location`には測位時刻・緯度経度と、受信した位置精度・速度・活動状態を保存する。
`received_at`はGCS保存時刻、`loaded_at`はDB取込時刻を表す。
`motionactivities`は未知の活動名も配列のまま保持する。欠損・nullの`NULL`と空配列の`[]`を区別し、欠損を静止中と解釈しない。

`marts.owntracks_location_time_series`はTokyo時刻`event_at_jst`と日付`activity_date`を加えるViewで、測位点を日次に集約しない。
