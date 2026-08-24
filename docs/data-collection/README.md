# データ取得仕様

このディレクトリは、データソース固有の取得仕様を管理する。

最初の対象はiPhone・MacのScreen Timeとする。今後追加するデータソースも、sourceごとの取得仕様としてこのディレクトリで管理する。

記載する内容:

- 取得元とアクセス方法
- 取得できるデータ
- source固有のデータ形式と時刻形式
- 差分取得と再取得の方法
- 必要な実行権限
- 欠損、重複、取得エラーの扱い

プラットフォーム全体で共有するRaw保存や再構築の原則は[`architecture/`](../architecture/)、正規化後のeventやtableの定義は[`data-model/`](../data-model/)で管理する。

現在のデータソース:

- [iPhone・Mac Screen Time](screen-time.md)
