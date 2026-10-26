import pytest


def test_secret_json_is_validated_without_exposing_values():
    from personal_data_platform.config import secret_config

    assert secret_config("SECRET", ("key",), {"SECRET": '{"key":"value"}'}) == {"key": "value"}
    for raw in (
        '{"key":"sensitive", "key":"other"}',
        '{"key":3}',
        '{"other":"sensitive"}',
        "not-json",
    ):
        with pytest.raises(ValueError) as error:
            secret_config("SECRET", ("key",), {"SECRET": raw})
        assert raw not in str(error.value)
        assert "sensitive" not in str(error.value)
