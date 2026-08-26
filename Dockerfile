FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml /app/
COPY src /app/src

RUN python -m pip install --no-cache-dir .

ENTRYPOINT ["python", "-m", "personal_data_platform.entrypoint"]
