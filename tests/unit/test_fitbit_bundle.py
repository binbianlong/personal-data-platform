import gzip
import random
from datetime import UTC, datetime, timedelta

import pytest

from personal_data_platform.sources.fitbit.models import CapturedSnapshot, Snapshot, Window


def acquisition():
    when = datetime(2026, 10, 1, tzinfo=UTC)
    return CapturedSnapshot(
        Snapshot("self", Window("steps", when, when + timedelta(days=1)), when, ()),
        ({"dataPoints": [], "unknown": random.Random(1).randbytes(20000).hex()},),
    )


def test_bundle_preserves_unknown_page_fields_and_chunks(monkeypatch):
    from personal_data_platform.sources.fitbit import raw
    from personal_data_platform.sources.fitbit.models import BundleEntry, FitbitBundle

    monkeypatch.setattr(raw, "MAX_COMPRESSED_BYTES", 2048)
    original = FitbitBundle("bundle", (BundleEntry("attempt", acquisition()),))
    chunks = raw.encode_bundle(original)
    assert len(chunks) > 1
    assert max(map(len, (payload for _, payload in chunks))) <= 2048
    assert (
        raw.decode_bundle(tuple(gzip.decompress(payload) for _, payload in reversed(chunks)))
        == original
    )
    with pytest.raises(ValueError, match="chunk"):
        raw.decode_bundle(tuple(gzip.decompress(payload) for _, payload in chunks[:-1]))
    with pytest.raises(ValueError, match="chunk"):
        raw.decode_bundle(
            tuple(gzip.decompress(payload) for _, payload in chunks)
            + (gzip.decompress(chunks[0][1]),)
        )


def test_bundle_rejects_mixed_subjects_and_repeated_attempts():
    from personal_data_platform.sources.fitbit.models import BundleEntry, FitbitBundle

    entry = BundleEntry("attempt", acquisition())
    with pytest.raises(ValueError):
        FitbitBundle("bundle", (entry, entry))
