# Architecture

Personal Data Platform全体で共有する設計と不変条件を管理する。

現在はScreen Timeの取得から始めるため、source固有の詳細ではなく、今後も維持する必要がある境界だけを扱う。

## このディレクトリの責務

- Local Collector、Raw保存、Loader、分析層の責務境界
- Rawを正本として失わないための原則
- 同じデータを再処理しても重複しないための原則
- 分析層をRawから再構築できるための原則
- 複数の実装箇所で共有するデータフローやinterface

BiomeのpathやSEGB・protobuf形式は[`data-collection/`](../data-collection/)、分析用event・tableの定義は[`data-model/`](../data-model/)、実際の運用手順は[`operations/`](../operations/)で管理する。
