"""Durable, paginated training progress, independent of the raw log tail.

Only the worker reads the remote append-only event stream. The byte cursor,
deduplicated events and compact run summary commit in one SQLite transaction,
so a restart or repeated poll cannot lose or duplicate an observed step.
Telemetry is informational and never supplies scheduler completion evidence.
"""
from __future__ import annotations

from datetime import datetime
import json
import math
import re

from .db import utcnow

KINDS = {"phase", "step", "evaluation", "checkpoint", "complete"}
CHUNK_BYTES = 256 * 1024
MAX_EVENT_BYTES = 64 * 1024
MAX_METRICS = 128


class TelemetryError(ValueError):
    """A remote response could not safely advance the recorded byte cursor."""


def normalize_event(value: object) -> dict | None:
    if not isinstance(value, dict) or type(value.get("schema_version")) is not int or value["schema_version"] != 1:
        return None
    if (type(value.get("seq")) is not int or not 1 <= value["seq"] <= 2**53 - 1
            or not isinstance(value.get("kind"), str) or value["kind"] not in KINDS):
        return None
    stamp = value.get("timestamp")
    try:
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return None
    except (ValueError, TypeError, AttributeError):
        return None
    event = {"schema_version": 1, "seq": value["seq"], "kind": value["kind"], "timestamp": parsed.isoformat()}
    for key in ("step", "total_steps", "optimizer_updates", "size"):
        if key in value:
            if type(value[key]) is not int or not 0 <= value[key] <= 2**53 - 1:
                return None
            event[key] = value[key]
    metrics = value.get("metrics", {})
    if not isinstance(metrics, dict) or len(metrics) > MAX_METRICS:
        return None
    event["metrics"] = {}
    for name, number in metrics.items():
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", name):
            continue
        if number is None:
            event["metrics"][name] = None
        elif type(number) in (int, float):
            try:
                event["metrics"][name] = number if math.isfinite(number) else None
            except OverflowError:
                event["metrics"][name] = None
    phase = value.get("phase")
    if isinstance(phase, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", phase):
        event["phase"] = phase
    name = value.get("name")
    if isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,199}", name):
        event["name"] = name
    digest = value.get("sha256")
    if isinstance(digest, str) and re.fullmatch(r"[a-f0-9]{64}", digest):
        event["sha256"] = digest
    reason = value.get("reason")
    if isinstance(reason, str):
        event["reason"] = reason[:500]
    return event


def ingest_chunk(store, run_id: str, response: dict) -> dict:
    """Validate a bounded complete-line response and atomically advance it."""
    if not isinstance(response, dict):
        raise TelemetryError("Invalid training progress response")
    offset, next_offset, text = response.get("offset"), response.get("next_offset"), response.get("text")
    if (type(offset) is not int or type(next_offset) is not int or offset < 0
            or not isinstance(text, str) or type(response.get("eof")) is not bool):
        raise TelemetryError("Invalid training progress cursor")
    raw = text.encode("utf-8")
    if len(raw) > CHUNK_BYTES or next_offset != offset + len(raw) or (raw and not raw.endswith(b"\n")):
        raise TelemetryError("Incomplete or oversized training progress response")
    parsed, invalid = [], 0
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            event = normalize_event(json.loads(line)) if len(line) <= MAX_EVENT_BYTES else None
        except (ValueError, UnicodeError, RecursionError):
            event = None
        if event is None:
            invalid += 1
        else:
            parsed.append(event)
    with store.connection(write=True) as con:
        row = con.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(run_id)
        run = store._run(row)
        current = run.get("training_cursor", 0)
        if offset != current:
            if next_offset <= current:
                return run  # Already committed response retried after a restart.
            raise TelemetryError("Training progress cursor does not match saved state")
        summary = dict(run.get("training") or {})
        for event in parsed:
            cursor = con.execute(
                "INSERT OR IGNORE INTO training_events(run_id,seq,kind,step,timestamp,payload) VALUES (?,?,?,?,?,?)",
                (run_id, event["seq"], event["kind"], event.get("step"), event["timestamp"], json.dumps(event, allow_nan=False)),
            )
            if not cursor.rowcount:
                continue
            summary["event_count"] = summary.get("event_count", 0) + 1
            if event["seq"] <= summary.get("last_seq", 0):
                continue
            summary.update(last_seq=event["seq"], last_event_at=event["timestamp"])
            # Final evaluation may load the best checkpoint from an earlier
            # step. Keep that event's step while preserving total train progress.
            if "step" in event:
                summary["step"] = max(summary.get("step", 0), event["step"])
            for field in ("total_steps", "optimizer_updates", "phase"):
                if field in event:
                    summary[field] = event[field]
            kind = event["kind"]
            if kind == "step":
                summary.update(latest_metrics=event["metrics"], phase="training")
                updates = event["metrics"].get("optimizer_updates")
                if type(updates) is int and updates >= 0:
                    summary["optimizer_updates"] = updates
            elif kind == "evaluation":
                summary["latest_evaluation"] = event
            elif kind == "checkpoint" and event.get("name"):
                checkpoints = {p["name"]: p for p in summary.get("checkpoints", []) if p.get("name")}
                checkpoints[event["name"]] = {k: event[k] for k in ("name", "step", "timestamp", "seq", "size", "sha256") if k in event}
                summary["checkpoints"] = sorted(checkpoints.values(), key=lambda p: p["seq"], reverse=True)[:100]
            elif kind == "complete":
                summary["complete_reason"] = event.get("reason", "")
                if event.get("reason") in {"completed", "budget_exhausted", "failed"}:
                    summary["phase"] = event["reason"]
        if invalid:
            summary["invalid_events"] = summary.get("invalid_events", 0) + invalid
        run.update(training=summary, training_cursor=next_offset, telemetry_eof=response["eof"], telemetry_error=None, updated_at=utcnow())
        con.execute("UPDATE runs SET updated_at=?,data=? WHERE id=?", (run["updated_at"], json.dumps(run, allow_nan=False), run_id))
    return run


def sync_training_telemetry(settings, store, adapter, run: dict) -> dict:
    if (run.get("config") or {}).get("job_type") != "dmasif_train":
        return run
    for _ in range(4):
        response = adapter.telemetry(run, offset=run.get("training_cursor", 0), max_bytes=CHUNK_BYTES)
        previous = run.get("training_cursor", 0)
        run = ingest_chunk(store, run["id"], response)
        if response["eof"] or run.get("training_cursor", 0) == previous:
            break
    return run


def event_page(store, run_id: str, *, kind: str | None = None, before_seq: int | None = None,
               step: int | None = None, limit: int = 50) -> dict:
    if kind is not None and kind not in KINDS:
        raise ValueError("Unknown training event kind")
    limit = max(1, min(200, limit))
    terms, values = ["run_id=?"], [run_id]
    if kind:
        terms.append("kind=?")
        values.append(kind)
    if step is not None:
        terms.append("step=?")
        values.append(step)
    where = " AND ".join(terms)
    with store.connection() as con:
        total = con.execute("SELECT count(*) FROM training_events WHERE " + where, values).fetchone()[0]
        if before_seq is not None:
            where += " AND seq<?"
            values.append(before_seq)
        rows = con.execute("SELECT payload FROM training_events WHERE " + where + " ORDER BY seq DESC LIMIT ?",
                           (*values, limit + 1)).fetchall()
    events = [json.loads(row[0]) for row in rows[:limit]]
    return {"events": events, "total": total,
            "next_before_seq": events[-1]["seq"] if len(rows) > limit else None}


def iter_events(store, run_id: str):
    """Stream retained events with bounded reads and no long-held DB connection."""
    after = 0
    with store.connection() as con:
        last = con.execute("SELECT max(seq) FROM training_events WHERE run_id=?", (run_id,)).fetchone()[0] or 0
    while after < last:
        with store.connection() as con:
            rows = con.execute("SELECT seq,payload FROM training_events WHERE run_id=? AND seq>? AND seq<=? ORDER BY seq LIMIT 200",
                               (run_id, after, last)).fetchall()
        if not rows:
            break
        for row in rows:
            yield json.loads(row["payload"])
        after = rows[-1]["seq"]
