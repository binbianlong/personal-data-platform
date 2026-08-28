import sqlite3

import pytest

from personal_data_platform.collectors.screen_time import (
    BiomeScreenTimeSource,
    CollectorSourceError,
)


def _create_sync_db(path, rows: list[tuple[str, str, str, int]]) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE DevicePeer (
                device_identifier STRING NOT NULL,
                name STRING,
                model STRING,
                platform INTEGER,
                protocol_version INTEGER NOT NULL
            )
            """
        )
        connection.executemany(
            """
            INSERT INTO DevicePeer (
                device_identifier, name, model, platform, protocol_version
            ) VALUES (?, ?, ?, ?, 1)
            """,
            rows,
        )


def test_lists_only_platform_two_devices_and_regular_segments(tmp_path) -> None:
    sync_db = tmp_path / "sync.db"
    remote = tmp_path / "remote"
    _create_sync_db(
        sync_db,
        [
            ("synthetic-iphone", "Test Phone", "Synthetic1,1", 2),
            ("synthetic-other", "Other", "Synthetic2,1", 1),
        ],
    )
    device_dir = remote / "synthetic-iphone"
    device_dir.mkdir(parents=True)
    (device_dir / "segment-001").write_bytes(b"synthetic")
    (device_dir / "nested").mkdir()
    (device_dir / "nested/segment-002").write_bytes(b"synthetic-two")

    source = BiomeScreenTimeSource(sync_db_path=sync_db, remote_dir=remote)
    devices = source.list_iphone_devices()

    assert [device.identifier for device in devices] == ["synthetic-iphone"]
    assert [relative for _, relative in source.list_segments(devices[0])] == [
        "nested/segment-002",
        "segment-001",
    ]


def test_rejects_device_identifier_that_can_escape_remote_directory(tmp_path) -> None:
    sync_db = tmp_path / "sync.db"
    _create_sync_db(sync_db, [("../outside", "Test Phone", "Synthetic1,1", 2)])
    source = BiomeScreenTimeSource(sync_db_path=sync_db, remote_dir=tmp_path / "remote")

    with pytest.raises(CollectorSourceError, match="unsafe path character"):
        source.list_iphone_devices()


def test_reports_missing_sync_database_without_creating_it(tmp_path) -> None:
    sync_db = tmp_path / "missing.db"
    source = BiomeScreenTimeSource(sync_db_path=sync_db, remote_dir=tmp_path / "remote")

    with pytest.raises(CollectorSourceError, match="not readable"):
        source.list_iphone_devices()

    assert not sync_db.exists()
