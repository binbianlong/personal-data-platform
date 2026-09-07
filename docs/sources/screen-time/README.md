# Screen Time

Macへ同期されたiPhoneのBiome `App.InFocus`から、アプリのforeground遷移を取得する。

初期対象は`sync.db`で`platform = 2`として識別できるiPhoneだけである。Mac自身の
`ScreenTime.AppUsage/local`、Web利用、通知、表示用アプリ名の補完は対象に含めない。

共通処理へは`source_id=screen_time`、`stream=app-in-focus`、Raw schema v1として登録する。
`loader`、`reconciliation`、`rebuild`の既定対象であり、明示時は
`--source screen_time --stream app-in-focus`を使う。dbt selectorは`tag:screen_time_app_in_focus`である。

取得、設定、SQLite state、LaunchAgent、Raw key / control、GCSのcontrol更新、decoder、型付きbatch、
Collector稼働監査の実装は`src/personal_data_platform/sources/screen_time/`に置く。既存のRaw key、
SQLite database、LaunchAgent label、cloud Job名は継続して使う。共通処理はdeviceやSEGBの構造に依存しない。

## ファイル

- [`acquisition.md`](acquisition.md)
- [`data-model.md`](data-model.md)
- [`operations.md`](operations.md)

共通のGCS Raw、MotherDuck、セキュリティ、復旧契約は[`../../platform/`](../../platform/)を参照する。
