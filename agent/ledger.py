"""SQLite ledger: runs, per-record items (the checkpoint), and an event log.

Item status lifecycle:
  planned -> (approve) -> pending -> in_flight -> created -> verified
                                   \\-> failed           \\-> failed (verification mismatch)
  planned -> (approve) -> skipped   (duplicates, already present, needs review, invalid)
"in_flight" is written BEFORE the browser clicks Save, so a crash mid-save is
detected on retry and reconciled against the console instead of re-submitted.
"""
import json
import sqlite3
import threading
import time
from datetime import datetime, timezone

import config

_lock = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY, created_at TEXT, tenant TEXT, scenario TEXT, approval_ref TEXT, csv_name TEXT,
  status TEXT, stage TEXT, plan_hash TEXT, approved_hash TEXT, approved_by TEXT, approved_at TEXT,
  admin_grants_approved INTEGER DEFAULT 0, apply_attempts INTEGER DEFAULT 0, data TEXT DEFAULT '{}',
  live_shot TEXT, error TEXT
);
CREATE TABLE IF NOT EXISTS items (
  run_id TEXT, record_id TEXT, line INTEGER, name TEXT, email TEXT, csv_role TEXT, department TEXT,
  target_role TEXT, action TEXT, reason TEXT, batch INTEGER, status TEXT, attempts INTEGER DEFAULT 0,
  created_in_attempt INTEGER, evidence TEXT DEFAULT '{}', updated_at TEXT,
  PRIMARY KEY (run_id, record_id)
);
CREATE TABLE IF NOT EXISTS role_policy (
  tenant TEXT, role_key TEXT, csv_role TEXT, console_role TEXT, decided_by TEXT, approved_by TEXT,
  approved_at TEXT, run_id TEXT, times_used INTEGER DEFAULT 1, PRIMARY KEY (tenant, role_key)
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, ts TEXT, level TEXT, step TEXT, message TEXT, data TEXT
);
"""


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


with _lock:
    _c = _conn()
    _c.executescript(SCHEMA)
    _c.commit()
    _c.close()


def _exec(sql: str, params=()) -> None:
    with _lock:
        conn = _conn()
        conn.execute(sql, params)
        conn.commit()
        conn.close()


def _query(sql: str, params=()) -> list[dict]:
    with _lock:
        conn = _conn()
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
        conn.close()
    return rows


# ------------------------------------------------------------------ runs
def create_run(tenant: str, scenario: str | None, approval_ref: str, csv_name: str, data: dict) -> str:
    run_id = time.strftime("run-%Y%m%d-%H%M%S-") + f"{int(time.time() * 1000) % 1000:03d}"
    _exec("INSERT INTO runs (id, created_at, tenant, scenario, approval_ref, csv_name, status, stage, data) "
          "VALUES (?,?,?,?,?,?,?,?,?)",
          (run_id, now(), tenant, scenario, approval_ref, csv_name, "collected", "collect", json.dumps(data)))
    return run_id


def get_run(run_id: str) -> dict | None:
    rows = _query("SELECT * FROM runs WHERE id=?", (run_id,))
    if not rows:
        return None
    run = rows[0]
    run["data"] = json.loads(run["data"] or "{}")
    return run


def list_runs(limit: int = 50) -> list[dict]:
    return _query("SELECT id, created_at, tenant, scenario, status, stage, csv_name FROM runs "
                  "ORDER BY created_at DESC, id DESC LIMIT ?", (limit,))


def update_run(run_id: str, **fields) -> None:
    if "data" in fields:
        fields["data"] = json.dumps(fields["data"])
    cols = ", ".join(f"{k}=?" for k in fields)
    _exec(f"UPDATE runs SET {cols} WHERE id=?", (*fields.values(), run_id))


def merge_run_data(run_id: str, **values) -> dict:
    with _lock:
        run = get_run(run_id)
        data = run["data"]
        data.update(values)
        update_run(run_id, data=data)
        return data


# ------------------------------------------------------------------ items
def insert_items(run_id: str, records: list[dict]) -> None:
    with _lock:
        conn = _conn()
        for r in records:
            conn.execute(
                "INSERT INTO items (run_id, record_id, line, name, email, csv_role, department, status, "
                "evidence, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (run_id, r["record_id"], r["line"], r["name"], r["email"], r["csv_role"], r["department"],
                 "collected", json.dumps({"input": r}), now()))
        conn.commit()
        conn.close()


def get_items(run_id: str) -> list[dict]:
    items = _query("SELECT * FROM items WHERE run_id=? ORDER BY line", (run_id,))
    for it in items:
        it["evidence"] = json.loads(it["evidence"] or "{}")
    return items


def update_item(run_id: str, record_id: str, evidence_patch: dict | None = None, **fields) -> None:
    with _lock:
        if evidence_patch:
            row = _query("SELECT evidence FROM items WHERE run_id=? AND record_id=?", (run_id, record_id))
            ev = json.loads(row[0]["evidence"] or "{}") if row else {}
            ev.update(evidence_patch)
            fields["evidence"] = json.dumps(ev)
        fields["updated_at"] = now()
        cols = ", ".join(f"{k}=?" for k in fields)
        _exec(f"UPDATE items SET {cols} WHERE run_id=? AND record_id=?", (*fields.values(), run_id, record_id))


# ------------------------------------------------------------------ events
def log(run_id: str, step: str, message: str, data: dict | None = None, level: str = "info") -> None:
    _exec("INSERT INTO events (run_id, ts, level, step, message, data) VALUES (?,?,?,?,?,?)",
          (run_id, now(), level, step, message, json.dumps(data or {})))


def get_events(run_id: str, after_id: int = 0) -> list[dict]:
    evs = _query("SELECT * FROM events WHERE run_id=? AND id>? ORDER BY id", (run_id, after_id))
    for e in evs:
        e["data"] = json.loads(e["data"] or "{}")
    return evs


# ------------------------------------------------------------------ role policy (learned from approvals)
def _role_key(csv_role: str) -> str:
    return " ".join((csv_role or "").lower().split())


def get_role_policy(tenant: str) -> list[dict]:
    return _query("SELECT * FROM role_policy WHERE tenant=? ORDER BY csv_role", (tenant,))


def policy_for_roles(tenant: str, csv_roles: list[str]) -> dict:
    by_key = {r["role_key"]: r for r in get_role_policy(tenant)}
    return {role: by_key[_role_key(role)] for role in csv_roles if _role_key(role) in by_key}


def save_role_policy(tenant: str, csv_role: str, console_role: str, decided_by: str,
                     approved_by: str, run_id: str) -> None:
    _exec("INSERT INTO role_policy (tenant, role_key, csv_role, console_role, decided_by, approved_by, approved_at, run_id) "
          "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(tenant, role_key) DO UPDATE SET "
          "times_used = CASE WHEN role_policy.console_role = excluded.console_role THEN role_policy.times_used + 1 ELSE 1 END, "
          "decided_by = CASE WHEN role_policy.console_role = excluded.console_role THEN role_policy.decided_by ELSE excluded.decided_by END, "
          "console_role=excluded.console_role, csv_role=excluded.csv_role, approved_by=excluded.approved_by, "
          "approved_at=excluded.approved_at, run_id=excluded.run_id",
          (tenant, _role_key(csv_role), csv_role, console_role, decided_by, approved_by, now(), run_id))


def clear_role_policy(tenant: str, csv_role: str | None = None) -> None:
    if csv_role:
        _exec("DELETE FROM role_policy WHERE tenant=? AND role_key=?", (tenant, _role_key(csv_role)))
    else:
        _exec("DELETE FROM role_policy WHERE tenant=?", (tenant,))
