"""Dashboard API + static UI for the Bulk User Provisioning Agent."""
import json
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402
from agent import ledger, llm, pipeline  # noqa: E402
from scenarios.definitions import SCENARIOS, apply_console_config, csv_text, setup_console  # noqa: E402

STATIC = Path(__file__).resolve().parent / "static"
app = FastAPI(title="Bulk User Provisioning Agent")
app.mount("/static", StaticFiles(directory=STATIC), name="static")
app.mount("/files", StaticFiles(directory=config.RUNS_DIR), name="files")

_jobs: dict[str, threading.Thread] = {}


def _background(run_id: str, fn, *args) -> None:
    job = _jobs.get(run_id)
    if job and job.is_alive():
        raise HTTPException(409, "This run already has a job in progress")

    def runner():
        try:
            fn(run_id, *args)
        except Exception as exc:  # surfaced in the run log
            ledger.update_run(run_id, status="error", error=str(exc))
            ledger.log(run_id, "error", f"Stopped: {exc}", level="error")
    t = threading.Thread(target=runner, daemon=True)
    _jobs[run_id] = t
    t.start()


PENDING_HOLD_SECONDS = 15 * 60  # a scenario plan waiting for approval holds the shared console this long


def _console_guard(run_id: str | None = None, check_pending: bool = True) -> None:
    """The mock console is shared by everyone using the demo. Refuse to start a new run that would
    reset or change it while another run is working, or while another reviewer's test case plan is
    still waiting for approval (stale plans stop holding it after 15 minutes)."""
    for rid, job in _jobs.items():
        if job.is_alive() and rid != run_id:
            run = ledger.get_run(rid) or {}
            raise HTTPException(409, f"Another run ({run.get('scenario') or run.get('csv_name') or rid}) is in "
                                     f"progress on the shared demo console. Please try again in a minute or two.")
    if not check_pending:
        return
    now = datetime.now(timezone.utc)
    for r in ledger.list_runs(limit=20):
        if r["id"] == run_id or not r["scenario"] or r["status"] != "awaiting_approval":
            continue
        created = datetime.strptime(r["created_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        if (now - created).total_seconds() < PENDING_HOLD_SECONDS:
            raise HTTPException(409, f"Another reviewer's {r['scenario']} plan is waiting for approval on the shared "
                                     f"demo console. Open it from Run history, or try again in a few minutes.")


def _shot_url(path: str | None) -> str | None:
    if not path:
        return None
    try:
        return "/files/" + Path(path).relative_to(config.RUNS_DIR).as_posix()
    except ValueError:
        return None


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/info")
def info():
    return {"llm": llm.provider_label(), "console_url": config.CONSOLE_PUBLIC_URL, "batch_size": config.BATCH_SIZE,
            "scenarios": [{"id": k, **{f: v[f] for f in ("title", "expected", "csv")}} for k, v in SCENARIOS.items()]}


@app.get("/api/runs")
def runs():
    out = []
    for r in ledger.list_runs():
        full = ledger.get_run(r["id"])
        r["verdict"] = (full["data"].get("scenario_result") or {}).get("verdict")
        out.append(r)
    return out


@app.post("/api/runs")
def new_run(tenant: str = Form(...), approval_ref: str = Form(...), csv_file: UploadFile = File(...)):
    _console_guard()
    text = csv_file.file.read().decode("utf-8", errors="replace")
    try:
        run_id = pipeline.collect(tenant, text, csv_file.filename or "upload.csv", approval_ref)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    _background(run_id, pipeline.plan)
    return {"run_id": run_id}


@app.post("/api/scenarios/{tc}")
def start_scenario(tc: str):
    tc = tc.upper()
    if tc not in SCENARIOS:
        raise HTTPException(404, "Unknown scenario")
    _console_guard()
    try:
        setup_console(tc)
    except Exception as exc:
        raise HTTPException(502, f"Could not reach mock console test harness: {exc}")
    run_id = pipeline.collect("acme", csv_text(tc), SCENARIOS[tc]["csv"], f"SCENARIO-{tc}", scenario=tc)
    _background(run_id, pipeline.plan)
    return {"run_id": run_id}


@app.get("/api/runs/{run_id}")
def run_detail(run_id: str, after: int = 0):
    rep = pipeline.report(run_id) if ledger.get_run(run_id) else None
    if not rep:
        raise HTTPException(404, "Run not found")
    run = rep["run"]
    data = run["data"]
    for it in rep["items"]:
        ev = it["evidence"]
        shots = []
        for k, v in ev.items():
            if k.startswith("action_attempt_"):
                shots += [_shot_url(s) for s in v.get("screenshots", [])]
        it["screenshots"] = [s for s in shots if s]
    job = _jobs.get(run_id)
    return JSONResponse({
        "run": {k: run[k] for k in ("id", "created_at", "tenant", "scenario", "approval_ref", "csv_name", "status",
                                     "stage", "plan_hash", "approved_hash", "approved_by", "approved_at",
                                     "admin_grants_approved", "apply_attempts", "error")},
        "busy": bool(job and job.is_alive()),
        "live_shot": _shot_url(run.get("live_shot")),
        "tenant_info": data.get("tenant_info"),
        "field_map": data.get("field_map"),
        "console_roles": data.get("console_roles", []),
        "role_mappings": data.get("role_mappings", {}),
        "role_source": data.get("role_source"),
        "plan_counts": data.get("plan_counts"),
        "summary": data.get("summary"),
        "attempt_summaries": data.get("attempt_summaries", []),
        "scenario_result": data.get("scenario_result"),
        "scenario_info": SCENARIOS.get(run["scenario"]) if run["scenario"] else None,
        "items": rep["items"],
        "events": ledger.get_events(run_id, after),
        "llm": data.get("llm_provider"),
    })


@app.get("/api/policy/{tenant}")
def get_policy(tenant: str):
    return ledger.get_role_policy(tenant.strip().lower())


@app.delete("/api/policy/{tenant}")
def clear_policy(tenant: str, csv_role: str | None = None):
    ledger.clear_role_policy(tenant.strip().lower(), csv_role)
    return {"ok": True}


class RoleOverride(BaseModel):
    csv_role: str
    console_role: str


@app.post("/api/runs/{run_id}/roles")
def set_role(run_id: str, body: RoleOverride):
    try:
        pipeline.override_role(run_id, body.csv_role, body.console_role)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True}


class Approval(BaseModel):
    approver: str
    plan_hash: str
    admin_grants: bool = False


@app.post("/api/runs/{run_id}/approve")
def approve(run_id: str, body: Approval):
    _console_guard(run_id, check_pending=False)
    try:
        pipeline.approve(run_id, body.approver, body.plan_hash, body.admin_grants)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    run = ledger.get_run(run_id)
    if run["scenario"]:
        try:  # make sure this test case's console switches are still the ones it was planned with
            apply_console_config(run["scenario"])
        except Exception as exc:
            ledger.log(run_id, "apply", f"Could not re-assert scenario console settings: {exc}", level="warn")
    _background(run_id, pipeline.apply)
    return {"ok": True}


@app.post("/api/runs/{run_id}/retry")
def retry(run_id: str):
    run = ledger.get_run(run_id)
    if not run or run["status"] not in ("needs_retry", "error"):
        raise HTTPException(400, "Nothing to retry")
    _console_guard(run_id, check_pending=False)
    _background(run_id, pipeline.retry)
    return {"ok": True}


@app.get("/api/runs/{run_id}/input.csv", response_class=PlainTextResponse)
def run_input(run_id: str):
    run = ledger.get_run(run_id)
    if not run:
        raise HTTPException(404, "Run not found")
    return PlainTextResponse(run["data"].get("csv_text") or "(input not stored for this run)")


@app.get("/api/runs/{run_id}/evidence.json")
def run_evidence(run_id: str):
    if not ledger.get_run(run_id):
        raise HTTPException(404, "Run not found")
    body = json.dumps(pipeline.evidence(run_id), indent=2, default=str)
    return PlainTextResponse(body, media_type="application/json",
                             headers={"Content-Disposition": f'attachment; filename="{run_id}-evidence.json"'})


@app.get("/healthz")
def healthz():
    """Lightweight endpoint for an uptime pinger, so the free server stays awake during judging."""
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=config.DASHBOARD_PORT)
