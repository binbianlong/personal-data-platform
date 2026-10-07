# マイグレーション整理の実装計画

Spec: `docs/superpowers/specs/2026-10-07-current-schema.md`

## Global Constraints

ブランチは `refactor/migration-cleanup`、開始点は `702be47`。ローカルの実装・検証・コミットまでを対象とする。旧履歴を書き換える互換移行は作らず、Screen Timeの取得・保留規則、Raw形式、既存の書き込みleaseは維持する。

### Task 1: 現行スキーマを単一のDDLとCLIで初期化

Interfaces: Produces `001_current.sql`, profile引数のない `Warehouse.migrate()`, `pdp migrate`。Task 2はこのDDLのFitbit部分を置換する。

- [x] 空のDB、旧DBの拒否、再実行、チェックサム、rollback、並行起動、別catalogの履歴についてテストを書く。
- [x] `.venv/bin/pytest -q tests/integration/test_current_schema.py` を実行。Expected: 新しい履歴とCLIの未実装により失敗。
- [x] 現行のScreen Time定義を1本へまとめ、旧SQLを撤去する。profile設定・引数とFitbit専用migrateを撤去し、共通CLIを追加する。
- [x] 関連テストを現行履歴へ更新し、`.venv/bin/pytest -q tests/integration/test_migrations.py tests/integration/test_current_schema.py tests/unit/test_fitbit_cli.py tests/unit/test_dbt_runner.py tests/unit/test_preflight.py tests/unit/test_container_contract.py` を実行。Expected: 全件成功。
- [x] `refactor: 現行スキーマの初期化を1本に統一` としてコミット。

### Task 2: Fitbitの5テーブルとcoverageに整理

Interfaces: Consumes Task 1のDDLとmigrate API。Produces 型ごとの5テーブル、統一coverage、writer・acquisition・dbtの現行参照。

- [x] metricとdetail種別の分離、DATEのタイムゾーン非依存、APIの睡眠分数、minute coverage統合のテストを書く。
- [x] `.venv/bin/pytest -q tests/integration/test_fitbit_schema.py` を実行。Expected: 新しい5テーブルがなく失敗。
- [x] DDLと必要なwriter・取得済み判定・source依存・dbtだけを更新する。古いorigin優先処理とscope互換分岐を削除する。
- [x] `.venv/bin/pytest -q tests/integration/test_fitbit_schema.py tests/integration/test_fitbit_writer.py tests/integration/test_fitbit_acquisition.py tests/integration/test_fitbit_analytics.py tests/integration/test_fitbit_repair_runtime.py tests/integration/test_fitbit_loader.py tests/integration/test_fitbit_service.py` を実行。Expected: 全件成功。
- [x] 運用・データモデル資料を更新し、`refactor: Fitbitの保存モデルと取得範囲を整理` としてコミット。

### Task 3: 再構築と配布を検証

Interfaces: Consumes Task 2の現行スキーマ・集計。Uses 保存済みRawと比較用スナップショット。

- [x] `.venv/bin/pytest -q`、`.venv/bin/ruff check .`、`.venv/bin/ruff format --check .`、`.venv/bin/mypy`。Expected: 全件成功。
- [x] クリーンな作業ディレクトリからwheelを作り、配布SQLが1本でCLI初期化できることを確認する。
- [x] 保存済み56オブジェクトを新しいローカルDBへ再生する。イベント・補助状態の一致、集計キーと1マイクロ秒以内の数値一致、dbt検証の成功を確認する。
- [x] 全差分をレビューし、重大な問題は再現テストで修正する。必要な検証結果を資料へ記録しコミットする。

## Review Focus

同じIDを持つ異なるmetricと睡眠detailが干渉しないか。プロバイダーのDATEがAsia/TokyoセッションでもUTCの取得窓と一致するか。睡眠IDが日付を移動し、後日削除された後、古いRawで復活しないか。部分取得のcoverageに完全取得のハッシュを残さないか。旧スキーマへの書き込み拒否が全ランタイム経路で働くか。
