# データソース

各データソースの取得、データモデル、運用仕様をsource単位で管理する。

## Source catalog

| データソース | 取得方式 | 仕様 |
|---|---|---|
| Screen Time | Local Collector | [`screen-time/`](screen-time/) |

各source packageは`README.md`、`acquisition.md`、`data-model.md`、`operations.md`を基本構成とする。data typeごとの仕様が独立して増える場合はpackage内で追加分割し、`README.md`を入口として維持する。
