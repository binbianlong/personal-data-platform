"""Read-only Mac App.InFocus inspection through the public CLI."""

from __future__ import annotations

import json
import struct

from personal_data_platform.cli import main
from tests.screen_time_helpers import segb, text_field, tombstone


def _focus_event(bundle_id: str, timestamp: float, *, in_foreground: bool) -> bytes:
    return (
        b"\x10\x01\x18"
        + bytes([int(in_foreground)])
        + b"\x21"
        + struct.pack("<d", timestamp)
        + text_field(6, bundle_id)
    )


def _write_segment(directory, name: str, payload: bytes, *, state: int = 1, crc=None) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_bytes(segb(payload, state=state, crc=crc)[0])


def test_inspect_mac_reports_apps_and_record_types_without_open_segments(tmp_path, capsys) -> None:
    _write_segment(tmp_path, "100", _focus_event("com.z", 10.0, in_foreground=True))
    _write_segment(tmp_path, "101", _focus_event("com.a", 20.0, in_foreground=False))
    _write_segment(tmp_path, "102", _focus_event("com.a", 30.0, in_foreground=True))
    _write_segment(tmp_path, "103", _focus_event("com.deleted", 40.0, in_foreground=True), state=3)
    _write_segment(tmp_path, "104", _focus_event("com.badcrc", 50.0, in_foreground=True), crc=0)
    _write_segment(tmp_path, "105", _focus_event("com.open", 60.0, in_foreground=True))
    _write_segment(tmp_path / "tombstone", "200", tombstone("100", 12, 10))
    _write_segment(tmp_path / "tombstone", "201", tombstone("101", 12, 10))

    assert main(["screen-time", "inspect-mac", "--directory", str(tmp_path)]) == 0

    output = capsys.readouterr()
    assert output.err == ""
    assert json.loads(output.out) == {
        "segment_count": 8,
        "checked_segment_count": 6,
        "deferred_segment_count": 2,
        "event_record_count": 3,
        "start_record_count": 2,
        "end_record_count": 1,
        "deleted_record_count": 1,
        "crc_failure_record_count": 1,
        "tombstone_record_count": 1,
        "first_event_at": "2001-01-01T00:00:10+00:00",
        "last_event_at": "2001-01-01T00:00:30+00:00",
        "apps": [
            {
                "bundle_id": "com.a",
                "event_record_count": 2,
                "start_record_count": 1,
                "end_record_count": 1,
            },
            {
                "bundle_id": "com.z",
                "event_record_count": 1,
                "start_record_count": 1,
                "end_record_count": 0,
            },
        ],
    }


def test_inspect_mac_rejects_malformed_completed_segment_without_partial_json(
    tmp_path, capsys
) -> None:
    _write_segment(tmp_path, "100", _focus_event("com.bad", 10.0, in_foreground=True), state=99)
    _write_segment(tmp_path, "101", _focus_event("com.open", 20.0, in_foreground=True))

    assert main(["screen-time", "inspect-mac", "--directory", str(tmp_path)]) == 1

    output = capsys.readouterr()
    assert output.out == ""
    assert "100" in output.err
    assert "unsupported SEGB record state" in output.err


def test_inspect_mac_rejects_nonnumeric_segment_name(tmp_path, capsys) -> None:
    (tmp_path / "not-a-segment").write_bytes(b"invalid")

    assert main(["screen-time", "inspect-mac", "--directory", str(tmp_path)]) == 1

    output = capsys.readouterr()
    assert output.out == ""
    assert "non-numeric segment name" in output.err


def test_inspect_mac_rejects_missing_directory(tmp_path, capsys) -> None:
    missing = tmp_path / "missing"

    assert main(["screen-time", "inspect-mac", "--directory", str(missing)]) == 1

    output = capsys.readouterr()
    assert output.out == ""
    assert "not readable" in output.err
