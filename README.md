# Personal Data Platform

Screen TimeのRawをMacのSQLiteへ、OwnTracksの位置RawをGCSへ保存し、Fitbitは通知受信時にAPIからMotherDuckへ直接取り込む個人データ基盤。
GCPは`us-west1`、MotherDuckは`us-west-2`を使う。運用と仕様は[ドキュメント](docs/README.md)を参照する。

## 開発環境

Python 3.13以降と[uv](https://docs.astral.sh/uv/)を使用する。macOSでは`brew install uv`で導入する。

```bash
uv sync --locked
source .venv/bin/activate
pdp --help
python -m personal_data_platform.entrypoint --help
```

`uv sync --locked`は`uv.lock`の依存バージョンから開発環境を作る。
依存の追加は`uv add <package>`、開発用ツールの追加は`uv add --dev <package>`、
依存の更新は`uv lock --upgrade-package <package>`を使い、`pyproject.toml`と`uv.lock`を一緒に管理する。

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
pdp fitbit sync --from YYYY-MM-DD --to YYYY-MM-DD
```

`loader`・`rebuild`は省略時にiPhoneの`screen_time / app-in-focus`を選ぶ。
単一streamは`--source screen_time --stream app-in-focus`または`app-usage`を指定する。
`pdp dbt`は指定なしで全model、source/stream指定時は対応model/testを実行する。
FitbitのRawは保存せず、復旧には`pdp fitbit sync --from ... --to ...`を使う。
`pdp reconciliation`はFitbit補修・dbtとMacのScreen Time取込成功記録の監査を行う日次処理である。

Collectorは専用Keychain tokenを使い、最新segmentも検査してSQLiteへ保存後にMotherDuckへ直接取り込む。
各ファイルの最新成功RawとpendingだけをMacに保持する。
初期設定・LaunchAgent・休止・復旧は[Screen Time運用](docs/sources/screen-time/operations.md)、
通知・API補修は[Fitbit運用](docs/sources/fitbit/operations.md)、
位置通知・Raw補修は[OwnTracks運用](docs/sources/owntracks/operations.md)、
DB更新・監視・Rebuildは[Platform運用](docs/platform/operations.md)に従う。

## 検証とコンテナ

workflowの回帰テストにはBashとjqも必要となる。mypyは`src`をstrictモードで検証する。

```bash
uv run --locked ruff check src tests
uv run --locked ruff format --check src tests
uv run --locked mypy src
uv run --locked pytest
uv build --wheel
docker build --tag personal-data-platform:dev .
docker run --rm personal-data-platform:dev --help
```

テストの配置は[テスト構成](tests/README.md)を参照する。
PR・mainへのpushではPython・コンテナ・TerraformをCIで検証する。
マージ条件は[repository ruleset](infra/github/README.md)で管理する。
