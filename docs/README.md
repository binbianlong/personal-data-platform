# ドキュメント

Personal Data Platformの設計、データ仕様、運用手順を管理する。

## 構成

- [`platform/`](platform/)
- [`sources/`](sources/)
- [`analytics/`](analytics/)

## 移行設計

- [Pub/Sub・GCSを使う西部リージョンへの移行設計](superpowers/specs/2026-10-05-pubsub-gcs-migration-design.md): Screen Time・Fitbitの収集、Raw 90日保持、心拍の1分集約、リージョン切替の設計案。実装・本番反映は未実施。

## 管理ルール

- 同じ情報を複数の文書に書かず、正本となる文書へリンクする。
