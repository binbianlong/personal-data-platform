import pytest

from personal_data_platform.sources import registry


def test_registry_exposes_independent_sources_and_preserves_iphone_default() -> None:
    phone = registry.get_source("iphone_screen_time")
    mac = registry.get_source("mac_screen_time")
    assert (phone.source_id, phone.stream) == ("iphone_screen_time", "app-in-focus")
    assert (mac.source_id, mac.stream) == ("mac_screen_time", "app-usage")
    assert registry.get_source().source_id == phone.source_id
    assert registry.source_ids() == ("iphone_screen_time", "mac_screen_time", "owntracks")
    assert set(phone.required_relations).isdisjoint(mac.required_relations)
    assert phone.raw_prefixes == ("raw/iphone_screen_time/v2/",)
    assert mac.raw_prefixes == ("raw/mac_screen_time/v2/",)
    assert phone.dbt_selector == "tag:iphone_screen_time"
    assert mac.dbt_selector == "tag:mac_screen_time"
    assert registry.get_dbt_selector("fitbit", None) == "tag:fitbit"
    with pytest.raises(ValueError, match="unsupported source"):
        registry.get_source("screen_time")


def test_all_streams_is_restricted_to_selected_source() -> None:
    sources = registry.get_sources("iphone_screen_time", all_streams=True)
    assert [(s.source_id, s.stream) for s in sources] == [("iphone_screen_time", "app-in-focus")]
    with pytest.raises(ValueError, match="requires source"):
        registry.get_sources(all_streams=True)
    with pytest.raises(ValueError, match="cannot be combined"):
        registry.get_sources("iphone_screen_time", "app-in-focus", all_streams=True)


@pytest.mark.parametrize(
    ("source_id", "stream", "message"),
    [
        (None, "app-in-focus", "requires source_id"),
        ("unregistered", None, "unsupported source"),
        ("iphone_screen_time", "app-usage", "unsupported stream"),
        ("mac_screen_time", "app-in-focus", "unsupported stream"),
    ],
)
def test_registry_rejects_mixed_or_unknown_scope(source_id, stream, message) -> None:
    with pytest.raises(ValueError, match=message):
        registry.get_source(source_id, stream)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("PDP_RAW_PREFIXES_JSON", '["raw/synthetic/v1/"]'),
        ("PDP_RAW_SUFFIXES_JSON", '[".json.gz"]'),
        ("PDP_RAW_PREFIXES_JSON", "invalid"),
        ("PDP_RAW_PREFIXES_JSON", "[null]"),
    ],
)
def test_storage_policy_drift_is_rejected(name, value, monkeypatch):
    from personal_data_platform.sources.contracts import validate_runtime_policy

    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match=name):
        validate_runtime_policy(registry.get_source())


def test_storage_policy_accepts_the_registered_namespace(monkeypatch):
    from personal_data_platform.sources.contracts import validate_runtime_policy

    monkeypatch.setenv("PDP_RAW_PREFIXES_JSON", '["raw/iphone_screen_time/v2/"]')
    monkeypatch.setenv("PDP_RAW_SUFFIXES_JSON", '[".segb.gz"]')
    validate_runtime_policy(registry.get_source())
