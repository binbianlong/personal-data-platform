"""Persistent notification bindings and complete acquisition outcomes."""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from personal_data_platform.storage.motherduck import Warehouse

from .models import AcquisitionScope, Notification, Window, aware


@dataclass(frozen=True, slots=True)
class Success:
    attempt_id: str
    started_at: datetime
    source_sha256: str
    raw_keys: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BundleIntent:
    bundle_id: str
    attempt_ids: tuple[str, ...]
    chunks: tuple[tuple[str, str, int], ...]


class AcquisitionState:
    def __init__(self, warehouse: Warehouse) -> None:
        self.warehouse = warehouse
        self.connection = warehouse.connection

    def register_notifications(
        self, notifications: tuple[Notification, ...]
    ) -> Mapping[str, tuple[AcquisitionScope, ...]]:
        result: dict[str, tuple[AcquisitionScope, ...]] = {}
        for notification in notifications:
            scopes = tuple(
                AcquisitionScope(
                    notification.subject_key,
                    window,
                    "heart-rate-minute-v1" if window.data_type == "heart-rate" else "fitbit-v2",
                )
                for window in notification.windows
            )
            existing = self.connection.execute(
                "SELECT subject_key, received_at FROM ops.fitbit_notification WHERE notification_id=?",
                [notification.notification_id],
            ).fetchone()
            if existing:
                keys = {
                    row[0]
                    for row in self.connection.execute(
                        "SELECT scope_key FROM ops.fitbit_notification_scope WHERE notification_id=?",
                        [notification.notification_id],
                    ).fetchall()
                }
                if existing != (notification.subject_key, notification.received_at) or keys != {
                    s.key for s in scopes
                }:
                    raise ValueError("notification identity changed")
            else:
                self.connection.execute(
                    "INSERT INTO ops.fitbit_notification VALUES (?, ?, ?)",
                    [
                        notification.notification_id,
                        notification.subject_key,
                        notification.received_at,
                    ],
                )
                for scope in scopes:
                    self._ensure_scope(scope)
                    self.connection.execute(
                        "INSERT INTO ops.fitbit_notification_scope VALUES (?, ?, NULL)",
                        [notification.notification_id, scope.key],
                    )
            result[notification.notification_id] = scopes
        return result

    def _ensure_scope(self, scope: AcquisitionScope) -> None:
        self.connection.execute(
            "INSERT INTO ops.fitbit_scope VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
            [
                scope.key,
                scope.subject_key,
                scope.window.data_type,
                scope.window.start,
                scope.window.end,
                scope.aggregation_version,
            ],
        )

    def start_attempt(self, scope: AcquisitionScope, *, started_at: datetime) -> str:
        self._ensure_scope(scope)
        attempt_id = uuid.uuid4().hex
        self.connection.execute(
            "INSERT INTO ops.fitbit_attempt VALUES (?, ?, ?, 'pending', NULL, NULL, NULL)",
            [attempt_id, scope.key, aware(started_at)],
        )
        return attempt_id

    def bind_attempt(
        self, notification_ids: tuple[str, ...], scope: AcquisitionScope, attempt_id: str
    ) -> None:
        attempt = self.connection.execute(
            "SELECT scope_key, started_at FROM ops.fitbit_attempt WHERE attempt_id=?", [attempt_id]
        ).fetchone()
        if attempt is None or attempt[0] != scope.key:
            raise ValueError("attempt does not match scope")
        for identity in notification_ids:
            row = self.connection.execute(
                """SELECT n.received_at, ns.attempt_id, a.status
                FROM ops.fitbit_notification n JOIN ops.fitbit_notification_scope ns USING(notification_id)
                LEFT JOIN ops.fitbit_attempt a USING(attempt_id)
                WHERE notification_id=? AND ns.scope_key=?""",
                [identity, scope.key],
            ).fetchone()
            if row is None or row[0] > attempt[1]:
                raise ValueError(
                    "attempt started before notification was received or scope is absent"
                )
            if row[2] == "succeeded" and row[1] != attempt_id:
                raise ValueError("completed notification binding cannot change")
        for identity in notification_ids:
            self.connection.execute(
                "UPDATE ops.fitbit_notification_scope SET attempt_id=? WHERE notification_id=? AND scope_key=?",
                [attempt_id, identity, scope.key],
            )

    def ackable_ids(self, notification_ids: tuple[str, ...]) -> frozenset[str]:
        if not notification_ids:
            return frozenset()
        rows = self.connection.execute(
            """SELECT ns.notification_id
            FROM ops.fitbit_notification_scope ns LEFT JOIN ops.fitbit_attempt a USING(attempt_id)
            WHERE ns.notification_id IN (SELECT unnest(?))
            GROUP BY ns.notification_id HAVING count(*)=count(*) FILTER (WHERE a.status='succeeded')""",
            [list(notification_ids)],
        ).fetchall()
        return frozenset(row[0] for row in rows)

    def latest_success(self, scope: AcquisitionScope) -> Success | None:
        row = self.connection.execute(
            "SELECT attempt_id, started_at, source_sha256, raw_keys FROM ops.fitbit_scope_success WHERE scope_key=?",
            [scope.key],
        ).fetchone()
        return Success(row[0], row[1], row[2], tuple(row[3])) if row else None

    def prepare_bundle(
        self, bundle_id: str, attempt_ids: tuple[str, ...], chunks: tuple[tuple[str, str, int], ...]
    ) -> None:
        if (
            not bundle_id
            or not attempt_ids
            or len(set(attempt_ids)) != len(attempt_ids)
            or not chunks
        ):
            raise ValueError("invalid bundle intent")
        if len({chunk[0] for chunk in chunks}) != len(chunks) or any(
            not key.startswith("raw/fitbit/v2/")
            or re.fullmatch("[a-f0-9]{64}", digest) is None
            or not 0 < size <= 16 * 1024 * 1024
            for key, digest, size in chunks
        ):
            raise ValueError("invalid bundle intent chunks")
        existing = self.connection.execute(
            "SELECT bundle_id FROM ops.fitbit_bundle WHERE bundle_id=?", [bundle_id]
        ).fetchone()
        if existing:
            if self._intent(bundle_id) != BundleIntent(
                bundle_id, tuple(sorted(attempt_ids)), chunks
            ):
                raise ValueError("bundle intent changed")
            return
        count = self.connection.execute(
            "SELECT count(*) FROM ops.fitbit_attempt WHERE attempt_id IN (SELECT unnest(?)) AND status='pending'",
            [list(attempt_ids)],
        ).fetchone()
        assert count is not None
        if count[0] != len(attempt_ids):
            raise ValueError("bundle intent has unknown or completed attempts")
        self.connection.execute("INSERT INTO ops.fitbit_bundle VALUES (?, 'pending')", [bundle_id])
        for identity in attempt_ids:
            self.connection.execute(
                "INSERT INTO ops.fitbit_bundle_attempt VALUES (?, ?)", [bundle_id, identity]
            )
        for index, (key, digest, size) in enumerate(chunks):
            self.connection.execute(
                "INSERT INTO ops.fitbit_bundle_chunk VALUES (?, ?, ?, ?, ?, NULL)",
                [bundle_id, index, key, digest, size],
            )

    def _intent(self, bundle_id: str) -> BundleIntent:
        ids = self.connection.execute(
            "SELECT attempt_id FROM ops.fitbit_bundle_attempt WHERE bundle_id=? ORDER BY attempt_id",
            [bundle_id],
        ).fetchall()
        chunks = self.connection.execute(
            "SELECT raw_key, compressed_sha256, compressed_size FROM ops.fitbit_bundle_chunk WHERE bundle_id=? ORDER BY chunk_index",
            [bundle_id],
        ).fetchall()
        return BundleIntent(bundle_id, tuple(row[0] for row in ids), tuple(chunks))

    def pending_bundles(self) -> tuple[BundleIntent, ...]:
        return tuple(
            self._intent(row[0])
            for row in self.connection.execute(
                "SELECT bundle_id FROM ops.fitbit_bundle WHERE status='pending' ORDER BY bundle_id"
            ).fetchall()
        )

    def mark_chunk_saved(self, key: str, generation: int) -> None:
        row = self.connection.execute(
            "SELECT storage_generation FROM ops.fitbit_bundle_chunk WHERE raw_key=?", [key]
        ).fetchone()
        if row is None or generation < 1 or row[0] not in (None, generation):
            raise ValueError("bundle chunk generation does not match intent")
        self.connection.execute(
            "UPDATE ops.fitbit_bundle_chunk SET storage_generation=? WHERE raw_key=?",
            [generation, key],
        )

    def finish_attempt(
        self, attempt_id: str, *, source_sha256: str, raw_keys: tuple[str, ...]
    ) -> None:
        """Participate in the caller's data/Raw transaction without committing it."""
        if re.fullmatch("[a-f0-9]{64}", source_sha256) is None:
            raise ValueError("invalid acquisition digest")
        row = self.connection.execute(
            "SELECT scope_key, started_at, status, source_sha256, raw_keys FROM ops.fitbit_attempt WHERE attempt_id=?",
            [attempt_id],
        ).fetchone()
        if row is None:
            raise ValueError("unknown acquisition attempt")
        if row[2] == "succeeded":
            if (row[3], tuple(row[4])) != (source_sha256, raw_keys):
                raise ValueError("completed acquisition changed")
            return
        self.connection.execute(
            "UPDATE ops.fitbit_attempt SET status='succeeded', source_sha256=?, raw_keys=?, completed_at=? WHERE attempt_id=?",
            [source_sha256, list(raw_keys), datetime.now(UTC), attempt_id],
        )
        self.connection.execute(
            """INSERT INTO ops.fitbit_scope_success VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(scope_key) DO UPDATE SET attempt_id=excluded.attempt_id, started_at=excluded.started_at,
                source_sha256=excluded.source_sha256, raw_keys=excluded.raw_keys
            WHERE excluded.started_at >= fitbit_scope_success.started_at""",
            [row[0], attempt_id, row[1], source_sha256, list(raw_keys)],
        )
        self.connection.execute(
            """UPDATE ops.fitbit_bundle SET status='succeeded'
            WHERE bundle_id IN (SELECT bundle_id FROM ops.fitbit_bundle_attempt WHERE attempt_id=?)
            AND NOT EXISTS (SELECT 1 FROM ops.fitbit_bundle_attempt ba JOIN ops.fitbit_attempt a USING(attempt_id)
                WHERE ba.bundle_id=fitbit_bundle.bundle_id AND a.status!='succeeded')""",
            [attempt_id],
        )

    def attempt_scope(self, attempt_id: str) -> AcquisitionScope:
        row = self.connection.execute(
            """SELECT subject_key, data_type, range_start, range_end, aggregation_version
            FROM ops.fitbit_scope JOIN ops.fitbit_attempt USING(scope_key) WHERE attempt_id=?""",
            [attempt_id],
        ).fetchone()
        if row is None:
            raise ValueError("unknown acquisition attempt")
        return AcquisitionScope(row[0], Window(row[1], row[2], row[3]), row[4])
