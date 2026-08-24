# データソース

各データソースの取得、データモデル、運用仕様をsource単位で管理する。

## Source packageの責務

| ファイル | 管理する内容 |
|---|---|
| `README.md` | sourceの目的・対象とpackage内の文書案内。仕様本文は置かない |
| `acquisition.md` | 取得方式・取得元、認証・権限、pagination・rate limit、source固有のRaw・wire format、時刻形式 |
| `data-model.md` | Rawの保存単位、field、key、正規化後のschema、品質、source固有の派生値 |
| `operations.md` | 取得契機・頻度、cursor・watermark、retry・backfill・補正、異常条件、復旧方法 |

data typeごとの仕様が独立して増える場合はpackage内で追加分割し、`README.md`を入口として維持する。

## Source catalog

| データソース | 仕様 |
|---|---|
| Screen Time | [`screen-time/`](screen-time/) |
