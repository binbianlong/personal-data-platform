"""Small generation-guarded GCS receipts; completed history is never copied."""

from __future__ import annotations

import json
import logging
import re
import uuid
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import datetime, time, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from google.api_core.exceptions import NotFound, PreconditionFailed

from personal_data_platform.raw.models import RawObject

from .models import DATE_TYPES, Window, aware, date_cursor, object_dict, parse_time, string

RECEIPT_PREFIX = "receipts/fitbit/v1/"
LOGGER = logging.getLogger(__name__)
_TOKYO = ZoneInfo("Asia/Tokyo")
_KEY_PATTERN = re.compile(
    r"receipts/fitbit/v1/\d{4}-\d{2}-\d{2}/"
    r"(?:[a-f0-9]{32}|daily|bootstrap|weekly|device-[a-f0-9]{12})\.json"
)


def validate_receipt_key(key: str) -> None:
    if _KEY_PATTERN.fullmatch(key) is None:
        raise ValueError("invalid Fitbit receipt key")


def _integer(value: object) -> int:
    if type(value) is not int:
        raise ValueError("expected an integer")
    return value


def _raw_from_dict(value: object) -> RawObject:
    data = object_dict(value)
    return RawObject(
        key=string(data["key"]),
        source_id=string(data["source_id"]),
        schema_version=_integer(data["schema_version"]),
        subject_key=string(data["subject_key"]),
        stream=string(data["stream"]),
        logical_key=string(data["logical_key"]),
        observed_at=parse_time(string(data["observed_at"])),
        sha256=string(data["sha256"]),
        storage_created_at=parse_time(string(data["storage_created_at"])),
        storage_generation=_integer(data["storage_generation"]),
    )


def _json_default(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError("unsupported receipt value")


@dataclass(frozen=True, slots=True)
class ReceiptWork:
    window: Window
    raw: RawObject | None = None
    completed: bool = False
    fetched_at: datetime | None = None
    source_sha256: str | None = None

    def __post_init__(self) -> None:
        if self.fetched_at is not None:
            object.__setattr__(self, "fetched_at", aware(self.fetched_at))
        if (self.fetched_at is None) != (self.source_sha256 is None):
            raise ValueError("skipped acquisition needs both time and digest")
        if self.source_sha256 is not None and re.fullmatch(r"[0-9a-f]{64}", self.source_sha256) is None:
            raise ValueError("invalid acquisition digest")
        if self.source_sha256 is not None and (self.raw is not None or not self.completed):
            raise ValueError("no-change evidence requires completed work without Raw")


@dataclass(frozen=True, slots=True)
class Receipt:
    key: str
    subject_key: str
    received_at: datetime
    work: tuple[ReceiptWork, ...]
    completed_at: datetime | None = None
    origin: str = "webhook"

    def __post_init__(self) -> None:
        validate_receipt_key(self.key)
        if re.fullmatch(r"[A-Za-z0-9_-]{1,128}", self.subject_key) is None:
            raise ValueError("invalid pseudonymous subject key")
        if not self.work or self.origin not in (
            "webhook", "daily", "manual", "bootstrap", "weekly", "device-sync"
        ):
            raise ValueError("invalid receipt work or origin")
        object.__setattr__(self, "received_at", aware(self.received_at))
        if self.completed_at is not None:
            object.__setattr__(self, "completed_at", aware(self.completed_at))
            if not all(work.completed for work in self.work):
                raise ValueError("receipt completion needs every window")
        for item in self.work:
            if item.raw is not None and (
                item.raw.source_id != "fitbit"
                or item.raw.subject_key != self.subject_key
                or item.raw.stream != "health"
            ):
                raise ValueError("receipt Raw belongs to another source or subject")

    @classmethod
    def create(
        cls,
        subject_key: str,
        windows: tuple[Window, ...],
        *,
        received_at: datetime,
        daily: bool = False,
        origin: str = "webhook",
        key: str | None = None,
    ) -> Receipt:
        now = aware(received_at)
        identity = "daily" if daily else uuid.uuid4().hex
        work: list[ReceiptWork] = []
        for window in windows:
            start = window.start
            while start < window.end:
                # Physical ranges follow Tokyo days; provider civil dates use
                # their UTC-midnight comparison cursors.
                boundary = (
                    date_cursor(start.date()) + timedelta(days=1)
                    if window.data_type in DATE_TYPES
                    else datetime.combine(
                        start.astimezone(_TOKYO).date() + timedelta(days=1), time(), _TOKYO
                    )
                )
                end = min(window.end, boundary)
                work.append(ReceiptWork(Window(window.data_type, start, end)))
                start = end
        return cls(
            key or f"{RECEIPT_PREFIX}{now.date().isoformat()}/{identity}.json",
            subject_key,
            now,
            tuple(work),
            origin="daily" if daily else origin,
        )

    def to_bytes(self) -> bytes:
        return json.dumps(
            {"schema_version": 1, **asdict(self)},
            default=_json_default,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()

    @classmethod
    def from_bytes(cls, payload: bytes) -> Receipt:
        data = object_dict(json.loads(payload))
        if type(data.get("schema_version")) is not int or data["schema_version"] != 1:
            raise ValueError("unsupported receipt schema")
        work = data["work"]
        if not isinstance(work, list):
            raise ValueError("receipt work must be an array")
        items: list[ReceiptWork] = []
        for value in work:
            item = object_dict(value)
            scope = object_dict(item["window"])
            completed = item.get("completed", False)
            if not isinstance(completed, bool):
                raise ValueError("invalid receipt completion flag")
            items.append(
                ReceiptWork(
                    Window(
                        string(scope["data_type"]),
                        parse_time(string(scope["start"])),
                        parse_time(string(scope["end"])),
                    ),
                    _raw_from_dict(item["raw"]) if item.get("raw") is not None else None,
                    completed,
                    parse_time(string(item["fetched_at"]))
                    if item.get("fetched_at") is not None else None,
                    string(item["source_sha256"])
                    if item.get("source_sha256") is not None else None,
                )
            )
        return cls(
            string(data["key"]),
            string(data["subject_key"]),
            parse_time(string(data["received_at"])),
            tuple(items),
            parse_time(string(data["completed_at"]))
            if data.get("completed_at") is not None
            else None,
            string(data["origin"]),
        )


@dataclass(frozen=True, slots=True)
class StoredReceipt:
    receipt: Receipt
    generation: int


@dataclass(frozen=True, slots=True)
class ReceiptInventory:
    pending: tuple[StoredReceipt, ...]
    latest_received_at: datetime | None
    latest_completed_at: datetime | None
    deferred_count: int = 0


class ReceiptReadConflict(RuntimeError):
    """A receipt kept changing throughout the bounded generation-pinned reads."""


class ReceiptRepository(Protocol):
    def create(self, receipt: Receipt) -> StoredReceipt: ...
    def read(self, key: str) -> StoredReceipt: ...
    def replace(self, stored: StoredReceipt, receipt: Receipt) -> StoredReceipt: ...
    def inventory(self) -> ReceiptInventory: ...


class ReceiptBlob(Protocol):
    @property
    def name(self) -> str: ...
    @property
    def generation(self) -> int | None: ...

    metadata: dict[str, str] | None

    def upload_from_string(
        self, data: bytes, *, content_type: str, if_generation_match: int
    ) -> None: ...
    def download_as_bytes(self, *, raw_download: bool, if_generation_match: int) -> bytes: ...


class ReceiptBucket(Protocol):
    def blob(self, name: str) -> ReceiptBlob: ...
    def get_blob(self, name: str) -> ReceiptBlob | None: ...


class ReceiptClient(Protocol):
    def bucket(self, name: str) -> ReceiptBucket: ...
    def list_blobs(self, bucket: ReceiptBucket, *, prefix: str) -> Iterable[ReceiptBlob]: ...


class GCSReceiptRepository:
    def __init__(self, *, client: ReceiptClient, bucket: str) -> None:
        self._client = client
        self._bucket = client.bucket(bucket)

    def _write(self, receipt: Receipt, *, generation: int) -> StoredReceipt:
        blob = self._bucket.blob(receipt.key)
        metadata = {
            "state": "completed" if receipt.completed_at is not None else "pending",
            "received_at": receipt.received_at.isoformat(),
            "origin": receipt.origin,
        }
        if receipt.completed_at is not None:
            metadata["completed_at"] = receipt.completed_at.isoformat()
        blob.metadata = metadata
        blob.upload_from_string(
            receipt.to_bytes(), content_type="application/json", if_generation_match=generation
        )
        if blob.generation is None:
            raise RuntimeError("receipt upload omitted its generation")
        return StoredReceipt(receipt, int(blob.generation))

    def create(self, receipt: Receipt) -> StoredReceipt:
        try:
            return self._write(receipt, generation=0)
        except PreconditionFailed:
            existing = self.read(receipt.key)
            if existing.receipt.subject_key != receipt.subject_key:
                raise ValueError("receipt identity belongs to another subject") from None
            return existing

    def _read_blob(self, blob: ReceiptBlob) -> StoredReceipt:
        validate_receipt_key(blob.name)
        if blob.generation is None:
            raise RuntimeError("receipt metadata omitted its generation")
        generation = int(blob.generation)
        receipt = Receipt.from_bytes(
            blob.download_as_bytes(raw_download=True, if_generation_match=generation)
        )
        if receipt.key != blob.name:
            raise ValueError("receipt body does not match object key")
        return StoredReceipt(receipt, generation)

    def read(self, key: str) -> StoredReceipt:
        validate_receipt_key(key)
        blob = self._bucket.get_blob(key)
        if blob is None:
            raise FileNotFoundError("Fitbit receipt does not exist")
        _, stored = self._read_with_retry(blob)
        assert stored is not None
        return stored

    def _read_with_retry(
        self, blob: ReceiptBlob, *, skip_completed: bool = False
    ) -> tuple[ReceiptBlob, StoredReceipt | None]:
        key = blob.name
        validate_receipt_key(key)
        for attempt in range(3):
            if skip_completed and (blob.metadata or {}).get("state") == "completed":
                return blob, None
            try:
                return blob, self._read_blob(blob)
            except (NotFound, PreconditionFailed) as error:
                if attempt == 2:
                    raise ReceiptReadConflict("Fitbit receipt changed during three reads") from error
                refreshed = self._bucket.get_blob(key)
                if refreshed is None:
                    raise FileNotFoundError("Fitbit receipt does not exist") from error
                blob = refreshed
        raise AssertionError("receipt retry bound was not enforced")

    def replace(self, stored: StoredReceipt, receipt: Receipt) -> StoredReceipt:
        if stored.receipt.key != receipt.key or stored.receipt.subject_key != receipt.subject_key:
            raise ValueError("receipt identity cannot change")
        return self._write(receipt, generation=stored.generation)

    def inventory(self) -> ReceiptInventory:
        pending: list[StoredReceipt] = []
        latest_received: datetime | None = None
        latest_completed: datetime | None = None
        deferred = 0
        for blob in self._client.list_blobs(self._bucket, prefix=RECEIPT_PREFIX):
            validate_receipt_key(blob.name)
            try:
                blob, stored = self._read_with_retry(blob, skip_completed=True)
            except FileNotFoundError:
                continue
            except ReceiptReadConflict:
                deferred += 1
                LOGGER.warning("fitbit receipt read deferred key=%s", blob.name)
                continue
            metadata = blob.metadata or {}
            if metadata.get("origin") == "webhook" and "received_at" in metadata:
                received = parse_time(metadata["received_at"])
                latest_received = max(latest_received, received) if latest_received else received
            if metadata.get("state") == "completed":
                completed = parse_time(metadata["completed_at"])
                latest_completed = (
                    max(latest_completed, completed) if latest_completed else completed
                )
            elif stored is not None:
                pending.append(stored)
        return ReceiptInventory(
            tuple(
                sorted(pending, key=lambda entry: (entry.receipt.received_at, entry.receipt.key))
            ),
            latest_received,
            latest_completed,
            deferred,
        )
