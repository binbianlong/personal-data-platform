# Platform

すべてのデータソースに共通する設計、データ契約、運用手順を管理する。

## 文書

| 文書 | 内容 |
|---|---|
| [`architecture.md`](architecture.md) | 共通処理とsourceの境界、source追加手順、保証 |
| [`raw-data.md`](raw-data.md) | GCS Rawのidentity・source別保持期限・再生契約 |
| [`analytics.md`](analytics.md) | Loader、MotherDuck、dbt、ChatGPTの契約 |
| [`chatgpt-mcp.md`](chatgpt-mcp.md) | ChatGPTへのread-only MCP接続と受入確認 |
| [`security.md`](security.md) | IAM、secret、暗号化、個人情報の扱い |
| [`operations.md`](operations.md) | デプロイ、定期実行、監視、照合、再構築 |
