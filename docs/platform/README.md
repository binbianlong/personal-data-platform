# Platform

すべてのデータソースに共通する設計、データ契約、運用手順を管理する。

## 構成

| ディレクトリ | 管理する内容 |
|---|---|
| [`architecture/`](architecture/) | システム全体の構成、データフロー、信頼性原則 |
| [`data-contracts/`](data-contracts/) | Rawとbaseに共通するデータ契約 |
| [`operations/`](operations/) | デプロイ、監視、復旧の共通手順 |

データソース固有のfield、取得頻度、欠損処理、異常条件は[`sources/`](../sources/)を正本とする。
