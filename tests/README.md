# テスト構成

- `unit/`: 外部サービスを使わない小さな単位の検証。Fitbit・OwnTracks・Screen Timeはソース別に配置する。
- `integration/`: DB・API・処理フローを組み合わせた検証。ソース共通の処理は直下に置く。
- `contract/`: Mac側の入力形式やCollectorの境界を確認する。
- `infra/`: TerraformとCIの構成を確認する。

複数の階層で使うScreen Timeのテストデータ生成処理は`screen_time_helpers.py`にまとめる。
