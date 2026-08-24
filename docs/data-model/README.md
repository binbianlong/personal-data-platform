# データモデル

Raw、base、mart、metricのschemaと意味を管理する。

## ファイル

| ファイル | 管理する内容 |
|---|---|
| [`raw.md`](raw.md) | 取得したRawデータの保存単位、識別方法、metadataを定義する |
| [`base.md`](base.md) | Rawデータから正規化するbase tableのgrain、key、schemaを定義する |
| [`marts.md`](marts.md) | 分析用途のmartについて、入力model、grain、変換結果を定義する |
| [`metrics.md`](metrics.md) | 分析で参照するmetricの計算方法、単位、欠損時の意味を定義する |
