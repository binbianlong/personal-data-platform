import json
from datetime import UTC, datetime

import pytest

from personal_data_platform.sources.registry import get_source


def test_location_raw_keeps_only_selected_fields_and_retry_identity():
    source = get_source("owntracks", "location")
    from personal_data_platform.sources.owntracks.raw import prepare_location

    first = prepare_location(
        b'{"lon":139.0,"tst":1791504000,"_type":"location","lat":35.0,'
        b'"acc":7,"vel":12,"batt":79,"bs":1,"motionactivities":["walking"],'
        b'"alt":30,"vac":4,"m":1,"t":"u","ssid":"home","inregions":["home"],'
        b'"created_at":1791504001,"tid":"AB","topic":"owntracks/user/phone","p":100}',
        device_key="a" * 64,
    )
    retry = prepare_location(
        b'{"lat":35.0,"_type":"location","tst":1791504000,"lon":139.0,'
        b'"acc":7,"vel":12,"batt":80,"bs":2,"motionactivities":["walking"],'
        b'"alt":99,"vac":8,"m":2,"t":"p","ssid":"other","inregions":[],"tag":"trip"}',
        device_key="a" * 64,
    )
    assert first == retry
    key, payload, compressed = first
    assert payload == (
        b'{"acc":7,"lat":35.0,"lon":139.0,"motionactivities":["walking"],"tst":1791504000,"vel":12}'
    )
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


@pytest.mark.parametrize("value", ["walking", {}, [1], [True], [None], [["walking"]]])
@pytest.mark.parametrize("from_raw", [False, True])
def test_motionactivities_rejects_values_other_than_string_arrays(value, from_raw):
    from personal_data_platform.sources.owntracks.raw import location_message

    payload = json.dumps(
        {"_type": "location", "lat": 35, "lon": 139, "tst": 1, "motionactivities": value}
    ).encode()
    with pytest.raises(ValueError, match="motionactivities"):
        location_message(payload, from_raw=from_raw)
