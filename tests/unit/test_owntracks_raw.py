from datetime import UTC, datetime

import pytest

from personal_data_platform.sources.registry import get_source


def test_location_canonicalization_preserves_extra_fields_and_retry_identity():
    source = get_source("owntracks", "location")
    from personal_data_platform.sources.owntracks.raw import prepare_location

    first = prepare_location(
        b'{"lon":139.0,"tst":1791504000,"_type":"location","lat":35.0,"tag":"trip"}',
        device_key="a" * 64,
    )
    retry = prepare_location(
        b'{"tag":"trip","lat":35.0,"_type":"location","tst":1791504000,"lon":139.0}',
        device_key="a" * 64,
    )
    assert first == retry
    key, payload, compressed = first
    assert b'"tag":"trip"' in payload
    import gzip

    assert gzip.decompress(compressed) == payload
    raw = source.parse_raw_key(
        key, storage_created_at=datetime(2026, 10, 9, tzinfo=UTC), storage_generation=12
    )
    assert raw.observed_at == datetime(2026, 10, 9, tzinfo=UTC)
    assert raw.subject_key == "a" * 64
    assert raw.storage_generation == 12
    assert source.decode(raw, payload).record_count == 1


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b"[]",
        b'{"_type":"location","lat":91,"lon":139,"tst":1}',
        b'{"_type":"location","lat":35,"lon":181,"tst":1}',
        b'{"_type":"location","lat":true,"lon":139,"tst":1}',
        b'{"_type":"location","lat":NaN,"lon":139,"tst":1}',
        b'{"_type":"location","lat":35,"lon":139,"tst":true}',
        b'{"_type":"location","lat":35,"lon":139,"tst":1,"tst":2}',
        b'{"_type":"location","lat":35,"lon":139,"tst":1,"acc":"bad"}',
        b'{"_type":"encrypted","data":"opaque"}',
    ],
)
def test_invalid_location_is_rejected_before_raw_storage(body):
    get_source("owntracks")
    from personal_data_platform.sources.owntracks.raw import prepare_location

    with pytest.raises(ValueError):
        prepare_location(body, device_key="a" * 64)


@pytest.mark.parametrize("body", [b"", b'{"_type":"transition","event":"enter"}'])
def test_non_location_has_no_raw(body):
    get_source("owntracks")
    from personal_data_platform.sources.owntracks.raw import prepare_location

    assert prepare_location(body, device_key="a" * 64) is None
