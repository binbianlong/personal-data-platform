# Personal Data Platform

個人データの取得、Raw保存、MotherDuckへの取込、dbt分析を行うPythonプロジェクト。

Screen TimeはiPhoneの`App.InFocus`とMacの`ScreenTime.AppUsage`を扱う。
FitbitはGoogle Health WebhookとPub/Sub経由で毎時取り込み、日次Jobで両sourceの取得・分析・監査を行う。
GCPは`us-west1`、MotherDuckは`us-west-2`を使う。共通処理とsourceの分離・追加手順は
[`アーキテクチャ`](docs/platform/architecture.md)を参照する。

## 開発環境

Python 3.13以降を使用する。

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
```

CLIはローカルCollectorとCloud Run Jobの共通entrypointを提供する。

```bash
pdp --help
python -m personal_data_platform.entrypoint --help
```

主なcommand:

```text
pdp screen-time devices
pdp screen-time doctor
pdp screen-time inspect-mac
pdp screen-time collect --once
pdp screen-time collect --watch
pdp screen-time launch-agent --output <plist-path>
pdp loader
pdp dbt
pdp reconciliation
pdp rebuild --dry-run
pdp rebuild --target-db <scratch-database> --allow-partial-history
pdp preflight
pdp fitbit migrate --database <scratch.duckdb> --profile west
pdp fitbit serve
pdp fitbit ingest-notifications
pdp fitbit sync --from 2026-09-20 --to 2026-09-27
```

`loader`、`rebuild`は指定を省略するとiPhoneの`screen_time / app-in-focus`を対象にする。
`reconciliation`はScreen Time両stream・Fitbitの直近7完了日・dbt・監査をまとめた日次処理である。
`loader`、`rebuild`で両端末を処理する場合は`--source screen_time --all-streams`、単一streamなら
`--source screen_time --stream app-in-focus`または`--stream app-usage`を付ける。
未登録の組合せは拒否する。`reconciliation`の対象は常に日次全体である。
`pdp dbt`は指定なしでは全model、source / stream指定時は対応するmodelとtestを実行する。

Fitbitの設定・スキーマ準備・復旧手順は[`Fitbit運用`](docs/sources/fitbit/operations.md)を参照する。
デプロイ、DB更新、日次処理、監視、再構築は[`Platform運用`](docs/platform/operations.md)を参照する。
完了したリージョン移行の記録は[`西部移行記録`](docs/platform/west-migration-2026-10-05.md)に残す。
ZIP取り込み機能はアプリに含めない。

`pdp screen-time inspect-mac`はMac自身の`App.InFocus/local`にある完成済みsegmentを読み取り専用で
解析する。`--directory PATH`で検証対象を変更できる。JSONにはBundle ID別の開始・終了レコード件数、
全体のrecord種別件数とevent時刻の範囲を表示する。これは利用時間の集計ではなく、RawやDBへの保存、
GCS認証、Keychainの設定は行わない。詳細は[`Screen Time取得仕様`](docs/sources/screen-time/acquisition.md#appinfocus診断command)を参照する。

## Screen Time Collector

CollectorはMac上のBiomeから完成済みsegmentをGCSへ保存する。
疑似化secretはKeychain、認証は専用Service AccountをimpersonateするADCを使う。
`devices`の疑似化keyからiPhoneのallowlistとMacの対象を設定し、収集プロセスへFull Disk Accessを付与する。

```bash
pdp screen-time devices
pdp screen-time doctor
pdp screen-time collect --once
pdp screen-time collect --watch
```

`--watch`は30分ごとに走査する。最新の未完了segmentは後続segmentが現れるまで保留し、
upload失敗時はlocal SQLiteに保存した同じkeyとbytesで再送する。
認証・疑似化は[`取得仕様`](docs/sources/screen-time/acquisition.md#認証と疑似化secret)、
Raw形式は[`データモデル`](docs/sources/screen-time/data-model.md)、
環境変数・LaunchAgent・端末の休止・復旧は[`Screen Time運用`](docs/sources/screen-time/operations.md)を正本とする。

## テスト

workflowの回帰テストには、Pythonに加えてBashとjqが必要になる。

```bash
ruff check src tests
ruff format --check src tests
mypy src
pytest
```

Python本体（`src`）はPython 3.13を対象にmypyのstrictモードで型チェックする。
テストコードは型チェック対象に含めず、pytestで動作を検証する。
GCS・SEGBの型情報がない依存は、利用する操作に絞ったProtocolで境界を定義する。
汎用SQLの結果とソース固有の詳細値の変換では、実行時に型が決まるため限定的にAnyを使用する。

## コンテナ実行

```bash
docker build --tag personal-data-platform:dev .
docker run --rm personal-data-platform:dev --help
```

## CI

Pull Requestと`main`へのpushでは、Python、コンテナ、Terraformの検証をGitHub Actionsで実行する。default branchへのマージ条件は[`infra/github/`](infra/github/)のrepository rulesetで管理する。

設計資料は[`docs/`](docs/)を参照する。
