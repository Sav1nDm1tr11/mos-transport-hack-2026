"""SQLite integration profile: durable inbox and atomic, replayable projections.

One Store owns one serialized connection. Transactions never span network I/O.
WAL readers can coexist with the writer; busy_timeout bounds external contention.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path


class Conflict(ValueError):
    pass


class Backpressure(RuntimeError):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def timestamp(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        raise ValueError("timezone_required")
    return value.astimezone(UTC).isoformat()


def now():
    return datetime.now(UTC).isoformat()


class Store:
    def __init__(self, path: str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, timeout=5, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA foreign_keys=ON")
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1, 2):
            self.db.close()
            raise RuntimeError("unsupported_database_version")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS bindings(unit_id TEXT PRIMARY KEY, tr_id TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS telemetry(
          seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
          event_id TEXT NOT NULL, tr_id TEXT NOT NULL, event_time TEXT NOT NULL,
          receive_time TEXT NOT NULL, digest TEXT NOT NULL, body TEXT NOT NULL,
          processed INTEGER NOT NULL DEFAULT 0, UNIQUE(run_id,event_id));
        CREATE INDEX IF NOT EXISTS telemetry_vehicle ON telemetry(run_id,tr_id,event_time);
        CREATE INDEX IF NOT EXISTS telemetry_availability ON telemetry(run_id,receive_time,event_time);
        CREATE INDEX IF NOT EXISTS telemetry_vehicle_availability ON telemetry(run_id,tr_id,receive_time,event_time);
        CREATE INDEX IF NOT EXISTS telemetry_pending ON telemetry(processed,seq);
        CREATE TABLE IF NOT EXISTS states(run_id TEXT, tr_id TEXT, body TEXT NOT NULL,
          PRIMARY KEY(run_id,tr_id));
        CREATE TABLE IF NOT EXISTS schedule(version TEXT, visit_id TEXT, tr_id TEXT,
          time_begin TEXT, body TEXT NOT NULL, PRIMARY KEY(version,visit_id));
        CREATE INDEX IF NOT EXISTS schedule_time ON schedule(version,tr_id,time_begin);
        CREATE TABLE IF NOT EXISTS issues(id INTEGER PRIMARY KEY AUTOINCREMENT,
          run_id TEXT, unit_id TEXT, reason TEXT, created_at TEXT, detail TEXT);
        CREATE TABLE IF NOT EXISTS batches(request_id TEXT PRIMARY KEY, run_id TEXT,
          as_of TEXT, body TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending');
        CREATE TABLE IF NOT EXISTS predictions(prediction_id TEXT PRIMARY KEY,
          request_id TEXT NOT NULL REFERENCES batches(request_id), run_id TEXT,
          tr_id TEXT, as_of TEXT, body TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS predictions_vehicle ON predictions(run_id,tr_id,as_of);
        CREATE TABLE IF NOT EXISTS changes(id INTEGER PRIMARY KEY AUTOINCREMENT,
          run_id TEXT, type TEXT, entity_id TEXT, created_at TEXT, body TEXT);
        CREATE TABLE IF NOT EXISTS prediction_cycles(
          id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, body TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS run_clocks(
          run_id TEXT PRIMARY KEY, as_of TEXT NOT NULL, updated_at TEXT NOT NULL);
        PRAGMA user_version=2;
        """)
        self.db.commit()

    @contextmanager
    def transaction(self):
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield self.db
                self.db.commit()
            except BaseException:
                self.db.rollback()
                raise

    def close(self):
        with self.lock:
            self.db.close()

    def ready(self):
        with self.lock:
            return self.db.execute("SELECT 1").fetchone()[0] == 1

    def set_run_clock(self, run_id, as_of):
        """Set the domain clock for a replay run. Wall-clock metadata stays separate."""
        value = timestamp(as_of)
        with self.transaction() as db:
            db.execute(
                "INSERT INTO run_clocks(run_id,as_of,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(run_id) DO UPDATE SET as_of=excluded.as_of,updated_at=excluded.updated_at",
                (run_id, value, now()),
            )
            self._change(db, run_id, "clock.updated", run_id, {"as_of": value})
        return value

    def run_clock(self, run_id):
        with self.lock:
            row = self.db.execute(
                "SELECT as_of FROM run_clocks WHERE run_id=?", (run_id,)
            ).fetchone()
            return row[0] if row else None

    def latest_telemetry_time(self, run_id):
        """Latest availability time, used only to bootstrap an existing replay DB."""
        with self.lock:
            row = self.db.execute(
                "SELECT max(receive_time) FROM telemetry WHERE run_id=?", (run_id,)
            ).fetchone()
            return row[0] if row and row[0] else None

    def vehicle_at(self, run_id, tr_id, as_of):
        """Reconstruct one causal vehicle state at ``as_of``."""
        cutoff = timestamp(as_of)
        with self.lock:
            latest = self.db.execute(
                "SELECT body FROM telemetry WHERE run_id=? AND tr_id=? "
                "AND event_time<=? AND receive_time<=? "
                "ORDER BY event_time DESC,seq DESC LIMIT 1",
                (run_id, tr_id, cutoff, cutoff),
            ).fetchone()
            if latest is None:
                return None
            position = self.db.execute(
                "SELECT body FROM telemetry WHERE run_id=? AND tr_id=? "
                "AND event_time<=? AND receive_time<=? "
                "AND json_extract(body,'$.location_valid')=1 "
                "AND json_extract(body,'$.lat') IS NOT NULL AND json_extract(body,'$.lon') IS NOT NULL "
                "ORDER BY event_time DESC,seq DESC LIMIT 1",
                (run_id, tr_id, cutoff, cutoff),
            ).fetchone()
        event = json.loads(latest[0])
        good = json.loads(position[0]) if position else None
        return dict(
            run_id=run_id,
            tr_id=tr_id,
            entity_version=0,
            event_time=event["event_time"],
            receive_time=event["receive_time"],
            unit_id=event["unit_id"],
            speed_kmh=event.get("speed_kmh"),
            heading_deg=event.get("heading_deg"),
            location_valid=event.get("location_valid", False),
            quality_flags=event.get("quality_flags", []),
            lat=good.get("lat") if good else None,
            lon=good.get("lon") if good else None,
            position_time=good.get("event_time") if good else None,
        )

    def vehicles_at(self, run_id, as_of, limit=50, offset=0):
        """Reconstruct a causal vehicle snapshot at ``as_of`` for replay UI."""
        cutoff = timestamp(as_of)
        with self.lock:
            latest = self.db.execute(
                "WITH ranked AS ("
                " SELECT tr_id,body,row_number() OVER (PARTITION BY tr_id ORDER BY event_time DESC,seq DESC) AS rn"
                " FROM telemetry WHERE run_id=? AND event_time<=? AND receive_time<=?"
                ") SELECT tr_id,body FROM ranked WHERE rn=1 ORDER BY tr_id LIMIT ? OFFSET ?",
                (run_id, cutoff, cutoff, limit, offset),
            ).fetchall()
            if not latest:
                return []
            tr_ids = [r["tr_id"] for r in latest]
            placeholders = ",".join("?" for _ in tr_ids)
            positions = self.db.execute(
                "WITH ranked AS ("
                " SELECT tr_id,body,row_number() OVER (PARTITION BY tr_id ORDER BY event_time DESC,seq DESC) AS rn"
                " FROM telemetry WHERE run_id=? AND event_time<=? AND receive_time<=?"
                " AND json_extract(body,'$.location_valid')=1"
                " AND json_extract(body,'$.lat') IS NOT NULL AND json_extract(body,'$.lon') IS NOT NULL"
                f") SELECT tr_id,body FROM ranked WHERE rn=1 AND tr_id IN ({placeholders})",
                (run_id, cutoff, cutoff, *tr_ids),
            ).fetchall()
        position_by_tr = {r["tr_id"]: json.loads(r["body"]) for r in positions}
        result = []
        for row in latest:
            event = json.loads(row["body"])
            position = position_by_tr.get(row["tr_id"])
            result.append(
                dict(
                    run_id=run_id,
                    tr_id=row["tr_id"],
                    entity_version=0,
                    event_time=event["event_time"],
                    receive_time=event["receive_time"],
                    unit_id=event["unit_id"],
                    speed_kmh=event.get("speed_kmh"),
                    heading_deg=event.get("heading_deg"),
                    location_valid=event.get("location_valid", False),
                    quality_flags=event.get("quality_flags", []),
                    lat=position.get("lat") if position else None,
                    lon=position.get("lon") if position else None,
                    position_time=position.get("event_time") if position else None,
                )
            )
        return result

    def snapshot_at(self, run_id, as_of, limit=50, offset=0):
        cutoff = timestamp(as_of)
        with self.lock:
            cursor = self.db.execute(
                "SELECT coalesce(max(id),0) FROM changes WHERE run_id=?", (run_id,)
            ).fetchone()[0]
            counts = dict(
                vehicles=self.db.execute(
                    "SELECT count(DISTINCT tr_id) FROM telemetry "
                    "WHERE run_id=? AND event_time<=? AND receive_time<=?",
                    (run_id, cutoff, cutoff),
                ).fetchone()[0],
                events=self.db.execute(
                    "SELECT count(*) FROM telemetry WHERE run_id=? AND event_time<=? AND receive_time<=?",
                    (run_id, cutoff, cutoff),
                ).fetchone()[0],
                pending=self.db.execute(
                    "SELECT count(*) FROM telemetry WHERE run_id=? AND processed=0 "
                    "AND event_time<=? AND receive_time<=?",
                    (run_id, cutoff, cutoff),
                ).fetchone()[0],
            )
        return dict(
            run_id=run_id,
            event_cursor=cursor,
            snapshot_id=f"{run_id}:{cursor}:{cutoff}",
            counts=counts,
            vehicles=self.vehicles_at(run_id, as_of, limit, offset),
        )

    def bind_device(self, unit_id, tr_id):
        with self.transaction() as db:
            old = db.execute(
                "SELECT tr_id FROM bindings WHERE unit_id=?", (unit_id,)
            ).fetchone()
            if old and old[0] != tr_id:
                raise Conflict("device_already_bound")
            db.execute("INSERT OR IGNORE INTO bindings VALUES(?,?)", (unit_id, tr_id))

    def resolve_device(self, unit_id):
        with self.lock:
            row = self.db.execute(
                "SELECT tr_id FROM bindings WHERE unit_id=?", (str(unit_id),)
            ).fetchone()
            return row[0] if row else None

    def _advance_replay_clock(self, db, event):
        if event.get("source") != "replay":
            return
        identity = event.get("source_identity") or {}
        if identity.get("advance_clock", True) is False:
            return
        run_id = event["run_id"]
        value = event["receive_time"]
        row = db.execute(
            "SELECT as_of FROM run_clocks WHERE run_id=?", (run_id,)
        ).fetchone()
        if row is not None and row[0] >= value:
            return
        previous = row[0] if row else None
        db.execute(
            "INSERT INTO run_clocks(run_id,as_of,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(run_id) DO UPDATE SET as_of=excluded.as_of,updated_at=excluded.updated_at",
            (run_id, value, now()),
        )
        # One SSE clock tick per virtual second is enough even for dense telemetry.
        if previous is None or previous[:19] != value[:19]:
            self._change(db, run_id, "clock.updated", run_id, {"as_of": value})

    def enqueue(self, event, max_pending=10000):
        event = dict(event)
        for key in ("event_time", "receive_time", "ingested_at"):
            event[key] = timestamp(event[key])
        # Delivery metadata can change when the same physical observation reconnects.
        semantic = {
            k: v
            for k, v in event.items()
            if k not in ("receive_time", "ingested_at", "source_identity")
        }
        digest = hashlib.sha256(canonical(semantic).encode()).hexdigest()
        with self.transaction() as db:
            old = db.execute(
                "SELECT digest FROM telemetry WHERE run_id=? AND event_id=?",
                (event["run_id"], event["event_id"]),
            ).fetchone()
            if old:
                if old[0] != digest:
                    raise Conflict("event_id_conflict")
                self._advance_replay_clock(db, event)
                return False
            if (
                db.execute(
                    "SELECT count(*) FROM telemetry WHERE processed=0"
                ).fetchone()[0]
                >= max_pending
            ):
                raise Backpressure("inbox_full")
            db.execute(
                "INSERT INTO telemetry(run_id,event_id,tr_id,event_time,receive_time,digest,body) VALUES(?,?,?,?,?,?,?)",
                (
                    event["run_id"],
                    event["event_id"],
                    event["tr_id"],
                    event["event_time"],
                    event["receive_time"],
                    digest,
                    canonical(event),
                ),
            )
            self._advance_replay_clock(db, event)
            return True

    def _change(self, db, run_id, kind, entity_id, body):
        db.execute(
            "INSERT INTO changes(run_id,type,entity_id,created_at,body) VALUES(?,?,?,?,?)",
            (run_id, kind, entity_id, now(), canonical(body)),
        )

    def process_pending(self, limit=200):
        with self.transaction() as db:
            rows = db.execute(
                "SELECT seq,body FROM telemetry WHERE processed=0 ORDER BY seq LIMIT ?",
                (limit,),
            ).fetchall()
            for row in rows:
                event = json.loads(row["body"])
                key = (event["run_id"], event["tr_id"])
                old = db.execute(
                    "SELECT body FROM states WHERE run_id=? AND tr_id=?", key
                ).fetchone()
                state = (
                    json.loads(old[0])
                    if old
                    else dict(
                        run_id=key[0],
                        tr_id=key[1],
                        entity_version=0,
                        lat=None,
                        lon=None,
                        position_time=None,
                    )
                )
                newer = not old or event["event_time"] > state["event_time"]
                if newer:
                    state.update(
                        {
                            k: event[k]
                            for k in (
                                "event_time",
                                "receive_time",
                                "unit_id",
                                "speed_kmh",
                                "heading_deg",
                                "location_valid",
                                "quality_flags",
                            )
                        }
                    )
                position_newer = (
                    event["location_valid"]
                    and event["lat"] is not None
                    and event["lon"] is not None
                    and (
                        state["position_time"] is None
                        or event["event_time"] > state["position_time"]
                    )
                )
                if position_newer:
                    state.update(
                        lat=event["lat"],
                        lon=event["lon"],
                        position_time=event["event_time"],
                    )
                if newer or position_newer:
                    state["entity_version"] += 1
                    db.execute(
                        "INSERT INTO states VALUES(?,?,?) ON CONFLICT(run_id,tr_id) DO UPDATE SET body=excluded.body",
                        (*key, canonical(state)),
                    )
                    self._change(db, key[0], "vehicle.updated", key[1], state)
                db.execute(
                    "UPDATE telemetry SET processed=1 WHERE seq=?", (row["seq"],)
                )
            return len(rows)

    def vehicle(self, run_id, tr_id):
        with self.lock:
            row = self.db.execute(
                "SELECT body FROM states WHERE run_id=? AND tr_id=?", (run_id, tr_id)
            ).fetchone()
            return json.loads(row[0]) if row else None

    def vehicles(self, run_id, limit=50, offset=0):
        with self.lock:
            rows = self.db.execute(
                "SELECT body FROM states WHERE run_id=? ORDER BY tr_id LIMIT ? OFFSET ?",
                (run_id, limit, offset),
            ).fetchall()
            return [json.loads(r[0]) for r in rows]

    def telemetry(
        self, run_id, tr_id=None, limit=200, as_of=None, since=None, offset=0
    ):
        query = "SELECT body FROM telemetry WHERE run_id=?"
        params = [run_id]
        if tr_id is not None:
            query += " AND tr_id=?"
            params.append(tr_id)
        if as_of is not None:
            query += " AND event_time<=? AND receive_time<=?"
            params.extend([timestamp(as_of)] * 2)
        if since is not None:
            query += " AND event_time>=?"
            params.append(timestamp(since))
        query += " ORDER BY event_time DESC,seq DESC LIMIT ? OFFSET ?"
        with self.lock:
            return [
                json.loads(r[0])
                for r in self.db.execute(query, (*params, limit, offset))
            ]

    def issue(self, run_id, unit_id, reason, detail=""):
        with self.transaction() as db:
            db.execute(
                "INSERT INTO issues(run_id,unit_id,reason,created_at,detail) VALUES(?,?,?,?,?)",
                (run_id, unit_id, reason, now(), str(detail)[:512]),
            )
            # Bound the diagnostic table; telemetry and model inputs are never trimmed here.
            db.execute(
                "DELETE FROM issues WHERE id <= (SELECT coalesce(max(id),0)-10000 FROM issues)"
            )

    def issues(self, run_id, limit=50):
        with self.lock:
            return [
                dict(r)
                for r in self.db.execute(
                    "SELECT * FROM issues WHERE run_id=? ORDER BY id DESC LIMIT ?",
                    (run_id, limit),
                )
            ]

    def put_schedule(self, visits):
        with self.transaction() as db:
            for raw in visits:
                visit = dict(raw)
                visit["time_begin"] = timestamp(visit["time_begin"])
                key = (visit["schedule_version"], visit["stop_visit_id"])
                body = canonical(visit)
                old = db.execute(
                    "SELECT body FROM schedule WHERE version=? AND visit_id=?", key
                ).fetchone()
                if old and old[0] != body:
                    raise Conflict("schedule_version_immutable")
                db.execute(
                    "INSERT OR IGNORE INTO schedule VALUES(?,?,?,?,?)",
                    (*key, visit["tr_id"], visit["time_begin"], body),
                )

    def schedule(self, version, tr_id=None, limit=10000):
        with self.lock:
            if tr_id is not None:
                rows = self.db.execute(
                    "SELECT body FROM schedule WHERE version=? AND tr_id=? ORDER BY time_begin,visit_id LIMIT ?",
                    (version, tr_id, limit),
                )
            else:
                rows = self.db.execute(
                    "SELECT body FROM schedule WHERE version=? ORDER BY time_begin,visit_id LIMIT ?",
                    (version, limit),
                )
            return [json.loads(r[0]) for r in rows]

    def targets(self, version, as_of):
        start = timestamp(as_of + timedelta(minutes=10))
        end = timestamp(as_of + timedelta(minutes=15))
        with self.lock:
            rows = self.db.execute(
                "SELECT body FROM schedule WHERE version=? AND time_begin>? AND time_begin<=? ORDER BY tr_id,time_begin,visit_id",
                (version, start, end),
            ).fetchall()
        grouped = {}
        for row in rows:
            v = json.loads(row[0])
            grouped.setdefault(v["tr_id"], []).append(v)
        return [
            vs[0]
            for vs in grouped.values()
            if len(vs) == 1 or vs[0]["time_begin"] != vs[1]["time_begin"]
        ]

    def save_batch(self, batch):
        body = canonical(batch)
        with self.transaction() as db:
            old = db.execute(
                "SELECT body FROM batches WHERE request_id=?", (batch["request_id"],)
            ).fetchone()
            if old:
                if old[0] != body:
                    raise Conflict("batch_input_conflict")
                return False
            db.execute(
                "INSERT INTO batches(request_id,run_id,as_of,body) VALUES(?,?,?,?)",
                (batch["request_id"], batch["run_id"], timestamp(batch["as_of"]), body),
            )
            return True

    def pending_batches(self, limit=1):
        with self.lock:
            return [
                json.loads(r[0])
                for r in self.db.execute(
                    "SELECT body FROM batches WHERE status='pending' ORDER BY as_of LIMIT ?",
                    (limit,),
                )
            ]

    def finish_batch(self, batch, results):
        targets = {t["prediction_id"]: t for t in batch["targets"]}
        if len(results) != len(targets) or {r["prediction_id"] for r in results} != set(
            targets
        ):
            raise ValueError("result_id_mismatch")
        with self.transaction() as db:
            existing = db.execute(
                "SELECT status FROM batches WHERE request_id=?", (batch["request_id"],)
            ).fetchone()
            if not existing:
                raise ValueError("batch_not_saved")
            if existing[0] == "complete":
                return
            for result in results:
                target = targets[result["prediction_id"]]
                body = {
                    **result,
                    "tr_id": target["tr_id"],
                    "target_stop_id": target["target_stop_id"],
                    "target_time_begin": target["target_time_begin"],
                    "cur_dev_s": target.get("cur_dev_s"),
                    "cur_dev_source": target.get("cur_dev_source"),
                    "cur_dev_time": target.get("cur_dev_time"),
                    "cur_dev_quality": target.get("cur_dev_quality"),
                    "as_of": batch["as_of"],
                    "request_id": batch["request_id"],
                    "run_id": batch["run_id"],
                    "schedule_version": batch.get("schedule_version"),
                    "mode": "model" if result["status"] == "ok" else "unavailable",
                }
                db.execute(
                    "INSERT INTO predictions VALUES(?,?,?,?,?,?)",
                    (
                        result["prediction_id"],
                        batch["request_id"],
                        batch["run_id"],
                        target["tr_id"],
                        timestamp(batch["as_of"]),
                        canonical(body),
                    ),
                )
                self._change(
                    db,
                    batch["run_id"],
                    "prediction.updated",
                    result["prediction_id"],
                    body,
                )
            db.execute(
                "UPDATE batches SET status='complete' WHERE request_id=?",
                (batch["request_id"],),
            )

    def predictions(self, run_id, tr_id=None, limit=50, offset=0, as_of=None):
        cutoff = timestamp(as_of) if as_of is not None else None
        with self.lock:
            query = "SELECT body FROM predictions WHERE run_id=?"
            params = [run_id]
            if tr_id is not None:
                query += " AND tr_id=?"
                params.append(tr_id)
            if cutoff is not None:
                query += " AND as_of<=?"
                params.append(cutoff)
            query += " ORDER BY as_of DESC,prediction_id LIMIT ? OFFSET ?"
            params.extend([limit, offset])
            rows = self.db.execute(query, params)
            return [json.loads(r[0]) for r in rows]

    def snapshot(self, run_id, limit=50):
        with self.lock:
            cursor = self.db.execute(
                "SELECT coalesce(max(id),0) FROM changes WHERE run_id=?", (run_id,)
            ).fetchone()[0]
            counts = dict(
                vehicles=self.db.execute(
                    "SELECT count(*) FROM states WHERE run_id=?", (run_id,)
                ).fetchone()[0],
                events=self.db.execute(
                    "SELECT count(*) FROM telemetry WHERE run_id=?", (run_id,)
                ).fetchone()[0],
                pending=self.db.execute(
                    "SELECT count(*) FROM telemetry WHERE run_id=? AND processed=0",
                    (run_id,),
                ).fetchone()[0],
            )
            return dict(
                run_id=run_id,
                event_cursor=cursor,
                snapshot_id=f"{run_id}:{cursor}",
                counts=counts,
                vehicles=self.vehicles(run_id, limit),
            )

    def changes(self, run_id, after, limit=200):
        with self.lock:
            return [
                {**dict(r), "body": json.loads(r["body"])}
                for r in self.db.execute(
                    "SELECT * FROM changes WHERE run_id=? AND id>? ORDER BY id LIMIT ?",
                    (run_id, after, limit),
                )
            ]

    def record_cycle(self, run_id, **detail):
        with self.transaction() as db:
            db.execute(
                "INSERT INTO prediction_cycles(run_id,body) VALUES (?,?)",
                (run_id, canonical(dict(recorded_at=now(), **detail))),
            )
            db.execute(
                "DELETE FROM prediction_cycles WHERE id <= "
                "(SELECT max(id)-10000 FROM prediction_cycles)"
            )

    def cycles(self, run_id, limit=50, as_of=None):
        cutoff = timestamp(as_of) if as_of is not None else None
        with self.lock:
            fetch_limit = 10000 if cutoff is not None else limit
            items = [
                json.loads(r[0])
                for r in self.db.execute(
                    "SELECT body FROM prediction_cycles WHERE run_id=? ORDER BY id DESC LIMIT ?",
                    (run_id, fetch_limit),
                )
            ]
        if cutoff is None:
            return items
        return [
            item
            for item in items
            if item.get("as_of") is not None and timestamp(item["as_of"]) <= cutoff
        ][:limit]

    def coverage(self, run_id, schedule_version, as_of, max_age_seconds=90):
        with self.lock:
            targets = {
                (v["tr_id"], v["stop_visit_id"], timestamp(v["time_begin"]))
                for v in self.targets(schedule_version, as_of)
            }
            seen, predicted = set(), set()
            for row in self.db.execute(
                "SELECT p.body,b.body FROM predictions p JOIN batches b USING(request_id) "
                "WHERE p.run_id=? AND p.as_of>=? AND p.as_of<=? ORDER BY p.as_of DESC,p.rowid DESC",
                (
                    run_id,
                    timestamp(as_of - timedelta(seconds=max_age_seconds)),
                    timestamp(as_of),
                ),
            ):
                prediction, batch = json.loads(row[0]), json.loads(row[1])
                if batch.get("schedule_version") != schedule_version:
                    continue
                key = (
                    prediction["tr_id"],
                    prediction["target_stop_id"],
                    timestamp(prediction["target_time_begin"]),
                )
                if key in seen:
                    continue
                seen.add(key)
                if key in targets and prediction["status"] == "ok":
                    predicted.add(prediction["tr_id"])
            return dict(
                target_vehicles=len({t[0] for t in targets}),
                predicted_vehicles=len(predicted),
                max_age_seconds=max_age_seconds,
                evaluated_at=timestamp(as_of),
            )

    def dashboard(self, run_id, limit, schedule_version, as_of, causal=False):
        with self.lock:
            data = (
                self.snapshot_at(run_id, as_of, limit)
                if causal
                else self.snapshot(run_id, limit)
            )
            data["schedule_version"] = schedule_version
            data["coverage"] = self.coverage(run_id, schedule_version, as_of)
            data["prediction_cycles"] = self.cycles(
                run_id, 10, as_of if causal else None
            )
            if causal:
                backlog = self.db.execute(
                    "SELECT count(*) FROM batches WHERE run_id=? AND status='pending' AND as_of<=?",
                    (run_id, timestamp(as_of)),
                ).fetchone()[0]
            else:
                backlog = self.db.execute(
                    "SELECT count(*) FROM batches WHERE run_id=? AND status='pending'",
                    (run_id,),
                ).fetchone()[0]
            data["prediction_backlog"] = backlog
            return data
