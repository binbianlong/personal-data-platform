"""Offline Google OAuth refresh and token-cache contracts."""

from urllib.parse import parse_qs

import pytest

from tests.fitbit_points import FakeTransport


def test_oauth_refresh_is_form_encoded_and_cached_only_until_safety_margin():
    from personal_data_platform.sources.fitbit.oauth import GoogleOAuth

    current = [100.0]
    transport = FakeTransport(
        [
            {"access_token": "synthetic-access-1", "expires_in": 3600, "token_type": "Bearer"},
            {"access_token": "synthetic-access-2", "expires_in": 3600, "token_type": "Bearer"},
        ]
    )
    oauth = GoogleOAuth(
        client_id="synthetic-client",
        client_secret="synthetic-secret&+",
        refresh_token="synthetic-refresh=+",
        transport=transport,
        clock=lambda: current[0],
    )
    assert oauth() == "synthetic-access-1"
    current[0] = 3600.0
    assert oauth() == "synthetic-access-1"
    assert len(transport.calls) == 1
    current[0] = 3641.0
    assert oauth() == "synthetic-access-2"
    method, url, headers, body, timeout = transport.calls[0]
    assert (method, url) == ("POST", "https://oauth2.googleapis.com/token")
    assert headers["Content-Type"] == "application/x-www-form-urlencoded"
    assert parse_qs(body.decode()) == {
        "client_id": ["synthetic-client"],
        "client_secret": ["synthetic-secret&+"],
        "refresh_token": ["synthetic-refresh=+"],
        "grant_type": ["refresh_token"],
    }
    assert timeout == 30.0
    assert "synthetic-secret" not in repr(oauth)
    assert "synthetic-refresh" not in repr(oauth)


def test_oauth_reads_only_explicitly_supplied_environment_credentials():
    import json

    from personal_data_platform.sources.fitbit.oauth import GoogleOAuth

    transport = FakeTransport(
        [{"access_token": "synthetic-access", "expires_in": 3600, "token_type": "Bearer"}]
    )
    config = {
        "client_id": "synthetic-client",
        "client_secret": "synthetic-secret",
        "refresh_token": "synthetic-refresh",
        "health_user_id": "owner",
    }
    oauth = GoogleOAuth.from_env(
        {"PDP_FITBIT_OAUTH_CONFIG": json.dumps(config)}, transport=transport
    )
    assert oauth() == "synthetic-access"
    del config["refresh_token"]
    with pytest.raises(ValueError, match="refresh_token"):
        GoogleOAuth.from_env({"PDP_FITBIT_OAUTH_CONFIG": json.dumps(config)})


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"access_token": "", "expires_in": 3600, "token_type": "Bearer"},
        {"access_token": "synthetic-token", "expires_in": -1, "token_type": "Bearer"},
        {"access_token": "synthetic-token", "expires_in": True, "token_type": "Bearer"},
        {"access_token": "synthetic-token", "expires_in": 3600, "token_type": "other"},
    ],
)
def test_malformed_refresh_does_not_return_or_cache_a_token(payload):
    from personal_data_platform.sources.fitbit.api import InvalidResponseError
    from personal_data_platform.sources.fitbit.oauth import GoogleOAuth

    transport = FakeTransport(
        [payload, {"access_token": "valid-token", "expires_in": 3600, "token_type": "Bearer"}]
    )
    oauth = GoogleOAuth(
        client_id="client", client_secret="secret", refresh_token="refresh", transport=transport
    )
    with pytest.raises(InvalidResponseError) as caught:
        oauth()
    assert "synthetic-token" not in str(caught.value)
    assert oauth() == "valid-token"


def test_revoked_refresh_token_is_classified_without_exposing_provider_error_body():
    from personal_data_platform.sources.fitbit.api import AuthenticationError
    from personal_data_platform.sources.fitbit.oauth import GoogleOAuth

    oauth = GoogleOAuth(
        client_id="client",
        client_secret="secret",
        refresh_token="refresh",
        transport=FakeTransport(
            [(400, {"error": "invalid_grant", "error_description": "synthetic-secret"}, {})]
        ),
    )
    with pytest.raises(AuthenticationError) as caught:
        oauth()
    assert "synthetic-secret" not in str(caught.value)
