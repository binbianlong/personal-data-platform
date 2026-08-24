# データモデル

取得したRawデータを、再処理可能な正規化eventと分析用tableへ変換する際のschemaと意味を管理する。

## このディレクトリの責務

- Screen Time transition eventと利用区間のgrain
- stable key、source record ID、重複判定key
- event time、observed time、ingested timeの使い分け
- columnの型、単位、nullability
- 欠損、推定値、無効値、真のゼロの区別
- Raw objectへのprovenance
- MotherDuck base tableとdbt modelの定義
- Screen Time利用時間など、分析で参照するmetricの意味

端末上の取得path、権限、wire format、scan方法は[`data-collection/`](../data-collection/)で管理する。Raw保存や再構築に共通する原則は[`architecture/`](../architecture/)を正本とする。

base、mart、metricを別ディレクトリへ分けず、データmodelの文書としてこの階層でまとめて管理する。
