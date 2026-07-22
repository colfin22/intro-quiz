let ws, state = {phase: "idle"}, myName = localStorage.getItem("quizName") || "";
let lastBuzzRound = "";
let joined = false, myPick = null, timerHandle = null, payoffHandle = null;
let myTf = null, tfKey = "", timerKey = "", lastGameNo, finishedBuzz, extendTimer = null;

function connect() {
  ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
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
    }
  };
  ws.onclose = () => setTimeout(connect, 1500);
}

function send(obj) { ws.send(JSON.stringify(obj)); }
function showErr(m) { const e = document.getElementById("err"); e.textContent = "⚠️ " + m;
                      if (navigator.vibrate) navigator.vibrate(100);
                      setTimeout(() => { e.textContent = ""; }, 7000); }

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
  send({type: "join", name: myName});
  joined = true;
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
        li.innerHTML = `<span>${p.name}</span><b>${p.ready ? "✅ READY" : "⏳ picking artists…"}</b>`;
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

// reveal: hold the next button until the payoff clip has played out (server enforces too)
function startPayoffLock(btn, label) {
  stopPayoffLock();
  let left = Math.ceil(state.payoff_wait || 0);
  const tick = () => {
    if (left > 0) {
      btn.disabled = true;
      btn.textContent = `🎶 enjoy the song… ${left}s`;
      left -= 1;
    } else {
      btn.disabled = false;
      btn.textContent = label;
      stopPayoffLock();
    }
  };
  tick();
  if (left > 0) payoffHandle = setInterval(tick, 1000);
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

connect();
