const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const STEPS = [["collect", "Collect"], ["plan", "Plan"], ["approve", "Approve"], ["apply", "Apply"], ["verify", "Verify"], ["report", "Report"]];
const TRANSIENT = new Set(["collected", "planning", "approved", "applying", "verifying"]);

let state = { runId: null, lastEvent: 0, planKey: null, tab: "created", timer: null, data: null };

async function api(path, opts = {}) {
  const res = await fetch(path, opts);
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.detail || res.statusText);
  return body;
}

function show(view) {
  $("view-new").classList.toggle("hidden", view !== "new");
  $("view-run").classList.toggle("hidden", view !== "run");
}

// ------------------------------------------------------------------ sidebar
async function loadInfo() {
  const info = await api("/api/info");
  $("llm-chip").textContent = "LLM: " + info.llm;
  $("console-link").href = info.console_url;
  $("scenario-list").innerHTML = info.scenarios.map((s) => `
    <button class="tc-row" data-tc="${s.id}" title="Expected: ${esc(s.expected)}">
      <b>${s.id}</b><span>${esc(s.title)}</span><i>▶</i></button>`).join("");
  document.querySelectorAll("[data-tc]").forEach((b) => b.onclick = async () => {
    b.disabled = true;
    $("tc-msg").textContent = "";
    try { const r = await api(`/api/scenarios/${b.dataset.tc}`, { method: "POST" }); openRun(r.run_id); }
    catch (e) { $("tc-msg").textContent = e.message; } finally { b.disabled = false; }
  });
}

async function loadRuns() {
  const runs = await api("/api/runs");
  $("run-count").textContent = runs.length || "";
  $("run-list").innerHTML = runs.slice(0, 12).map((r) => `
    <a href="#${r.id}" class="${r.id === state.runId ? "active" : ""}" data-run="${r.id}">
      <b>${esc(r.scenario || r.csv_name)}</b>
      <span class="muted">${esc(r.created_at.slice(11, 16))} · ${esc(r.verdict ? r.verdict.toUpperCase() : r.status.replace(/_/g, " "))}</span>
    </a>`).join("") || '<span class="muted">No runs yet</span>';
  document.querySelectorAll("[data-run]").forEach((a) => a.onclick = (e) => { e.preventDefault(); openRun(a.dataset.run); });
}

async function loadPolicy() {
  const rows = await api("/api/policy/acme");
  $("policy-count").textContent = rows.length || "";
  $("policy-list").innerHTML = rows.map((r) => `<div title="Approved by ${esc(r.approved_by)} · used ${r.times_used}× · first decided by ${esc(r.decided_by)}">
      <span>${esc(r.csv_role)} → <b>${esc(r.console_role)}</b></span>
      <button title="Forget this mapping" data-forget="${esc(r.csv_role)}">×</button></div>`).join("")
    || '<span class="muted small">Nothing learned yet.</span>';
  document.querySelectorAll("[data-forget]").forEach((b) => b.onclick = async () => {
    await api(`/api/policy/acme?csv_role=${encodeURIComponent(b.dataset.forget)}`, { method: "DELETE" });
    loadPolicy();
  });
}
$("btn-clear-policy").onclick = async () => { await api("/api/policy/acme", { method: "DELETE" }); loadPolicy(); };

// ------------------------------------------------------------------ new run
$("btn-new").onclick = () => { state.runId = null; clearTimeout(state.timer); show("new"); location.hash = ""; };
$("new-form").onsubmit = async (e) => {
  e.preventDefault();
  $("new-error").textContent = "";
  try {
    const r = await api("/api/runs", { method: "POST", body: new FormData(e.target) });
    e.target.reset();
    openRun(r.run_id);
  } catch (err) { $("new-error").textContent = err.message; }
};

// ------------------------------------------------------------------ run view
function openRun(id) {
  clearTimeout(state.timer);
  state = { ...state, runId: id, lastEvent: 0, planKey: null, tab: "created", data: null };
  $("log").innerHTML = "";
  $("approver").value = "";
  $("admin-grants").checked = false;
  $("live-shot").removeAttribute("src");
  location.hash = id;
  show("run");
  poll();
}

async function poll() {
  const id = state.runId;
  if (!id) return;
  try {
    const d = await api(`/api/runs/${id}?after=${state.lastEvent}`);
    if (id !== state.runId) return;
    state.data = d;
    render(d);
    loadRuns();
    loadPolicy();
    state.timer = setTimeout(poll, d.busy || TRANSIENT.has(d.run.status) ? 1000 : 4000);
  } catch (e) {
    state.timer = setTimeout(poll, 3000);
  }
}

function render(d) {
  const r = d.run;
  $("run-title").textContent = r.scenario ? `${r.scenario} · ${d.scenario_info.title}` : r.csv_name;
  $("run-meta").innerHTML = `Tenant <b>${esc(d.tenant_info ? d.tenant_info.display_name : r.tenant)}</b> · ref ${esc(r.approval_ref)}` +
    (r.approved_by ? ` · approved by <b>${esc(r.approved_by)}</b>` : "");
  $("run-meta").title = r.id;
  $("link-input").href = `/api/runs/${r.id}/input.csv`;
  $("link-evidence").href = `/api/runs/${r.id}/evidence.json`;
  $("run-status").className = "pill " + r.status;
  $("run-status").textContent = r.status.replace(/_/g, " ");
  $("busy").textContent = d.busy ? "working…" : "";

  const idx = STEPS.findIndex((s) => s[0] === r.stage);
  $("stepper").innerHTML = STEPS.map((s, i) =>
    `<li class="${i < idx || r.status === "completed" ? "done" : i === idx ? "current" : ""}">${s[1]}</li>`).join("");

  $("error-card").classList.toggle("hidden", !r.error || r.status !== "error");
  $("error-card").innerHTML = `<b>Stopped:</b> ${esc(r.error)}`;

  renderScenario(d);
  if (d.live_shot) { $("live-shot").src = d.live_shot; $("live-link").href = d.live_shot; }
  renderLog(d.events);
  if (d.plan_counts) renderPlan(d);
  renderResult(d);
}

function renderScenario(d) {
  const card = $("scenario-card");
  if (!d.scenario_info) { card.classList.add("hidden"); return; }
  card.classList.remove("hidden");
  const res = d.scenario_result;
  const verdict = res ? res.verdict.replace("_", " ").toUpperCase() : "NOT RUN YET";
  card.innerHTML = `<span><b>Expected:</b> ${esc(d.scenario_info.expected)}</span>
    <span class="pill ${res ? res.verdict : ""}">${verdict}</span>
    ${res ? `<details><summary>${esc(res.detail)}</summary>
      <ul class="checks">${res.checks.map(([n, ok]) => `<li>${ok ? "✅" : "❌"} ${esc(n)}</li>`).join("")}</ul></details>` : ""}`;
}

function renderLog(events) {
  const box = $("log");
  for (const e of events) {
    state.lastEvent = Math.max(state.lastEvent, e.id);
    const div = document.createElement("div");
    div.className = e.level;
    div.innerHTML = `<span class="step">${esc(e.ts.slice(11, 19))} ${esc(e.step)}</span>${esc(e.message)}`;
    box.prepend(div);
  }
}

function renderPlan(d) {
  $("plan-card").classList.remove("hidden");
  const r = d.run, c = d.plan_counts;
  const editable = r.status === "awaiting_approval" && !d.busy;
  const parts = [`<b>${c.create}</b> to create`];
  if (c.skip) parts.push(`<b>${c.skip}</b> skipped`);
  if (c.review) parts.push(`<b class="warn-text">${c.review}</b> need review`);
  if (c.privileged) parts.push(`<b class="warn-text">${c.privileged}</b> admin`);
  parts.push(`${c.batches} batch${c.batches === 1 ? "" : "es"}`);
  $("plan-summary").innerHTML = parts.join(" · ") + (d.role_source ? ` · roles by ${esc(d.role_source)}` : "");

  const key = r.plan_hash + editable;
  if (state.planKey === key) return;  // rebuild only when the plan or its editability changes
  const firstRender = state.planKey === null || state.planKey.endsWith("true") !== editable;
  state.planKey = key;
  if (firstRender) $("plan-details").open = editable;  // fold the plan away once it's approved

  $("role-table").innerHTML = `<tr><th>Job title (CSV)</th><th>Console role</th><th>Confidence</th><th>Source</th></tr>` +
    Object.entries(d.role_mappings).map(([csvRole, m]) => `<tr>
      <td>${esc(csvRole)}<div class="why" title="${esc(m.reason)}">${esc(m.source === "policy" ? `learned · approved by ${m.approved_by || "?"}` : m.reason)}</div></td>
      <td>${editable ? `<select data-role="${esc(csvRole)}">${["", ...d.console_roles].map((o) =>
        `<option ${o === (m.console_role || "") ? "selected" : ""} value="${esc(o)}">${o || "—"}</option>`).join("")}</select>`
        : `<b>${esc(m.console_role || "—")}</b>`}
        ${m.status !== "ok" ? `<span class="tag ${m.status}">${esc(m.status)}</span>` : ""}</td>
      <td>${(m.confidence * 100).toFixed(0)}%</td>
      <td><span class="tag ${m.source === "policy" ? "policy" : ""}">${esc(m.source)}</span></td></tr>`).join("");
  document.querySelectorAll("[data-role]").forEach((s) => s.onchange = async () => {
    if (!s.value) return;
    try { await api(`/api/runs/${r.id}/roles`, { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ csv_role: s.dataset.role, console_role: s.value }) }); poll(); }
    catch (e) { alert(e.message); }
  });

  const exceptions = d.items.filter((i) => i.action !== "create" || (i.reason || "").includes("PRIVILEGED"));
  $("exceptions").innerHTML = exceptions.map((i) => `<div><span class="tag ${i.action === "create" ? "privileged" : i.action}">
      ${i.action === "create" ? "admin" : esc(i.action)}</span> <b>L${i.line}</b> ${esc(i.email)} <span class="muted">— ${esc(i.reason)}</span></div>`).join("")
    || '<span class="muted small">None. Every row will be created with its mapped role.</span>';

  $("row-count").textContent = d.items.length;
  $("plan-table").innerHTML = `<tr><th>Line</th><th>Name</th><th>Email</th><th>Role</th><th>Decision</th><th>Batch</th></tr>` +
    d.items.map((i) => `<tr title="${esc(i.record_id)} · ${esc(i.reason)}"><td>${i.line}</td><td>${esc(i.name)}</td><td>${esc(i.email)}</td>
      <td>${esc(i.csv_role)} → <b>${esc(i.target_role || "—")}</b></td><td><span class="tag ${i.action}">${esc(i.action)}</span></td>
      <td>${i.batch ?? ""}</td></tr>`).join("");

  $("approve-box").classList.toggle("hidden", !editable);
  $("admin-box").classList.toggle("hidden", !c.privileged);
  $("admin-count").textContent = c.privileged;
}

$("btn-approve").onclick = async () => {
  const d = state.data;
  $("approve-error").textContent = "";
  try {
    await api(`/api/runs/${d.run.id}/approve`, { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ approver: $("approver").value, plan_hash: d.run.plan_hash, admin_grants: $("admin-grants").checked }) });
    state.tab = "created";
    poll();
  } catch (e) { $("approve-error").textContent = e.message; }
};

$("btn-retry").onclick = async () => {
  $("btn-retry").disabled = true;
  try { await api(`/api/runs/${state.runId}/retry`, { method: "POST" }); poll(); }
  catch (e) { alert(e.message); } finally { $("btn-retry").disabled = false; }
};

function renderResult(d) {
  const has = d.summary || d.run.apply_attempts;
  $("result-card").classList.toggle("hidden", !has);
  if (!has) return;
  $("btn-retry").classList.toggle("hidden", !(["needs_retry", "error"].includes(d.run.status) && !d.busy));
  const groups = { created: [], skipped: [], failed: [], pending: [] };
  d.items.forEach((i) => groups[{ verified: "created", skipped: "skipped", failed: "failed" }[i.status] || "pending"].push(i));
  $("result-tabs").innerHTML = Object.entries(groups).map(([k, v]) =>
    `<button data-tab="${k}" class="${state.tab === k ? "active" : ""} ${v.length && k === "failed" ? "bad" : ""}">
      ${k[0].toUpperCase() + k.slice(1)} <b>${v.length}</b></button>`).join("");
  document.querySelectorAll("[data-tab]").forEach((b) => b.onclick = () => { state.tab = b.dataset.tab; renderResult(state.data); });

  $("result-table").innerHTML = `<tr><th>Line</th><th>Email</th><th>Role</th><th>Reason</th><th>Evidence</th></tr>` +
    (groups[state.tab].map((i) => {
      const v = i.evidence.verification || {};
      const seen = v.at ? (v.found ? `Seen in console as ${v.observed_role}, ${v.occurrences}× at ${v.at.slice(11, 19)}` : `Not found at ${v.at.slice(11, 19)}`) : "";
      return `<tr><td title="${esc(i.record_id)}">${i.line}</td><td>${esc(i.email)}</td><td>${esc(i.target_role || "—")}</td>
        <td class="muted" title="${esc(seen)}">${esc(i.reason)}</td>
        <td class="shots">${i.screenshots.length ? `<a href="${i.screenshots[0]}" target="_blank">before</a>` : ""}
          ${i.screenshots.length > 1 ? `<a href="${i.screenshots[i.screenshots.length - 1]}" target="_blank">after</a>` : ""}</td></tr>`;
    }).join("") || `<tr><td colspan="5" class="muted">None</td></tr>`);

  const a = d.attempt_summaries;
  const showAttempts = a.length > 1 || (a[0] && (a[0].failed || a[0].pending));
  $("attempts-wrap").classList.toggle("hidden", !showAttempts);
  $("attempt-table").innerHTML = `<tr><th>Attempt</th><th>Created this attempt</th><th>Total created</th><th>Failed</th><th>Pending</th><th>Duplicates</th></tr>` +
    a.map((x) => `<tr><td>#${x.attempt}</td><td><b>${x.created_this_attempt}</b></td><td>${x.created}</td><td>${x.failed}</td>
      <td>${x.pending}</td><td>${x.duplicate_emails_in_console.length || "none"}</td></tr>`).join("");
}

// ------------------------------------------------------------------ boot
loadInfo();
loadRuns();
loadPolicy();
if (location.hash.length > 1) openRun(location.hash.slice(1)); else show("new");
