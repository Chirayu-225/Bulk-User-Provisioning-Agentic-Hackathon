"""Runs the official test cases end to end from the terminal and writes evidence files.

  python run_scenarios.py              # all four, asks for approval in the terminal
  python run_scenarios.py TC3          # one scenario
  python run_scenarios.py --yes        # scripted approval (recorded as such in the evidence)
  python run_scenarios.py --fresh      # forget the saved role policy first (LLM decides every title again)

Needs the mock console running (python run.py, or python mock_console/app.py).
"""
import json
import sys

import requests

import config
from agent import ledger, pipeline
from scenarios.definitions import SCENARIOS, csv_text, setup_console


def approve_interactively(run_id: str, auto: bool) -> bool:
    run = ledger.get_run(run_id)
    counts = run["data"]["plan_counts"]
    print(f"  Plan {run['plan_hash']}: create {counts['create']}, skip {counts['skip']}, review {counts['review']}")
    for it in ledger.get_items(run_id):
        print(f"    L{it['line']:<3} {it['email']:<34} {it['action']:<7} {it['target_role'] or '-':<8} {it['reason']}")
    for e in ledger.get_events(run_id):
        if e["step"] == "memory":
            print(f"  [memory] {e['message']}")
    if auto:
        approver = "scenario-runner (--yes flag, scripted approval)"
    else:
        answer = input("  Approve this plan? [y/N] ").strip().lower()
        if answer != "y":
            return False
        approver = input("  Your name: ").strip() or "terminal user"
    pipeline.approve(run_id, approver, run["plan_hash"], admin_grants=False)
    for e in ledger.get_events(run_id):
        if e["step"] == "memory" and e["message"].startswith("Saved"):
            print(f"  [memory] {e['message']}")
    return True


def write_evidence(tc: str, run_id: str) -> dict:
    rep = pipeline.report(run_id)
    run = rep["run"]
    result = run["data"].get("scenario_result", {})
    out = {
        "test_case": tc, "scenario": SCENARIOS[tc]["title"], "expected": SCENARIOS[tc]["expected"],
        "run_id": run_id, "verdict": result.get("verdict"), "detail": result.get("detail"),
        "checks": result.get("checks"), "input_csv": SCENARIOS[tc]["csv"],
        "console_setup": SCENARIOS[tc]["console"], "approved_by": run["approved_by"],
        "plan_hash": run["plan_hash"], "attempt_summaries": run["data"].get("attempt_summaries"),
        "results": {k: [{"record_id": i["record_id"], "line": i["line"], "email": i["email"],
                         "role": i["target_role"], "reason": i["reason"],
                         "verification": i["evidence"].get("verification")} for i in v]
                    for k, v in rep["groups"].items()},
        "events": [f"{e['ts']} [{e['step']}] {e['message']}" for e in ledger.get_events(run_id)],
    }
    path = config.EVIDENCE_DIR / f"{tc}_{run_id}.json"
    path.write_text(json.dumps(out, indent=2), encoding="utf-8")
    return out


def run_tc(tc: str, auto: bool) -> dict:
    print(f"\n=== {tc}: {SCENARIOS[tc]['title']}  (expected: {SCENARIOS[tc]['expected']})")
    setup_console(tc)
    run_id = pipeline.collect("acme", csv_text(tc), SCENARIOS[tc]["csv"], f"SCENARIO-{tc}", scenario=tc)
    pipeline.plan(run_id)
    run = ledger.get_run(run_id)
    if run["status"] != "awaiting_approval":
        print(f"  Planning failed: {run['error']}")
        return {"test_case": tc, "verdict": "error", "detail": run["error"]}
    if not approve_interactively(run_id, auto):
        print("  Not approved — nothing was created.")
        return {"test_case": tc, "verdict": "not approved", "detail": ""}
    summary = pipeline.apply(run_id)
    print(f"  Attempt 1: {summary}")
    if ledger.get_run(run_id)["status"] == "needs_retry":
        print("  Run needs retry — retrying (re-reads console, creates only missing users)")
        summary = pipeline.retry(run_id)
        print(f"  Attempt 2: {summary}")
    out = write_evidence(tc, run_id)
    print(f"  RESULT: {str(out['verdict']).upper()} — {out['detail']}")
    return out


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    auto = "--yes" in sys.argv
    try:
        requests.get(config.CONSOLE_URL + "/login", timeout=5)
    except requests.RequestException:
        sys.exit(f"Mock console not reachable at {config.CONSOLE_URL}. Start it first: python run.py")
    if "--fresh" in sys.argv:
        ledger.clear_role_policy("acme")
        print("Saved role policy for tenant 'acme' cleared.")
    tcs = [a.upper() for a in args] or list(SCENARIOS)
    results = [run_tc(tc, auto) for tc in tcs]
    lines = ["# Test case results", "", "| Test | Scenario | Expected | Verdict | Detail |", "|---|---|---|---|---|"]
    for r in results:
        tc = r["test_case"]
        lines.append(f"| {tc} | {SCENARIOS[tc]['title']} | {SCENARIOS[tc]['expected']} | "
                     f"{str(r.get('verdict')).upper()} | {r.get('detail')} |")
    (config.EVIDENCE_DIR / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n" + "\n".join(lines))
    print(f"\nEvidence written to {config.EVIDENCE_DIR}")


if __name__ == "__main__":
    main()
