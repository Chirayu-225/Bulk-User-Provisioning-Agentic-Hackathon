"""The four official test cases: input, console setup, expected result, and an
evaluator that compares the actual run against the expectation using the
evidence the agent recorded (including its independent re-read of the console).
"""
from pathlib import Path

import requests

import config
from agent import ledger

HERE = Path(__file__).resolve().parent

SCENARIOS = {
    "TC1": {
        "title": "Approved CSV of 20 users",
        "expected": "Users created with mapped roles",
        "csv": "tc1_approved_20_users.csv",
        "console": {"default_role_admin": False, "fail_at": None},
    },
    "TC2": {
        "title": "Same email twice in CSV",
        "expected": "One user created",
        "csv": "tc2_duplicate_email.csv",
        "console": {"default_role_admin": False, "fail_at": None},
    },
    "TC3": {
        "title": "Console defaults new users to Admin",
        "expected": "Corrected before save",
        "csv": "tc3_console_defaults_admin.csv",
        "console": {"default_role_admin": True, "fail_at": None},
    },
    "TC4": {
        "title": "Batch fails at user 12",
        "expected": "Retry creates only the missing users",
        "csv": "tc4_batch_fails_at_12.csv",
        "console": {"default_role_admin": False, "fail_at": 12, "fail_mode": "before_save", "fail_once": True},
    },
}


def csv_text(tc: str) -> str:
    return (HERE / SCENARIOS[tc]["csv"]).read_text(encoding="utf-8")


def setup_console(tc: str) -> None:
    """Test-harness only: reset the mock console and switch on this scenario's trap."""
    base = config.CONSOLE_URL.rstrip("/")
    requests.post(f"{base}/__control/reset", timeout=10).raise_for_status()
    requests.post(f"{base}/__control/config", json=SCENARIOS[tc]["console"], timeout=10).raise_for_status()


def evaluate(run_id: str) -> dict:
    run = ledger.get_run(run_id)
    items = ledger.get_items(run_id)
    summary = run["data"].get("summary", {})
    tc = run["scenario"]
    creates = [i for i in items if i["action"] == "create"]
    verified = [i for i in items if i["status"] == "verified"]
    checks: list[tuple[str, bool]] = []

    def role_ok(i):
        return i["evidence"].get("verification", {}).get("observed_role") == i["target_role"]

    if tc == "TC1":
        checks += [
            ("all 20 rows planned for creation", len(creates) == 20),
            ("20 users verified in console", len(verified) == 20),
            ("every observed role equals the approved mapped role", all(role_ok(i) for i in verified)),
            ("no failures", summary.get("failed", 0) == 0),
            ("no duplicate accounts", not summary.get("duplicate_emails_in_console")),
        ]
    elif tc == "TC2":
        by_email: dict[str, list] = {}
        for i in items:
            by_email.setdefault(i["email"], []).append(i)
        dup_groups = [g for g in by_email.values() if len(g) > 1]
        checks.append(("duplicate email detected in CSV", len(dup_groups) == 1))
        for g in dup_groups:
            checks += [
                ("exactly one of the duplicate rows created", sum(1 for i in g if i["status"] == "verified") == 1),
                ("the other row skipped as duplicate",
                 any(i["status"] == "skipped" and "Duplicate" in (i["reason"] or "") for i in g)),
                ("console holds one account for that email",
                 max(i["evidence"].get("verification", {}).get("occurrences", 0) for i in g) == 1),
            ]
        checks.append(("all unique users created", len(verified) == len(by_email)))
    elif tc == "TC3":
        corrected = [i for i in verified if any(
            k.startswith("action_attempt_") and v.get("default_role_seen") == "Admin" and v.get("corrected")
            for k, v in i["evidence"].items())]
        checks += [
            ("console pre-selected Admin on the form (trap active)", len(corrected) > 0),
            ("agent corrected the role before every save", len(corrected) == len(creates)),
            ("all users verified with intended roles", len(verified) == len(creates) and all(role_ok(i) for i in verified)),
            ("no user ended up as Admin", not any(i["evidence"].get("verification", {}).get("observed_role") == "Admin"
                                                  for i in verified)),
        ]
    elif tc == "TC4":
        history = run["data"].get("attempt_summaries", [])
        first = history[0] if history else {}
        if len(history) < 2:
            ok = first.get("failed", 0) >= 1 and first.get("created") == 11
            return {"verdict": "in_progress",
                    "detail": ("First attempt halted at user 12 as expected (11 created, 1 failed, "
                               f"{first.get('pending', 0)} pending) — run Retry") if ok else
                              f"First attempt did not halt as expected: {first}",
                    "checks": [["first attempt stopped at user 12 with 11 created", ok]]}
        last = history[-1]
        checks += [
            ("first attempt halted with 11 created", first.get("created") == 11 and first.get("failed", 0) >= 1),
            ("retry created only the missing users", last.get("created_this_attempt") == 20 - first.get("created", 0)),
            ("all 20 users verified after retry", last.get("created") == 20 and last.get("failed") == 0),
            ("no duplicate accounts created", not last.get("duplicate_emails_in_console")),
        ]
    else:
        return {"verdict": "n/a", "detail": "not a scenario run", "checks": []}

    passed = all(ok for _, ok in checks)
    failed = [name for name, ok in checks if not ok]
    return {"verdict": "pass" if passed else "fail",
            "detail": "all checks passed" if passed else "failed: " + "; ".join(failed),
            "checks": [[n, ok] for n, ok in checks]}
