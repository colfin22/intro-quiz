// Server control page (#52). Polls /api/admin/status and renders per-action
// cards; ADMIN_PASSWORD (when set server-side) is asked for once and kept in
// localStorage, sent as X-Admin-Token on every call.
"use strict";

const ACTIONS = ["sync", "lastfm", "tiers", "clips", "quality", "bootstrap"];
let adminState = null;   // last /api/admin/status payload
let statsState = null;   // last /api/stats payload
let healthState = null;  // last /health payload (ready_to_play, playable, version)
let speakerState = null; // last /api/admin/speakers payload (loaded on tab open)
let pollTimer = 0;

function token() { return localStorage.getItem("adminToken") || ""; }

const TABS = ["game", "speakers", "library", "trivia", "scores"];
function showTab(name) {
  for (const t of TABS) {
    document.getElementById(`sec-${t}`).hidden = t !== name;
    document.getElementById(`tab-${t}`).classList[t === name ? "add" : "remove"]("sel");
  }
  localStorage.setItem("adminTab", name);
  // Speakers are loaded on demand, not by the 5s poll: each load asks HA for
  // every entity state, which is far too heavy to do on a timer.
  if (name === "speakers") loadSpeakers();
}

async function call(path, opts) {
  const r = await fetch(path, Object.assign({}, opts, {
    headers: Object.assign({ "X-Admin-Token": token() }, (opts || {}).headers),
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
  renderTrivia();
}

function renderTrivia() {
  const tr = adminState.trivia || {};
  const part = (label, d) => d
    ? `<span>${label} <b>${d.total.toLocaleString()}</b> · played <b>${d.played.toLocaleString()}</b> · left <b>${d.left.toLocaleString()}</b></span>`
    : `<span>${label} <b>0</b></span>`;
  document.getElementById("trivia-row").innerHTML =
    part("facts", tr.fact) + part("true/false", tr.tf);
  const games = adminState.leaderboard_games;
  document.getElementById("leaderboard-info").textContent =
    `Marks every fact and T/F fresh again (nothing is deleted)`;
  document.getElementById("leaderboard-wipe-btn").textContent =
    `Wipe leaderboard${typeof games === "number" ? ` (${games} games)` : ""}`;
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
  if (healthState) {
    const ready = healthState.ready_to_play;
    const chip = document.getElementById("ready-chip");
    chip.textContent = ready ? "● ready to play" : "● not ready to play";
    chip.style.color = ready ? "var(--good)" : "var(--bad)";
    document.getElementById("app-version").textContent = `v${healthState.version || "?"}`;
  }
  if (!statsState) return;
  const tiers = {};
  for (const t of statsState.tiered || []) tiers[t.tier] = t.c;
  const playable = healthState ? `<span>playable <b>${(healthState.tracks_playable || 0).toLocaleString()}</b></span>` : "";
  document.getElementById("stats-row").innerHTML =
    `<span>tracks <b>${(statsState.tracks_active || 0).toLocaleString()}</b></span>` +
    playable +
    `<span>easy <b>${(tiers.easy || 0).toLocaleString()}</b></span>` +
    `<span>medium <b>${(tiers.medium || 0).toLocaleString()}</b></span>` +
    `<span>hard <b>${(tiers.hard || 0).toLocaleString()}</b></span>` +
    `<span>annotations <b>${(statsState.with_family_score || 0).toLocaleString()}</b></span>`;
}

// --- speakers: where clips play, and which speakers get grouped for a game ---

function esc(s) {
  return String(s).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

function renderSpeakers() {
  const sel = document.getElementById("speaker-select");
  const picks = document.getElementById("speaker-group");
  const st = document.getElementById("speaker-status");
  const info = document.getElementById("speaker-group-info");
  const s = speakerState;
  if (!s) return;
  if (!s.configured) {
    sel.innerHTML = '<option value="">— Home Assistant not configured —</option>';
    picks.innerHTML = '<span class="dim">set HA_URL and HA_TOKEN to pick speakers here</span>';
    st.className = "status";
    st.textContent = "no HA connection — clips play on the cast board only";
    info.textContent = "";
    return;
  }
  const players = s.players || [];
  // Music Assistant entities are the only ones that can actually play a URL for
  // us, so they go first; the rest stay reachable but labelled, since a plain
  // entity is a silent-failure trap rather than an error.
  const ma = players.filter(p => p.is_ma);
  const other = players.filter(p => !p.is_ma);
  const opt = p => `<option value="${esc(p.entity_id)}"${p.entity_id === s.target ? " selected" : ""}>` +
                   `${esc(p.name)}${p.entity_id === s.env_default ? " (.env)" : ""}</option>`;
  let html = '<option value="">— use .env default —</option>';
  if (ma.length) html += `<optgroup label="Music Assistant">${ma.map(opt).join("")}</optgroup>`;
  if (other.length) html += `<optgroup label="Other (may play a chime and no audio)">` +
                            `${other.map(opt).join("")}</optgroup>`;
  // the saved target may be offline or missing from HA right now — keep it in the
  // list, or Save would quietly swap it for whatever happened to be first
  if (s.target && !players.some(p => p.entity_id === s.target))
    html += `<option value="${esc(s.target)}" selected>${esc(s.target)} (not in HA right now)</option>`;
  sel.innerHTML = html;

  const groupable = players.filter(p => p.can_group);
  const chosen = new Set(s.group || []);
  picks.innerHTML = groupable.length
    ? groupable.map(p => `<label><input type="checkbox" value="${esc(p.entity_id)}"` +
        `${chosen.has(p.entity_id) ? " checked" : ""}> ${esc(p.name)}</label>`).join("")
    : '<span class="dim">no groupable speakers found</span>';

  if (s.error) {
    st.className = "status fail";
    st.textContent = `couldn't reach Home Assistant — ${s.error}`;
  } else {
    st.className = "status ok";
    st.textContent = `${players.length} media players · playing on ` +
      `${s.target || "(nothing chosen)"}${s.overridden ? " — overrides .env" : ""}`;
  }
  renderGroupInfo(chosen.size);
}

function renderGroupInfo(n) {
  document.getElementById("speaker-group-info").textContent =
    n > 1 ? `${n} speakers grouped at game start`
    : n === 1 ? "one speaker — nothing to group"
    : "no grouping — clips play on the target above only";
}

function groupPicks() {
  return [...document.getElementById("speaker-group").querySelectorAll("input")]
    .filter(i => i.checked).map(i => i.value);
}

async function loadSpeakers() {
  try {
    const r = await call("/api/admin/speakers");
    if (!r.ok) { err(`speakers failed (${r.status})`); return; }
    speakerState = await r.json();
    renderSpeakers();
  } catch (e) { err(e.message); }
}

async function saveSpeakers() {
  try {
    const r = await call("/api/admin/speakers", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ target: document.getElementById("speaker-select").value,
                             group: groupPicks() }),
    });
    if (!r.ok) { err((await r.json()).detail || `save failed (${r.status})`); return; }
    err("saved — takes effect on the next round, no restart needed");
    await loadSpeakers();
  } catch (e) { err(e.message); }
}

async function testSpeaker() {
  // the dropdown value, not the saved one: hear the speaker before committing to it
  const target = document.getElementById("speaker-select").value;
  try {
    const r = await call("/api/admin/speakers/test", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ target }),
    });
    const d = await r.json();
    if (!r.ok) { err(d.detail || `test failed (${r.status})`); return; }
    err(d.played ? `fanfare sent to ${d.target}` : "nothing played — casting is disabled or unconfigured");
  } catch (e) { err(e.message); }
}

async function runAction(name) {
  try {
    const r = await call(`/api/admin/run/${name}`, { method: "POST" });
    if (r.status === 409) { err((await r.json()).detail || "busy"); return; }
    if (!r.ok) { err(`start failed (${r.status})`); return; }
    await refresh();
  } catch (e) { err(e.message); }
}

async function copyPrompt() {
  try {
    const region = encodeURIComponent(document.getElementById("prompt-region").value || "");
    const facts = parseInt(document.getElementById("prompt-facts").value, 10) || 60;
    const tf = parseInt(document.getElementById("prompt-tf").value, 10) || 80;
    const r = await call(`/api/admin/trivia/prompt?region=${region}&facts=${facts}&tf=${tf}`);
    if (!r.ok) { err(`prompt failed (${r.status})`); return; }
    const p = (await r.json()).prompt;
    let copied = false;
    try { await navigator.clipboard.writeText(p); copied = true; }
    catch (e) { /* clipboard API needs a secure (https) page */ }
    if (!copied) {
      // plain-http fallback: a hidden textarea + execCommand keeps the newlines
      // (window.prompt is single-line and silently mangled the text)
      const ta = document.createElement("textarea");
      ta.value = p;
      ta.style.position = "fixed"; ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      try { copied = document.execCommand("copy"); } catch (e) { /* fall through */ }
      ta.remove();
    }
    if (copied) err("prompt copied — paste it to your LLM");
    else {  // last resort: show it in the import box to copy by hand
      document.getElementById("import-text").value = p;
      err("couldn't reach the clipboard — prompt placed in the box below, copy it from there");
    }
  } catch (e) { err(e.message); }
}

async function importTrivia(commit) {
  const text = document.getElementById("import-text").value;
  const out = document.getElementById("import-result");
  const commitBtn = document.getElementById("import-commit-btn");
  try {
    const r = await call("/api/admin/trivia/import", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text, commit }),
    });
    const d = await r.json();
    if (!r.ok) { out.textContent = d.detail || `failed (${r.status})`; commitBtn.hidden = true; return; }
    if (d.preview) {
      const rej = (d.would_reject || []).length;
      out.textContent = `${d.fact} facts + ${d.tf} T/F ready` + (rej ? ` · ${rej} would be skipped` : "");
      commitBtn.hidden = false;
    } else {
      out.textContent = `imported ${d.added} · ${d.duplicates} duplicates · ${(d.rejects || []).length} skipped`;
      commitBtn.hidden = true;
      document.getElementById("import-text").value = "";
      await refresh();
    }
  } catch (e) { out.textContent = e.message; }
}

async function resetTrivia() {
  if (!confirm("Mark ALL trivia (facts + true/false) as never played?")) return;
  try {
    const r = await call("/api/admin/trivia/reset", { method: "POST" });
    if (!r.ok) { err(`reset failed (${r.status})`); return; }
    err(`trivia reset — ${(await r.json()).marked_fresh} items fresh again`);
    await refresh();
  } catch (e) { err(e.message); }
}

async function wipeLeaderboard() {
  if (!confirm("Wipe the ALL-TIME leaderboard? Every game and score ever recorded is deleted. This cannot be undone.")) return;
  try {
    const r = await call("/api/admin/leaderboard/wipe", { method: "POST" });
    if (!r.ok) { err(`wipe failed (${r.status})`); return; }
    err(`leaderboard wiped — ${(await r.json()).games_removed} games removed`);
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
    try { healthState = await (await fetch("/health")).json(); } catch (e) { /* likewise */ }
    render();
  } catch (e) { err(e.message); }
  // poll faster while something is running
  clearTimeout(pollTimer);
  pollTimer = setTimeout(refresh, adminState && adminState.current ? 2000 : 5000);
}

document.addEventListener("DOMContentLoaded", () => {
  document.getElementById("master-select")
    .addEventListener("change", ev => { changeMaster(ev.target.value); ev.target.value = ""; });
  // delegated: the checkboxes are rebuilt on every load, so the count line is
  // kept honest here rather than re-bound per box
  document.getElementById("speaker-group")
    .addEventListener("change", () => renderGroupInfo(groupPicks().length));
  const saved = localStorage.getItem("adminTab");
  showTab(TABS.includes(saved) ? saved : "game");
});
refresh();
