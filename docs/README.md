# ドキュメント

Personal Data Platformの設計、データ仕様、運用手順を管理する。

## 構成

- [`platform/`](platform/)
- [`sources/`](sources/)
- [`analytics/`](analytics/)

## 移行設計

- [Pub/Sub・GCSを使う西部リージョンへの移行設計](superpowers/specs/2026-10-05-pubsub-gcs-migration-design.md): Screen Time・Fitbitの収集、Raw 90日保持、心拍の1分集約、リージョン切替の設計案。実装・本番反映は未実施。
- [Pub/Sub・GCS西部リージョン移行計画](superpowers/plans/2026-10-05-pubsub-gcs-migration-plan.md): 実装単位、検証、GCP配置、Screen Time移行とFitbit初期化、マイグレーション整理、切替・rollback・費用確認、最後の全体整理・リファクタリング。

## 管理ルール

- 同じ情報を複数の文書に書かず、正本となる文書へリンクする。
