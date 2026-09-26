"""Read-only format inspection of this Mac's local App.InFocus segments."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

from personal_data_platform.raw.models import RawObject

from .collector import (
    CollectorSourceError,
    _read_stable_bytes,
    list_segment_files,
    select_completed_segments,
)
from .parser import parse_segb_bytes
from .raw import APP_IN_FOCUS_STREAM, sha256_hex

DEFAULT_MAC_DIRECTORY = Path.home() / "Library/Biome/streams/restricted/App.InFocus/local"
_INSPECTION_TIME = datetime(1970, 1, 1, tzinfo=UTC)


def inspect_mac_directory(directory: Path) -> dict[str, object]:
    """Summarize completed local segments without retaining or printing their payloads."""
    directory = directory.expanduser()
    if not directory.is_dir():
        raise CollectorSourceError(f"Mac App.InFocus directory is not readable: {directory}")
    segments = list_segment_files(directory)
    completed = select_completed_segments(segments)
    record_counts: Counter[str] = Counter()
    app_counts: defaultdict[str, Counter[str]] = defaultdict(Counter)
    first_event_at: datetime | None = None
    last_event_at: datetime | None = None

    for path, relative_path in completed:
        try:
            segment = _read_stable_bytes(path)
            raw = RawObject(
                key=f"local/{relative_path}",
                source_id="screen_time",
                schema_version=1,
                subject_key="local-mac-inspection",
                stream=APP_IN_FOCUS_STREAM,
                logical_key=relative_path,
                observed_at=_INSPECTION_TIME,
                sha256=sha256_hex(segment),
                storage_created_at=_INSPECTION_TIME,
                storage_generation=1,
            )
            records = parse_segb_bytes(
                raw,
                segment,
                segment_kind="tombstones" if path.parent.name == "tombstone" else "events",
            )
        except Exception as error:
            raise CollectorSourceError(
                f"failed to inspect Mac segment {relative_path}: {error}"
            ) from error

        for record in records:
            record_counts[record.record_kind] += 1
            if record.record_kind != "event":
                continue
            if record.bundle_id is None or record.event_at is None or record.in_foreground is None:
                raise CollectorSourceError(
                    f"Mac segment {relative_path} has an incomplete decoded event"
                )
            direction = "start" if record.in_foreground else "end"
            record_counts[direction] += 1
            app_counts[record.bundle_id][direction] += 1
            if first_event_at is None or record.event_at < first_event_at:
                first_event_at = record.event_at
            if last_event_at is None or record.event_at > last_event_at:
                last_event_at = record.event_at

    return {
        "segment_count": len(segments),
        "checked_segment_count": len(completed),
        "deferred_segment_count": len(segments) - len(completed),
        "event_record_count": record_counts["event"],
        "start_record_count": record_counts["start"],
        "end_record_count": record_counts["end"],
        "deleted_record_count": record_counts["deleted"],
        "crc_failure_record_count": record_counts["crc_failure"],
        "tombstone_record_count": record_counts["tombstone"],
        "first_event_at": first_event_at.astimezone(UTC).isoformat() if first_event_at else None,
        "last_event_at": last_event_at.astimezone(UTC).isoformat() if last_event_at else None,
        "apps": [
            {
                "bundle_id": bundle_id,
                "event_record_count": counts["start"] + counts["end"],
                "start_record_count": counts["start"],
                "end_record_count": counts["end"],
            }
            for bundle_id, counts in sorted(app_counts.items())
        ],
    }
