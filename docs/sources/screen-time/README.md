# Screen Time

Macへ同期されたiPhoneのBiome `App.InFocus`から、アプリのforeground遷移を取得する。

初期対象は`sync.db`で`platform = 2`として識別できるiPhoneだけである。Mac自身の
`ScreenTime.AppUsage/local`、Web利用、通知、表示用アプリ名の補完は対象に含めない。

## ファイル

- [`acquisition.md`](acquisition.md)
- [`data-model.md`](data-model.md)
- [`operations.md`](operations.md)

共通のB2 Raw、MotherDuck、セキュリティ、復旧契約は[`../../platform/`](../../platform/)を参照する。
