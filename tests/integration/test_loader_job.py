from __future__ import annotations

import gzip
import hashlib
from datetime import UTC, datetime, timedelta

from personal_data_platform.loader.job import run_loader
from personal_data_platform.loader.models import RawObject
from personal_data_platform.storage.motherduck import Warehouse, WarehouseConfig, connect


class _Repository:
    def __init__(self, observations: list[RawObject], objects: dict[str, bytes]) -> None:
        self.observations = observations
        self.objects = objects

    def list_raw(self, prefix: str) -> list[RawObject]:
        return list(reversed(self.observations))

    def get_raw(self, key: str) -> bytes:
        return self.objects[key]


def _raw(key: str, content: bytes, observed_at: datetime) -> RawObject:
    return RawObject(
        key=key,
        device_key="a" * 64,
        stream="app-in-focus",
        segment_key=hashlib.sha256(key.encode()).hexdigest(),
        observed_at=observed_at,
        sha256=hashlib.sha256(content).hexdigest(),
    )


def test_loader_continues_after_poison_object_and_retries_it(tmp_path, monkeypatch) -> None:
    now = datetime(2026, 8, 27, tzinfo=UTC)
    good_content = b"good"
    repaired_content = b"expected"
    good = _raw("raw/good.segb.gz", good_content, now)
    poison = _raw("raw/poison.segb.gz", repaired_content, now + timedelta(seconds=1))
    repository = _Repository(
        [good, poison],
        {
            good.key: gzip.compress(good_content),
            poison.key: gzip.compress(b"wrong"),
        },
    )
    monkeypatch.setattr(
        "personal_data_platform.loader.job.parse_segb_bytes", lambda raw, segment: []
    )
    warehouse = Warehouse(connect(WarehouseConfig(str(tmp_path / "loader.duckdb"))))
    warehouse.migrate()
    try:
        first = run_loader(repository, warehouse)

        assert first.succeeded == 1
        assert first.failed == 1
        assert not first.ok
        assert warehouse.ingestion_counts() == {"failed": 1, "succeeded": 1}

        repository.objects[poison.key] = gzip.compress(repaired_content)
        second = run_loader(repository, warehouse)

        assert second.skipped == 1
        assert second.succeeded == 1
        assert second.failed == 0
        assert second.ok
        assert warehouse.ingestion_counts() == {"succeeded": 2}
    finally:
        warehouse.close()
