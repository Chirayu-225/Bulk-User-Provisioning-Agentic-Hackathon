"""Step 1 — Collect the details: parse and validate the approved CSV.

Every row gets a stable ID (CSV line + hash of the normalised email), and
duplicates are detected with plain code, never the LLM.
"""
import csv
import hashlib
import io
import re

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
HEADER_ALIASES = {
    "name": {"name", "full name", "full_name", "display name", "employee name"},
    "email": {"email", "e-mail", "work email", "work_email", "email address", "mail"},
    "role": {"role", "access role", "job role", "requested role", "access level", "title"},
    "department": {"department", "dept", "team"},
}


def normalise_email(raw: str) -> str:
    return (raw or "").strip().lower()


def _map_headers(fieldnames: list[str]) -> dict[str, str]:
    mapping = {}
    for original in fieldnames:
        key = (original or "").strip().lower()
        for canonical, aliases in HEADER_ALIASES.items():
            if key in aliases and canonical not in mapping:
                mapping[canonical] = original
    return mapping


def load_csv(text: str) -> tuple[list[dict], list[str]]:
    """Returns (records, file_level_errors). Row-level problems are kept on the record."""
    text = text.lstrip("﻿")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        return [], ["CSV is empty or has no header row"]
    headers = _map_headers(reader.fieldnames)
    missing = [c for c in ("name", "email", "role") if c not in headers]
    if missing:
        return [], [f"CSV is missing required column(s): {', '.join(missing)}. Found: {reader.fieldnames}"]

    records, first_seen = [], {}
    for line_no, row in enumerate(reader, start=2):  # line 1 is the header
        name = (row.get(headers["name"]) or "").strip()
        email_raw = row.get(headers["email"]) or ""
        email = normalise_email(email_raw)
        role = (row.get(headers["role"]) or "").strip()
        dept = (row.get(headers.get("department", ""), "") or "").strip() if "department" in headers else ""
        if not (name or email or role):
            continue  # blank line
        rec = {
            "record_id": f"L{line_no:03d}-{hashlib.sha1(email.encode()).hexdigest()[:6]}",
            "line": line_no,
            "name": name,
            "email": email,
            "email_raw": email_raw,
            "csv_role": role,
            "department": dept,
            "problem": None,
            "duplicate_of": None,
        }
        problems = []
        if not name:
            problems.append("missing name")
        if not EMAIL_RE.match(email):
            problems.append(f"invalid email '{email_raw.strip()}'")
        if not role:
            problems.append("missing role")
        if problems:
            rec["problem"] = "; ".join(problems)
        elif email in first_seen:
            rec["duplicate_of"] = first_seen[email]
        else:
            first_seen[email] = rec["record_id"]
        records.append(rec)
    if not records:
        return [], ["CSV has a header but no data rows"]
    return records, []
