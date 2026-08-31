"""Explicit connectivity checks against isolated B2 and MotherDuck test namespaces."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import uuid
from datetime import UTC, datetime
from typing import Any

from personal_data_platform.config import B2Config
from personal_data_platform.recovery.rebuild import validate_rebuild_target
from personal_data_platform.storage.motherduck import WarehouseConfig, connect

PREFLIGHT_PREFIX = "test/preflight/"
LOGGER = logging.getLogger(__name__)


def probe_b2(client: Any, *, bucket: str, prefix: str = PREFLIGHT_PREFIX) -> dict[str, object]:
    """Write, read, and remove one uniquely named object under the test prefix."""

    payload = uuid.uuid4().bytes
    key = f"{prefix.rstrip('/')}/{uuid.uuid4()}.bin"
    version_id: str | None = None
    probe_error: Exception | None = None
    try:
        uploaded = client.put_object(
            Bucket=bucket,
            Key=key,
            Body=payload,
            ContentType="application/octet-stream",
            ServerSideEncryption="AES256",
        )
        version_id = uploaded.get("VersionId")
        if not version_id:
            raise RuntimeError("B2 preflight upload did not return a cleanup VersionId")
        response = client.get_object(Bucket=bucket, Key=key)
        body = response["Body"]
        try:
            downloaded = body.read()
        finally:
            close = getattr(body, "close", None)
            if close is not None:
                close()
        if downloaded != payload:
            raise RuntimeError("B2 preflight round trip changed the object bytes")
        listed = client.list_objects_v2(Bucket=bucket, Prefix=key).get("Contents", [])
        if not any(item.get("Key") == key for item in listed):
            raise RuntimeError("B2 preflight object was not returned by listing")
        return {
            "ok": True,
            "prefix": prefix,
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
    except Exception as error:
        probe_error = error
        raise
    finally:
        if version_id is not None:
            try:
                client.delete_object(Bucket=bucket, Key=key, VersionId=version_id)
            except Exception:
                if probe_error is None:
                    raise
                LOGGER.exception("B2 preflight cleanup failed after the probe failed")


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
    try:
        import boto3
    except ImportError as error:  # pragma: no cover - packaging failure
        raise RuntimeError("boto3 is required for B2 preflight") from error

    b2 = B2Config.from_env()
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

    b2_client = boto3.client(
        "s3",
        endpoint_url=b2.endpoint,
        aws_access_key_id=b2.key_id,
        aws_secret_access_key=b2.application_key,
        region_name=b2.region,
    )
    test_prefix = os.environ.get("B2_RAW_PREFIX", PREFLIGHT_PREFIX).strip().strip("/")
    if not test_prefix.startswith("test/") or ".." in test_prefix.split("/"):
        raise ValueError("preflight B2 prefix must be isolated below test/")
    results = {"b2": probe_b2(b2_client, bucket=b2.bucket, prefix=f"{test_prefix}/")}
    warehouse_connection = connect(WarehouseConfig(test_database, token))
    try:
        results["motherduck"] = probe_warehouse(warehouse_connection)
    finally:
        warehouse_connection.close()
    print(json.dumps(results, sort_keys=True))
    return 0
