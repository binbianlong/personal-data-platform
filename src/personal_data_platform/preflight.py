"""Explicit connectivity checks against isolated GCS and MotherDuck test namespaces."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import uuid
from datetime import UTC, datetime
from typing import Any

from google.cloud import storage

from personal_data_platform.config import GCSConfig
from personal_data_platform.recovery.rebuild import validate_rebuild_target
from personal_data_platform.storage.motherduck import WarehouseConfig, connect

PREFLIGHT_PREFIX = "test/preflight/"
LOGGER = logging.getLogger(__name__)


def probe_gcs(client: Any, *, bucket: str, prefix: str = PREFLIGHT_PREFIX) -> dict[str, object]:
    """Write, read, list, and generation-delete one object in the test bucket."""

    payload = uuid.uuid4().bytes
    key = f"{prefix.rstrip('/')}/{uuid.uuid4()}.bin"
    bucket_ref = client.bucket(bucket)
    generation: int | None = None
    probe_error: Exception | None = None
    try:
        uploaded = bucket_ref.blob(key)
        uploaded.upload_from_string(
            payload,
            content_type="application/octet-stream",
            if_generation_match=0,
        )
        generation = uploaded.generation
        if generation is None:
            raise RuntimeError("GCS preflight upload did not return a cleanup generation")

        exact_generation = bucket_ref.blob(key, generation=generation)
        downloaded = exact_generation.download_as_bytes(
            raw_download=True,
            if_generation_match=generation,
        )
        if downloaded != payload:
            raise RuntimeError("GCS preflight round trip changed the object bytes")
        if not any(blob.name == key for blob in client.list_blobs(bucket_ref, prefix=key)):
            raise RuntimeError("GCS preflight object was not returned by listing")
        return {
            "ok": True,
            "prefix": prefix,
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
    except Exception as error:
        probe_error = error
        raise
    finally:
        if generation is not None:
            try:
                bucket_ref.blob(key, generation=generation).delete(
                    if_generation_match=generation,
                )
            except Exception:
                if probe_error is None:
                    raise
                LOGGER.exception("GCS preflight cleanup failed after the probe failed")


def probe_warehouse(connection: Any) -> dict[str, object]:
    """Exercise DDL and DML in a disposable schema within the test database."""

    table = f"probe_{uuid.uuid4().hex}"
    connection.execute("CREATE SCHEMA IF NOT EXISTS preflight")
    try:
        connection.execute(f"CREATE TABLE preflight.{table} (value INTEGER NOT NULL)")
        connection.execute(f"INSERT INTO preflight.{table} VALUES (42)")
        value = connection.execute(f"SELECT value FROM preflight.{table}").fetchone()[0]
        if value != 42:
            raise RuntimeError("MotherDuck preflight round trip returned the wrong value")
        return {"ok": True, "tested_at": datetime.now(UTC).isoformat()}
    finally:
        connection.execute(f"DROP TABLE IF EXISTS preflight.{table}")


def run_preflight_from_env() -> int:
    gcs = GCSConfig.from_env()
    preflight_bucket = os.environ.get("GCS_PREFLIGHT_BUCKET", "").strip()
    if not preflight_bucket:
        raise ValueError("GCS_PREFLIGHT_BUCKET is required")
    if preflight_bucket == gcs.bucket:
        raise ValueError("GCS_PREFLIGHT_BUCKET must differ from GCS_BUCKET")

    test_database = os.environ.get("PREFLIGHT_MOTHERDUCK_DATABASE")
    if not test_database:
        raise ValueError("PREFLIGHT_MOTHERDUCK_DATABASE is required")
    production_database = os.environ.get("MOTHERDUCK_DATABASE")
    if not production_database:
        raise ValueError("MOTHERDUCK_DATABASE is required to protect production")
    validate_rebuild_target(test_database, production_database)
    token = os.environ.get("MOTHERDUCK_TOKEN")
    if not token:
        raise ValueError("MOTHERDUCK_TOKEN is required")

    client = storage.Client(project=gcs.project_id)
    results = {"gcs": probe_gcs(client, bucket=preflight_bucket)}
    warehouse_connection = connect(WarehouseConfig(test_database, token))
    try:
        results["motherduck"] = probe_warehouse(warehouse_connection)
    finally:
        warehouse_connection.close()
    print(json.dumps(results, sort_keys=True))
    return 0
