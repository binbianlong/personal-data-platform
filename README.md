# Personal Data Platform

Screen TimeとFitbitを取得し、GCS Raw、MotherDuck、dbtで保存・分析する個人データ基盤。
GCPは`us-west1`、MotherDuckは`us-west-2`を使う。運用と仕様は[ドキュメント](docs/README.md)を参照する。

## 開発環境

Python 3.13以降を使用する。

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
pdp --help
python -m personal_data_platform.entrypoint --help
```

## 主な操作

```bash
pdp screen-time devices
pdp screen-time doctor
pdp screen-time collect --once
pdp screen-time collect --watch
pdp loader --source screen_time --all-streams
pdp dbt
pdp reconciliation
pdp rebuild --source screen_time --all-streams --dry-run
pdp fitbit ingest-notifications
pdp fitbit sync --from YYYY-MM-DD --to YYYY-MM-DD
```

`loader`・`rebuild`は省略時にiPhoneの`screen_time / app-in-focus`を選ぶ。
単一streamは`--source screen_time --stream app-in-focus`または`app-usage`を指定する。
`pdp dbt`は指定なしで全model、source/stream指定時は対応model/testを実行する。
`pdp reconciliation`は両Screen Time stream・Fitbit補修・dbt・監査をまとめた日次処理である。

Collectorは疑似化keyと専用ADCを使い、未完了の最新segmentは後続segmentが現れるまで保留する。
初期設定・LaunchAgent・休止・復旧は[Screen Time運用](docs/sources/screen-time/operations.md)、
通知・API補修は[Fitbit運用](docs/sources/fitbit/operations.md)、
DB更新・監視・Rebuildは[Platform運用](docs/platform/operations.md)に従う。

## 検証とコンテナ

workflowの回帰テストにはBashとjqも必要となる。mypyは`src`をstrictモードで検証する。

```bash
ruff check src tests
ruff format --check src tests
mypy src
pytest
docker build --tag personal-data-platform:dev .
docker run --rm personal-data-platform:dev --help
```

PR・mainへのpushではPython・コンテナ・TerraformをCIで検証する。
マージ条件は[repository ruleset](infra/github/README.md)で管理する。
