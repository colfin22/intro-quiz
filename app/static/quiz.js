let ws, state = {phase: "idle"}, myName = localStorage.getItem("quizName") || "";
let lastBuzzRound = "";
let joined = false, myPick = null, timerHandle = null, payoffHandle = null;
// payoff lock deadline, pinned per round so a local re-render can't restart it
let payoffUntil = 0, payoffUntilKey = "";
let myTf = null, tfKey = "", timerKey = "", lastGameNo, finishedBuzz, extendTimer = null;
// remote play: this phone is not in the room, so the clips come out of it.
// Sticky across reloads — someone on a train shouldn't have to re-declare it.
let iAmRemote = localStorage.getItem("quizRemote") === "1";

// --- connection resilience (#50) -------------------------------------------
// A backgrounded phone tab can leave the socket half-dead: no close event ever
// fires, the page keeps its last render, and taps send into a black hole. So:
// reconnect on close/error with backoff, force-reconnect when a ping goes
// unanswered or the tab comes back to the foreground, re-join on reconnect
// (join is idempotent server-side) and show a banner while disconnected.
let reconnectTimer = null, reconnectDelay = 1500, lastHeard = 0;

function connBanner(on) {
  const e = document.getElementById("err");
  if (on) e.textContent = "📡 reconnecting…";
  else if (e.textContent === "📡 reconnecting…") e.textContent = "";
}

function scheduleReconnect() {
  connBanner(true);
  if (reconnectTimer) return;
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    reconnectDelay = Math.min(reconnectDelay * 2, 10000);
    connect();
  }, reconnectDelay);
}

function reconnectNow() {
  if (ws && ws.readyState === WebSocket.OPEN) return;
  if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
  connBanner(true);
  connect();
}

setInterval(() => {
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  if (lastHeard && Date.now() - lastHeard > 25000) { ws.close(); return; }  // pings unanswered — half-dead
  ws.send(JSON.stringify({type: "ping"}));
}, 10000);

document.addEventListener("visibilitychange", () => {
  if (document.visibilityState !== "visible") return;
  if (!ws || ws.readyState !== WebSocket.OPEN) reconnectNow();
  else ws.send(JSON.stringify({type: "ping"}));  // probe a possibly half-dead socket right away
});

function connect() {
  ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
  ws.onopen = () => {
    reconnectDelay = 1500;
    lastHeard = Date.now();
    connBanner(false);
    // restore this socket's identity — host checks ride on it server-side
    if (joined && myName) ws.send(JSON.stringify({type: "join", name: myName}));
  };
  ws.onerror = () => { try { ws.close(); } catch (e) {} };
  ws.onmessage = (ev) => {
    lastHeard = Date.now();
    const msg = JSON.parse(ev.data);
    if (msg.type === "pong") return;
    if (msg.type === "error") { showErr(msg.message); return; }
    if (msg.type === "state") {
      const prevRound = state.round;
      // a NEW game (Play Again included) resets all per-game state — resetting
      // only on "idle" left stale artist picks hiding the wall after rotation
      if (msg.game_no !== undefined && msg.game_no !== lastGameNo) {
        lastGameNo = msg.game_no;
        artistsSent = false; myArtists = []; myPick = null; myTf = null;
        tfKey = ""; timerKey = ""; lastBuzzRound = ""; flagArmed = false; abandonArmed = false;
        joined = false;  // fresh roster — everyone joins the new game
      }
      state = msg;
      if (state.phase !== "question" || state.round !== prevRound) myPick = null;
      if (state.phase === "idle") { artistsSent = false; myArtists = []; }
      render();
      // after render, so it sees the `joined` flag render() may have just cleared
      remoteAudioTick();
    }
  };
  ws.onclose = () => scheduleReconnect();
}

function send(obj) {
  if (!ws || ws.readyState !== WebSocket.OPEN) { reconnectNow(); return; }  // drop it — the state re-sync re-renders
  ws.send(JSON.stringify(obj));
}

// --- remote audio ----------------------------------------------------------
// Only for players who declared they're NOT in the room: the clip streams to
// this phone instead. Locals stay silent, or the room is a mess of echoes a
// second out of step with the speaker.
//
// Deliberately the same shape as board.html's engine (#47): ONE AudioContext for
// the session, each clip fetch()'d and decodeAudioData'd through a short-lived
// source node. A fresh <audio> per clip leaked native decoder buffers until the
// audio engine died — don't reintroduce that here.
//
// Clips come from /api/round/audio, NEVER /clips/{id}/... — the former is
// phase-gated and hides the track id, the latter would hand over the answer.
let actx = null, curSource = null, curBuffer = null, audioKey = "";
let audioRetry = null, audioRetries = 0, audioReported = "";

function ensureCtx() {
  if (!actx) {
    const C = (typeof window !== "undefined") && (window.AudioContext || window.webkitAudioContext);
    if (!C) return null;                       // no Web Audio (or the node smoke) — stay silent
    actx = new C();
  }
  return actx;
}

function aStatus(text) {
  const el = document.getElementById("q-audio");
  if (!el) return;
  el.textContent = text;
  el.hidden = !text;
}

function stopClip() {
  if (curSource) {
    try { curSource.onended = null; curSource.stop(); } catch (e) {}
    try { curSource.disconnect(); } catch (e) {}
    curSource = null;
  }
}

function startBuffer(buf) {
  const ctx = ensureCtx();
  if (!ctx) return;
  stopClip();
  const src = ctx.createBufferSource();
  src.buffer = buf;
  src.connect(ctx.destination);
  try { src.start(); } catch (e) { return; }
  curSource = src; curBuffer = buf;
  aStatus("🔊 playing on this phone");
  // The speed bonus is measured from HERE, not from when the room heard it —
  // otherwise everyone not in the room pays for their own buffering. Once per
  // round: the server ignores repeats, and this keeps a replay honest too.
  if (audioReported !== audioKey) {
    audioReported = audioKey;
    send({type: "audio_started"});
  }
}

function showTapUnlock() {
  const el = document.getElementById("tap-unlock");
  if (el) el.style.display = "flex";
  aStatus("🔇 tap the screen once to allow sound");
}

async function loadAndPlay(url, key) {
  const ctx = ensureCtx();
  if (!ctx) return;
  if (ctx.state === "suspended") { try { await ctx.resume(); } catch (e) {} }
  if (ctx.state === "suspended") { showTapUnlock(); return; }  // still blocked — wait for a tap
  try {
    const resp = await fetch(url, {cache: "no-store"});
    if (!resp.ok) throw new Error("HTTP " + resp.status);
    const buf = await ctx.decodeAudioData(await resp.arrayBuffer());
    if (key !== audioKey) return;              // a newer round already took over
    audioRetries = 0;
    startBuffer(buf);
  } catch (e) {
    if (key !== audioKey) return;
    audioRetries++;
    aStatus(audioRetries > 2 ? "⚠️ can't load the clip — the others can still hear it" : "⏳ loading the clip…");
    if (audioRetries < 8)
      audioRetry = setTimeout(() => { if (key === audioKey) loadAndPlay(url, key); },
                              audioRetries < 3 ? 900 : 2000);
  }
}

// keyed exactly like the board's so a re-broadcast of the same round doesn't
// restart the clip from the top mid-listen
function playRemote(kind, key) {
  if (key === audioKey) return;
  clearTimeout(audioRetry); audioRetries = 0;
  audioKey = key;
  loadAndPlay(`/api/round/audio?kind=${kind}&k=${encodeURIComponent(key)}`, key);
}

function remoteAudioTick() {
  if (!iAmRemote || !joined) {
    if (audioKey) { stopClip(); audioKey = ""; aStatus(""); }
    return;
  }
  if (state.phase === "question")
    playRemote(state.clip_len, `q${state.round}-${state.clip_len}-${state.replay || 0}`);
  else if (state.phase === "reveal" && state.track)
    playRemote("payoff", `p${state.round}`);
  else if (audioKey) { stopClip(); audioKey = ""; aStatus(""); }
}

if (typeof document !== "undefined" && document.addEventListener) {
  document.addEventListener("pointerdown", () => {
    const el = document.getElementById("tap-unlock");
    if (el) el.style.display = "none";
    if (!iAmRemote) return;
    const ctx = ensureCtx();
    if (!ctx) return;
    if (ctx.state === "suspended") ctx.resume().catch(() => {});
    // Recover whatever the autoplay block swallowed. If we got as far as decoding,
    // replay that buffer; if the block hit before the fetch (the usual case, since
    // we bail early on a suspended context) there's nothing decoded yet — clear the
    // key so the current round's clip is fetched fresh.
    if (curBuffer && audioKey) startBuffer(curBuffer);
    else { audioKey = ""; remoteAudioTick(); }
  }, {capture: true});
}
function showErr(m) { const e = document.getElementById("err"); e.textContent = "⚠️ " + m;
                      if (navigator.vibrate) navigator.vibrate(100);
                      setTimeout(() => { if (e.textContent === "⚠️ " + m) e.textContent = ""; }, 7000); }

let wall = [], myArtists = [], artistsSent = false;
function loadWall() {
  fetch("/api/artists/wall").then(r => r.json()).then(list => { wall = list; renderWall(); });
}
function renderWall() {
  const box = document.getElementById("artist-wall");
  if (!box) return;
  box.innerHTML = "";
  for (const a of wall) {
    const b = document.createElement("button");
    b.textContent = a.artist;
    if (myArtists.includes(a.artist)) b.classList.add("sel");
    b.onclick = () => {
      const i = myArtists.indexOf(a.artist);
      if (i >= 0) myArtists.splice(i, 1);
      else if (myArtists.length < 3) myArtists.push(a.artist);
      renderWall();
    };
    box.appendChild(b);
  }
  const done = document.getElementById("artist-done");
  done.disabled = myArtists.length !== 3;
  done.textContent = myArtists.length === 3 ? "✅ Lock in my 3" : `Pick ${3 - myArtists.length} more`;
}
function sendArtists() {
  send({type: "set_artists", artists: myArtists});
  artistsSent = true;
  render();
}
function skipArtists() { artistsSent = true; send({type: "ready"}); render(); }

let flagArmed = false;
function flagTap() {
  if (state.flagged) return;
  if (!flagArmed) {
    flagArmed = true;
    document.getElementById("r-flag").textContent = "🚫 tap again to confirm — bans this song forever";
    setTimeout(() => {
      flagArmed = false;
      if (state.phase === "reveal" && !state.flagged) render();
    }, 4000);
    return;
  }
  flagArmed = false;
  send({type: "flag_clip"});
}

let abandonArmed = false;
function abandonTap() {
  // two-tap confirm — a stray tap must not kill the whole game (#30)
  const link = document.querySelector("#abort-row a");
  if (!abandonArmed) {
    abandonArmed = true;
    link.textContent = "⚠️ tap again to abandon";
    link.style.color = "#ff6b6b";
    setTimeout(() => {
      abandonArmed = false;
      link.textContent = "abandon game";
      link.style.color = "";
    }, 4000);
    return;
  }
  abandonArmed = false;
  link.textContent = "abandon game";
  link.style.color = "";
  send({type: "abort"});
}

function stopBoard() {
  // one tap — quitting a stuck cast is idempotent and reversible (re-pick the display)
  send({type: "stop_board"});
}

function join() {
  myName = document.getElementById("name").value.trim();
  if (!myName) return;
  localStorage.setItem("quizName", myName);
  send({type: "join", name: myName, remote: iAmRemote});
  joined = true;
}

// where you are, on the join screen (before joining) and in the lobby (after)
function setWhere(remote) {
  iAmRemote = !!remote;
  localStorage.setItem("quizRemote", iAmRemote ? "1" : "0");
  if (joined) send({type: "set_remote", remote: iAmRemote});
  if (iAmRemote) {
    // grab the audio unlock off THIS tap while we still have a user gesture —
    // asking again mid-round means missing the first clip
    const ctx = ensureCtx();
    if (ctx && ctx.state === "suspended") ctx.resume().catch(() => {});
  } else {
    stopClip(); audioKey = ""; aStatus("");
  }
  render();
}

function toggleWhere() { setWhere(!iAmRemote); }

function renderWhere() {
  for (const [id, on] of [["where-local", !iAmRemote], ["where-remote", iAmRemote]]) {
    const b = document.getElementById(id);
    if (b) b.classList[on ? "add" : "remove"]("picked");
  }
  const hint = document.getElementById("where-hint");
  if (hint) hint.textContent = iAmRemote
    ? "🔊 the clips will play out of this phone — headphones or speaker up"
    : "🔇 this phone stays silent; you'll hear the clips in the room";
  const link = document.getElementById("lobby-where-link");
  if (link) link.textContent = iAmRemote
    ? "🌐 playing remotely — tap if you're actually in the room"
    : "🏠 in the room — tap if you're somewhere else";
}

function show(id) {
  for (const v of document.querySelectorAll("[id^=v-]")) v.hidden = true;
  document.getElementById(id).hidden = false;
  // first-timers see the rules while joining and in the lobby
  document.getElementById("v-howto").hidden = !(id === "v-join" || id === "v-lobby");
  if (id !== "v-lobby") document.getElementById("v-master").hidden = true;
}

function scoresInto(el, players) {
  el.innerHTML = "";
  players.forEach((p, i) => {
    const li = document.createElement("li");
    li.innerHTML = `<span>${["🥇","🥈","🥉"][i] || "&nbsp;&nbsp;"} ${p.name}</span><b>${p.score}</b>`;
    el.appendChild(li);
  });
}

// --- all-time top scores ----------------------------------------------------
// Every game ever played, totalled from the `results` table, so it survives
// restarts and deploys. Shown in two places: on the join screen (walk up, see
// who's winning overall) and under the final scores (see what tonight changed).
//
// Fetched, not pushed on the socket: it changes once per GAME, whereas states
// broadcast many times per round, and the board does the same with the same
// endpoint. Keyed so a re-render doesn't re-request it — but the key includes
// the phase and game number, so finishing a game refetches and the new totals
// land. finish() writes the DB before broadcasting "finished", so the numbers
// already include the game just played.
let alltimeKey = "";
function renderAlltime() {
  const card = document.getElementById("alltime-card");
  const wanted = state.phase === "finished" ||
                 (!joined && ["idle", "lobby"].includes(state.phase));
  card.hidden = !wanted;
  if (!wanted) { alltimeKey = ""; return; }
  document.getElementById("alltime-title").textContent =
    state.phase === "finished" ? "🏆 All-time top scores" : "🏆 All-time top scores — who to beat";
  const key = `${state.phase}-${state.game_no}`;
  if (key === alltimeKey) return;
  alltimeKey = key;
  fetch("/api/leaderboard").then(r => r.json()).then(rows => {
    const ul = document.getElementById("alltime-list");
    ul.innerHTML = "";
    if (!rows.length) {
      ul.innerHTML = '<li class="dim">no games finished yet — tonight\'s could be the first</li>';
      return;
    }
    rows.slice(0, 8).forEach((r, i) => {
      const li = document.createElement("li");
      // own row highlighted: on a phone this list is the only place you see
      // where you stand across every game, not just this one
      const me = r.player === myName;
      li.innerHTML =
        `<span${me ? ' style="color:var(--accent);font-weight:700"' : ""}>` +
        `${["🥇","🥈","🥉"][i] || "&nbsp;&nbsp;"} ${r.player}</span>` +
        `<b>${r.total_score}<span class="dim" style="font-weight:400;font-size:.82em">` +
        ` · ${r.games} game${r.games === 1 ? "" : "s"}</span></b>`;
      ul.appendChild(li);
    });
  }).catch(() => { alltimeKey = ""; });  // let a failed fetch retry on the next render
}

function renderDisplays() {
  const box = document.getElementById("display-choice");
  if (!state.displays) { box.innerHTML = ""; return; }
  box.innerHTML = "";
  for (const name of [...state.displays, "none"]) {
    const b = document.createElement("button");
    b.textContent = (name === state.display ? "✅ " : "") + (name === "none" ? "No scoreboard (music on the sitting room speaker)" : name);
    b.onclick = () => send({type: "set_display", display: name});
    box.appendChild(b);
  }
}

function render() {
  renderDisplays();
  renderWhere();
  renderAlltime();  // up here with the others: render() returns early on idle/join
  if (state.phase !== "question") timerKey = "";  // fresh countdown next round
  const hostOnly = !state.host || state.host === myName;
  // is the crowned master actually in the game? if not, anyone may take over / abandon (#46)
  const hostJoined = !state.host || (state.players || []).some(p => p.name === state.host);
  // who's holding the mic — pinned above every screen for the whole game
  const mb = document.getElementById("master-banner");
  if (state.host && joined && state.phase && !["idle", "finished"].includes(state.phase)) {
    mb.hidden = false;
    mb.innerHTML = state.host === myName
      ? '🎤 <b style="color:var(--accent)">You\'re the game master</b>'
      : `🎤 Game master: <b>${state.host}</b>`;
  } else mb.hidden = true;
  document.getElementById("abort-row").hidden = !((hostOnly || !hostJoined) && state.phase && state.phase !== "idle" && state.phase !== "finished");
  // Kill the cast on the active TV (#31) — but only AT THE END of a game, or when idle.
  // It used to sit under the master's thumb for the whole game, right beside the controls
  // they actually need mid-round, which is one mis-tap away from killing the TV in the
  // middle of a song.
  const casting = state.display && state.display !== "none";
  document.getElementById("stop-board-row").hidden =
      !(casting && hostOnly && state.phase === "finished");
  if (state.phase === "idle") { show("v-idle"); joined = false; return; }
  if (state.phase === "lobby" || (!joined && state.phase !== "finished")) {
    if (!joined) {
      document.getElementById("name").value = myName;
      show("v-join");
      if (state.phase !== "lobby") return;
      // allow late joiners only in the lobby
    }
    if (joined && state.phase === "lobby") {
      document.getElementById("lobby-count").textContent = state.players.length + " player" + (state.players.length === 1 ? "" : "s");
      const roster = document.getElementById("lobby-roster");
      roster.innerHTML = "";
      let allReady = state.players.length > 0;
      for (const p of state.players) {
        const li = document.createElement("li");
        li.innerHTML = `<span>${p.remote ? "🌐 " : ""}${p.name}</span><b>${p.ready ? "✅ READY" : "⏳ picking artists…"}</b>`;
        if (!p.ready) { li.style.opacity = ".7"; allReady = false; }
        roster.appendChild(li);
      }
      document.getElementById("v-master").hidden = !(state.host && state.host === myName);
      document.getElementById("artist-pick").hidden = artistsSent;
      document.getElementById("artist-picked").hidden = !artistsSent;
      if (!artistsSent && wall.length === 0) loadWall();
      const sb = document.querySelector("#v-lobby > button.primary");
      sb.hidden = !(hostOnly || !hostJoined);  // absent rotated master: anyone can take over
      sb.disabled = !allReady;
      sb.textContent = !allReady ? "waiting for everyone to be ready…"
        : (hostOnly ? "▶ Start round 1" : `▶ Start (take over from ${state.host})`);
      document.getElementById("lobby-wait").hidden = hostOnly || !hostJoined;
      if (state.host) document.getElementById("lobby-wait").textContent = `${state.host} starts the game 🎤`;
      show("v-lobby");
    }
    if (!joined) return;
  }
  if (state.phase === "question") {
    const buzzKey = state.round + "-" + (state.replay || 0);
    if (buzzKey !== lastBuzzRound) {
      lastBuzzRound = buzzKey;
      if (navigator.vibrate) navigator.vibrate(200);
    }
    show("v-question");
    document.getElementById("q-progress").textContent =
      `Round ${state.round} of ${state.total_rounds} — ${state.clip_len}s clip`;
    const opts = document.getElementById("q-options");
    opts.innerHTML = "";
    state.options.forEach((o, i) => {
      const b = document.createElement("button");
      b.textContent = o;
      if (myPick !== null) b.disabled = true;
      if (myPick === i) b.classList.add("picked");
      b.onclick = () => { myPick = i; send({type: "answer", name: myName, choice: i}); render(); };
      opts.appendChild(b);
    });
    document.getElementById("q-answered").textContent =
      state.answered.length ? `answered: ${state.answered.join(", ")}` : "";
    // one extend at a time: while the longer clip plays, the button locks
    // (server enforces too — this just stops the mash, #27)
    const ext = document.getElementById("q-extend");
    const extWait = state.extend_wait || 0;
    ext.hidden = state.clip_len >= 20;
    ext.disabled = extWait > 0;
    ext.textContent = extWait > 0 ? "🎧 listen — extend unlocks when the clip ends" : "🔁 Play a bit more";
    clearTimeout(extendTimer);
    if (extWait > 0) extendTimer = setTimeout(() => {
      ext.disabled = false;
      ext.textContent = "🔁 Play a bit more";
    }, extWait * 1000 + 300);
    // restart the countdown only when the window actually changed (new round,
    // replay, or an extended clip) — not on every broadcast
    const tKey = `${state.round}-${state.replay || 0}-${state.clip_len}`;
    if (tKey !== timerKey) {
      timerKey = tKey;
      startTimer(state.window_left || 20);
    }
  }
  if (state.phase === "reveal") {
    stopTimer();
    show("v-reveal");
    document.getElementById("r-art").src = `/api/art/${state.track.id}`;
    document.getElementById("r-title").textContent = state.track.title;
    document.getElementById("r-detail").textContent =
      `${state.track.artist}${state.track.year ? " (" + state.track.year + ")" : ""}`;
    const res = document.getElementById("r-results");
    res.innerHTML = "";
    for (const p of state.players) {
      const a = (state.round_answers || {})[p.name];
      const li = document.createElement("li");
      li.innerHTML = a
        ? (a.points > 0 ? `<span>✅ ${p.name}</span><b style="color:var(--good)">+${a.points}</b>`
                        : `<span>❌ ${p.name}</span><b style="color:var(--bad)">0</b>`)
        : `<span>😴 ${p.name}</span><b>—</b>`;
      res.appendChild(li);
    }
    scoresInto(document.getElementById("r-scores"), state.players);
    document.getElementById("r-flag").parentElement.hidden = !hostOnly;
    if (!flagArmed) document.getElementById("r-flag").textContent =
      state.flagged ? "🚫 flagged — this song won't appear again" : "🚫 bad clip — don't use this song again";
    const nextBtn = document.getElementById("r-next");
    nextBtn.hidden = !hostOnly;
    document.getElementById("r-wait").hidden = hostOnly;
    if (state.host) document.getElementById("r-wait").textContent = `🎤 ${state.host} has the next-song button`;
    // the payoff plays in full — the next button unlocks when the song's done
    startPayoffLock(nextBtn,
      state.round >= state.total_rounds ? "🏁 Finish" : `▶ Round ${state.round + 1}`);
  }
  if (state.phase !== "reveal") stopPayoffLock();
  if (state.phase === "break") {
    stopTimer();
    show("v-break");
    const stage = state.break_stage || "facts";
    const myFact = (state.facts || {})[myName];
    const factBox = document.getElementById("bk-fact");
    factBox.hidden = !(stage === "facts" && myFact);
    if (!factBox.hidden) document.getElementById("bk-fact-text").textContent = myFact;
    const tfBox = document.getElementById("bk-tf");
    tfBox.hidden = stage !== "tf";
    let nextLabel = state.tf || (state.facts && Object.keys(state.facts).length)
      ? "🎯 On to the true or false…" : "▶ Second half";
    if (stage === "tf" && state.tf) {
      const q = state.tf;
      const key = "tf-" + q.num;
      if (key !== tfKey) {
        tfKey = key; myTf = null;
        if (navigator.vibrate) navigator.vibrate(200);
      }
      document.getElementById("bk-tf-progress").textContent =
        `True or false? ${q.num} of ${q.total} — +50 points`;
      document.getElementById("bk-tf-text").textContent = q.text;
      for (const [id, val] of [["bk-tf-true", true], ["bk-tf-false", false]]) {
        const b = document.getElementById(id);
        b.disabled = myTf !== null || q.revealed;
        b.classList[myTf === val ? "add" : "remove"]("picked");
      }
      const st = document.getElementById("bk-tf-status");
      if (q.revealed) {
        const bits = state.players.map(p => {
          const pts = (q.results || {})[p.name];
          return pts === undefined ? `😴 ${p.name}` : (pts > 0 ? `✅ ${p.name} +${pts}` : `❌ ${p.name}`);
        });
        st.textContent = `It's ${q.answer ? "TRUE" : "FALSE"}!   ${bits.join("   ")}`;
        nextLabel = q.last ? "▶ Second half" : "▶ Next question";
      } else {
        st.textContent = q.answered.length ? `answered: ${q.answered.join(", ")}` : "";
        nextLabel = "👀 Reveal the answer";
      }
    }
    document.getElementById("bk-standings-label").hidden = stage === "tf";
    document.getElementById("bk-scores").parentElement.hidden = stage === "tf";
    scoresInto(document.getElementById("bk-scores"), state.players);
    const nb = document.getElementById("bk-next");
    nb.hidden = !hostOnly;
    nb.textContent = nextLabel;
    document.getElementById("bk-wait").hidden = hostOnly;
    if (state.host) document.getElementById("bk-wait").textContent = `🎤 ${state.host} runs the half-time show`;
  }
  if (state.phase === "finished") {
    stopTimer();
    if (finishedBuzz !== state.game_no && navigator.vibrate) {  // 🎺 once per game
      finishedBuzz = state.game_no;
      navigator.vibrate([120, 60, 120, 60, 350]);
    }
    show("v-finished");
    scoresInto(document.getElementById("f-scores"), state.players);
    const nm = document.getElementById("f-next-master");
    nm.hidden = !state.next_host;
    if (state.next_host) nm.innerHTML = state.next_host === myName
      ? '🎤 <b style="color:var(--accent)">You\'re the game master next game!</b>'
      : `🎤 <b>${state.next_host}</b> is the game master next game`;
    joined = false;
  }
}

function startTimer(seconds) {
  stopTimer();
  const bar = document.getElementById("q-bar");
  const total = Math.max(1, Math.ceil(seconds || 20));
  let left = total;
  bar.style.width = "100%";
  timerHandle = setInterval(() => {
    left -= 1;
    bar.style.width = Math.max(0, (left / total) * 100) + "%";
    if (left <= 0) stopTimer();
  }, 1000);
}
function stopTimer() { if (timerHandle) clearInterval(timerHandle); timerHandle = null; }

// reveal: hold the next button until the payoff clip has played out (server enforces too).
//
// Counts down against a wall-clock deadline instead of decrementing a counter on
// a 1s interval. The counter version could leave the button disabled FOREVER,
// while CSS still gave it a pointer cursor — a dead button that looks alive, and
// the game can't move on:
//
//   - the opening tick() decremented `left` before the `left > 0` guard decided
//     whether to schedule the interval, so any render with 0 < payoff_wait <= 1
//     scheduled nothing and stuck on "…1s". Every re-broadcast during the last
//     second of the payoff (a flag, a set_remote, a reconnect re-join) hit this.
//   - a phone that slept or backgrounded mid-payoff had its interval throttled,
//     so the countdown resumed from where it froze rather than from the real time
//     left, holding the button well past the end of the song.
//
// A deadline is immune to both: every tick re-derives the remainder, so a missed
// or late tick corrects itself, and the unlock can't be skipped.
//
// The deadline is pinned to the round (payoffUntilKey) rather than recomputed per
// render, because render() runs on local events too — a flag tap, the remote
// toggle, an error banner — and each of those carried the ORIGINAL payoff_wait,
// restarting a full 12s countdown from whenever it happened. Two taps and the
// button outlived the song by half a minute.
function startPayoffLock(btn, label) {
  const key = `${state.game_no}-${state.round}`;
  if (payoffUntilKey !== key) {
    payoffUntilKey = key;
    payoffUntil = Date.now() + (state.payoff_wait || 0) * 1000;
  }
  stopPayoffLock();
  const until = payoffUntil;
  const tick = () => {
    const left = Math.ceil((until - Date.now()) / 1000);
    if (left > 0) {
      btn.disabled = true;
      btn.textContent = `🎶 enjoy the song… ${left}s`;
    } else {
      btn.disabled = false;
      btn.textContent = label;
      stopPayoffLock();
    }
  };
  tick();
  // 250ms, not 1s: the label only changes once a second, but a short tick keeps
  // the unlock prompt instead of trailing the song by up to a second
  if (btn.disabled) payoffHandle = setInterval(tick, 250);
}
function stopPayoffLock() { if (payoffHandle) clearInterval(payoffHandle); payoffHandle = null; }

function tfPick(val) {
  if (myTf !== null || !state.tf || state.tf.revealed) return;
  myTf = val;
  send({type: "tf_answer", answer: val});
  render();
}

// family phones have fixed IPs — prefill the name for fresh browsers
if (!myName) {
  fetch("/api/whoami").then(r => r.json()).then(d => {
    if (d.name && !myName) {
      myName = d.name;
      const f = document.getElementById("name");
      if (f && !f.value) f.value = d.name;
    }
  }).catch(() => {});
}

// invite others without the TV: a scan-to-join QR + shareable link in the lobby.
// Uses the same server resolver as the board's QR (honours JOIN_URL / BOARD_URL).
// Guarded on `location` so the node render-smoke (which has no global location)
// skips it cleanly rather than throwing at module load.
let joinUrl = "";
(function initInvite() {
  if (typeof location === "undefined" || !location.origin) return;
  joinUrl = location.origin;
  const qs = "?url=" + encodeURIComponent(location.origin);
  const img = document.getElementById("join-qr");
  if (img) {
    img.onload = () => { img.hidden = false; };
    img.onerror = () => { img.hidden = true; };
    img.src = "/api/join-qr.svg" + qs;
  }
  const urlEl = document.getElementById("join-url");
  fetch("/api/join-url" + qs).then(r => r.json())
    .then(d => { joinUrl = d.url || location.origin;
                 if (urlEl) urlEl.textContent = joinUrl.replace(/^https?:\/\//, ""); })
    .catch(() => { if (urlEl) urlEl.textContent = location.origin.replace(/^https?:\/\//, ""); });
})();

function shareJoin() {
  const url = joinUrl || location.origin;
  const link = document.getElementById("join-share");
  if (navigator.share) { navigator.share({ title: "Intro Quiz", text: "Join the quiz", url }).catch(() => {}); return; }
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(url).then(() => {
      if (link) { link.textContent = "✅ Link copied"; setTimeout(() => { link.textContent = "🔗 Share the link"; }, 2000); }
    }).catch(() => {});
  } else if (link) {
    link.textContent = url;  // no share/clipboard API — just show it to read out
  }
}

connect();
