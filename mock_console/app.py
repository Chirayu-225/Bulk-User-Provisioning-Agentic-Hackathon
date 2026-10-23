"""Mock SaaS admin console — the demo target.

It deliberately has NO API for user management: users can only be listed and
created through HTML pages, like many real admin consoles.

Built-in traps used by the official test cases (switched on from /__control):
  * default_role_admin  — the "Add user" form pre-selects Admin, and resets the
                          role back to Admin whenever the email field changes (TC3)
  * fail_at             — the Nth create request fails once (TC4)
  * fail_mode           — "before_save" (nothing saved) or "after_save"
                          (saved, but the console still shows an error)

/__control is a test-harness page for setting up scenarios. The agent never uses it.
"""
import html
import secrets
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402

app = FastAPI(title="Mock Admin Console", docs_url=None, redoc_url=None, openapi_url=None)

ROLES = [("r-admin", "Admin"), ("r-manager", "Manager"), ("r-member", "Member"),
         ("r-billing", "Billing"), ("r-viewer", "Viewer")]
ROLE_BY_VALUE = dict(ROLES)
TENANTS = {"acme": "Acme Corp", "globex": "Globex Inc"}
PAGE_SIZE = 10

_lock = threading.Lock()
_sessions: set[str] = set()
STATE: dict = {}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _seed_users() -> list[dict]:
    return [
        {"id": 1, "tenant": "acme", "name": "Ava Owner", "email": "ava.owner@acme.example",
         "role": "Admin", "department": "IT", "created_at": _now()},
        {"id": 2, "tenant": "acme", "name": "Ben Ops", "email": "ben.ops@acme.example",
         "role": "Member", "department": "Operations", "created_at": _now()},
        {"id": 3, "tenant": "globex", "name": "Gia Lead", "email": "gia.lead@globex.example",
         "role": "Admin", "department": "IT", "created_at": _now()},
    ]


def reset_state() -> None:
    with _lock:
        STATE.clear()
        STATE.update({
            "users": _seed_users(),
            "next_id": 4,
            "config": {"default_role_admin": False, "fail_at": None, "fail_mode": "before_save",
                       "fail_once": True},
            "create_attempts": 0,
            "failure_fired": False,
        })


reset_state()

CSS = """
*{box-sizing:border-box}body{margin:0;font-family:Segoe UI,Arial,sans-serif;background:#f3f4f6;color:#1f2937}
header{background:#1e293b;color:#fff;padding:12px 24px;display:flex;justify-content:space-between;align-items:center}
header a{color:#cbd5e1;margin-left:16px;text-decoration:none}main{max-width:980px;margin:24px auto;padding:0 16px}
.card{background:#fff;border:1px solid #e5e7eb;border-radius:8px;padding:20px;margin-bottom:16px}
table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:8px;border-bottom:1px solid #e5e7eb;font-size:14px}
th{background:#f9fafb}label{display:block;font-weight:600;margin:12px 0 4px;font-size:14px}
input,select{width:100%;padding:8px;border:1px solid #d1d5db;border-radius:6px;font-size:14px}
button,.btn{background:#2563eb;color:#fff;border:0;padding:9px 16px;border-radius:6px;cursor:pointer;text-decoration:none;font-size:14px;display:inline-block}
.flash{padding:10px 14px;border-radius:6px;margin-bottom:16px}.flash.success{background:#dcfce7;color:#166534}
.flash.error{background:#fee2e2;color:#991b1b}.pager a{margin-right:12px}.muted{color:#6b7280;font-size:13px}
.badge{padding:2px 8px;border-radius:10px;font-size:12px;background:#e0e7ff}.badge.Admin{background:#fde68a}
"""


def page(title: str, body: str, tenant: str | None = None, status: int = 200) -> HTMLResponse:
    nav = ""
    if tenant:
        nav = (f'<span id="tenant-name" data-tenant="{tenant}">{html.escape(TENANTS[tenant])}</span>'
               f'<span><a href="/t/{tenant}/users">Users</a><a href="/t/{tenant}/users/new">Add user</a>'
               f'<a href="/tenants">Switch tenant</a><a href="/logout">Sign out</a></span>')
    doc = (f"<!doctype html><html><head><meta charset='utf-8'><title>{html.escape(title)} · Admin Console</title>"
           f"<style>{CSS}</style></head><body data-tenant='{tenant or ''}'>"
           f"<header><strong>Admin Console</strong>{nav}</header><main>{body}</main></body></html>")
    return HTMLResponse(doc, status_code=status)


def logged_in(request: Request) -> bool:
    return request.cookies.get("session") in _sessions


def need_login() -> RedirectResponse:
    return RedirectResponse("/login", status_code=303)


@app.get("/", response_class=HTMLResponse)
def root(request: Request):
    return RedirectResponse("/tenants" if logged_in(request) else "/login", status_code=303)


@app.get("/login", response_class=HTMLResponse)
def login_page(error: str = ""):
    msg = '<div class="flash error">Invalid username or password</div>' if error else ""
    return page("Sign in", f"""<div class="card" style="max-width:380px;margin:60px auto">{msg}
      <h2>Sign in</h2><form method="post" action="/login" id="login-form">
      <label for="username">Username</label><input id="username" name="username" autocomplete="off">
      <label for="password">Password</label><input id="password" name="password" type="password">
      <p><button type="submit" id="login-submit">Sign in</button></p></form>
      <p class="muted">Demo credentials: {config.CONSOLE_USER} / {config.CONSOLE_PASSWORD}</p></div>""")


@app.post("/login")
def login(username: str = Form(""), password: str = Form("")):
    if username == config.CONSOLE_USER and password == config.CONSOLE_PASSWORD:
        token = secrets.token_hex(16)
        _sessions.add(token)
        resp = RedirectResponse("/tenants", status_code=303)
        resp.set_cookie("session", token, httponly=True)
        return resp
    return RedirectResponse("/login?error=1", status_code=303)


@app.get("/logout")
def logout(request: Request):
    _sessions.discard(request.cookies.get("session"))
    return RedirectResponse("/login", status_code=303)


@app.get("/tenants", response_class=HTMLResponse)
def tenants(request: Request):
    if not logged_in(request):
        return need_login()
    links = "".join(f'<li><a href="/t/{k}/users" data-tenant="{k}">{html.escape(v)}</a></li>' for k, v in TENANTS.items())
    return page("Tenants", f'<div class="card"><h2>Choose a tenant</h2><ul id="tenant-list">{links}</ul></div>')


@app.get("/t/{tenant}/users", response_class=HTMLResponse)
def list_users(request: Request, tenant: str, page_no: int = 1, created: str = ""):
    if not logged_in(request):
        return need_login()
    if tenant not in TENANTS:
        return page("Not found", '<div class="flash error">Unknown tenant</div>', status=404)
    with _lock:
        users = [u for u in STATE["users"] if u["tenant"] == tenant]
    total = len(users)
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page_no = min(max(1, page_no), pages)
    rows = "".join(
        f'<tr class="user-row" data-user-id="{u["id"]}" data-email="{html.escape(u["email"])}">'
        f'<td class="c-name">{html.escape(u["name"])}</td><td class="c-email">{html.escape(u["email"])}</td>'
        f'<td class="c-role"><span class="badge {u["role"]}">{u["role"]}</span></td>'
        f'<td>{html.escape(u.get("department") or "")}</td><td class="muted">{u["created_at"]}</td></tr>'
        for u in users[(page_no - 1) * PAGE_SIZE: page_no * PAGE_SIZE])
    pager = ""
    if page_no > 1:
        pager += f'<a rel="prev" href="/t/{tenant}/users?page_no={page_no - 1}">← Previous</a>'
    if page_no < pages:
        pager += f'<a rel="next" href="/t/{tenant}/users?page_no={page_no + 1}">Next →</a>'
    flash = f'<div class="flash success" id="flash">User {html.escape(created)} created</div>' if created else ""
    return page("Users", f"""{flash}<div class="card"><div style="display:flex;justify-content:space-between">
      <h2>Users <span class="muted" id="user-total">({total} total)</span></h2>
      <div><a class="btn" href="/t/{tenant}/users/new">+ Add user</a></div></div>
      <table id="user-table"><thead><tr><th>Name</th><th>Email</th><th>Role</th><th>Department</th><th>Created</th></tr></thead>
      <tbody>{rows}</tbody></table>
      <p class="pager">{pager} <span class="muted">Page {page_no} of {pages}</span></p></div>""", tenant)


def _form_html(tenant: str, error: str = "", values: dict | None = None) -> str:
    values = values or {}
    cfg = STATE["config"]
    opts = "" if cfg["default_role_admin"] else '<option value="">— Select role —</option>'
    for val, label in ROLES:
        selected = " selected" if (cfg["default_role_admin"] and val == "r-admin") else ""
        opts += f'<option value="{val}"{selected}>{label}</option>'
    trap_js = ""
    if cfg["default_role_admin"]:
        # Realistic console quirk: editing the email re-applies the tenant's default role.
        trap_js = """<script>document.getElementById('f-mail').addEventListener('change',function(){
          document.getElementById('f-access').value='r-admin';});</script>"""
    err = f'<div class="flash error" id="form-error">{html.escape(error)}</div>' if error else ""
    v = {k: html.escape(values.get(k, "")) for k in ("full_name", "work_email", "dept")}
    return f"""{err}<div class="card" style="max-width:560px"><h2>Add user</h2>
      <form method="post" id="add-user-form" action="/t/{tenant}/users/new">
      <label for="f-display">Full name</label><input id="f-display" name="full_name" value="{v['full_name']}">
      <label for="f-mail">Work email</label><input id="f-mail" name="work_email" value="{v['work_email']}">
      <label for="f-dept">Department (optional)</label><input id="f-dept" name="dept" value="{v['dept']}">
      <label for="f-access">Access level</label><select id="f-access" name="access_role">{opts}</select>
      <p><button type="submit" id="save-user">Save user</button></p></form></div>{trap_js}"""


@app.get("/t/{tenant}/users/new", response_class=HTMLResponse)
def new_user_form(request: Request, tenant: str):
    if not logged_in(request):
        return need_login()
    if tenant not in TENANTS:
        return page("Not found", '<div class="flash error">Unknown tenant</div>', status=404)
    return page("Add user", _form_html(tenant), tenant)


@app.post("/t/{tenant}/users/new")
def create_user(request: Request, tenant: str, full_name: str = Form(""), work_email: str = Form(""),
                dept: str = Form(""), access_role: str = Form("")):
    if not logged_in(request):
        return need_login()
    if tenant not in TENANTS:
        return page("Not found", '<div class="flash error">Unknown tenant</div>', status=404)
    values = {"full_name": full_name, "work_email": work_email, "dept": dept}
    email = work_email.strip()
    if not full_name.strip() or "@" not in email or access_role not in ROLE_BY_VALUE:
        return page("Add user", _form_html(tenant, "Name, a valid email and an access level are required", values), tenant, 422)
    with _lock:
        STATE["create_attempts"] += 1
        cfg = STATE["config"]
        should_fail = (cfg["fail_at"] and STATE["create_attempts"] == cfg["fail_at"]
                       and not (cfg["fail_once"] and STATE["failure_fired"]))
        if any(u["tenant"] == tenant and u["email"].lower() == email.lower() for u in STATE["users"]):
            dup = True
        else:
            dup = False
        if should_fail:
            STATE["failure_fired"] = True
        if not dup and (not should_fail or cfg["fail_mode"] == "after_save"):
            STATE["users"].append({"id": STATE["next_id"], "tenant": tenant, "name": full_name.strip(),
                                   "email": email, "role": ROLE_BY_VALUE[access_role],
                                   "department": dept.strip(), "created_at": _now()})
            STATE["next_id"] += 1
    if dup:
        return page("Add user", _form_html(tenant, f"A user with email {email} already exists", values), tenant, 409)
    if should_fail:
        return page("Error", '<div class="flash error" id="server-error">Something went wrong while saving. '
                    'Please try again later. (Error 500)</div>', tenant, 500)
    return RedirectResponse(f"/t/{tenant}/users?created={email}", status_code=303)


# ---------------- Test harness (scenario setup only — not used by the agent) ----------------

@app.get("/__control", response_class=HTMLResponse)
def control_page():
    c = STATE["config"]
    return page("Test harness", f"""<div class="card"><h2>Test harness</h2>
      <p class="muted">Scenario setup for demos. The provisioning agent never calls these endpoints.</p>
      <pre id="cfg">{html.escape(str(c))}</pre><p>Create attempts so far: {STATE['create_attempts']}</p>
      <p><button onclick="post('/__control/config',{{default_role_admin:{str(not c['default_role_admin']).lower()}}})">
      Toggle "default new users to Admin"</button></p>
      <p><button onclick="post('/__control/config',{{fail_at:12}})">Fail at create #12 (once)</button>
      <button onclick="post('/__control/config',{{fail_at:null}})">Clear failure</button></p>
      <p><button onclick="post('/__control/reset',{{}})" style="background:#dc2626">Reset console data</button></p></div>
      <script>function post(u,b){{fetch(u,{{method:'POST',headers:{{'Content-Type':'application/json'}},
      body:JSON.stringify(b)}}).then(()=>location.reload())}}</script>""")


@app.post("/__control/config")
async def control_config(request: Request):
    body = await request.json()
    with _lock:
        for key in ("default_role_admin", "fail_at", "fail_mode", "fail_once"):
            if key in body:
                STATE["config"][key] = body[key]
        STATE["create_attempts"] = 0
        STATE["failure_fired"] = False
    return JSONResponse({"ok": True, "config": STATE["config"]})


@app.post("/__control/reset")
def control_reset():
    reset_state()
    return JSONResponse({"ok": True})


@app.get("/__control/state")
def control_state():
    with _lock:
        return JSONResponse({"config": STATE["config"], "create_attempts": STATE["create_attempts"],
                             "users": STATE["users"]})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=config.CONSOLE_PORT)
