"""LLM reasoning steps, each with a deterministic fallback and safety checks.

1. understand_form — work out which field on the console's "Add user" form holds
   the name, email and role, from what the page shows (labels, names, options),
   instead of hard-coding selectors.
2. map_roles — map the role names used in the CSV to the roles the console
   actually offers, with a confidence score and a reason for each.
   Low-confidence mappings go to human review; privileged roles are never
   assigned unless the CSV asked for them explicitly.
"""
import difflib
import json

import config
from agent import llm

FORM_SYSTEM = (
    "You inspect web forms for an automation agent. Given the fields of an 'Add user' form, "
    "identify which field is used for the user's full name, email address and role/access level. "
    "Reply with JSON only: {\"full_name\": \"<field name>\", \"email\": \"<field name>\", "
    "\"role\": \"<field name>\", \"reason\": \"<one sentence>\"}. Use the exact 'name' values given."
)

ROLE_SYSTEM = (
    "You map job roles from an HR-approved CSV onto the access roles offered by a SaaS admin console. "
    "Follow least privilege: pick the lowest console role that fits the job. Never map to an "
    "administrative role (Admin, Owner, etc.) unless the CSV role itself clearly asks for admin rights. "
    "If a CSV role is ambiguous, give a low confidence instead of guessing. "
    "Reply with JSON only: {\"mappings\": [{\"csv_role\": str, \"console_role\": str, "
    "\"confidence\": number 0-1, \"reason\": str}]}. console_role must be one of the given console roles."
)

# Fallback knowledge used when no LLM is available
ROLE_SYNONYMS = {
    "Admin": ["admin", "administrator", "owner", "super admin", "it admin", "tenant admin"],
    "Manager": ["manager", "lead", "head", "director", "supervisor", "team lead", "engineering manager"],
    "Member": ["member", "developer", "engineer", "employee", "staff", "user", "support", "agent",
               "designer", "analyst", "sales", "marketing", "standard", "software engineer"],
    "Billing": ["billing", "finance", "accountant", "accounts", "payroll", "procurement",
                "finance analyst", "billing specialist"],
    "Viewer": ["viewer", "read-only", "read only", "readonly", "auditor", "contractor", "guest",
               "intern", "observer", "external"],
}


def is_privileged(role: str) -> bool:
    return (role or "").strip().lower() in config.PRIVILEGED_ROLES


def _csv_asks_for_admin(csv_role: str) -> bool:
    low = csv_role.lower()
    return any(word in low for word in ("admin", "owner", "superuser"))


# ---------------------------------------------------------------- form understanding

def _heuristic_form(fields: list[dict]) -> dict:
    def pick(*keywords, want_select=False):
        for f in fields:
            hay = f"{f['name']} {f['label']} {f['id']}".lower()
            if want_select and f["tag"] != "select":
                continue
            if any(k in hay for k in keywords):
                return f["name"]
        return None
    return {
        "full_name": pick("full name", "display", "name"),
        "email": pick("email", "mail"),
        "role": pick("role", "access", "permission", want_select=True),
        "reason": "keyword match on field labels (fallback)",
    }


def understand_form(fields: list[dict]) -> dict:
    names = {f["name"] for f in fields}
    compact = [{"name": f["name"], "label": f["label"], "tag": f["tag"], "type": f["type"],
                "options": [o["text"] for o in f.get("options", [])][:12]} for f in fields]
    result, meta = llm.chat_json(FORM_SYSTEM, json.dumps({"fields": compact}))
    source = "llm"
    if not result or not all(result.get(k) in names for k in ("full_name", "email", "role")):
        fallback = _heuristic_form(fields)
        if result:
            meta["error"] = (meta.get("error") or "") + " LLM answer referenced unknown fields"
        result, source = fallback, "fallback"
    by_name = {f["name"]: f for f in fields}
    if by_name.get(result["role"], {}).get("tag") != "select":
        raise RuntimeError(f"Role field '{result['role']}' is not a dropdown; cannot continue safely")
    return {"map": {k: result[k] for k in ("full_name", "email", "role")},
            "reason": result.get("reason", ""), "source": source, "llm": meta}


# ---------------------------------------------------------------- role mapping

def _fallback_role(csv_role: str, console_roles: list[str]) -> dict:
    low = csv_role.lower().strip()
    for console_role in console_roles:
        if low == console_role.lower():
            return {"console_role": console_role, "confidence": 1.0, "reason": "exact name match"}
    # Specific (multi-word) synonyms first, so "IT Admin" beats "admin" style collisions
    best = None
    for console_role in console_roles:
        for syn in ROLE_SYNONYMS.get(console_role, []):
            if syn in low and (best is None or len(syn) > len(best[1])):
                best = (console_role, syn)
    if best:
        return {"console_role": best[0], "confidence": 0.75, "reason": f"keyword '{best[1]}' (fallback rules)"}
    close = difflib.get_close_matches(csv_role, console_roles, n=1, cutoff=0.6)
    if close:
        return {"console_role": close[0], "confidence": 0.6, "reason": "closest spelling (fallback rules)"}
    return {"console_role": None, "confidence": 0.0, "reason": "no rule matched (fallback rules)"}


def map_roles(csv_roles: list[str], console_roles: list[str], overrides: dict | None = None,
              policy: dict | None = None) -> tuple[dict, dict]:
    """Returns ({csv_role: mapping}, llm_meta). Each mapping:
    console_role, confidence, reason, source (override|policy|llm|fallback), status (ok|review|privileged).

    Order of precedence: explicit override for this run > saved role policy (mappings a human
    approved in an earlier run) > LLM > deterministic rules. Only titles not covered by an
    override or the policy are sent to the LLM, so repeat runs map the same title the same way."""
    overrides = overrides or {}
    policy = {k: v for k, v in (policy or {}).items() if v.get("console_role") in console_roles}
    todo = [r for r in csv_roles if r not in overrides and r not in policy]
    llm_answers, meta = {}, {"provider": llm.provider_label(), "error": None}
    if todo:
        prompt = json.dumps({"csv_roles": todo, "console_roles": console_roles})
        result, meta = llm.chat_json(ROLE_SYSTEM, prompt)
        if result and isinstance(result.get("mappings"), list):
            for m in result["mappings"]:
                if isinstance(m, dict) and m.get("csv_role") in todo:
                    llm_answers[m["csv_role"]] = m

    mappings = {}
    for csv_role in csv_roles:
        if csv_role in overrides:
            entry = {"console_role": overrides[csv_role], "confidence": 1.0,
                     "reason": "set by a human reviewer", "source": "override"}
        elif csv_role in policy:
            p = policy[csv_role]
            entry = {"console_role": p["console_role"], "confidence": 1.0, "source": "policy",
                     "approved_by": p.get("approved_by"), "approved_at": p.get("approved_at"),
                     "decided_by": p.get("decided_by"), "times_used": p.get("times_used"),
                     "reason": f"saved role policy — approved by {p.get('approved_by')} on "
                               f"{(p.get('approved_at') or '')[:10]} (first decided by {p.get('decided_by', '?')})"}
        elif csv_role in llm_answers and llm_answers[csv_role].get("console_role") in console_roles:
            ans = llm_answers[csv_role]
            try:
                conf = max(0.0, min(1.0, float(ans.get("confidence", 0))))
            except (TypeError, ValueError):
                conf = 0.0
            entry = {"console_role": ans["console_role"], "confidence": conf,
                     "reason": str(ans.get("reason", ""))[:240], "source": "llm"}
        else:
            entry = {**_fallback_role(csv_role, console_roles), "source": "fallback"}

        # Safety checks applied to every source except an explicit human override
        status = "ok"
        if entry["console_role"] is None:
            status = "review"
        elif entry["source"] not in ("override", "policy") and is_privileged(entry["console_role"]) \
                and not _csv_asks_for_admin(csv_role):
            entry["reason"] += " — BLOCKED: privileged role not requested by CSV"
            entry["confidence"] = min(entry["confidence"], 0.3)
            status = "review"
        elif entry["source"] not in ("override", "policy") and entry["confidence"] < config.ROLE_CONFIDENCE_THRESHOLD:
            status = "review"
        if status == "ok" and is_privileged(entry["console_role"]):
            status = "privileged"
        entry["status"] = status
        mappings[csv_role] = entry
    return mappings, meta
