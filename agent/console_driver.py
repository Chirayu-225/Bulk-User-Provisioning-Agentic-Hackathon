"""Browser tool: drives the admin console's web UI with Playwright (there is no API).

Every read returns the source URL and time it was observed, so decisions can be
traced back to evidence. Screenshots are saved for each create.
"""
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

from playwright.sync_api import sync_playwright

import config
from agent.reasoning import is_privileged


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class ConsoleError(RuntimeError):
    pass


class ConsoleDriver:
    def __init__(self, shots_dir: Path, on_event=None, on_shot=None):
        self.base = config.CONSOLE_URL.rstrip("/")
        self.shots_dir = Path(shots_dir)
        self.shots_dir.mkdir(parents=True, exist_ok=True)
        self.on_event = on_event or (lambda *a, **k: None)
        self.on_shot = on_shot or (lambda path: None)
        self._pw = self._browser = self.page = None

    # ------------------------------------------------------------ lifecycle
    def __enter__(self):
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=config.HEADLESS, slow_mo=config.SLOW_MO_MS)
        context = self._browser.new_context(viewport={"width": 1100, "height": 760})
        self.page = context.new_page()
        self.page.set_default_timeout(15000)
        return self

    def __exit__(self, *exc):
        try:
            if self._browser:
                self._browser.close()
        finally:
            if self._pw:
                self._pw.stop()

    def shot(self, label: str) -> str:
        name = f"{int(time.time() * 1000)}-{label}.png"
        path = self.shots_dir / name
        self.page.screenshot(path=str(path))
        self.on_shot(str(path))
        return str(path)

    # ------------------------------------------------------------ navigation
    def login(self) -> dict:
        self.page.goto(f"{self.base}/login")
        self.page.fill("#username", config.CONSOLE_USER)
        self.page.fill("#password", config.CONSOLE_PASSWORD)
        self.page.click("#login-submit")
        self.page.wait_for_load_state("domcontentloaded")
        if "/login" in self.page.url:
            raise ConsoleError("Login failed — check CONSOLE_USER / CONSOLE_PASSWORD")
        self.on_event("browse", "Signed in to admin console", {"url": self.page.url})
        return {"url": self.page.url, "at": _now()}

    def open_tenant(self, tenant: str) -> dict:
        resp = self.page.goto(f"{self.base}/t/{tenant}/users")
        if resp is not None and resp.status == 404:
            raise ConsoleError(f"Tenant '{tenant}' does not exist in the console")
        shown = self.page.get_attribute("body", "data-tenant")
        display = self.page.text_content("#tenant-name") or ""
        if shown != tenant:
            raise ConsoleError(f"Console is showing tenant '{shown}', expected '{tenant}' — stopping")
        self.on_event("browse", f"Confirmed tenant context: {display.strip()} ({tenant})", {"url": self.page.url})
        return {"tenant": tenant, "display_name": display.strip(), "url": self.page.url, "at": _now()}

    # ------------------------------------------------------------ reads
    def read_users(self, tenant: str) -> dict:
        """Reads every page of the user list. Returns {users: [...], pages: [...urls], at}."""
        url = f"{self.base}/t/{tenant}/users"
        users, pages = [], []
        while url:
            self.page.goto(url)
            pages.append(self.page.url)
            rows = self.page.eval_on_selector_all(
                "#user-table tbody tr",
                """rows => rows.map(r => ({
                    name: (r.querySelector('.c-name')||{}).innerText || '',
                    email: (r.querySelector('.c-email')||{}).innerText || '',
                    role: (r.querySelector('.c-role')||{}).innerText || ''}))""")
            for r in rows:
                users.append({"name": r["name"].strip(), "email": r["email"].strip().lower(),
                              "role": r["role"].strip(), "source_url": self.page.url})
            nxt = self.page.query_selector("a[rel=next]")
            url = urljoin(self.page.url, nxt.get_attribute("href")) if nxt else None
            if len(pages) > 200:
                raise ConsoleError("Pagination did not terminate")
        self.on_event("observe", f"Read {len(users)} existing users across {len(pages)} page(s)",
                      {"pages": pages})
        return {"users": users, "pages": pages, "at": _now()}

    def inspect_add_form(self, tenant: str) -> dict:
        self.page.goto(f"{self.base}/t/{tenant}/users/new")
        fields = self.page.eval_on_selector_all(
            "form#add-user-form input, form#add-user-form select, form input, form select",
            """els => els.filter(e => e.name).map(e => {
                const lab = e.id ? document.querySelector(`label[for="${e.id}"]`) : null;
                return {name: e.name, id: e.id || '', tag: e.tagName.toLowerCase(), type: e.type || '',
                        label: lab ? lab.innerText.trim() : '',
                        options: e.tagName === 'SELECT' ? Array.from(e.options).map(o =>
                            ({value: o.value, text: o.text.trim(), selected: o.selected})) : []};
            })""")
        seen, unique = set(), []
        for f in fields:
            if f["name"] not in seen:
                seen.add(f["name"])
                unique.append(f)
        self.on_event("observe", f"Inspected 'Add user' form: {len(unique)} fields",
                      {"fields": [f"{f['label'] or f['name']} ({f['tag']})" for f in unique]})
        return {"fields": unique, "url": self.page.url, "at": _now(), "screenshot": self.shot("add-form")}

    # ------------------------------------------------------------ action
    def _selected_role(self, role_field: str) -> str:
        return self.page.eval_on_selector(
            f"select[name='{role_field}']",
            "s => s.selectedIndex >= 0 ? s.options[s.selectedIndex].text.trim() : ''")

    def create_user(self, tenant: str, field_map: dict, item: dict) -> dict:
        """Fills and submits the Add-user form for one record. Never retries on its own."""
        target_role = item["target_role"]
        result = {"outcome": None, "message": "", "default_role_seen": None, "corrected": False,
                  "role_before_save": None, "status_code": None, "at": _now(), "screenshots": []}
        self.page.goto(f"{self.base}/t/{tenant}/users/new")
        role_sel = f"select[name='{field_map['role']}']"
        has_default = self.page.eval_on_selector(role_sel, "s => s.selectedIndex >= 0 && s.value !== ''")
        result["default_role_seen"] = self._selected_role(field_map["role"]) if has_default else None

        self.page.fill(f"[name='{field_map['full_name']}']", item["name"])
        self.page.fill(f"[name='{field_map['email']}']", item["email"])
        self.page.press(f"[name='{field_map['email']}']", "Tab")  # fire 'change' like a real user would
        if item.get("department"):
            dept = self.page.query_selector("[name='dept']")
            if dept:
                dept.fill(item["department"])

        # Role is set LAST, then re-read, so console scripts that reset it can't slip through.
        for _ in range(2):
            self.page.select_option(role_sel, label=target_role)
            if self._selected_role(field_map["role"]) == target_role:
                break
        result["role_before_save"] = self._selected_role(field_map["role"])
        if result["role_before_save"] != target_role:
            result.update(outcome="failed", message=f"Role field shows '{result['role_before_save']}' "
                          f"after selecting '{target_role}' — not saving")
            result["screenshots"].append(self.shot(f"{item['record_id']}-role-mismatch"))
            return result
        seen = result["default_role_seen"]
        if seen and seen != target_role:
            result["corrected"] = True
            level = "warn" if is_privileged(seen) else "info"
            self.on_event("guard", f"{item['email']}: console pre-selected '{seen}' — corrected to "
                          f"'{target_role}' before save", {"record_id": item["record_id"]}, level=level)
        result["screenshots"].append(self.shot(f"{item['record_id']}-before-save"))

        with self.page.expect_response(lambda r: r.request.method == "POST") as resp_info:
            self.page.click("form#add-user-form button[type=submit], form button[type=submit]")
        response = resp_info.value
        self.page.wait_for_load_state("domcontentloaded")
        result["status_code"] = response.status
        flash = self.page.query_selector("#flash")
        form_err = self.page.query_selector("#form-error")
        server_err = self.page.query_selector("#server-error")
        if flash and item["email"] in (flash.inner_text() or "").lower():
            result.update(outcome="created", message=flash.inner_text().strip())
        elif form_err and "already exists" in (form_err.inner_text() or "").lower():
            result.update(outcome="exists", message=form_err.inner_text().strip())
        else:
            text = (server_err or form_err).inner_text().strip() if (server_err or form_err) else \
                f"Unexpected page after save (HTTP {response.status}) at {self.page.url}"
            result.update(outcome="failed", message=text)
        result["screenshots"].append(self.shot(f"{item['record_id']}-after-save"))
        return result
