// Server control page (#52). Polls /api/admin/status and renders per-action
// cards; ADMIN_PASSWORD (when set server-side) is asked for once and kept in
// localStorage, sent as X-Admin-Token on every call.
"use strict";

const ACTIONS = ["sync", "lastfm", "tiers", "clips", "quality", "bootstrap"];
let adminState = null;   // last /api/admin/status payload
let statsState = null;   // last /api/stats payload
let pollTimer = 0;

function token() { return localStorage.getItem("adminToken") || ""; }

async function call(path, opts) {
  const r = await fetch(path, Object.assign({}, opts, {
    headers: { "X-Admin-Token": token() },
  }));
  if (r.status === 401) {
    const pw = prompt("Admin password");
    if (pw === null) throw new Error("admin password required");
    localStorage.setItem("adminToken", pw);
    return call(path, opts);
  }
  return r;
}

function err(msg) {
  const el = document.getElementById("err");
  el.textContent = msg || "";
  if (msg) setTimeout(() => { el.textContent = ""; }, 6000);
}

function ago(ts) {
  const s = Math.max(0, Math.round(Date.now() / 1000 - ts));
  if (s < 90) return `${s}s ago`;
  if (s < 5400) return `${Math.round(s / 60)}m ago`;
  return `${Math.round(s / 3600)}h ago`;
}

function summaryLine(sum) {
  if (!sum) return "";
  return Object.entries(sum)
    .filter(([k]) => k !== "warning")
    .map(([k, v]) => `${k.replace(/_/g, " ")} ${typeof v === "object" ? JSON.stringify(v) : v}`)
    .join(" · ");
}

function render() {
  if (!adminState) return;
  const jobs = adminState.jobs || {};
  const current = adminState.current;
  for (const name of ACTIONS) {
    const j = jobs[name];
    const st = document.getElementById(`status-${name}`);
    const btn = document.getElementById(`run-${name}`);
    const wrap = document.getElementById(`logwrap-${name}`);
    const card = document.getElementById(`card-${name}`);
    btn.disabled = !!current;               // one job at a time, server-enforced
    btn.textContent = current === name ? "Running…" : "Run";
    card.classList[current === name ? "add" : "remove"]("busy");
    if (!j) {
      st.className = "status";
      st.textContent = "no runs since restart";
      wrap.hidden = true;
      continue;
    }
    if (j.running) {
      st.className = "status run";
      st.textContent = `${j.stage} · started ${ago(j.started_at)}`;
    } else if (j.error) {
      st.className = "status fail";
      st.textContent = `failed · ${ago(j.finished_at)} — ${j.error}`;
    } else {
      st.className = "status ok";
      const warn = j.summary && j.summary.warning ? ` — ⚠ ${j.summary.warning}` : "";
      st.textContent = `ok · ${ago(j.finished_at)} — ${summaryLine(j.summary) || "done"}${warn}`;
    }
    const log = (j.log || []).join("\n");
    wrap.hidden = !log;
    document.getElementById(`log-${name}`).textContent = log;
  }
  renderGame(adminState.game || { phase: "idle" });
  renderStats();
}

function renderGame(g) {
  const info = document.getElementById("game-info");
  const players = document.getElementById("game-players");
  const detail = document.getElementById("game-detail");
  const last = document.getElementById("game-last");
  const controls = document.getElementById("game-controls");
  const sel = document.getElementById("master-select");
  if (!g || g.phase === "idle" || !g.phase) {
    info.textContent = "no game running";
    players.innerHTML = "";
    detail.hidden = last.hidden = controls.hidden = true;
    return;
  }
  info.innerHTML = `Game in progress — round ${g.round} of ${g.total_rounds} · ` +
                   `<span style="color:var(--accent)">${g.phase}</span>` +
                   `<div class="dim">game master: <b>${g.host || "—"}</b> · display: ${g.display}</div>`;
  players.innerHTML = (g.players || [])
    .map(p => `<span>${p.name} <b>${p.score}</b></span>`).join("");
  if (g.phase === "question") {
    detail.hidden = false;
    detail.textContent = `${g.answered} of ${(g.players || []).length} answered · ` +
      `${Math.ceil(g.window_left || 0)}s left — current song hidden until reveal`;
  } else {
    detail.hidden = true;
  }
  if (g.last_revealed) {
    last.hidden = false;
    last.textContent = `Last revealed (round ${g.last_revealed.round}): ` +
      `“${g.last_revealed.title}” — ${g.last_revealed.artist}`;
  } else {
    last.hidden = true;
  }
  controls.hidden = false;
  const names = (g.players || []).map(p => p.name);
  sel.innerHTML = '<option value="">Change game master…</option>' +
    names.map(n => `<option value="${n}">${n}${n === g.host ? " (current)" : ""}</option>`).join("");
}

function renderStats() {
  if (!statsState) return;
  const tiers = {};
  for (const t of statsState.tiered || []) tiers[t.tier] = t.c;
  document.getElementById("stats-row").innerHTML =
    `<span>tracks <b>${(statsState.tracks_active || 0).toLocaleString()}</b></span>` +
    `<span>easy <b>${(tiers.easy || 0).toLocaleString()}</b></span>` +
    `<span>medium <b>${(tiers.medium || 0).toLocaleString()}</b></span>` +
    `<span>hard <b>${(tiers.hard || 0).toLocaleString()}</b></span>` +
    `<span>annotations <b>${(statsState.with_family_score || 0).toLocaleString()}</b></span>`;
}

async function runAction(name) {
  try {
    const r = await call(`/api/admin/run/${name}`, { method: "POST" });
    if (r.status === 409) { err((await r.json()).detail || "busy"); return; }
    if (!r.ok) { err(`start failed (${r.status})`); return; }
    await refresh();
  } catch (e) { err(e.message); }
}

async function abandonGame() {
  if (!confirm("Abandon the running game for everyone?")) return;
  try {
    const r = await call("/api/admin/game/abandon", { method: "POST" });
    if (!r.ok) err(`abandon failed (${r.status})`);
    await refresh();
  } catch (e) { err(e.message); }
}

async function changeMaster(name) {
  if (!name) return;
  try {
    const r = await call(`/api/admin/game/master?name=${encodeURIComponent(name)}`, { method: "POST" });
    if (!r.ok) err((await r.json()).detail || `change failed (${r.status})`);
    await refresh();
  } catch (e) { err(e.message); }
}

async function refresh() {
  try {
    const r = await call("/api/admin/status");
    if (!r.ok) { err(`status failed (${r.status})`); return; }
    adminState = await r.json();
    try { statsState = await (await fetch("/api/stats")).json(); } catch (e) { /* stats are decoration */ }
    render();
  } catch (e) { err(e.message); }
  // poll faster while something is running
  clearTimeout(pollTimer);
  pollTimer = setTimeout(refresh, adminState && adminState.current ? 2000 : 5000);
}

document.addEventListener("DOMContentLoaded", () => {
  document.getElementById("master-select")
    .addEventListener("change", ev => { changeMaster(ev.target.value); ev.target.value = ""; });
});
refresh();
