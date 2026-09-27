# Fitbit

Fitbit端末の歩数、心拍、日次安静時心拍、アクティブゾーン時間、睡眠を扱う。
source / streamは`fitbit / health`。Webhookを契機にGoogle Health APIから取得する。

| 文書 | 内容 |
|---|---|
| [acquisition.md](acquisition.md) | API・Webhookの取得と認証 |
| [data-model.md](data-model.md) | Raw、テーブル、範囲更新、分析ビュー |
| [operations.md](operations.md) | CLI、導入、停止、復旧、検証状況 |

ローカル実装・検証済み。本番導入と実使用量の確認は[導入前の確認事項](operations.md#導入前の確認事項)に従う。
