"""One-time, generation-pinned GCS Screen Time import; stop the Collector first."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import subprocess
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from google.cloud import storage
from google.oauth2.credentials import Credentials

from personal_data_platform.loader.job import LOADER_LEASE_SECONDS, run_loader_objects
from personal_data_platform.raw.models import RawObject
from personal_data_platform.sources.registry import get_source
from personal_data_platform.sources.screen_time.cli import _warehouse_config
from personal_data_platform.sources.screen_time.raw import format_observed_at, sha256_hex
from personal_data_platform.sources.screen_time.state import CollectorState
from personal_data_platform.sources.screen_time.storage import ScreenTimeGCSRepository
from personal_data_platform.storage.motherduck import Warehouse, connect


def import_latest(
    state: CollectorState, warehouse: Warehouse, observations: list[tuple[RawObject, bytes]]
) -> None:
    """Keep verified latest copies without discarding local pending or newer Raw."""
    for raw, body in observations:
        if sha256_hex(gzip.decompress(body)) != raw.sha256:
            raise ValueError("Raw SHA-256 mismatch")
        source = get_source(raw.source_id, raw.stream)
        if raw.key not in warehouse.succeeded_keys_for(
            (raw,), parser_version=source.parser_version
        ):
            raise ValueError("Raw is not confirmed by the production ingestion ledger")
    with state._connect() as db:
        for raw, body in observations:
            db.execute(
                """INSERT INTO segment_observation
                (device_key,stream,segment_key,observed_at,sha256,object_key,compressed_payload,
                 status,storage_created_at,storage_generation,retention_started_at)
                VALUES (?,?,?,?,?,?,?,'uploaded',?,?,?) ON CONFLICT(object_key) DO UPDATE SET
                  compressed_payload=excluded.compressed_payload,
                  storage_created_at=excluded.storage_created_at,
                  storage_generation=excluded.storage_generation,
                  retention_started_at=excluded.retention_started_at""",
                (
                    raw.subject_key,
                    raw.stream,
                    raw.logical_key,
                    format_observed_at(raw.observed_at),
                    raw.sha256,
                    raw.key,
                    body,
                    format_observed_at(raw.storage_created_at),
                    raw.storage_generation,
                    format_observed_at(raw.retention_started_at)
                    if raw.retention_started_at
                    else None,
                ),
            )
    for raw, _ in observations:
        state.mark_uploaded(raw.key, datetime.now(UTC))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--bucket", action="append", required=True, help="Current bucket first")
    parser.add_argument("--state-db", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    access = subprocess.run(
        ["gcloud", "auth", "print-access-token"], capture_output=True, text=True, check=True
    ).stdout.strip()
    client = storage.Client(project=args.project, credentials=Credentials(token=access))
    warehouse = Warehouse(connect(_warehouse_config()))
    owner = str(uuid.uuid4())
    if not warehouse.acquire_job_lock("loader", owner, lease_seconds=LOADER_LEASE_SECONDS):
        raise RuntimeError("An active warehouse writer must finish before migration")
    manifest = []
    seen = set()
    latest = {}
    repaired = 0
    try:
        for bucket in args.bucket:
            for blob in client.list_blobs(bucket, prefix="raw/screen_time/"):
                generation = int(blob.generation)
                body = blob.download_as_bytes(if_generation_match=generation, raw_download=True)
                manifest.append(
                    {
                        "bucket": bucket,
                        "key": blob.name,
                        "generation": generation,
                        "stored_sha256": hashlib.sha256(body).hexdigest(),
                        "size": len(body),
                    }
                )
                if not blob.name.endswith(".segb.gz"):
                    continue
                source = get_source("screen_time", blob.name.split("/")[4])
                raw = replace(
                    source.parse_raw_key(
                        blob.name,
                        storage_created_at=blob.time_created,
                        storage_generation=generation,
                    ),
                    retention_started_at=blob.custom_time,
                )
                if sha256_hex(gzip.decompress(body)) != raw.sha256:
                    raise ValueError("GCS Raw SHA-256 mismatch")
                if raw.key in seen:
                    continue
                seen.add(raw.key)
                if raw.key not in warehouse.succeeded_keys_for(
                    (raw,), parser_version=source.parser_version
                ):
                    repo = ScreenTimeGCSRepository(client=client, bucket=bucket, source=source)
                    summary = run_loader_objects(
                        repo,
                        warehouse,
                        (raw,),
                        source=source,
                        buffered_payloads={raw.key: body},
                        _lease_owner=owner,
                    )
                    if not summary.ok:
                        raise RuntimeError("Unconfirmed GCS Raw could not be ingested")
                    repaired += summary.succeeded
                scope = (raw.subject_key, raw.stream, raw.logical_key)
                previous = latest.get(scope)
                if previous is None or (raw.observed_at, raw.key) > (
                    previous[0].observed_at,
                    previous[0].key,
                ):
                    latest[scope] = (raw, body)
        warehouse.require_job_lock(owner)
        state = CollectorState(args.state_db)
        import_latest(state, warehouse, list(latest.values()))
        args.manifest.parent.mkdir(parents=True, exist_ok=True)
        args.manifest.write_text(json.dumps(manifest, indent=2) + "\n")
        print(
            json.dumps(
                {
                    "verified_gcs_objects": len(manifest),
                    "repaired_raw": repaired,
                    "local_latest_count": len(latest),
                    "pending_count": len(state.pending()),
                    "compressed_bytes": sum(len(body) for _, body in latest.values()),
                }
            ),
            flush=True,
        )
    finally:
        if warehouse.connection_usable:
            warehouse.release_job_lock("loader", owner)
        warehouse.close()


if __name__ == "__main__":
    main()
