# Personal Data Platform

個人データ基盤を構築するためのPythonプロジェクト。

## 開発環境

Python 3.13以降を使用する。

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
```

Screen Timeのローカル収集とRawのLoaderをCLIから実行できる。

```bash
pdp screen-time devices
pdp screen-time doctor
pdp screen-time collect --once
pdp loader
```

## テスト

```bash
ruff check src tests
pytest
```

## コンテナ実行

```bash
docker build --tag personal-data-platform:dev .
docker run --rm personal-data-platform:dev screen-time --help
```

## CI

Pull Requestと`main`へのpushでは、Python、コンテナ、Terraformの検証をGitHub Actionsで実行する。default branchへのマージ条件は[`infra/github/`](infra/github/)のrepository rulesetで管理する。

設計資料は[`docs/`](docs/)を参照する。
