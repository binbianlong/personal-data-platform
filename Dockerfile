FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PDP_PROJECT_ROOT=/app

WORKDIR /app

COPY pyproject.toml /app/
COPY src /app/src
COPY dbt /app/dbt

RUN python -m pip install --no-cache-dir .

ENTRYPOINT ["python", "-m", "personal_data_platform.entrypoint"]
