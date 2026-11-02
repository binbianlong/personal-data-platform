# OwnTracksデータモデル

## Rawと分析行

Rawには`tst`、`lat`、`lon`、`acc`、`vel`、`motionactivities`のうち受信したフィールドだけを保存し、無期限に保持する。

```text
raw/owntracks/v1/<device_key>/location/YYYY/MM/DD/YYYYMMDDThhmmssZ_<sha256>.json.gz
```

日付・時刻は`tst`のUTC表現で、同じ端末・時刻・内容の再送は同じobject keyになる。
`base.location_point`には測位時刻・緯度経度と、受信した位置精度・速度・活動状態を保存する。
`observed_at`は測位時刻。受信・取り込みの運用時刻はbaseに保存しない。
`motion_activities`は未知の活動名も配列のまま保持する。NULLと空配列を区別し、欠損を静止中とは解釈しない。

`location_point_id`は端末と内容のhashから決まり、同じ時刻でも内容が異なる測位を保持する。
通常の再送はbase IDで判定し、GCS RawとのID差分で取り込み漏れを補修する。専用のops台帳はない。
parserの変更時は対象prefixを明示して`pdp loader --source owntracks --prefix ... --reprocess`を実行する。
個々のparser適用履歴は保存しない。位置の時系列はbaseを直接参照する。
