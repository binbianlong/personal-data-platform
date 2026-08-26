# Personal Data Platform

個人データ基盤を構築するためのPythonプロジェクト。

## 開発環境

Python 3.13以降を使用する。

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
```

現在のentrypointは実行roleを検証するところまでを提供する。

```bash
python -m personal_data_platform.entrypoint webhook
```

利用できるroleは`webhook`、`fetch`、`loader`、`dbt`、`reconciliation`。

## テスト

```bash
ruff check src tests
pytest
```

## コンテナ実行

```bash
docker build --tag personal-data-platform:dev .
docker run --rm personal-data-platform:dev webhook
```

## CI

Pull Requestと`main`へのpushでは、Python、コンテナ、Terraformの検証をGitHub Actionsで実行する。default branchへのマージ条件は[`infra/github/`](infra/github/)のrepository rulesetで管理する。

設計資料は[`docs/`](docs/)を参照する。
