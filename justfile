set positional-arguments

# コマンド一覧
default:
    @just --list

# ロック済みの依存から開発環境を作成・同期
sync:
    uv sync --locked

# Lint
lint:
    uv run --locked ruff check src tests

# コードを整形
format:
    uv run --locked ruff format src tests

# コードの整形を検証
format-check:
    uv run --locked ruff format --check src tests

# 型検査
typecheck:
    uv run --locked mypy src

# テスト（pytestの引数を指定可能）
test *args:
    uv run --locked pytest "$@"

# Lint・整形・型・テストをまとめて検証
check: lint format-check typecheck test

# Wheelをビルド
build:
    uv build --wheel

# CLIを実行
pdp *args:
    uv run --locked pdp "$@"

# 開発用コンテナをビルド
docker-build:
    docker build --tag personal-data-platform:dev .
