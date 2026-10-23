"""Bulk user provisioning pipeline.

  1. collect  — approved CSV + tenant + approval reference; validate, give stable IDs
  2. plan     — read the console (existing users, form, roles), deduplicate,
                map roles (LLM + guards), split into checkpointed batches
  3. approve  — a human approves the exact plan (bound by hash); admin grants need
                a separate tick
  4. apply    — create users through the console UI, batch by batch, writing a
                checkpoint before and after every save; halts on the first failure
  5. verify   — a separate browser session re-reads the user list and compares it
                with what was intended

retry() re-runs apply + verify: it re-reads the console first and only creates
users that are still missing.
"""
import hashlib
import json
import threading

import config
from agent import ledger, llm, reasoning
from agent.console_driver import ConsoleDriver
from agent.csv_input import load_csv

_browser_lock = threading.Lock()  # one browser job at a time


def _driver(run_id: str) -> ConsoleDriver:
    def on_event(step, message, data=None, level="info"):
        ledger.log(run_id, step, message, data, level)
    return ConsoleDriver(config.RUNS_DIR / run_id, on_event=on_event,
                         on_shot=lambda p: ledger.update_run(run_id, live_shot=p))


def _fail(run_id: str, stage: str, exc: Exception) -> None:
    ledger.update_run(run_id, status="error", error=f"{type(exc).__name__}: {exc}")
    ledger.log(run_id, stage, f"Stopped: {exc}", level="error")


# ====================================================================== 1. collect
def collect(tenant: str, csv_text: str, csv_name: str, approval_ref: str,
            scenario: str | None = None, role_policy: dict | None = None) -> str:
    tenant = (tenant or "").strip().lower()
    if not tenant:
        raise ValueError("Tenant is required")
    if not (approval_ref or "").strip():
        raise ValueError("Approval reference is required (who approved this CSV, e.g. a ticket ID)")
    records, errors = load_csv(csv_text)
    if errors:
        raise ValueError("; ".join(errors))
    run_id = ledger.create_run(tenant, scenario, approval_ref.strip(), csv_name,
                               {"role_policy": role_policy or {}, "llm_provider": llm.provider_label(),
                                "csv_text": csv_text[:200_000]})
    ledger.insert_items(run_id, records)
    invalid = sum(1 for r in records if r["problem"])
    dups = sum(1 for r in records if r["duplicate_of"])
    ledger.log(run_id, "collect", f"Loaded {len(records)} rows from {csv_name} for tenant '{tenant}' "
               f"(approval ref: {approval_ref.strip()}) — {invalid} invalid, {dups} duplicate email(s)",
               {"rows": len(records), "invalid": invalid, "duplicates": dups})
    return run_id


# ====================================================================== 2. plan
def plan(run_id: str) -> None:
    run = ledger.get_run(run_id)
    ledger.update_run(run_id, status="planning", stage="plan", error=None)
    try:
        with _browser_lock, _driver(run_id) as d:
            d.login()
            tenant_info = d.open_tenant(run["tenant"])
            snapshot = d.read_users(run["tenant"])
            form = d.inspect_add_form(run["tenant"])

        understanding = reasoning.understand_form(form["fields"])
        fm = understanding["map"]
        ledger.log(run_id, "reason", f"Form fields identified ({understanding['source']}): name → '{fm['full_name']}', "
                   f"email → '{fm['email']}', role → '{fm['role']}'. {understanding['reason']}",
                   {"llm": understanding["llm"]})
        role_field = next(f for f in form["fields"] if f["name"] == fm["role"])
        console_roles = [o["text"] for o in role_field["options"] if o["value"]]
        ledger.log(run_id, "observe", f"Console offers roles: {', '.join(console_roles)}")

        ledger.merge_run_data(
            run_id, tenant_info=tenant_info, field_map=fm, console_roles=console_roles,
            form={"url": form["url"], "at": form["at"], "screenshot": form["screenshot"],
                  "fields": form["fields"]},
            console_snapshot={"at": snapshot["at"], "pages": snapshot["pages"],
                              "users": {u["email"]: u for u in snapshot["users"]}})

        items = ledger.get_items(run_id)
        csv_roles = sorted({i["csv_role"] for i in items if not i["evidence"]["input"]["problem"]
                            and not i["evidence"]["input"]["duplicate_of"]})
        saved = ledger.policy_for_roles(run["tenant"], csv_roles)
        if saved:
            ledger.log(run_id, "memory", f"Loaded saved role policy for {len(saved)} of {len(csv_roles)} job title(s) "
                       f"(approved in earlier runs): " + ", ".join(f"{k} → {v['console_role']}" for k, v in saved.items()))
        mappings, meta = reasoning.map_roles(csv_roles, console_roles, run["data"].get("role_policy"), saved)
        sources = {m["source"] for m in mappings.values()}
        src = " + ".join(label for key, label in (("policy", "saved policy"), ("override", "reviewer"),
                                                  ("llm", "LLM"), ("fallback", "fallback rules")) if key in sources)
        if meta.get("error"):
            ledger.log(run_id, "reason", f"LLM unavailable ({meta['error']}); using deterministic rules",
                       level="warn")
        for csv_role, m in mappings.items():
            ledger.log(run_id, "reason", f"Role '{csv_role}' → {m['console_role'] or '?'} "
                       f"(confidence {m['confidence']:.2f}, {m['source']}, {m['status']}): {m['reason']}",
                       level="warn" if m["status"] != "ok" else "info")
        ledger.merge_run_data(run_id, role_mappings=mappings, role_llm=meta, role_source=src)
        _build_plan(run_id)
    except Exception as exc:
        _fail(run_id, "plan", exc)


def _build_plan(run_id: str) -> None:
    """Turns observations + role mappings into a per-record decision. No browser needed."""
    run = ledger.get_run(run_id)
    data = run["data"]
    mappings = data["role_mappings"]
    existing = data["console_snapshot"]["users"]
    snap_at = data["console_snapshot"]["at"]
    items = ledger.get_items(run_id)

    decisions, create_idx = [], 0
    for it in items:
        inp = it["evidence"]["input"]
        m = mappings.get(it["csv_role"])
        target, action, batch = None, "skip", None
        if inp["problem"]:
            reason = f"Invalid input: {inp['problem']}"
        elif inp["duplicate_of"]:
            reason = f"Duplicate email in CSV — same as {inp['duplicate_of']}; only the first row is used"
        elif it["email"] in existing:
            reason = (f"Already exists in console with role {existing[it['email']]['role']} "
                      f"(observed {snap_at}) — not re-created")
        elif m is None or m["status"] == "review":
            target = m["console_role"] if m else None
            action = "review"
            reason = f"Role needs human review: {m['reason'] if m else 'no mapping'}"
        else:
            target, action = m["console_role"], "create"
            batch = create_idx // config.BATCH_SIZE + 1
            create_idx += 1
            reason = f"'{it['csv_role']}' → {target} ({m['source']}, confidence {m['confidence']:.2f})"
            if m["status"] == "privileged":
                reason += " — PRIVILEGED: needs separate admin-grant approval"
        ledger.update_item(run_id, it["record_id"], target_role=target, action=action, reason=reason,
                           batch=batch, status="planned",
                           evidence_patch={"plan": {"mapping": m, "console_snapshot_at": snap_at}})
        decisions.append([it["record_id"], action, target, batch])

    plan_hash = hashlib.sha256(json.dumps([run["tenant"], decisions]).encode()).hexdigest()[:16]
    counts = {a: sum(1 for d in decisions if d[1] == a) for a in ("create", "skip", "review")}
    counts["batches"] = (counts["create"] + config.BATCH_SIZE - 1) // config.BATCH_SIZE
    counts["privileged"] = sum(1 for d in decisions if d[1] == "create" and reasoning.is_privileged(d[2]))
    ledger.merge_run_data(run_id, plan_counts=counts)
    ledger.update_run(run_id, plan_hash=plan_hash, approved_hash=None, status="awaiting_approval", stage="approve")
    ledger.log(run_id, "plan", f"Plan ready ({plan_hash}): create {counts['create']} in {counts['batches']} "
               f"batch(es) of {config.BATCH_SIZE}, skip {counts['skip']}, review {counts['review']}. "
               f"Waiting for human approval.", counts)


def override_role(run_id: str, csv_role: str, console_role: str) -> None:
    run = ledger.get_run(run_id)
    if run["status"] != "awaiting_approval":
        raise ValueError("Roles can only be changed while the plan is awaiting approval")
    if console_role not in run["data"]["console_roles"]:
        raise ValueError(f"'{console_role}' is not a role offered by the console")
    mappings = run["data"]["role_mappings"]
    if csv_role not in mappings:
        raise ValueError(f"'{csv_role}' is not a role in this CSV")
    mappings[csv_role] = {"console_role": console_role, "confidence": 1.0, "reason": "set by a human reviewer",
                          "source": "override",
                          "status": "privileged" if reasoning.is_privileged(console_role) else "ok"}
    ledger.merge_run_data(run_id, role_mappings=mappings)
    ledger.log(run_id, "plan", f"Reviewer changed role mapping: '{csv_role}' → {console_role}. Plan rebuilt; "
               f"previous approval (if any) cleared.")
    _build_plan(run_id)


# ====================================================================== 3. approve
def approve(run_id: str, approver: str, plan_hash: str, admin_grants: bool = False) -> None:
    run = ledger.get_run(run_id)
    if run["status"] != "awaiting_approval":
        raise ValueError(f"Run is '{run['status']}', not awaiting approval")
    if not (approver or "").strip():
        raise ValueError("Approver name is required")
    if plan_hash != run["plan_hash"]:
        raise ValueError("The plan changed since you reviewed it — reload and review again")
    items = ledger.get_items(run_id)
    approved = []
    for it in items:
        if it["action"] == "create":
            if reasoning.is_privileged(it["target_role"]) and not admin_grants:
                ledger.update_item(run_id, it["record_id"], status="skipped",
                                   reason=it["reason"] + " — admin grant NOT approved, so not created")
                continue
            ledger.update_item(run_id, it["record_id"], status="pending")
            approved.append({"record_id": it["record_id"], "email": it["email"], "role": it["target_role"]})
        else:
            ledger.update_item(run_id, it["record_id"], status="skipped")
    ledger.merge_run_data(run_id, approved_plan=approved)

    # Learn from the approval: remember each approved title → role mapping for this tenant
    learned = []
    used_roles = {it["csv_role"] for it in items if it["action"] == "create"
                  and (admin_grants or not reasoning.is_privileged(it["target_role"]))}
    for csv_role, m in run["data"].get("role_mappings", {}).items():
        if csv_role in used_roles and m.get("console_role") and m.get("status") in ("ok", "privileged"):
            decided_by = m.get("decided_by") or {"llm": f"LLM ({run['data'].get('llm_provider', '')})",
                                                 "fallback": "fallback rules", "override": approver.strip(),
                                                 "policy": "saved policy"}.get(m["source"], m["source"])
            ledger.save_role_policy(run["tenant"], csv_role, m["console_role"], decided_by, approver.strip(), run_id)
            if m["source"] != "policy":
                learned.append(f"{csv_role} → {m['console_role']}")
    if learned:
        ledger.log(run_id, "memory", f"Saved {len(learned)} newly approved role mapping(s) to the tenant's role policy: "
                   + ", ".join(learned) + ". Future runs will reuse them instead of asking the LLM again.")
    ledger.update_run(run_id, approved_hash=plan_hash, approved_by=approver.strip(), approved_at=ledger.now(),
                      admin_grants_approved=int(bool(admin_grants)), status="approved", stage="apply")
    ledger.log(run_id, "approve", f"Plan {plan_hash} approved by {approver.strip()} — {len(approved)} user(s) "
               f"to create{' (admin grants approved)' if admin_grants else ''}")


# ====================================================================== 4. apply
def apply(run_id: str) -> dict:
    run = ledger.get_run(run_id)
    if not run["approved_hash"] or run["approved_hash"] != run["plan_hash"]:
        raise ValueError("No valid approval for the current plan")
    attempt = (run["apply_attempts"] or 0) + 1
    ledger.update_run(run_id, status="applying", stage="apply", apply_attempts=attempt, error=None)
    tenant, fmap = run["tenant"], run["data"]["field_map"]
    ledger.log(run_id, "apply", f"Apply attempt #{attempt} started")
    try:
        with _browser_lock, _driver(run_id) as d:
            d.login()
            d.open_tenant(tenant)
            live = {u["email"]: u for u in d.read_users(tenant)["users"]}
            _reconcile(run_id, live, attempt)

            halted_on = None
            items = ledger.get_items(run_id)
            batches = sorted({i["batch"] for i in items if i["status"] == "pending" and i["batch"]})
            for b in batches:
                todo = [i for i in items if i["batch"] == b and i["status"] == "pending"]
                ledger.log(run_id, "apply", f"Batch {b}: creating {len(todo)} user(s)")
                for it in todo:
                    ledger.update_item(run_id, it["record_id"], status="in_flight", attempts=it["attempts"] + 1)
                    res = d.create_user(tenant, fmap, it)
                    patch = {f"action_attempt_{attempt}": res}
                    if res["outcome"] == "created":
                        ledger.update_item(run_id, it["record_id"], status="created", created_in_attempt=attempt,
                                           evidence_patch=patch)
                        ledger.log(run_id, "apply", f"Created {it['email']} as {it['target_role']} (line {it['line']})",
                                   {"record_id": it["record_id"]})
                    elif res["outcome"] == "exists":
                        ledger.update_item(run_id, it["record_id"], status="skipped",
                                           reason="Console reported the email already exists — not re-created",
                                           evidence_patch=patch)
                        ledger.log(run_id, "apply", f"{it['email']} already exists in console — skipped", level="warn")
                    else:
                        ledger.update_item(run_id, it["record_id"], status="failed", evidence_patch=patch)
                        ledger.log(run_id, "apply", f"FAILED creating {it['email']} (line {it['line']}): "
                                   f"{res['message']}", {"record_id": it["record_id"]}, level="error")
                        halted_on = it
                        break
                if halted_on:
                    break
                done = sum(1 for i in ledger.get_items(run_id) if i["status"] == "created")
                ledger.log(run_id, "checkpoint", f"Checkpoint saved after batch {b} — {done} created so far")

        if halted_on:
            remaining = sum(1 for i in ledger.get_items(run_id) if i["status"] == "pending")
            ledger.log(run_id, "apply", f"Halted at line {halted_on['line']} ({halted_on['email']}). {remaining} user(s) "
                       f"not attempted. Progress is checkpointed — Retry will create only the missing users.",
                       level="warn")
        ledger.update_run(run_id, status="verifying", stage="verify")
    except Exception as exc:
        _fail(run_id, "apply", exc)
        return {}
    return verify(run_id)


def _reconcile(run_id: str, live: dict, attempt: int) -> None:
    """Before creating anything, compare the ledger with what the console shows right now."""
    notes = []
    for it in ledger.get_items(run_id):
        if it["status"] not in ("pending", "in_flight", "failed"):
            continue
        observed = live.get(it["email"])
        if observed:
            if it["status"] in ("in_flight", "failed"):
                ledger.update_item(run_id, it["record_id"], status="created",
                                   created_in_attempt=attempt - 1 or 1,
                                   evidence_patch={"reconciled": {"observed": observed, "attempt": attempt,
                                                                  "note": "previous save went through"}})
                notes.append(f"{it['email']} found in console (previous save went through) — not re-submitted")
            else:
                ledger.update_item(run_id, it["record_id"], status="skipped",
                                   reason=f"Appeared in console after planning (role {observed['role']}) — not re-created",
                                   evidence_patch={"reconciled": {"observed": observed, "attempt": attempt}})
                notes.append(f"{it['email']} appeared in console since planning — skipped")
        elif it["status"] in ("in_flight", "failed"):
            ledger.update_item(run_id, it["record_id"], status="pending")
            notes.append(f"{it['email']} not in console — queued for retry")
    pending = sum(1 for i in ledger.get_items(run_id) if i["status"] == "pending")
    ledger.log(run_id, "reconcile", f"Reconciled ledger with live console: {pending} user(s) still need creating"
               + (f"; {len(notes)} adjustment(s)" if notes else ""), {"notes": notes})


def retry(run_id: str) -> dict:
    run = ledger.get_run(run_id)
    if run["status"] not in ("needs_retry", "error"):
        raise ValueError(f"Nothing to retry (run is '{run['status']}')")
    ledger.log(run_id, "retry", "Retry requested — using the already-approved plan; only missing users will be created")
    return apply(run_id)


# ====================================================================== 5. verify
def verify(run_id: str) -> dict:
    """Independent check: fresh browser session, re-read the user list, compare with intent."""
    run = ledger.get_run(run_id)
    try:
        with _browser_lock, _driver(run_id) as d:
            d.login()
            d.open_tenant(run["tenant"])
            read = d.read_users(run["tenant"])
    except Exception as exc:
        _fail(run_id, "verify", exc)
        return {}
    counts: dict[str, int] = {}
    for u in read["users"]:
        counts[u["email"]] = counts.get(u["email"], 0) + 1
    live = {u["email"]: u for u in read["users"]}

    for it in ledger.get_items(run_id):
        obs = live.get(it["email"])
        v = {"at": read["at"], "found": bool(obs), "observed_role": obs["role"] if obs else None,
             "occurrences": counts.get(it["email"], 0), "source_url": obs["source_url"] if obs else read["pages"][0]}
        status, reason = it["status"], it["reason"]
        if status in ("created", "verified", "in_flight") or (status == "failed" and obs):
            if obs and obs["role"] == it["target_role"]:
                note = " (console showed an error but the user exists)" if status == "failed" else ""
                status, reason = "verified", f"Verified in console as {obs['role']}{note}"
            elif obs:
                status, reason = "failed", f"Verification mismatch: console shows role {obs['role']}, expected {it['target_role']}"
            else:
                status, reason = "failed", "Verification failed: user not found on re-read"
        if counts.get(it["email"], 0) > 1:
            reason += f" — WARNING: {counts[it['email']]} accounts with this email in console"
        ledger.update_item(run_id, it["record_id"], status=status, reason=reason, evidence_patch={"verification": v})

    items = ledger.get_items(run_id)
    summary = {
        "created": sum(1 for i in items if i["status"] == "verified"),
        "skipped": sum(1 for i in items if i["status"] == "skipped"),
        "failed": sum(1 for i in items if i["status"] == "failed"),
        "pending": sum(1 for i in items if i["status"] in ("pending", "in_flight", "created")),
        "console_total_after": len(read["users"]),
        "duplicate_emails_in_console": sorted(e for e, c in counts.items() if c > 1),
        "verified_at": read["at"],
        "attempt": run["apply_attempts"],
    }
    final = "completed" if summary["failed"] == 0 and summary["pending"] == 0 else "needs_retry"
    history = run["data"].get("attempt_summaries", [])
    history.append({**summary, "created_this_attempt": sum(1 for i in items if i["created_in_attempt"] == run["apply_attempts"])})
    ledger.merge_run_data(run_id, summary=summary, attempt_summaries=history)
    ledger.update_run(run_id, status=final, stage="report")
    ledger.log(run_id, "verify", f"Verification (fresh session, {len(read['pages'])} page(s)): {summary['created']} created, "
               f"{summary['skipped']} skipped, {summary['failed']} failed, {summary['pending']} pending",
               summary, level="info" if final == "completed" else "warn")

    if run["scenario"]:
        from scenarios.definitions import evaluate
        result = evaluate(run_id)
        ledger.merge_run_data(run_id, scenario_result=result)
        ledger.log(run_id, "scenario", f"{run['scenario']}: {result['verdict'].upper()} — {result['detail']}",
                   level="info" if result["verdict"] == "pass" else "warn")
    return summary


# ====================================================================== report
def report(run_id: str) -> dict:
    run = ledger.get_run(run_id)
    items = ledger.get_items(run_id)
    groups = {"created": [], "skipped": [], "failed": [], "pending": []}
    for it in items:
        key = {"verified": "created", "skipped": "skipped", "failed": "failed"}.get(it["status"], "pending")
        groups[key].append(it)
    return {"run": run, "items": items, "groups": groups}


def evidence(run_id: str) -> dict:
    """Everything a reviewer needs to audit one run, in one JSON document."""
    rep = report(run_id)
    run, data = rep["run"], rep["run"]["data"]
    return {
        "run_id": run["id"], "created_at": run["created_at"], "tenant": run["tenant"],
        "scenario": run["scenario"], "scenario_result": data.get("scenario_result"),
        "input": {"csv_name": run["csv_name"], "approval_ref": run["approval_ref"], "csv_text": data.get("csv_text")},
        "observed_console": {"tenant": data.get("tenant_info"), "form_fields": data.get("field_map"),
                             "roles_offered": data.get("console_roles"),
                             "users_before_plan": len((data.get("console_snapshot") or {}).get("users", {})),
                             "snapshot_at": (data.get("console_snapshot") or {}).get("at")},
        "plan": {"hash": run["plan_hash"], "counts": data.get("plan_counts"), "role_mappings": data.get("role_mappings"),
                 "role_source": data.get("role_source")},
        "approval": {"approved_by": run["approved_by"], "approved_at": run["approved_at"],
                     "approved_hash": run["approved_hash"], "admin_grants_approved": bool(run["admin_grants_approved"])},
        "attempts": data.get("attempt_summaries", []),
        "summary": data.get("summary"),
        "results": {group: [{"record_id": i["record_id"], "line": i["line"], "name": i["name"], "email": i["email"],
                             "csv_role": i["csv_role"], "role": i["target_role"], "status": i["status"],
                             "reason": i["reason"], "evidence": i["evidence"]} for i in items]
                    for group, items in rep["groups"].items()},
        "events": [{"ts": e["ts"], "step": e["step"], "level": e["level"], "message": e["message"]}
                   for e in ledger.get_events(run_id)],
    }
