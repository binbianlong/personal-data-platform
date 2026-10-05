import gzip
import json
import random
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from personal_data_platform.sources.fitbit import raw
from personal_data_platform.sources.fitbit.models import (
    CapturedSnapshot,
    FitbitBundle,
    Snapshot,
    Window,
)


def acquisition(day=0, size=1000):
    when = datetime(2026, 10, 1, tzinfo=UTC) + timedelta(days=day)
    return CapturedSnapshot(
        Snapshot("self", Window("steps", when, when + timedelta(days=1)), when, ()),
        ({"dataPoints": [], "unknown": random.Random(day).randbytes(size).hex()},),
    )


def test_bundle_splits_only_between_complete_entries(monkeypatch):
    monkeypatch.setattr(raw, "MAX_COMPRESSED_BYTES", 1800)
    entries = tuple(acquisition(i) for i in range(3))
    objects = raw.encode_bundle(FitbitBundle("bundle", entries))
    assert len(objects) == 3
    restored = []
    for key, stored in objects:
        assert key.startswith("raw/fitbit/v3/") and len(stored) <= 1800
        decoded = gzip.decompress(stored)
        assert isinstance(json.loads(decoded), list)
        restored.extend(raw.decode_bundle(decoded))
    assert tuple(restored) == entries


def test_single_oversized_entry_is_rejected(monkeypatch):
    monkeypatch.setattr(raw, "MAX_COMPRESSED_BYTES", 1800)
    with pytest.raises(ValueError, match="limit"):
        raw.encode_bundle(FitbitBundle("bundle", (acquisition(size=4000),)))


def test_bundle_rejects_mixed_subjects():
    one = acquisition()
    other = replace(one, snapshot=replace(one.snapshot, subject_key="other"))
    with pytest.raises(ValueError):
        FitbitBundle("bundle", (one, other))
