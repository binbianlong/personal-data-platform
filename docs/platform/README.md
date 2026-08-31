# Platform

すべてのデータソースに共通する設計、データ契約、運用手順を管理する。

## 文書

| 文書 | 内容 |
|---|---|
| [`architecture.md`](architecture.md) | システム構成、データフロー、初期coreの保証 |
| [`raw-data.md`](raw-data.md) | B2 Rawの保存・観測・再生契約 |
| [`analytics.md`](analytics.md) | Loader、MotherDuck、dbt、ChatGPTの契約 |
| [`chatgpt-mcp.md`](chatgpt-mcp.md) | ChatGPTへのread-only MCP接続と受入確認 |
| [`security.md`](security.md) | IAM、secret、暗号化、個人情報の扱い |
| [`operations.md`](operations.md) | デプロイ、定期実行、監視、照合、再構築 |
