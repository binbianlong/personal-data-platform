"""Offline Google Health v4 contract tests using synthetic points."""

import json
from datetime import UTC, date, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest

from personal_data_platform.sources.fitbit.models import Window

START = datetime(2026, 9, 1, tzinfo=UTC)


def interval(start="2026-09-01T00:00:00Z", end="2026-09-01T00:01:00Z"):
    return {
        "startTime": start,
        "endTime": end,
        "startUtcOffset": "32400s",
        "endUtcOffset": "32400s",
    }


def steps(count="12", **times):
    return {"steps": {"interval": interval(**times), "count": count}}


def heart_rate(when="2026-09-01T00:00:00Z", value="60"):
    return {
        "heartRate": {
            "sampleTime": {"physicalTime": when, "utcOffset": "32400s"},
            "beatsPerMinute": value,
            "metadata": {"motionContext": "SEDENTARY", "sensorLocation": "WRIST"},
        }
    }


class FakeTransport:
    def __init__(self, responses, *, before_request=None):
        self.responses = iter(responses)
        self.calls = []
        self.before_request = before_request

    def request(self, method, url, *, headers, body=None, timeout):
        from personal_data_platform.sources.fitbit.api import HttpResponse

        if self.before_request is not None:
            self.before_request()
        self.calls.append((method, url, headers, body, timeout))
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        status, payload, response_headers = (
            response if isinstance(response, tuple) else (200, response, {})
        )
        payload_bytes = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        return HttpResponse(status, payload_bytes, response_headers)


def client(transport, **kwargs):
    from personal_data_platform.sources.fitbit.api import HealthClient

    return HealthClient(access_token=lambda: "synthetic-token", transport=transport, **kwargs)


def test_paired_device_pages_use_remaining_job_budget():
    remaining = [0.25]
    transport = FakeTransport(
        [{"nextPageToken": "next"}, {}],
        before_request=lambda: remaining.__setitem__(0, 0),
    )
    health = client(transport)

    def guard():
        if remaining[0] <= 0:
            raise TimeoutError("job deadline")
        return remaining[0]

    health.request_guard = guard
    with pytest.raises(TimeoutError, match="deadline"):
        health.latest_tracker_sync()
    assert len(transport.calls) == 1
    assert transport.calls[0][-1] == 0.25


def test_latest_tracker_sync_reads_every_page_and_preserves_nanosecond_order():
    transport = FakeTransport(
        [
            {
                "pairedDevices": [
                    {
                        "deviceType": "TRACKER",
                        "deviceVersion": "Sense 2",
                        "lastSyncTime": "2026-09-27T10:00:00.123456001Z",
                    },
                    {"deviceType": "SCALE", "lastSyncTime": "2026-09-27T12:00:00Z"},
                ],
                "nextPageToken": "second",
            },
            {
                "pairedDevices": [
                    {
                        "deviceType": "TRACKER",
                        "deviceVersion": "Inspire 3",
                        "lastSyncTime": "2026-09-27T19:00:00.123456002+09:00",
                    },
                ]
            },
        ]
    )
    observed = client(transport).latest_tracker_sync()
    assert observed is not None
    assert observed.text == "2026-09-27T10:00:00.123456002Z"
    assert [parse_qs(urlsplit(call[1]).query) for call in transport.calls] == [
        {"pageSize": ["100"]},
        {"pageSize": ["100"], "pageToken": ["second"]},
    ]
    assert all(urlsplit(call[1]).path == "/v4/users/me/pairedDevices" for call in transport.calls)


def test_latest_tracker_sync_without_tracker_returns_none():
    transport = FakeTransport(
        [{"pairedDevices": [{"deviceType": "SCALE", "lastSyncTime": "2026-09-27T12:00:00Z"}]}]
    )
    assert client(transport).latest_tracker_sync() is None


def test_latest_tracker_sync_rejects_repeated_pages_or_malformed_tracker_time():
    from personal_data_platform.sources.fitbit.api import InvalidResponseError

    transport = FakeTransport(
        [
            {"pairedDevices": [], "nextPageToken": "same"},
            {"pairedDevices": [], "nextPageToken": "same"},
        ]
    )
    with pytest.raises(InvalidResponseError, match="page token"):
        client(transport).latest_tracker_sync()
    transport = FakeTransport(
        [{"pairedDevices": [{"deviceType": "TRACKER", "lastSyncTime": "invalid"}]}]
    )
    with pytest.raises(InvalidResponseError, match="tracker sync"):
        client(transport).latest_tracker_sync()


def test_unbounded_unique_pagination_stops_without_returning_partial_data():
    from personal_data_platform.sources.fitbit.api import InvalidResponseError

    transport = FakeTransport([{"nextPageToken": "page2"}, {"nextPageToken": "page3"}])
    with pytest.raises(InvalidResponseError, match="limit"):
        client(transport, max_pages=2).fetch(
            Window("steps", START, START + timedelta(days=1)), subject_key="self"
        )
    assert len(transport.calls) == 2


def fetch_points(data_type, points, *, subject_key="owner"):
    return client(FakeTransport([{"dataPoints": points}])).fetch(
        Window(data_type, START, START + timedelta(days=1)), subject_key=subject_key
    )


def test_fetch_completes_all_pages_and_keeps_fetch_start_and_original_points():
    events = []

    def clock():
        events.append("clock")
        return START + timedelta(days=2)

    first = steps()
    second = steps("4", start="2026-09-01T00:01:00Z", end="2026-09-01T00:02:00Z")
    transport = FakeTransport(
        [{"dataPoints": [first], "nextPageToken": "second"}, {"dataPoints": [second]}],
        before_request=lambda: events.append("request"),
    )
    window = Window("steps", START, START + timedelta(days=1))
    result = client(transport, clock=clock).fetch(window, subject_key="owner")

    assert events == ["clock", "request", "request"]
    assert result.fetched_at == datetime(2026, 9, 3, tzinfo=UTC)
    assert result.window == window
    assert result.complete is True
    assert [record.value for record in result.records] == [12, 4]
    assert result.source_payload == (first, second)
    first_query = parse_qs(urlsplit(transport.calls[0][1]).query)
    second_query = parse_qs(urlsplit(transport.calls[1][1]).query)
    assert first_query == {
        "filter": [
            'steps.interval.start_time >= "2026-09-01T00:00:00Z" AND '
            'steps.interval.start_time < "2026-09-02T00:00:00Z"'
        ],
        "pageSize": ["10000"],
        "dataSourceFamily": ["users/me/dataSourceFamilies/google-wearables"],
    }
    assert second_query == {**first_query, "pageToken": ["second"]}
    assert transport.calls[0][2]["Authorization"] == "Bearer synthetic-token"
    assert transport.calls[0][4] == 30.0


@pytest.mark.parametrize(
    ("data_type", "field", "start", "end", "page_size"),
    [
        (
            "heart-rate",
            "heart_rate.sample_time.physical_time",
            "2026-09-01T00:00:00Z",
            "2026-09-02T00:00:00Z",
            "10000",
        ),
        (
            "active-zone-minutes",
            "active_zone_minutes.interval.start_time",
            "2026-09-01T00:00:00Z",
            "2026-09-02T00:00:00Z",
            "10000",
        ),
        (
            "daily-resting-heart-rate",
            "daily_resting_heart_rate.date",
            "2026-09-01",
            "2026-09-02",
            "10000",
        ),
        ("sleep", "sleep.interval.civil_end_time", "2026-09-01", "2026-09-02", "25"),
    ],
)
def test_query_uses_the_data_types_own_filter_cursor(data_type, field, start, end, page_size):
    transport = FakeTransport([{}])
    snapshot = client(transport).fetch(
        Window(data_type, START, START + timedelta(days=1)), subject_key="owner"
    )
    query = parse_qs(urlsplit(transport.calls[0][1]).query)
    assert query["filter"] == [f'{field} >= "{start}" AND {field} < "{end}"']
    assert query["pageSize"] == [page_size]
    assert urlsplit(transport.calls[0][1]).path == (
        f"/v4/users/me/dataTypes/{data_type}/dataPoints:reconcile"
    )
    assert snapshot.records == ()


@pytest.mark.parametrize(
    ("data_type", "days", "boundary"),
    [("heart-rate", 15, "2026-09-15T00:00:00Z"), ("sleep", 91, "2026-11-30")],
)
def test_large_ranges_are_split_without_reusing_previous_page_tokens(data_type, days, boundary):
    transport = FakeTransport([{"nextPageToken": "p2"}, {}, {}])
    snapshot = client(transport).fetch(
        Window(data_type, START, START + timedelta(days=days)), subject_key="owner"
    )
    queries = [parse_qs(urlsplit(call[1]).query) for call in transport.calls]
    assert len(queries) == 3
    assert "pageToken" not in queries[0]
    assert queries[1]["pageToken"] == ["p2"]
    assert "pageToken" not in queries[2]
    assert f'< "{boundary}"' in queries[0]["filter"][0]
    assert f'>= "{boundary}"' in queries[2]["filter"][0]
    assert snapshot.records == ()


@pytest.mark.parametrize(
    "payload",
    [
        [],
        None,
        {"dataPoints": None},
        {"dataPoints": {}},
        {"dataPoints": [42]},
        {"nextPageToken": 123},
        {"error": {"message": "synthetic-sensitive-text"}},
        b"not-json",
    ],
)
def test_malformed_page_cannot_become_an_empty_complete_snapshot(payload):
    from personal_data_platform.sources.fitbit.api import InvalidResponseError

    with pytest.raises(InvalidResponseError) as caught:
        client(FakeTransport([payload])).fetch(
            Window("steps", START, START + timedelta(days=1)), subject_key="owner"
        )
    assert "synthetic-sensitive-text" not in str(caught.value)


def test_repeated_page_token_is_rejected_before_completing():
    from personal_data_platform.sources.fitbit.api import InvalidResponseError

    transport = FakeTransport(
        [
            {"dataPoints": [steps()], "nextPageToken": "loop"},
            {"dataPoints": [], "nextPageToken": "loop"},
        ]
    )
    with pytest.raises(InvalidResponseError, match="page token"):
        client(transport).fetch(
            Window("steps", START, START + timedelta(days=1)), subject_key="owner"
        )


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, "AuthenticationError"),
        (403, "AuthenticationError"),
        (429, "RateLimitError"),
        (500, "TransientError"),
        (503, "TransientError"),
        (400, "HealthError"),
    ],
)
def test_later_page_http_failure_is_classified_without_health_or_token_text(status, expected):
    from personal_data_platform.sources.fitbit import api

    transport = FakeTransport(
        [
            {"dataPoints": [steps()], "nextPageToken": "next"},
            (status, {"error": "synthetic-token synthetic-health"}, {"Retry-After": "120"}),
        ]
    )
    with pytest.raises(getattr(api, expected)) as caught:
        client(transport).fetch(
            Window("steps", START, START + timedelta(days=1)), subject_key="owner"
        )
    assert "synthetic-token" not in str(caught.value)
    assert "synthetic-health" not in str(caught.value)
    if status == 429:
        assert caught.value.retry_after == 120


def test_network_failure_is_transient_and_has_a_sanitized_message():
    from personal_data_platform.sources.fitbit.api import TransientError

    with pytest.raises(TransientError) as caught:
        client(FakeTransport([TimeoutError("synthetic-token")])).fetch(
            Window("steps", START, START + timedelta(days=1)), subject_key="owner"
        )
    assert "synthetic-token" not in str(caught.value)


def test_each_response_is_checked_against_its_chunk_not_only_total_window():
    from personal_data_platform.sources.fitbit.api import InvalidResponseError

    transport = FakeTransport([{"dataPoints": [heart_rate("2026-09-15T00:00:00Z")]}])
    with pytest.raises(InvalidResponseError, match="range"):
        client(transport).fetch(
            Window("heart-rate", START, START + timedelta(days=15)), subject_key="owner"
        )


def test_steps_true_zero_is_a_record_while_off_wrist_remains_missing():
    point = {"steps": {"interval": interval()}}
    result = fetch_points("steps", [point])
    assert len(result.records) == 1
    assert result.records[0].value == 0
    assert result.source_payload == (point,)
    assert fetch_points("steps", []).records == ()


def test_steps_keep_physical_start_cursor_and_both_offsets_and_provider_date():
    point = steps("15", start="2026-09-01T15:59:00Z", end="2026-09-01T16:01:00Z")
    point["steps"]["interval"].update(
        {
            "startUtcOffset": "28800s",
            "endUtcOffset": "32400s",
            "civilStartTime": {
                "date": {"year": 2026, "month": 9, "day": 1},
                "time": {"hours": 23, "minutes": 59},
            },
        }
    )
    (record,) = fetch_points("steps", [point]).records
    assert record.start == datetime(2026, 9, 1, 15, 59, tzinfo=UTC)
    assert record.cursor == record.start
    assert record.end == datetime(2026, 9, 1, 16, 1, tzinfo=UTC)
    assert (record.offset_seconds, record.end_offset_seconds) == (28800, 32400)
    assert record.source_date == date(2026, 9, 1)


def test_heart_rate_key_uses_instant_and_subject_and_does_not_change_with_value():
    (original,) = fetch_points("heart-rate", [heart_rate()]).records
    (changed,) = fetch_points("heart-rate", [heart_rate(value="65")]).records
    (other,) = fetch_points("heart-rate", [heart_rate()], subject_key="other").records
    assert original.cursor == original.start == START
    assert original.end is None
    assert original.record_id == changed.record_id
    assert original.record_id != other.record_id
    assert original.value == 60


def test_resting_heart_rate_uses_civil_date_not_a_timezone_shifted_instant():
    point = {
        "dailyRestingHeartRate": {
            "date": {"year": 2026, "month": 9, "day": 1},
            "beatsPerMinute": "54",
        }
    }
    (record,) = fetch_points("daily-resting-heart-rate", [point]).records
    assert record.cursor == record.start == START
    assert record.source_date == date(2026, 9, 1)
    assert record.offset_seconds is None
    assert record.value == 54


def test_active_zone_minutes_are_already_weighted():
    point = {
        "activeZoneMinutes": {
            "interval": interval(),
            "heartRateZone": "CARDIO",
            "activeZoneMinutes": "2",
        }
    }
    (record,) = fetch_points("active-zone-minutes", [point]).records
    assert record.value == 2
    assert record.category == "cardio"


@pytest.mark.parametrize("bad_value", [None, True, "NaN", "Infinity", "-1", "1.5", "1000001"])
def test_invalid_steps_values_fail_instead_of_selecting_or_coercing_data(bad_value):
    from personal_data_platform.sources.fitbit.api import InvalidResponseError

    with pytest.raises(InvalidResponseError):
        fetch_points("steps", [steps(bad_value)])


def test_conflicting_same_instant_points_are_rejected_instead_of_picking_one():
    from personal_data_platform.sources.fitbit.api import InvalidResponseError

    with pytest.raises(InvalidResponseError, match="duplicate"):
        fetch_points("heart-rate", [heart_rate(value="60"), heart_rate(value="65")])


def sleep_point():
    return {
        "dataPointName": "users/synthetic-user/dataTypes/sleep/dataPoints/sleep-001",
        "sleep": {
            "interval": interval("2026-08-31T14:00:00Z", "2026-08-31T22:00:00Z"),
            "type": "STAGES",
            "stages": [
                {**interval("2026-08-31T14:00:00Z", "2026-08-31T18:00:00Z"), "type": "LIGHT"},
                {**interval("2026-08-31T18:00:00Z", "2026-08-31T22:00:00Z"), "type": "DEEP"},
            ],
            "shortAwakenings": [
                {
                    "startTime": "2026-08-31T16:00:00Z",
                    "endTime": "2026-08-31T16:00:30Z",
                    "type": "AWAKE",
                }
            ],
            "outOfBedSegments": [interval("2026-08-31T18:00:00Z", "2026-08-31T18:01:00Z")],
            "summary": {"minutesAsleep": "470", "minutesAwake": "10"},
            "metadata": {"mainSleep": True, "processed": True, "nap": False},
        },
    }


def test_sleep_accepts_consistent_names_used_in_the_official_filter_example():
    point = sleep_point()
    point["name"] = point.pop("dataPointName")
    metadata = point["sleep"]["metadata"]
    metadata["main"] = metadata.pop("mainSleep")
    result = fetch_points("sleep", [point])
    assert result.records[0].record_id == "sleep-001"
    assert result.records[0].is_main_sleep is True


@pytest.mark.parametrize("conflict", ["identity", "main"])
def test_sleep_rejects_conflicting_alias_values(conflict):
    from personal_data_platform.sources.fitbit.api import InvalidResponseError

    point = sleep_point()
    if conflict == "identity":
        point["name"] = "users/synthetic-user/dataTypes/sleep/dataPoints/other"
    else:
        point["sleep"]["metadata"]["main"] = False
    with pytest.raises(InvalidResponseError):
        fetch_points("sleep", [point])


def test_sleep_preserves_end_civil_date_and_keeps_overlapping_wakes_separate():
    result = fetch_points("sleep", [sleep_point()])
    session = result.records[0]
    assert session.record_id == "sleep-001"
    assert session.cursor == START
    assert session.source_date == date(2026, 9, 1)
    assert session.start == datetime(2026, 8, 31, 14, tzinfo=UTC)
    assert session.value == 470
    assert session.is_main_sleep is True
    stages = [record for record in result.records if record.kind == "sleep-stage"]
    wakes = [record for record in result.records if record.kind == "sleep-wake"]
    assert [(row.category, row.value) for row in stages] == [("light", 14400), ("deep", 14400)]
    assert [(row.category, row.value) for row in wakes] == [
        ("short-awakening", 30),
        ("out-of-bed", 60),
    ]
    assert all(
        row.cursor == session.cursor and row.parent_id == "sleep-001" for row in (*stages, *wakes)
    )


def test_missing_sleep_summary_and_main_classification_stay_unknown():
    point = sleep_point()
    del point["sleep"]["summary"]
    point["sleep"]["metadata"] = {"nap": False}
    session = fetch_points("sleep", [point]).records[0]
    assert session.value is None
    assert session.is_main_sleep is None


def test_sleep_civil_end_with_changed_offset_controls_replacement_day():
    point = sleep_point()
    point["sleep"]["interval"].update(
        {
            "startUtcOffset": "28800s",
            "endUtcOffset": "32400s",
            "civilEndTime": {"date": {"year": 2026, "month": 9, "day": 1}, "time": {"hours": 7}},
        }
    )
    session = fetch_points("sleep", [point]).records[0]
    assert (session.offset_seconds, session.end_offset_seconds) == (28800, 32400)
    assert session.cursor == START


@pytest.mark.parametrize(
    "mutation",
    [
        "missing-id",
        "wrong-type",
        "invalid-offset",
        "wrong-civil-date",
        "overlapping-stages",
        "child-outside",
    ],
)
def test_invalid_sleep_records_fail_before_becoming_complete(mutation):
    from personal_data_platform.sources.fitbit.api import InvalidResponseError

    point = sleep_point()
    if mutation == "missing-id":
        del point["dataPointName"]
    elif mutation == "wrong-type":
        point["dataPointName"] = "users/u/dataTypes/steps/dataPoints/invalid"
    elif mutation == "invalid-offset":
        point["sleep"]["interval"]["endUtcOffset"] = "invalid"
    elif mutation == "wrong-civil-date":
        point["sleep"]["interval"]["civilEndTime"] = {"date": {"year": 2026, "month": 9, "day": 2}}
    elif mutation == "overlapping-stages":
        point["sleep"]["stages"][1]["startTime"] = "2026-08-31T17:00:00Z"
    else:
        point["sleep"]["shortAwakenings"][0]["startTime"] = "2026-08-31T13:00:00Z"
    with pytest.raises(InvalidResponseError):
        fetch_points("sleep", [point])


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
    from personal_data_platform.sources.fitbit.oauth import GoogleOAuth

    transport = FakeTransport(
        [{"access_token": "synthetic-access", "expires_in": 3600, "token_type": "Bearer"}]
    )
    oauth = GoogleOAuth.from_env(
        {
            "PDP_FITBIT_OAUTH_CLIENT_ID": "synthetic-client",
            "PDP_FITBIT_OAUTH_CLIENT_SECRET": "synthetic-secret",
            "PDP_FITBIT_OAUTH_REFRESH_TOKEN": "synthetic-refresh",
        },
        transport=transport,
    )
    assert oauth() == "synthetic-access"
    with pytest.raises(ValueError, match="PDP_FITBIT_OAUTH_REFRESH_TOKEN"):
        GoogleOAuth.from_env(
            {
                "PDP_FITBIT_OAUTH_CLIENT_ID": "synthetic-client",
                "PDP_FITBIT_OAUTH_CLIENT_SECRET": "synthetic-secret",
            }
        )


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


def rollup(start=START, *, average=60, minimum=55, maximum=65):
    return {
        "startTime": start.isoformat(),
        "endTime": (start + timedelta(minutes=1)).isoformat(),
        "heartRate": {
            "beatsPerMinuteAvg": average,
            "beatsPerMinuteMin": minimum,
            "beatsPerMinuteMax": maximum,
        },
    }


def test_rollup_uses_complete_utc_minutes_and_all_pages():
    first = {"rollupDataPoints": [rollup()], "nextPageToken": "next", "future": {"x": 1}}
    second = {"rollupDataPoints": [rollup(START + timedelta(minutes=1))]}
    transport = FakeTransport([first, second])
    result = client(
        transport, clock=lambda: START + timedelta(minutes=2, seconds=30)
    ).fetch_heart_rate_minutes(
        Window("heart-rate", START, START + timedelta(days=1)), subject_key="owner"
    )
    assert result.window.end == START + timedelta(minutes=2)
    assert len(result.minutes) == 2
    assert result.minutes[0].sample_count is None
    assert result.pages == (first, second)
    request = json.loads(transport.calls[0][3])
    assert transport.calls[0][0] == "POST"
    assert transport.calls[0][1].endswith("/dataPoints:rollUp")
    assert request["windowSize"] == "60s"
    assert request["dataSourceFamily"].endswith("/google-wearables")
    assert request["range"] == {
        "startTime": "2026-09-01T00:00:00Z",
        "endTime": "2026-09-01T00:02:00Z",
    }
    assert json.loads(transport.calls[1][3])["pageToken"] == "next"


def test_rollup_missing_is_not_zero():
    missing = {
        "startTime": START.isoformat(),
        "endTime": (START + timedelta(minutes=1)).isoformat(),
    }
    observed = rollup(START + timedelta(minutes=1), average=0, minimum=0, maximum=0)
    result = client(
        FakeTransport([{"rollupDataPoints": [missing, observed]}]),
        clock=lambda: START + timedelta(days=1),
    ).fetch_heart_rate_minutes(
        Window("heart-rate", START, START + timedelta(minutes=2)), subject_key="owner"
    )
    assert len(result.minutes) == 1
    assert result.minutes[0].start == START + timedelta(minutes=1)
    assert result.minutes[0].average == 0


def test_rollup_splits_requests_at_fourteen_days_and_ceil_start():
    transport = FakeTransport([{}, {}])
    result = client(transport, clock=lambda: START + timedelta(days=20)).fetch_heart_rate_minutes(
        Window("heart-rate", START + timedelta(seconds=30), START + timedelta(days=15, seconds=30)),
        subject_key="owner",
    )
    assert result.window.start == START + timedelta(minutes=1)
    assert result.window.end == START + timedelta(days=15)
    ranges = [json.loads(call[3])["range"] for call in transport.calls]
    assert datetime.fromisoformat(ranges[0]["endTime"]) - datetime.fromisoformat(
        ranges[0]["startTime"]
    ) == timedelta(days=14)
    assert ranges[0]["endTime"] == ranges[1]["startTime"]


@pytest.mark.parametrize(
    "mutation",
    [
        "duplicate",
        "outside",
        "unaligned",
        "short",
        "nan",
        "inconsistent",
        "missing-average",
        "loop",
        "wrong-union",
    ],
)
def test_rollup_invalid_pages_never_produce_a_complete_snapshot(mutation):
    from personal_data_platform.sources.fitbit.api import InvalidResponseError

    point = rollup()
    page = {"rollupDataPoints": [point]}
    pages = [page]
    if mutation == "duplicate":
        page["rollupDataPoints"].append(rollup())
    elif mutation == "outside":
        page["rollupDataPoints"] = [rollup(START + timedelta(days=2))]
    elif mutation == "unaligned":
        page["rollupDataPoints"] = [rollup(START + timedelta(seconds=1))]
    elif mutation == "short":
        point["endTime"] = (START + timedelta(seconds=30)).isoformat()
    elif mutation == "nan":
        point["heartRate"]["beatsPerMinuteAvg"] = float("nan")
    elif mutation == "inconsistent":
        point["heartRate"]["beatsPerMinuteMin"] = 70
    elif mutation == "missing-average":
        del point["heartRate"]["beatsPerMinuteAvg"]
    elif mutation == "wrong-union":
        point["steps"] = {"countSum": "0"}
    else:
        page["nextPageToken"] = "loop"
        pages.append({"nextPageToken": "loop"})
    with pytest.raises(InvalidResponseError):
        client(
            FakeTransport(pages), clock=lambda: START + timedelta(days=3)
        ).fetch_heart_rate_minutes(
            Window("heart-rate", START, START + timedelta(days=1)), subject_key="owner"
        )


def test_rollup_snapshot_roundtrip_and_digest_ignore_order_pagination_and_observation_time():
    from personal_data_platform.sources.fitbit.models import HeartRateMinuteSnapshot

    points = [rollup(), rollup(START + timedelta(minutes=1))]
    window = Window("heart-rate", START, START + timedelta(days=1))
    first = client(
        FakeTransport([{"rollupDataPoints": points, "future": 1}]),
        clock=lambda: START + timedelta(days=2),
    ).fetch_heart_rate_minutes(window, subject_key="owner")
    second = client(
        FakeTransport(
            [
                {"rollupDataPoints": [points[1]], "nextPageToken": "next", "future": 1},
                {"rollupDataPoints": [points[0]], "future": 1},
            ]
        ),
        clock=lambda: START + timedelta(days=3),
    ).fetch_heart_rate_minutes(window, subject_key="owner")
    assert HeartRateMinuteSnapshot.from_bytes(first.to_bytes()) == first
    assert first.source_sha256() == second.source_sha256()
    changed = client(
        FakeTransport([{"rollupDataPoints": points, "future": 2}]),
        clock=lambda: START + timedelta(days=2),
    ).fetch_heart_rate_minutes(window, subject_key="owner")
    assert first.source_sha256() != changed.source_sha256()


def test_captured_scalar_pages_preserve_unknown_fields_and_digest_pagination_independence():
    window = Window("steps", START, START + timedelta(days=1))
    points = [steps(), steps(start="2026-09-01T00:01:00Z", end="2026-09-01T00:02:00Z")]
    first = client(FakeTransport([{"dataPoints": points, "future": 1}])).fetch_captured(
        window, subject_key="owner"
    )
    second = client(
        FakeTransport(
            [
                {"dataPoints": [points[1]], "nextPageToken": "next", "future": 1},
                {"dataPoints": [points[0]], "future": 1},
            ]
        )
    ).fetch_captured(window, subject_key="owner")
    assert first.pages[0]["future"] == 1
    assert first.source_sha256() == second.source_sha256()
    third = client(FakeTransport([{"dataPoints": points, "future": 2}])).fetch_captured(
        window, subject_key="owner"
    )
    assert first.source_sha256() != third.source_sha256()


@pytest.mark.parametrize(
    "response", [(503, {"error": "synthetic-health"}, {}), TimeoutError("synthetic-health")]
)
def test_rollup_later_page_failure_never_returns_a_partial_snapshot(response):
    from personal_data_platform.sources.fitbit.api import TransientError

    transport = FakeTransport([{"rollupDataPoints": [rollup()], "nextPageToken": "next"}, response])
    with pytest.raises(TransientError) as caught:
        client(transport, clock=lambda: START + timedelta(days=2)).fetch_heart_rate_minutes(
            Window("heart-rate", START, START + timedelta(days=1)), subject_key="owner"
        )
    assert "synthetic-health" not in str(caught.value)


@pytest.mark.parametrize(
    "response", [{"rollupDataPoints": {}}, {"nextPageToken": 1}, {"rollupDataPoints": [True]}]
)
def test_rollup_malformed_page_is_not_a_successful_empty_acquisition(response):
    from personal_data_platform.sources.fitbit.api import InvalidResponseError

    with pytest.raises(InvalidResponseError):
        client(
            FakeTransport([response]), clock=lambda: START + timedelta(days=2)
        ).fetch_heart_rate_minutes(
            Window("heart-rate", START, START + timedelta(days=1)), subject_key="owner"
        )


def test_rollup_page_limit_prevents_unbounded_acquisition():
    from personal_data_platform.sources.fitbit.api import InvalidResponseError

    transport = FakeTransport([{"nextPageToken": "next"}])
    with pytest.raises(InvalidResponseError, match="limit"):
        client(
            transport, max_pages=1, clock=lambda: START + timedelta(days=2)
        ).fetch_heart_rate_minutes(
            Window("heart-rate", START, START + timedelta(days=1)), subject_key="owner"
        )


def test_rollup_no_complete_minute_is_deferred_without_an_api_request():
    transport = FakeTransport([])
    with pytest.raises(ValueError, match="complete minute"):
        client(transport, clock=lambda: START + timedelta(seconds=30)).fetch_heart_rate_minutes(
            Window("heart-rate", START, START + timedelta(minutes=1)), subject_key="owner"
        )
    assert transport.calls == []


def test_captured_snapshot_roundtrip_preserves_unknown_page_and_point_fields():
    from personal_data_platform.sources.fitbit.models import CapturedSnapshot

    point = steps()
    point["futurePoint"] = {"value": [1, 2]}
    page = {"dataPoints": [point], "futurePage": {"value": True}}
    result = client(FakeTransport([page])).fetch_captured(
        Window("steps", START, START + timedelta(days=1)), subject_key="owner"
    )
    assert CapturedSnapshot.from_bytes(result.to_bytes()) == result
    assert result.pages == (page,)
