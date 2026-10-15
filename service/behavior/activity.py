"""Opt-in aggregate device metadata. Never interprets idle as human absence.

Receipts are atomic and ordered; coarse 15-minute aggregates outlive raw rows.
The lease is process-local so a backend restart cannot accept an old capture.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import secrets
from collections import defaultdict
from zoneinfo import ZoneInfo

from service.behavior.store import _obs_row
from service.behavior.types import Observation

DEFAULTS = dict(
    activity=False,
    websites=False,
    email=False,
    messages=False,
    interpretation=False,
    remote_analysis=False,
    default_block_minutes=30,
)
BIN = 900


def finite(x):
    return type(x) in (int, float) and math.isfinite(x)


class Activity:
    def __init__(self, store):
        self.store = store
        self.process = secrets.token_hex(16)
        self.client = None
        self.token = None
        self.expiry = 0.0
        self.capture_since = 0.0

    def settings(self):
        return {**DEFAULTS, **(self.store.meta_get("signal_settings", {}) or {})}

    def configure(self, changes, now):
        if (
            not isinstance(changes, dict)
            or not changes
            or changes.keys() - DEFAULTS.keys()
        ):
            raise ValueError("Unknown activity/source preference")
        for key, value in changes.items():
            if key.endswith("_minutes"):
                if type(value) is not int or not 15 <= value <= 120:
                    raise ValueError("Duration estimate must be 15–120 minutes")
            elif type(value) is not bool:
                raise ValueError("Source preferences must be true or false")
        with self.store._lock, self.store._conn:
            settings = {**self.settings(), **changes}
            self._meta("signal_settings", settings)
            self._meta("signal_epoch", secrets.token_hex(16))
            self._meta("signal_epoch_start", now)
            self.token = None
            self.client = None
            if settings["activity"] is not True:
                self.store._conn.execute(
                    "DELETE FROM observations WHERE source='activity'"
                )
                self._meta("activity_receipt", {})
            # Source text is never persisted here. Disable immediately changes
            # both capture eligibility and every planning read.
        return settings

    def _meta(self, key, value):
        self.store._conn.execute(
            "INSERT INTO meta VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value, allow_nan=False)),
        )

    def lease(self, client, now):
        if not isinstance(client, str) or not re.fullmatch(
            r"[a-zA-Z0-9-]{16,80}", client
        ):
            raise ValueError("Invalid collector session")
        with self.store._lock:
            if self.settings()["activity"] is not True:
                return {"enabled": False}
            if self.client != client or self.token is None:
                self.client, self.token = client, secrets.token_hex(24)
                self.capture_since = now
            self.expiry = now + 90
            return dict(
                enabled=True,
                client=client,
                token=self.token,
                process=self.process,
                epoch=self.store.meta_get("signal_epoch", "initial"),
                issued_at=now,
                expires_at=self.expiry,
            )

    def ingest(self, payload, now):
        if not isinstance(payload, dict) or not isinstance(
            payload.get("intervals"), list
        ):
            raise ValueError("Activity intervals required")
        rows = payload["intervals"]
        if not 0 < len(rows) <= 128:
            raise ValueError("Activity batch must contain 1–128 intervals")
        lease = payload.get("lease") or {}
        if not isinstance(lease, dict):
            raise ValueError("Invalid capture lease")
        digest = hashlib.sha256(
            json.dumps(
                dict(
                    client=lease.get("client"), epoch=lease.get("epoch"), intervals=rows
                ),
                sort_keys=True,
                allow_nan=False,
            ).encode()
        ).hexdigest()
        observations = []
        previous = None
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("Invalid interval")
            a, b, idle = row.get("start"), row.get("end"), row.get("idle_seconds")
            seq, bundle, zone = (
                row.get("sequence"),
                row.get("bundle"),
                row.get("timezone"),
            )
            if (
                not finite(a)
                or not finite(b)
                or not now - 300 <= a < b <= now + 5
                or b - a > 90
                or not finite(idle)
                or not 0 <= idle <= b - a
                or type(seq) is not int
                or not 0 < seq < 2**53
                or not isinstance(bundle, str)
                or not re.fullmatch(r"[A-Za-z0-9._-]{0,180}", bundle)
                or not isinstance(zone, str)
            ):
                raise ValueError("Invalid interval bounds or metadata")
            ZoneInfo(zone)
            if previous is not None and (a < previous[0] or seq <= previous[1]):
                raise ValueError("Activity intervals overlap or are unordered")
            previous = (b, seq)
            if any(
                row.get(k) is not None and type(row[k]) is not bool
                for k in ("screen_awake", "session_active")
            ):
                raise ValueError("Unknown device state must be null")
            observations.append(
                Observation(
                    f"activity:{lease.get('client')}:{seq}",
                    "device_activity",
                    a,
                    end_ts=b,
                    source="activity",
                    subject=bundle,
                    attrs=dict(
                        idle_seconds=idle,
                        screen_awake=row.get("screen_awake"),
                        session_active=row.get("session_active"),
                        timezone=zone,
                    ),
                )
            )
        with self.store._lock, self.store._conn:
            if (
                self.settings()["activity"] is not True
                or lease.get("process") != self.process
                or lease.get("client") != self.client
                or lease.get("token") != self.token
                or lease.get("epoch") != self.store.meta_get("signal_epoch", "initial")
                or not finite(lease.get("expires_at"))
                or not now <= lease["expires_at"] <= self.expiry
                or now > self.expiry
            ):
                raise ValueError("Capture lease expired or source disabled")
            if observations[0].ts < max(
                self.capture_since, self.store.meta_get("signal_epoch_start", now)
            ):
                raise ValueError("Capture predates current consent")
            old = self.store.meta_get("activity_receipt", {})
            if old.get("digest") == digest:
                return {"accepted": 0, "duplicate": True}
            if old.get("end", 0) > observations[0].ts or (
                old.get("client") == self.client
                and old.get("sequence", 0) >= rows[0]["sequence"]
            ):
                raise ValueError("Stale or overlapping activity receipt")
            self.store._conn.executemany(
                "INSERT INTO observations VALUES (?,?,?,?,?,?,?,?,?)",
                [_obs_row(o) for o in observations],
            )
            buckets = {int(o.ts // BIN) * BIN for o in observations}
            buckets |= {int((o.end_ts - 0.001) // BIN) * BIN for o in observations}
            for start in buckets:
                self._aggregate(start)
            self._meta(
                "activity_receipt",
                dict(
                    digest=digest,
                    end=observations[-1].end_ts,
                    observed_at=now,
                    client=self.client,
                    sequence=rows[-1]["sequence"],
                ),
            )
            self.store._conn.execute(
                "DELETE FROM observations WHERE kind='device_activity' AND ts < ?",
                (now - 7 * 86400,),
            )
            self.store._conn.execute(
                "DELETE FROM observations WHERE kind='activity_bin' AND ts < ?",
                (now - 56 * 86400,),
            )
        return {"accepted": len(observations), "duplicate": False}

    def _aggregate(self, start):
        rows = self.store.observations(
            kinds=["device_activity"], since=start - 90, until=start + BIN
        )
        by_zone = defaultdict(list)
        for o in rows:
            if o.end_ts > start:
                by_zone[o.attrs["timezone"]].append(o)
        for zone, rows in by_zone.items():
            apps = defaultdict(float)
            covered = idle = inactive = screen_sleep = awake = 0.0
            for o in rows:
                seconds = max(0, min(start + BIN, o.end_ts) - max(start, o.ts))
                covered += seconds
                idle += max(
                    0,
                    min(start + BIN, o.end_ts)
                    - max(start, o.end_ts - o.attrs["idle_seconds"]),
                )
                if o.attrs["session_active"] is False:
                    inactive += seconds
                if o.attrs["screen_awake"] is False:
                    screen_sleep += seconds
                if o.attrs["screen_awake"] is True:
                    awake += seconds
                if (
                    o.attrs["session_active"] is True
                    and o.attrs["screen_awake"] is True
                ):
                    apps[o.subject] += seconds
            aggregate = Observation(
                f"activity-bin:{zone}:{start}",
                "activity_bin",
                start,
                source="activity",
                end_ts=start + BIN,
                attrs=dict(
                    timezone=zone,
                    covered=min(BIN, covered),
                    idle=min(BIN, idle),
                    inactive=min(BIN, inactive),
                    screen_sleep=min(BIN, screen_sleep),
                    awake=min(BIN, awake),
                    apps=dict(apps),
                ),
            )
            self.store._conn.execute(
                "INSERT INTO observations VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET attrs=excluded.attrs",
                _obs_row(aggregate),
            )
