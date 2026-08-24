# Documentation

Personal Data Platformの設計、データ仕様、運用手順を責務ごとに管理する。

最初の実装対象はiPhone・MacのScreen Timeとする。将来必要になるデータmodelと運用の責務も含めつつ、同じ内容を複数の階層へ分散させない。

## ディレクトリ

| ディレクトリ | 責務 |
|---|---|
| [`architecture/`](architecture/) | データソースをまたいで維持する設計と不変条件 |
| [`data-collection/`](data-collection/) | データソース固有の取得方法、Raw形式、差分取得、エラー処理 |
| [`data-model/`](data-model/) | Rawから分析用modelまでのschema、grain、key、metric定義 |
| [`operations/`](operations/) | Collector、データ取込、監視、補正、再構築の運用手順 |
| [`superpowers/`](superpowers/) | 検討中の設計案と実装計画。正式な仕様ではない |

## 管理方針

- Screen Time固有の内容は[`data-collection/screen-time.md`](data-collection/screen-time.md)を正本とする。
- sourceをまたいで共有する設計だけを`architecture/`へ置く。
- tableやeventの構造、stable key、metricの意味は`data-model/`へ置く。
- 実際に実行する導入、監視、補正、復旧手順は`operations/`へ置く。
- 同じ仕様を複数の文書へ複製しない。
- `superpowers/`の内容を正式文書や実装の根拠として直接参照しない。
