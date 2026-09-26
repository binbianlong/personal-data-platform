# Screen Time

Macへ同期されたiPhoneのBiome `App.InFocus/remote`と、Mac自身の
`ScreenTime.AppUsage/local`からアプリの開始・終了を取得する。

Macは`sync.db`の唯一の`platform = 3 AND me = 1`行から疑似化device keyを作り、
`PDP_SCREEN_TIME_MAC_DEVICE_KEY`を設定した場合に収集する。iPhoneは従来どおり
`PDP_SCREEN_TIME_DEVICE_ALLOWLIST`で選ぶ。両者は別のdevice keyとstreamを持ち、
同じ日次Viewで`ios`と`macos`として表示する。Webサイト利用、通知、表示用アプリ名の補完は対象外である。

`pdp screen-time inspect-mac`はMacの`App.InFocus/local`を調べる診断commandである。
継続収集する`ScreenTime.AppUsage/local`とは別のstreamを読むため、日次集計の入力には使わない。

共通処理には`source_id=screen_time`、`stream=app-in-focus`と`app-usage`を登録する。
新規segmentはどちらもRaw v2で保存する。`loader`、`reconciliation`、`rebuild`の引数省略時は
従来のiPhone stream、両方を処理する場合は`--source screen_time --all-streams`を使う。
dbt selectorは共通の`tag:screen_time`である。

取得、設定、SQLite state、LaunchAgent、Raw key / control、GCSのcontrol更新、decoder、型付きbatch、
Collector稼働監査の実装は`src/personal_data_platform/sources/screen_time/`に置く。既存の
SQLite database、LaunchAgent label、cloud Job名は共用し、manifestとreceiptはstream別に持つ。

## ファイル

- [`acquisition.md`](acquisition.md)
- [`data-model.md`](data-model.md)
- [`operations.md`](operations.md)

共通のGCS Raw、MotherDuck、セキュリティ、復旧契約は[`../../platform/`](../../platform/)を参照する。
