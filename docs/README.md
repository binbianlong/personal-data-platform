# ドキュメント

Personal Data Platformの設計、データ仕様、運用手順を管理する。

## 構成

| ディレクトリ | 管理する内容 |
|---|---|
| [`architecture/`](architecture/) | データソースをまたいで維持する設計と不変条件 |
| [`data-collection/`](data-collection/) | データソース固有の取得方法、Raw形式、差分取得、エラー処理 |
| [`data-model/`](data-model/) | Rawから分析用modelまでのschema、grain、key、metric定義 |
| [`operations/`](operations/) | Collector、データ取込、監視、補正、再構築の運用手順 |
| [`superpowers/`](superpowers/) | 検討中の設計案と実装計画。正式な仕様ではない |

## 管理ルール

- 同じ情報を複数の文書に書かず、正本となる文書へリンクする。
- `superpowers/`は下書きとして扱い、正式な仕様はそれ以外のディレクトリで管理する。
