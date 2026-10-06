"""Copy immutable Screen Time Raw while preserving its retention deadline."""

from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

import google.cloud.storage as storage
import google_crc32c
from google.api_core.exceptions import PreconditionFailed
from google.cloud.storage.retry import DEFAULT_RETRY_IF_GENERATION_SPECIFIED

from personal_data_platform.sources.registry import get_source
from personal_data_platform.storage.gcs import GCSRawRepository
from personal_data_platform.storage.gcs_types import GCSClient

STREAMS = ("app-in-focus", "app-usage")
_FIELDS = {
    "key",
    "stream",
    "source_generation",
    "source_created_at",
    "retention_started_at",
    "compressed_sha256",
    "compressed_size",
    "content_sha256",
    "target_generation",
    "target_created_at",
}


def artifact_path(path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    resolved = path.resolve()
    if resolved.is_relative_to(root) and not resolved.is_relative_to(root / "var"):
        raise ValueError("migration artifacts inside the repository must be under ignored var/")


def timestamp(value: object):
    from datetime import datetime

    if not isinstance(value, str):
        raise ValueError("manifest timestamp must be a string")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("manifest timestamp must be timezone-aware")
    return parsed


def read_manifest(path: Path) -> dict:
    """Reject foreign data and incomplete or ambiguous copy metadata."""

    def unique_fields(pairs):
        result = {}
        for name, value in pairs:
            if name in result:
                raise ValueError("duplicate manifest JSON field")
            result[name] = value
        return result

    document = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_fields)
    if (
        not isinstance(document, dict)
        or set(document) != {"version", "source_bucket", "target_bucket", "objects"}
        or document["version"] != 1
    ):
        raise ValueError("invalid Raw manifest envelope")
    if (
        not all(
            isinstance(document[name], str) and document[name]
            for name in ("source_bucket", "target_bucket")
        )
        or document["source_bucket"] == document["target_bucket"]
    ):
        raise ValueError("source and target must be distinct named buckets")
    if not isinstance(document["objects"], list):
        raise ValueError("manifest objects must be a list")
    seen = set()
    for row in document["objects"]:
        if not isinstance(row, dict) or set(row) != _FIELDS or row["stream"] not in STREAMS:
            raise ValueError("invalid Screen Time Raw manifest entry")
        if type(row["source_generation"]) is not int or row["source_generation"] < 1:
            raise ValueError("source generation must be positive")
        if type(row["compressed_size"]) is not int or row["compressed_size"] < 1:
            raise ValueError("compressed size must be positive")
        raw = get_source("screen_time", row["stream"]).parse_raw_key(
            row["key"],
            storage_created_at=timestamp(row["source_created_at"]),
            storage_generation=row["source_generation"],
        )
        if raw.source_id != "screen_time" or raw.stream != row["stream"]:
            raise ValueError("Raw identity is outside Screen Time scope")
        if raw.key in seen:
            raise ValueError("duplicate Raw manifest key")
        seen.add(raw.key)
        if row["content_sha256"] != raw.sha256:
            raise ValueError("manifest content hash disagrees with Raw key")
        digest = row["compressed_sha256"]
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise ValueError("invalid compressed hash")
        if timestamp(row["retention_started_at"]) > raw.storage_created_at:
            raise ValueError("retention origin cannot follow storage creation")
        if row["target_generation"] is None:
            if row["target_created_at"] is not None:
                raise ValueError("partial target metadata")
        elif type(row["target_generation"]) is not int or row["target_generation"] < 1:
            raise ValueError("target generation must be positive")
        else:
            timestamp(row["target_created_at"])
    return document


def save_manifest(path: Path, document: dict) -> None:
    """Replace the durable cursor atomically; keep its metadata private locally."""
    artifact_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as file:
        temporary = Path(file.name)
        try:
            json.dump(document, file, sort_keys=True, indent=2)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    temporary.replace(path)


def checked_bytes(data: bytes, row: dict) -> bytes:
    if (
        len(data) != row["compressed_size"]
        or hashlib.sha256(data).hexdigest() != row["compressed_sha256"]
    ):
        raise RuntimeError("Raw compressed hash or size mismatch")
    if hashlib.sha256(gzip.decompress(data)).hexdigest() != row["content_sha256"]:
        raise RuntimeError("Raw content hash mismatch")
    return data


def inventory(
    client: GCSClient, *, source_bucket: str, target_bucket: str, manifest_path: Path
) -> dict:
    """Inventory both Screen Time streams, including observations not yet loaded."""
    artifact_path(manifest_path)
    if not source_bucket or not target_bucket or source_bucket == target_bucket:
        raise ValueError("source and target must be distinct named buckets")
    prior = read_manifest(manifest_path) if manifest_path.exists() else None
    if prior and (prior["source_bucket"], prior["target_bucket"]) != (source_bucket, target_bucket):
        raise ValueError("manifest belongs to other buckets")
    previous = {row["key"]: row for row in prior["objects"]} if prior else {}
    rows = []
    for stream in STREAMS:
        repository = GCSRawRepository(
            client=client, bucket=source_bucket, source=get_source("screen_time", stream)
        )
        for raw in repository.list_raw():
            data = repository.get_raw(raw.key, generation=raw.storage_generation)
            row = {
                "key": raw.key,
                "stream": raw.stream,
                "source_generation": raw.storage_generation,
                "source_created_at": raw.storage_created_at.isoformat(),
                "retention_started_at": raw.retention_origin.isoformat(),
                "content_sha256": raw.sha256,
                "compressed_sha256": hashlib.sha256(data).hexdigest(),
                "compressed_size": len(data),
                "target_generation": None,
                "target_created_at": None,
            }
            checked_bytes(data, row)
            old = previous.get(raw.key)
            if old:
                if any(
                    old[field] != row[field]
                    for field in _FIELDS - {"target_generation", "target_created_at"}
                ):
                    raise RuntimeError("immutable source changed since inventory")
                row.update({name: old[name] for name in ("target_generation", "target_created_at")})
            rows.append(row)
    document = {
        "version": 1,
        "source_bucket": source_bucket,
        "target_bucket": target_bucket,
        "objects": rows,
    }
    save_manifest(manifest_path, document)
    return document


def _verify_target(client: GCSClient, document: dict, row: dict) -> None:
    generation = row["target_generation"]
    if generation is None:
        raise RuntimeError("Raw copy is incomplete")
    blob = client.bucket(document["target_bucket"]).blob(row["key"], generation=generation)
    blob.reload()
    if (
        blob.generation != generation
        or blob.time_created != timestamp(row["target_created_at"])
        or blob.custom_time != timestamp(row["retention_started_at"])
    ):
        raise RuntimeError("target generation, creation time or retention origin mismatch")
    checked_bytes(blob.download_as_bytes(raw_download=True, if_generation_match=generation), row)


def copy_manifest(client: GCSClient, manifest_path: Path) -> int:
    document = read_manifest(manifest_path)
    for row in document["objects"]:
        if row["target_generation"] is not None:
            _verify_target(client, document, row)
            continue
        source_blob = client.bucket(document["source_bucket"]).blob(
            row["key"], generation=row["source_generation"]
        )
        source_blob.reload()
        if (
            source_blob.generation != row["source_generation"]
            or source_blob.time_created != timestamp(row["source_created_at"])
            or (source_blob.custom_time or source_blob.time_created)
            != timestamp(row["retention_started_at"])
        ):
            raise RuntimeError("source generation or retention metadata changed")
        data = checked_bytes(
            source_blob.download_as_bytes(
                raw_download=True, if_generation_match=row["source_generation"]
            ),
            row,
        )
        target = client.bucket(document["target_bucket"]).blob(row["key"])
        target.custom_time = timestamp(row["retention_started_at"])
        target.content_encoding = "gzip"
        try:
            target.upload_from_string(
                data,
                content_type="application/octet-stream",
                if_generation_match=0,
                checksum="crc32c",
                crc32c_checksum_value=base64.b64encode(
                    google_crc32c.Checksum(data).digest()
                ).decode("ascii"),
                retry=DEFAULT_RETRY_IF_GENERATION_SPECIFIED,
            )
        except PreconditionFailed:
            pass  # A copy may have succeeded before the manifest cursor was saved.
        target.reload()
        if target.generation is None or target.time_created is None:
            raise RuntimeError("target omitted immutable metadata")
        row["target_generation"] = int(target.generation)
        row["target_created_at"] = target.time_created.isoformat()
        _verify_target(client, document, row)
        save_manifest(manifest_path, document)
    return len(document["objects"])


def verify_manifest(client: GCSClient, manifest_path: Path) -> int:
    document = read_manifest(manifest_path)
    for row in document["objects"]:
        _verify_target(client, document, row)
    return len(document["objects"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--inventory-only", action="store_true")
    action.add_argument("--copy", action="store_true")
    action.add_argument("--verify", action="store_true")
    parser.add_argument("--source-bucket")
    parser.add_argument("--target-bucket")
    parser.add_argument("--project", default=os.environ.get("GOOGLE_CLOUD_PROJECT"))
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.inventory_only and (not args.source_bucket or not args.target_bucket):
        parser.error("inventory requires --source-bucket and --target-bucket")
    client = storage.Client(project=args.project)
    if args.inventory_only:
        count = len(
            inventory(
                client,
                source_bucket=args.source_bucket,
                target_bucket=args.target_bucket,
                manifest_path=args.manifest,
            )["objects"]
        )
    elif args.copy:
        count = copy_manifest(client, args.manifest)
    else:
        count = verify_manifest(client, args.manifest)
    print(json.dumps({"raw_object_count": count}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
