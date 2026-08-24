# Screen Time

iPhone・MacのBiomeから取得するScreen Timeデータの仕様を管理する。

すべてのsourceに共通する設計、データ契約、運用原則は[`platform/`](../../platform/)を参照する。

## ファイル

| ファイル | 管理する内容 |
|---|---|
| [`acquisition.md`](acquisition.md) | 取得元、権限、SEGB・protobuf形式 |
| [`data-model.md`](data-model.md) | Raw record、利用event、利用区間、利用時間 |
| [`operations.md`](operations.md) | 差分取得、補正、異常判定 |
