# Personal Data Platform

iPhone Screen Time・Mac Screen Time・OwnTracks・Fitbitの行動・健康データを収集し、MotherDuckで分析する個人データ基盤。
構成・各ソースの仕様・運用手順は[ドキュメント](docs/README.md)を参照する。

## 開発環境

Python 3.13以降と[uv](https://docs.astral.sh/uv/)・[just](https://just.systems/)を使う。
macOSでは`brew install uv just`で導入する。

```bash
just sync
just pdp --help
```

ドキュメントの`pdp`コマンドを直接実行する場合は、先に`source .venv/bin/activate`する。

## 検証とコンテナ

workflowのテストにはBashとjqも必要。

```bash
just check
just build
just docker-build
docker run --rm personal-data-platform:dev --help
```

PRではPython・コンテナ・TerraformをCIで検証する。
マージ条件は[repository ruleset](infra/github/README.md)で管理する。
