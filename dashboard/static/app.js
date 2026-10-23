const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const STEPS = [
  ["collect", "1 · Collect", "CSV, tenant, approval ref"],
  ["plan", "2 · Plan", "read console, dedupe, map roles"],
  ["approve", "3 · Approve", "human signs off the plan"],
  ["apply", "4 · Apply", "create users in batches"],
  ["verify", "5 · Verify", "re-read user list"],
  ["report", "Report", "created / skipped / failed"],
];
const TRANSIENT = new Set(["collected", "planning", "approved", "applying", "verifying"]);

let state = { runId: null, lastEvent: 0, planHash: null, tab: "created", timer: null, data: null };

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
    <div class="scn"><b>${s.id} · ${esc(s.title)}</b><span class="muted">Expected: ${esc(s.expected)}</span>
    <button class="btn small" data-tc="${s.id}">Set up console &amp; plan</button></div>`).join("");
  document.querySelectorAll("[data-tc]").forEach((b) => b.onclick = async () => {
    b.disabled = true;
    try { const r = await api(`/api/scenarios/${b.dataset.tc}`, { method: "POST" }); openRun(r.run_id); }
    catch (e) { alert(e.message); } finally { b.disabled = false; }
  });
}

async function loadRuns() {
  const runs = await api("/api/runs");
  $("run-list").innerHTML = runs.map((r) => `
    <a href="#${r.id}" class="${r.id === state.runId ? "active" : ""}" data-run="${r.id}">
      <b>${esc(r.scenario || r.csv_name)}</b> · ${esc(r.tenant)}<br>
      <span class="muted">${esc(r.created_at.replace("T", " ").replace("Z", ""))} · ${esc(r.status)}${r.verdict ? " · " + r.verdict.toUpperCase() : ""}</span>
    </a>`).join("") || '<span class="muted">No runs yet</span>';
  document.querySelectorAll("[data-run]").forEach((a) => a.onclick = (e) => { e.preventDefault(); openRun(a.dataset.run); });
}

async function loadPolicy() {
  const rows = await api("/api/policy/acme");
  $("policy-list").innerHTML = rows.map((r) => `<div><span>${esc(r.csv_role)} → <b>${esc(r.console_role)}</b>
      <span class="muted">approved by ${esc(r.approved_by)} · used ${r.times_used}× · first: ${esc(r.decided_by)}</span></span>
      <button title="Forget this mapping" data-forget="${esc(r.csv_role)}">×</button></div>`).join("")
    || '<span class="muted">Nothing learned yet — approve a plan to teach it.</span>';
  document.querySelectorAll("[data-forget]").forEach((b) => b.onclick = async () => {
    await api(`/api/policy/acme?csv_role=${encodeURIComponent(b.dataset.forget)}`, { method: "DELETE" });
    loadPolicy();
  });
}
$("btn-clear-policy").onclick = async () => {
  await api("/api/policy/acme", { method: "DELETE" });
  loadPolicy();
};

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
  state = { ...state, runId: id, lastEvent: 0, planHash: null, tab: "created", data: null };
  $("log").innerHTML = "";
  $("approver").value = "";
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
    const again = d.busy || TRANSIENT.has(d.run.status);
    state.timer = setTimeout(poll, again ? 1000 : 4000);
  } catch (e) {
    state.timer = setTimeout(poll, 3000);
  }
}

function render(d) {
  const r = d.run;
  $("run-title").textContent = r.scenario ? `${r.scenario} · ${d.scenario_info.title}` : r.csv_name;
  $("run-meta").innerHTML = `Run <span class="mono">${esc(r.id)}</span> · tenant <b>${esc(r.tenant)}</b>` +
    (d.tenant_info ? ` (${esc(d.tenant_info.display_name)})` : "") + ` · approval ref: ${esc(r.approval_ref)}` +
    (r.approved_by ? ` · approved by <b>${esc(r.approved_by)}</b>` : "");
  $("run-status").className = "pill " + r.status;
  $("run-status").textContent = r.status.replace(/_/g, " ");
  $("busy").textContent = d.busy ? "agent working…" : "";

  const idx = STEPS.findIndex((s) => s[0] === r.stage);
  $("stepper").innerHTML = STEPS.map((s, i) =>
    `<li class="${i < idx || r.status === "completed" ? "done" : i === idx ? "current" : ""}"><b>${s[1]}</b>${s[2]}</li>`).join("");

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
  card.innerHTML = `<div class="card-head"><h3>Official test case ${esc(d.run.scenario)}</h3>
      <span class="pill ${res ? res.verdict : ""}">${res ? res.verdict.replace("_", " ").toUpperCase() : "NOT RUN YET"}</span></div>
    <div class="muted">Scenario: ${esc(d.scenario_info.title)} · Input: ${esc(d.scenario_info.csv)} ·
      <b>Expected: ${esc(d.scenario_info.expected)}</b></div>
    ${res ? `<div style="margin-top:6px">Actual: ${esc(res.detail)}</div>
      <ul class="checks">${res.checks.map(([n, ok]) => `<li>${ok ? "✅" : "❌"} ${esc(n)}</li>`).join("")}</ul>` : ""}`;
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
  $("plan-hash").textContent = "plan " + r.plan_hash;
  $("plan-stats").innerHTML = [["create", c.create], ["skip", c.skip], ["needs review", c.review],
    ["batches", c.batches], ["privileged", c.privileged]].map(([k, v]) => `<div class="stat"><b>${v}</b><span>${k}</span></div>`).join("");
  $("role-source").textContent = d.role_source ? `· mapped by ${d.role_source}` : "";

  const editable = r.status === "awaiting_approval" && !d.busy;
  if (state.planHash !== r.plan_hash + editable) {  // rebuild tables only when the plan changes
    state.planHash = r.plan_hash + editable;
    $("role-table").innerHTML = `<tr><th>CSV role</th><th>Console role</th><th>Confidence</th><th>Source</th><th>Status</th><th>Reason</th></tr>` +
      Object.entries(d.role_mappings).map(([csvRole, m]) => `<tr><td>${esc(csvRole)}</td>
        <td>${editable ? `<select data-role="${esc(csvRole)}">${["", ...d.console_roles].map((o) =>
          `<option ${o === (m.console_role || "") ? "selected" : ""} value="${esc(o)}">${o || "—"}</option>`).join("")}</select>` : esc(m.console_role || "—")}</td>
        <td>${(m.confidence * 100).toFixed(0)}%</td><td><span class="tag ${m.source === "policy" ? "policy" : ""}">${esc(m.source)}</span></td>
        <td><span class="tag ${m.status}">${esc(m.status)}</span></td><td class="muted">${esc(m.reason)}</td></tr>`).join("");
    document.querySelectorAll("[data-role]").forEach((s) => s.onchange = async () => {
      if (!s.value) return;
      try { await api(`/api/runs/${r.id}/roles`, { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ csv_role: s.dataset.role, console_role: s.value }) }); poll(); }
      catch (e) { alert(e.message); }
    });
    $("plan-table").innerHTML = `<tr><th>Line</th><th>Record ID</th><th>Name</th><th>Email</th><th>CSV role</th><th>Console role</th><th>Decision</th><th>Batch</th><th>Reason</th></tr>` +
      d.items.map((i) => `<tr><td>${i.line}</td><td class="mono">${esc(i.record_id)}</td><td>${esc(i.name)}</td><td>${esc(i.email)}</td>
        <td>${esc(i.csv_role)}</td><td>${esc(i.target_role || "—")}</td><td><span class="tag ${i.action}">${esc(i.action)}</span></td>
        <td>${i.batch ?? ""}</td><td class="muted">${esc(i.reason)}</td></tr>`).join("");
  }
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
  d.items.forEach((i) => {
    const g = { verified: "created", skipped: "skipped", failed: "failed" }[i.status] || "pending";
    groups[g].push(i);
  });
  $("result-tabs").innerHTML = Object.entries(groups).map(([k, v]) =>
    `<button data-tab="${k}" class="${state.tab === k ? "active" : ""}">${k[0].toUpperCase() + k.slice(1)} (${v.length})</button>`).join("");
  document.querySelectorAll("[data-tab]").forEach((b) => b.onclick = () => { state.tab = b.dataset.tab; renderResult(state.data); });
  const rows = groups[state.tab];
  $("result-table").innerHTML = `<tr><th>Line</th><th>Record ID</th><th>Email</th><th>Role</th><th>Status</th><th>Reason</th><th>Observed in console</th><th>Evidence</th></tr>` +
    (rows.map((i) => {
      const v = i.evidence.verification || {};
      return `<tr><td>${i.line}</td><td class="mono">${esc(i.record_id)}</td><td>${esc(i.email)}</td><td>${esc(i.target_role || "—")}</td>
        <td><span class="tag ${i.status}">${esc(i.status)}</span></td><td class="muted">${esc(i.reason)}</td>
        <td class="muted">${v.at ? (v.found ? `${esc(v.observed_role)} · ${v.occurrences}× · ${esc(v.at.slice(11, 19))}` : `not found · ${esc(v.at.slice(11, 19))}`) : "—"}</td>
        <td class="shots">${i.screenshots.map((s, n) => `<a href="${s}" target="_blank">shot ${n + 1}</a>`).join("")}</td></tr>`;
    }).join("") || `<tr><td colspan="8" class="muted">None</td></tr>`);
  $("attempt-table").innerHTML = `<tr><th>Attempt</th><th>Created this attempt</th><th>Total created</th><th>Skipped</th><th>Failed</th><th>Pending</th><th>Duplicates in console</th><th>Verified at</th></tr>` +
    (d.attempt_summaries.map((a) => `<tr><td>#${a.attempt}</td><td>${a.created_this_attempt}</td><td>${a.created}</td><td>${a.skipped}</td>
      <td>${a.failed}</td><td>${a.pending}</td><td>${a.duplicate_emails_in_console.length || "none"}</td><td>${esc(a.verified_at)}</td></tr>`).join("")
      || `<tr><td colspan="8" class="muted">Running…</td></tr>`);
}

// ------------------------------------------------------------------ boot
loadInfo();
loadRuns();
loadPolicy();
if (location.hash.length > 1) openRun(location.hash.slice(1)); else show("new");
