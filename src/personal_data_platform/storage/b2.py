"""Backblaze B2 repository for immutable Screen Time Raw objects."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from personal_data_platform.config import B2Config
from personal_data_platform.raw.screen_time import (
    RAW_PREFIX,
    ScreenTimeRawIdentity,
    gzip_raw_bytes,
    parse_observed_at,
    sha256_hex,
)

_RAW_KEY_PATTERN = re.compile(
    rf"^{re.escape(RAW_PREFIX)}/"
    r"(?P<device_key>[0-9a-f]{64})/"
    r"(?P<stream>[a-z0-9-]+)/"
    r"(?P<segment_key>[0-9a-f]{64})/"
    r"(?P<observed_at>\d{8}T\d{12}Z)/"
    r"(?P<sha256>[0-9a-f]{64})\.segb\.gz$"
)
SCAN_RECEIPT_PREFIX = f"{RAW_PREFIX}/_control/collector/latest"
_SCAN_RECEIPT_KEY_PATTERN = re.compile(
    rf"^{re.escape(SCAN_RECEIPT_PREFIX)}/(?P<device_key>[0-9a-f]{{64}})\.json$"
)


@dataclass(frozen=True, slots=True)
class RawObservationRef:
    """Screen Time observation metadata recoverable without reading its body."""

    key: str
    device_key: str
    stream: str
    segment_key: str
    observed_at: datetime
    sha256: str


@dataclass(frozen=True, slots=True)
class CollectorScanReceipt:
    """Mutable liveness receipt for one pseudonymized iPhone collector scope."""

    device_key: str
    completed_at: datetime
    segment_count: int

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[0-9a-f]{64}", self.device_key):
            raise ValueError("scan receipt device_key must be lowercase SHA-256 hex")
        if self.completed_at.tzinfo is None or self.completed_at.utcoffset() is None:
            raise ValueError("scan receipt completed_at must be timezone-aware")
        if self.segment_count < 0:
            raise ValueError("scan receipt segment_count must be non-negative")

    @property
    def key(self) -> str:
        return f"{SCAN_RECEIPT_PREFIX}/{self.device_key}.json"

    def to_bytes(self) -> bytes:
        return json.dumps(
            {
                "schema_version": 1,
                "device_key": self.device_key,
                "completed_at": self.completed_at.astimezone(UTC).isoformat(),
                "segment_count": self.segment_count,
                "status": "succeeded",
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()

    @classmethod
    def from_bytes(cls, key: str, value: bytes) -> CollectorScanReceipt:
        match = _SCAN_RECEIPT_KEY_PATTERN.fullmatch(key)
        if match is None:
            raise ValueError(f"invalid collector scan receipt key: {key}")
        try:
            decoded = json.loads(value)
            if decoded.get("schema_version") != 1 or decoded.get("status") != "succeeded":
                raise ValueError("unsupported collector scan receipt")
            receipt = cls(
                device_key=decoded["device_key"],
                completed_at=datetime.fromisoformat(decoded["completed_at"]),
                segment_count=int(decoded["segment_count"]),
            )
        except (AttributeError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise ValueError("invalid collector scan receipt JSON") from error
        if receipt.device_key != match.group("device_key"):
            raise ValueError("collector scan receipt device_key does not match its key")
        return receipt


def parse_raw_object_key(key: str) -> RawObservationRef:
    """Parse a canonical Screen Time v1 Raw key."""
    match = _RAW_KEY_PATTERN.fullmatch(key)
    if match is None:
        raise ValueError(f"invalid Screen Time Raw object key: {key}")
    values = match.groupdict()
    return RawObservationRef(
        key=key,
        device_key=values["device_key"],
        stream=values["stream"],
        segment_key=values["segment_key"],
        observed_at=parse_observed_at(values["observed_at"]),
        sha256=values["sha256"],
    )


class B2RawRepository:
    """Read/write adapter whose collector usage requires only B2 writeFiles."""

    def __init__(self, *, client: Any, bucket: str) -> None:
        self._client = client
        self._bucket = bucket

    @classmethod
    def from_config(cls, config: B2Config) -> B2RawRepository:
        """Build an S3-compatible client from runtime configuration."""
        import boto3

        client = boto3.client(
            "s3",
            endpoint_url=config.endpoint,
            aws_access_key_id=config.key_id,
            aws_secret_access_key=config.application_key,
            region_name=config.region,
        )
        return cls(client=client, bucket=config.bucket)

    @classmethod
    def from_env(cls) -> B2RawRepository:
        """Build the cloud-side repository from environment or Secret Manager values."""
        return cls.from_config(B2Config.from_env())

    def put_compressed_raw(self, key: str, compressed_bytes: bytes) -> None:
        """Upload pre-compressed Raw without any read/list request."""
        parse_raw_object_key(key)
        self._client.put_object(
            Bucket=self._bucket,
            Key=key,
            Body=compressed_bytes,
            ContentType="application/octet-stream",
            ContentEncoding="gzip",
            ServerSideEncryption="AES256",
        )

    def put_scan_receipt(self, receipt: CollectorScanReceipt) -> None:
        """Replace the mutable liveness receipt after a complete successful scan."""
        self._client.put_object(
            Bucket=self._bucket,
            Key=receipt.key,
            Body=receipt.to_bytes(),
            ContentType="application/json",
            ServerSideEncryption="AES256",
        )

    def store_raw(self, identity: ScreenTimeRawIdentity, raw_bytes: bytes) -> str:
        """Validate, compress, and upload a Raw observation."""
        actual_sha256 = sha256_hex(raw_bytes)
        if actual_sha256 != identity.sha256:
            raise ValueError(
                f"Raw SHA-256 mismatch: identity={identity.sha256}, actual={actual_sha256}"
            )
        self.put_compressed_raw(identity.object_key, gzip_raw_bytes(raw_bytes))
        return identity.object_key

    def get_raw(self, key: str) -> bytes:
        """Download the stored gzip bytes for loader-side expansion and verification."""
        parse_raw_object_key(key)
        response = self._client.get_object(Bucket=self._bucket, Key=key)
        body = response["Body"]
        try:
            compressed_bytes = body.read()
        finally:
            close = getattr(body, "close", None)
            if close is not None:
                close()
        return compressed_bytes

    def list_raw(self, prefix: str = f"{RAW_PREFIX}/") -> list[RawObservationRef]:
        """List every parseable Raw observation, following all B2 pages."""
        observations: list[RawObservationRef] = []
        continuation_token: str | None = None
        while True:
            request: dict[str, Any] = {"Bucket": self._bucket, "Prefix": prefix}
            if continuation_token is not None:
                request["ContinuationToken"] = continuation_token
            response = self._client.list_objects_v2(**request)
            for item in response.get("Contents", []):
                try:
                    observations.append(parse_raw_object_key(item["Key"]))
                except (KeyError, ValueError):
                    continue
            if not response.get("IsTruncated", False):
                break
            continuation_token = response.get("NextContinuationToken")
            if not continuation_token:
                raise RuntimeError("B2 returned a truncated listing without a continuation token")
        return sorted(observations, key=lambda item: (item.observed_at, item.key))

    def list_scan_receipts(self) -> list[CollectorScanReceipt]:
        """Return each current collector liveness receipt."""
        keys: list[str] = []
        continuation_token: str | None = None
        while True:
            request: dict[str, Any] = {
                "Bucket": self._bucket,
                "Prefix": f"{SCAN_RECEIPT_PREFIX}/",
            }
            if continuation_token is not None:
                request["ContinuationToken"] = continuation_token
            response = self._client.list_objects_v2(**request)
            keys.extend(
                item["Key"]
                for item in response.get("Contents", [])
                if _SCAN_RECEIPT_KEY_PATTERN.fullmatch(item.get("Key", ""))
            )
            if not response.get("IsTruncated", False):
                break
            continuation_token = response.get("NextContinuationToken")
            if not continuation_token:
                raise RuntimeError("B2 returned truncated scan receipts without a token")

        receipts: list[CollectorScanReceipt] = []
        for key in sorted(keys):
            response = self._client.get_object(Bucket=self._bucket, Key=key)
            body = response["Body"]
            try:
                receipts.append(CollectorScanReceipt.from_bytes(key, body.read()))
            finally:
                close = getattr(body, "close", None)
                if close is not None:
                    close()
        return receipts
