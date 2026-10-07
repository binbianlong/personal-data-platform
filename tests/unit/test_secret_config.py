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


def test_pubsub_oauth_requires_json_and_rejects_legacy_conflicts():
    from personal_data_platform.sources.fitbit.oauth import GoogleOAuth

    values = {
        "PDP_FITBIT_OAUTH_CONFIG": '{"client_id":"id","client_secret":"secret","refresh_token":"refresh","health_user_id":"owner"}',
    }
    assert isinstance(GoogleOAuth.from_env(values), GoogleOAuth)
    values["PDP_FITBIT_OAUTH_CLIENT_SECRET"] = "legacy"
    with pytest.raises(ValueError, match="conflict"):
        GoogleOAuth.from_env(values)
