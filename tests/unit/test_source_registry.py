from types import SimpleNamespace

import pytest

from personal_data_platform.sources import registry


def test_registry_defaults_and_explicit_scope_keep_screen_time() -> None:
    for source in (
        registry.get_source(),
        registry.get_source("screen_time", "app-in-focus"),
    ):
        assert (source.source_id, source.stream) == ("screen_time", "app-in-focus")
    assert registry.source_ids() == ("screen_time",)
    assert registry.get_source("screen_time", "app-usage").stream == "app-usage"
    with pytest.raises(ValueError, match="multiple streams"):
        registry.get_source("screen_time")


def test_all_streams_requires_explicit_source_and_excludes_other_sources() -> None:
    sources = registry.get_sources("screen_time", all_streams=True)
    assert [source.stream for source in sources] == ["app-in-focus", "app-usage"]
    with pytest.raises(ValueError, match="requires source"):
        registry.get_sources(all_streams=True)
    with pytest.raises(ValueError, match="cannot be combined"):
        registry.get_sources("screen_time", "app-in-focus", all_streams=True)


@pytest.mark.parametrize(
    ("source_id", "stream", "message"),
    [
        (None, "app-in-focus", "requires source_id"),
        ("unregistered", None, "unsupported source"),
        ("screen_time", "unregistered", "unsupported stream"),
    ],
)
def test_registry_rejects_ambiguous_or_unknown_scope(source_id, stream, message) -> None:
    with pytest.raises(ValueError, match=message):
        registry.get_source(source_id, stream)


def test_registry_requires_stream_when_a_source_has_multiple_streams(monkeypatch) -> None:
    other = SimpleNamespace(source_id="screen_time", stream="synthetic-other")
    monkeypatch.setitem(
        registry._SOURCE_FACTORIES, ("screen_time", "synthetic-other"), lambda: other
    )
    with pytest.raises(ValueError, match="multiple streams"):
        registry.get_source("screen_time")
    assert registry.get_source("screen_time", "synthetic-other") is other
    assert registry.get_source().stream == "app-in-focus"
    assert registry.source_ids() == ("screen_time",)


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

    monkeypatch.setenv("PDP_RAW_PREFIXES_JSON", '["raw/screen_time/v1/", "raw/screen_time/v2/"]')
    monkeypatch.setenv("PDP_RAW_SUFFIXES_JSON", '[".segb.gz"]')
    validate_runtime_policy(registry.get_source())
