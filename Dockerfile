FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PDP_PROJECT_ROOT=/app \
    UV_PROJECT_ENVIRONMENT=/usr/local \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

COPY pyproject.toml uv.lock /app/
COPY src /app/src
COPY dbt /app/dbt

RUN --mount=from=ghcr.io/astral-sh/uv:0.12.5,source=/uv,target=/bin/uv \
    uv sync --locked --no-dev --no-editable --no-cache \
    && useradd --uid 10001 --create-home app \
    && chown -R app:app /app

USER 10001

ENTRYPOINT ["python", "-m", "personal_data_platform.entrypoint"]
