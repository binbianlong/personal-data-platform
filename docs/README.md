# ドキュメント

Personal Data Platformの設計、データ仕様、運用手順を管理する。

## 構成

- [`platform/`](platform/)
- [`sources/`](sources/)
- [`analytics/`](analytics/)

## 通常運用

- [アーキテクチャ](platform/architecture.md): 現在の構成、共通処理とsourceの境界。
- [Platform運用](platform/operations.md): デプロイ、日次処理、監視、DB更新、再構築。
- [Fitbit運用](sources/fitbit/operations.md)と[Screen Time運用](sources/screen-time/operations.md): source固有の設定と復旧。

## 移行履歴

2026-10-06に西部への本番切替を完了した。移行用のコピー・整理scriptは撤去済みで、通常の復旧にはPlatform運用を使う。

- [西部リージョン移行記録](platform/west-migration-2026-10-05.md): 切替時の接続先、データ保全、復元、旧資産整理と検証結果。
- [移行設計の履歴](superpowers/specs/2026-10-05-pubsub-gcs-migration-design.md)、[実装・移行計画の履歴](superpowers/plans/2026-10-05-pubsub-gcs-migration-plan.md): 完了した移行の背景と実施内容。

## 管理ルール

- 同じ情報を複数の文書に書かず、正本となる文書へリンクする。
