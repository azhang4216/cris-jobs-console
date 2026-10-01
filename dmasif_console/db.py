"""Four-table durable state store; transactions protect acceptance and capacity."""
from __future__ import annotations

import json
import hashlib
import sqlite3
import uuid
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path


TERMINAL_STATES = {"REJECTED", "PREPARATION_FAILED", "SUCCEEDED", "COMPLETED", "PARTIAL", "FAILED", "CANCELLED", "TIMED_OUT"}


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class DeliveryConflict(ValueError):
    """One delivery identity was reused for different signed bytes."""


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    @contextmanager
    def connection(self, *, write: bool = False):
        con = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys = ON")
        con.execute("PRAGMA busy_timeout = 15000")
        try:
            if write:
                con.execute("BEGIN IMMEDIATE")
            yield con
            if write:
                con.commit()
        except BaseException:
            if write:
                con.rollback()
            raise
        finally:
            con.close()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.connection() as con:
            con.execute("PRAGMA journal_mode = WAL")
            version = con.execute("PRAGMA user_version").fetchone()[0]
            if version > 1:
                raise RuntimeError("database schema is newer than this service")
            sql = (Path(__file__).parent / "migrations/001_initial.sql").read_text()
            con.executescript(sql)
        self.path.chmod(0o600)

    @staticmethod
    def _run(row) -> dict | None:
        if row is None:
            return None
        data = json.loads(row["data"])
        data.update({k: row[k] for k in ("id", "state", "created_at", "updated_at")})
        data["capacity_reserved"] = bool(row["capacity_reserved"])
        data.setdefault("display_id", "run-" + row["id"][:8])
        return data

    @staticmethod
    def _event(con, run_id, kind, detail, actor="worker"):
        con.execute(
            "INSERT INTO events(run_id,created_at,kind,actor,detail) VALUES (?,?,?,?,?)",
            (run_id, utcnow(), kind, actor, json.dumps(detail)),
        )

    def accept_delivery(self, hook_id: str, delivery_id: str, body_hash: str, payload: dict,
                        decision: str, reason: str, run_data: dict | None = None,
                        queue_limit: int = 100) -> dict:
        now = utcnow()
        with self.connection(write=True) as con:
            same_id = con.execute("SELECT * FROM deliveries WHERE hook_id=? AND delivery_id=?",
                                  (hook_id, delivery_id)).fetchone()
            if same_id:
                if same_id["body_hash"] != body_hash:
                    raise DeliveryConflict("delivery ID reused with different payload")
                return {"decision": same_id["decision"], "run_id": same_id["run_id"], "duplicate": True}
            same_body = con.execute("SELECT * FROM deliveries WHERE body_hash=? AND canonical=1",
                                    (body_hash,)).fetchone()
            if same_body:
                con.execute(
                    "INSERT INTO deliveries(hook_id,delivery_id,body_hash,canonical,received_at,decision,reason,run_id,payload) "
                    "VALUES (?,?,?,0,?,'DUPLICATE','Verified payload already received',?,?)",
                    (hook_id, delivery_id, body_hash, now, same_body["run_id"], "{}"),
                )
                return {"decision": same_body["decision"], "run_id": same_body["run_id"], "duplicate": True}
            run_id = str(uuid.uuid4()) if run_data is not None else None
            data = dict(run_data or {})
            if run_id:
                pending = con.execute("SELECT COUNT(*) FROM runs WHERE state NOT IN "
                                      "('REJECTED','PREPARATION_FAILED','SUCCEEDED','COMPLETED','PARTIAL','FAILED','CANCELLED','TIMED_OUT')"
                                      ).fetchone()[0]
                if pending >= queue_limit:
                    data.update(state="REJECTED", reason="Application queue is full. Try a new push later.")
                    decision, reason = "REJECTED", data["reason"]
            cursor = con.execute(
                "INSERT INTO deliveries(hook_id,delivery_id,body_hash,received_at,decision,reason,run_id,payload) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (hook_id, delivery_id, body_hash, now, decision, reason, run_id, json.dumps(payload)),
            )
            if run_id:
                data.update(id=run_id, hook_id=hook_id, delivery_id=delivery_id, body_hash=body_hash,
                            created_at=now, updated_at=now, capacity_reserved=False)
                data.setdefault("state", "CHECKING_REQUEST")
                data.setdefault("reason", reason)
                data.setdefault("trigger", {"hook_id": hook_id, "delivery_id": delivery_id, "body_hash": body_hash})
                con.execute("INSERT INTO runs VALUES (?,?,?,?,?,?,?)",
                            (run_id, cursor.lastrowid, data["state"], 0, now, now, json.dumps(data)))
                self._event(con, run_id, "RECEIVED", {"reason": reason, "delivery_id": delivery_id}, "github")
            return {"decision": decision, "run_id": run_id, "duplicate": False}

    def get_run(self, run_id: str) -> dict | None:
        with self.connection() as con:
            return self._run(con.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone())

    def list_runs(self, states=None, limit: int = 200, actor=None, status=None, experiment=None) -> list[dict]:
        terms, values = [], []
        if states:
            states = list(states)
            terms.append("state IN (" + ",".join("?" for _ in states) + ")")
            values.extend(states)
        if status:
            terms.append("state=?")
            values.append(status)
        if actor:
            terms.append("json_extract(data,'$.actor_login') = ? COLLATE NOCASE")
            values.append(actor)
        if experiment:
            terms.append("instr(lower(json_extract(data,'$.experiment')), lower(?)) > 0")
            values.append(experiment)
        sql = "SELECT * FROM runs" + (" WHERE " + " AND ".join(terms) if terms else "")
        sql += " ORDER BY created_at DESC, rowid DESC LIMIT ?"
        with self.connection() as con:
            return [self._run(row) for row in con.execute(sql, (*values, limit))]

    def update_run(self, run_id: str, patch: dict, event=None) -> dict:
        with self.connection(write=True) as con:
            old = self._run(con.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone())
            if old is None:
                raise KeyError(run_id)
            data = {**old, **patch, "id": run_id, "created_at": old["created_at"], "updated_at": utcnow()}
            con.execute("UPDATE runs SET state=?,capacity_reserved=?,updated_at=?,data=? WHERE id=?",
                        (data["state"], int(data["capacity_reserved"]), data["updated_at"], json.dumps(data), run_id))
            if event or data["state"] != old["state"]:
                kind = event if isinstance(event, str) else (event or {}).get("kind", "STATE_CHANGED")
                detail = event.get("detail", event) if isinstance(event, dict) else {"from": old["state"], "to": data["state"], "reason": data.get("reason", "")}
                self._event(con, run_id, kind, detail)
            return data

    def reserve_next(self) -> dict | None:
        with self.connection(write=True) as con:
            control = con.execute("SELECT detail FROM events WHERE kind='CONTROL' ORDER BY id DESC LIMIT 1").fetchone()
            if control and json.loads(control["detail"]).get("paused"):
                return None
            if con.execute("SELECT 1 FROM runs WHERE capacity_reserved=1 LIMIT 1").fetchone():
                return None
            row = con.execute("SELECT * FROM runs WHERE state='WAITING_FOR_CAPACITY' ORDER BY created_at,rowid LIMIT 1").fetchone()
            if row is None:
                return None
            data = self._run(row)
            data.update(state="PREPARING", capacity_reserved=True, updated_at=utcnow(), reason="Preparing isolated inputs and source")
            con.execute("UPDATE runs SET state=?,capacity_reserved=1,updated_at=?,data=? WHERE id=?",
                        (data["state"], data["updated_at"], json.dumps(data), data["id"]))
            self._event(con, data["id"], "CAPACITY_RESERVED", {"state": "PREPARING"})
            return data

    def upsert_job(self, run_id: str, job: dict) -> None:
        job = {**job, "job_id": str(job["job_id"]), "cluster": job.get("cluster", "default")}
        with self.connection(write=True) as con:
            existing = con.execute("SELECT * FROM scheduler_jobs WHERE cluster=? AND job_id=?",
                                   (job["cluster"], job["job_id"])).fetchone()
            if existing and existing["run_id"] != run_id:
                raise ValueError("scheduler job already belongs to another run")
            if existing:
                merged = {**json.loads(existing["data"]), **job}
                con.execute("UPDATE scheduler_jobs SET data=? WHERE id=?", (json.dumps(merged), existing["id"]))
            else:
                con.execute("INSERT INTO scheduler_jobs(run_id,cluster,job_id,data) VALUES (?,?,?,?)",
                            (run_id, job["cluster"], job["job_id"], json.dumps(job)))

    def upsert_observed_run(self, cluster: str, job: dict, observed_at: str) -> dict:
        """Cache an existing allocation without inventing a submission or source.

        Slurm can reuse job IDs. The original submission time identifies the
        allocation incarnation and also namespaces the scheduler-job record.
        """
        identity = json.dumps([cluster, job["job_id"], job["submitted_at"]], separators=(",", ":"))
        run_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "slurm-observation:" + identity))
        cluster_key = "observed-" + hashlib.sha256(identity.encode()).hexdigest()
        now = utcnow()
        run = {
            "id": run_id, "display_id": "slurm-" + job["job_id"], "source_kind": "slurm", "source": {},
            "experiment": job["job_name"], "state": job["state"], "reason": job["reason"],
            "actor_login": None, "actor_id": None, "commit_sha": None, "commit_url": None,
            "branch": None, "dataset_id": None, "capacity_reserved": False,
            "created_at": job["submitted_at"], "updated_at": now,
            "submitted_at": job["submitted_at"], "started_at": job.get("started_at"),
            "ended_at": job.get("ended_at"), "last_observed_at": observed_at,
            "scheduler_job_id": job["job_id"], "scheduler_state": job["scheduler_state"], "log_tail": "", "artifacts": [],
            "validation": {}, "provenance": {}, "config": {},
        }
        scheduler_job = {
            "job_id": job["job_id"], "cluster": cluster_key, "state": job["scheduler_state"],
            "exit_code": job.get("exit_code"), "submitted_at": job["submitted_at"],
            "started_at": job.get("started_at"), "ended_at": job.get("ended_at"),
            "observed_at": observed_at,
        }
        with self.connection(write=True) as con:
            old = self._run(con.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone())
            if old is not None and old.get("source_kind") != "slurm":
                raise ValueError("observation identity conflicts with a submitted run")
            con.execute(
                "INSERT INTO runs(id,delivery_row_id,state,capacity_reserved,created_at,updated_at,data) "
                "VALUES (?,NULL,?,0,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                "state=excluded.state,updated_at=excluded.updated_at,data=excluded.data",
                (run_id, run["state"], run["created_at"], now, json.dumps(run)),
            )
            con.execute(
                "INSERT INTO scheduler_jobs(run_id,cluster,job_id,data) VALUES (?,?,?,?) "
                "ON CONFLICT(cluster,job_id) DO UPDATE SET data=excluded.data",
                (run_id, cluster_key, job["job_id"], json.dumps(scheduler_job)),
            )
            if old is None or old["state"] != run["state"]:
                self._event(con, run_id, "OBSERVED", {"reason": run["reason"]}, "observer")
        return run

    def record_observation(self, *, observed_at: str | None, error: str = "", job_count: int = 0) -> dict:
        """Persist one current health record, keeping the last successful check."""
        with self.connection(write=True) as con:
            row = con.execute("SELECT id,detail FROM events WHERE kind='OBSERVATION' ORDER BY id DESC LIMIT 1").fetchone()
            previous = json.loads(row["detail"]) if row else {}
            state = {
                "observed_at": observed_at or previous.get("observed_at"),
                "last_attempt_at": utcnow(), "error": error,
                "job_count": job_count if observed_at else previous.get("job_count", 0),
            }
            if row:
                con.execute("UPDATE events SET created_at=?,detail=? WHERE id=?",
                            (state["last_attempt_at"], json.dumps(state), row["id"]))
            else:
                self._event(con, None, "OBSERVATION", state, "observer")
            return state

    def observation_state(self) -> dict:
        with self.connection() as con:
            row = con.execute("SELECT detail FROM events WHERE kind='OBSERVATION' ORDER BY id DESC LIMIT 1").fetchone()
            return json.loads(row[0]) if row else {
                "observed_at": None, "last_attempt_at": None, "error": "", "job_count": 0,
            }

    def jobs(self, run_id: str) -> list[dict]:
        with self.connection() as con:
            return [json.loads(r["data"]) for r in con.execute("SELECT data FROM scheduler_jobs WHERE run_id=? ORDER BY id", (run_id,))]

    def add_event(self, run_id, kind, detail, actor="worker"):
        with self.connection(write=True) as con:
            self._event(con, run_id, kind, detail, actor)

    def events(self, run_id: str) -> list[dict]:
        with self.connection() as con:
            return [{**dict(r), "detail": json.loads(r["detail"])} for r in con.execute(
                "SELECT * FROM events WHERE run_id=? ORDER BY id DESC LIMIT 200", (run_id,))]

    def deliveries(self, limit=20) -> list[dict]:
        with self.connection() as con:
            return [{**dict(r), "payload": json.loads(r["payload"])} for r in con.execute(
                "SELECT * FROM deliveries WHERE canonical=1 ORDER BY id DESC LIMIT ?", (limit,))]

    def control_state(self) -> dict:
        with self.connection() as con:
            row = con.execute("SELECT detail FROM events WHERE kind='CONTROL' ORDER BY id DESC LIMIT 1").fetchone()
            return json.loads(row[0]) if row else {"paused": False, "reason": ""}

    def set_paused(self, paused: bool, reason: str, actor="operator"):
        self.add_event(None, "CONTROL", {"paused": paused, "reason": reason, "actor": actor, "at": utcnow()}, actor)

    def stats(self) -> dict:
        with self.connection() as con:
            rows = dict(con.execute("SELECT state,COUNT(*) FROM runs GROUP BY state"))
        return {"total": sum(rows.values()), "active": sum(n for s, n in rows.items() if s not in TERMINAL_STATES and s != "NEEDS_REVIEW"),
                "succeeded": rows.get("SUCCEEDED", 0), "attention": sum(rows.get(s, 0) for s in ("FAILED", "PARTIAL", "REJECTED", "PREPARATION_FAILED", "NEEDS_REVIEW", "SUBMISSION_UNKNOWN", "TIMED_OUT"))}

    def backup(self, path: str | Path) -> None:
        target = Path(path)
        if target.resolve() == self.path.resolve():
            raise ValueError("backup destination must differ from live database")
        target.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as source, closing(sqlite3.connect(target)) as destination:
            source.backup(destination)
            # A recovery snapshot is a standalone file, not a live WAL database.
            destination.execute("PRAGMA journal_mode = DELETE")
        target.chmod(0o600)

    def restore_run(self, run: dict, jobs: list[dict]) -> None:
        """Recover historical identity without launching work. Caller keeps service paused."""
        run = dict(run)
        run_id = str(uuid.UUID(run["id"]))
        trigger = run.get("trigger", {})
        hook = run.get("hook_id") or trigger["hook_id"]
        delivery = run.get("delivery_id") or trigger["delivery_id"]
        body_hash = run.get("body_hash") or trigger["body_hash"]
        now = utcnow()
        with self.connection(write=True) as con:
            existing = con.execute("SELECT * FROM deliveries WHERE (hook_id=? AND delivery_id=?) OR (body_hash=? AND canonical=1)",
                                   (hook, delivery, body_hash)).fetchall()
            if any(r["run_id"] != run_id or r["body_hash"] != body_hash for r in existing):
                raise DeliveryConflict("restore evidence conflicts with saved delivery/run identity")
            if not existing:
                cur = con.execute("INSERT INTO deliveries(hook_id,delivery_id,body_hash,received_at,decision,reason,run_id,payload) VALUES (?,?,?,?,?,?,?,?)",
                                  (hook, delivery, body_hash, run.get("created_at", now), "ACCEPTED", "Recovered from remote evidence", run_id, "{}"))
                row_id = cur.lastrowid
            else:
                row_id = existing[0]["id"]
            old = self._run(con.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone())
            if old is None:
                run.update(id=run_id, hook_id=hook, delivery_id=delivery, body_hash=body_hash,
                           state="NEEDS_REVIEW", capacity_reserved=False,
                           created_at=run.get("created_at", now), updated_at=now,
                           reason="Recovered from remote evidence. Reconcile before resuming submissions.")
                con.execute("INSERT INTO runs VALUES (?,?,?,?,?,?,?)", (run_id, row_id, run["state"], 0, run["created_at"], now, json.dumps(run)))
                self._event(con, run_id, "RESTORED", {"reason": run["reason"]}, "operator")
            else:
                if old.get("commit_sha") and run.get("commit_sha") != old["commit_sha"]:
                    raise DeliveryConflict("restored source identity differs from saved run")
                # A backup may predate source preparation. Recover immutable remote
                # configuration, but keep relocated local archives/cache metadata.
                merged = {**old, **run}
                for key in ("source", "artifacts", "log_tail"):
                    if old.get(key):
                        merged[key] = old[key]
                merged.update(id=run_id, hook_id=hook, delivery_id=delivery, body_hash=body_hash,
                              state="NEEDS_REVIEW", capacity_reserved=False,
                              created_at=old["created_at"], updated_at=now,
                              reason="Restore evidence refreshed. Reconcile before resuming submissions.")
                con.execute("UPDATE runs SET state=?,capacity_reserved=0,updated_at=?,data=? WHERE id=?",
                            (merged["state"], now, json.dumps(merged), run_id))
                self._event(con, run_id, "RESTORED", {"reason": merged["reason"]}, "operator")
        for job in jobs:
            self.upsert_job(run_id, job)
