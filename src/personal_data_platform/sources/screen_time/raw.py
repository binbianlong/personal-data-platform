"""Immutable Raw contract for Screen Time segment observations."""

from __future__ import annotations

import gzip
import hashlib
import hmac
import json
import re
import struct
from dataclasses import dataclass
from datetime import UTC, datetime

from personal_data_platform.raw.models import RawObject

APP_IN_FOCUS_STREAM = "app-in-focus"
APP_USAGE_STREAM = "app-usage"
RAW_SCHEMA_VERSION = 2
RAW_PREFIX = "raw/screen_time/v2"
_ENVELOPE_MAGIC = b"PDPST\x02"
_DEVICE_DOMAIN = b"screen-time/device/v1\0"
_SEGMENT_DOMAIN = b"screen-time/segment/v1\0"
_SAFE_KEY_PART = re.compile(r"^[a-z0-9-]+$")
_HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_RAW_KEY_PATTERN = re.compile(
    rf"^{re.escape(RAW_PREFIX)}/"
    r"(?P<device_key>[0-9a-f]{64})/"
    r"(?P<stream>[a-z0-9-]+)/"
    r"(?P<segment_key>[0-9a-f]{64})/"
    r"(?P<observed_at>\d{8}T\d{12}Z)/"
    r"(?P<sha256>[0-9a-f]{64})\.segb\.gz$"
)


def sha256_hex(raw_bytes: bytes) -> str:
    """Return the content hash of the uncompressed source bytes."""
    return hashlib.sha256(raw_bytes).hexdigest()


def gzip_raw_bytes(raw_bytes: bytes) -> bytes:
    """Compress Raw deterministically so crash recovery retains identical bytes."""
    return gzip.compress(raw_bytes, compresslevel=9, mtime=0)


def build_device_key(secret: bytes, device_identifier: str) -> str:
    """Pseudonymize an Apple device identifier with a domain-separated HMAC."""
    _require_secret(secret)
    _require_source_value("device identifier", device_identifier)
    return hmac.new(secret, _DEVICE_DOMAIN + device_identifier.encode(), hashlib.sha256).hexdigest()


def build_segment_key(
    secret: bytes,
    *,
    device_identifier: str,
    stream: str,
    relative_path: str,
) -> str:
    """Pseudonymize a device-scoped segment path with a domain-separated HMAC."""
    _require_secret(secret)
    _require_source_value("device identifier", device_identifier)
    _require_source_value("stream", stream)
    _require_relative_path(relative_path)
    message = b"\0".join(
        (
            _SEGMENT_DOMAIN.removesuffix(b"\0"),
            device_identifier.encode(),
            stream.encode(),
            relative_path.encode(),
        )
    )
    return hmac.new(secret, message, hashlib.sha256).hexdigest()


def format_observed_at(observed_at: datetime) -> str:
    """Format an aware observation time as a lexically sortable UTC value."""
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("observed_at must be timezone-aware")
    return observed_at.astimezone(UTC).strftime("%Y%m%dT%H%M%S%fZ")


def parse_observed_at(value: str) -> datetime:
    """Parse the canonical UTC observation timestamp."""
    try:
        return datetime.strptime(value, "%Y%m%dT%H%M%S%fZ").replace(tzinfo=UTC)
    except ValueError as error:
        raise ValueError(f"invalid observed_at: {value}") from error


@dataclass(frozen=True, slots=True)
class ScreenTimeRawIdentity:
    """Identity encoded entirely in a Screen Time Raw object key."""

    device_key: str
    stream: str
    segment_key: str
    observed_at: datetime
    sha256: str

    def __post_init__(self) -> None:
        if not _HEX_SHA256.fullmatch(self.device_key):
            raise ValueError("device_key must be a lowercase SHA-256 hex digest")
        if not _SAFE_KEY_PART.fullmatch(self.stream):
            raise ValueError("stream must contain only lowercase letters, digits, and hyphens")
        if not _HEX_SHA256.fullmatch(self.segment_key):
            raise ValueError("segment_key must be a lowercase SHA-256 hex digest")
        if not _HEX_SHA256.fullmatch(self.sha256):
            raise ValueError("sha256 must be a lowercase SHA-256 hex digest")
        format_observed_at(self.observed_at)

    @property
    def scope_prefix(self) -> str:
        """Return the prefix shared by every observation of this segment."""
        return f"{RAW_PREFIX}/{self.device_key}/{self.stream}/{self.segment_key}/"

    @property
    def object_key(self) -> str:
        """Return the versioned immutable object-storage key."""
        return f"{self.scope_prefix}{format_observed_at(self.observed_at)}/{self.sha256}.segb.gz"


def parse_raw_object_key(
    key: str, *, storage_created_at: datetime, storage_generation: int
) -> RawObject:
    """Parse a canonical Screen Time Raw key and its storage creation time."""
    values = _match_raw_object_key(key).groupdict()
    return RawObject(
        key=key,
        source_id="screen_time",
        schema_version=RAW_SCHEMA_VERSION,
        subject_key=values["device_key"],
        stream=values["stream"],
        logical_key=values["segment_key"],
        observed_at=parse_observed_at(values["observed_at"]),
        sha256=values["sha256"],
        storage_created_at=storage_created_at,
        storage_generation=storage_generation,
    )


def validate_raw_object_key(key: str) -> None:
    """Reject keys outside the supported Screen Time Raw namespaces."""
    _match_raw_object_key(key)


def _match_raw_object_key(key: str) -> re.Match[str]:
    match = _RAW_KEY_PATTERN.fullmatch(key)
    if match is None:
        raise ValueError(f"invalid Screen Time Raw object key: {key}")
    return match


def _require_secret(secret: bytes) -> None:
    if not secret:
        raise ValueError("pseudonym secret must not be empty")


def _require_source_value(label: str, value: str) -> None:
    if not value or "\0" in value:
        raise ValueError(f"{label} must be a non-empty string without NUL bytes")


def _require_relative_path(value: str) -> None:
    _require_source_value("relative path", value)
    parts = value.split("/")
    if value.startswith("/") or any(part in {"", ".", ".."} for part in parts):
        raise ValueError("relative path must be a normalized relative POSIX path")


def encode_segment_envelope(segment: bytes, *, name: str, kind: str) -> bytes:
    """Carry the source filename needed for exact tombstone references, without device paths."""
    _validate_envelope_identity(name, kind)
    metadata = json.dumps(
        {"source_segment_name": name, "segment_kind": kind},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return _ENVELOPE_MAGIC + struct.pack(">I", len(metadata)) + metadata + segment


def decode_segment_envelope(payload: bytes) -> tuple[bytes, str, str]:
    if not payload.startswith(_ENVELOPE_MAGIC) or len(payload) < 10:
        raise ValueError("invalid Screen Time envelope")
    size = struct.unpack_from(">I", payload, len(_ENVELOPE_MAGIC))[0]
    start = len(_ENVELOPE_MAGIC) + 4
    if not 0 < size <= 1024 or start + size >= len(payload):
        raise ValueError("invalid Screen Time envelope metadata size")
    try:
        metadata = json.loads(payload[start : start + size])
        if set(metadata) != {"source_segment_name", "segment_kind"}:
            raise ValueError("unexpected envelope metadata")
        name, kind = metadata["source_segment_name"], metadata["segment_kind"]
        _validate_envelope_identity(name, kind)
    except (TypeError, KeyError, ValueError) as error:
        raise ValueError("invalid Screen Time envelope metadata") from error
    return payload[start + size :], name, kind


def _validate_envelope_identity(name: str, kind: str) -> None:
    if not isinstance(name, str) or not name.isascii() or not name.isdecimal():
        raise ValueError("source segment name must be numeric")
    if kind not in ("events", "tombstones"):
        raise ValueError("unsupported source segment kind")
