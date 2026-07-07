let ws, state = {phase: "idle"}, myName = localStorage.getItem("quizName") || "";
let lastBuzzRound = "";
let joined = false, myPick = null, timerHandle = null;

function connect() {
  ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.type === "error") { showErr(msg.message); return; }
    if (msg.type === "state") {
      const prevRound = state.round;
      state = msg;
      if (state.phase !== "question" || state.round !== prevRound) myPick = null;
      if (state.phase === "idle") { artistsSent = false; myArtists = []; }
      render();
    }
  };
  ws.onclose = () => setTimeout(connect, 1500);
}

function send(obj) { ws.send(JSON.stringify(obj)); }
function showErr(m) { const e = document.getElementById("err"); e.textContent = m;
                      setTimeout(() => { e.textContent = ""; }, 4000); }

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
  document.getElementById("abort-row").hidden = !(state.phase && state.phase !== "idle" && state.phase !== "finished");
  if (state.phase === "idle") { show("v-idle"); joined = false; return; }
  if (state.phase === "lobby" || (!joined && state.phase !== "finished")) {
    if (!joined) {
      document.getElementById("name").value = myName;
      show("v-join");
      if (state.phase !== "lobby") return;
      // allow late joiners only in the lobby
    }
    const amHost = !state.host || state.host === myName;
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
      document.getElementById("artist-pick").hidden = artistsSent;
      document.getElementById("artist-picked").hidden = !artistsSent;
      if (!artistsSent && wall.length === 0) loadWall();
      const sb = document.querySelector("#v-lobby > button.primary");
      sb.hidden = !amHost;
      sb.disabled = !allReady;
      sb.textContent = allReady ? "▶ Start round 1" : "waiting for everyone to be ready…";
      document.getElementById("lobby-wait").hidden = amHost;
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
    document.getElementById("q-flag").textContent =
      state.flagged ? "🚫 flagged — this song won't appear again" : "🚫 bad clip — don't use this song again";
    startTimer();
  }
  if (state.phase === "reveal") {
    stopTimer();
    show("v-reveal");
    document.getElementById("r-art").src = `/api/art/${state.track.id}`;
    document.getElementById("r-title").textContent = state.track.title;
    document.getElementById("r-detail").textContent =
      `${state.track.artist} — ${state.track.album || ""} ${state.track.year ? "(" + state.track.year + ")" : ""}`;
    scoresInto(document.getElementById("r-scores"), state.players);
    document.getElementById("r-flag").textContent =
      state.flagged ? "🚫 flagged — this song won't appear again" : "🚫 bad clip — don't use this song again";
    document.getElementById("r-next").textContent =
      state.round >= state.total_rounds ? "🏁 Finish" : `▶ Round ${state.round + 1}`;
  }
  if (state.phase === "finished") {
    stopTimer();
    show("v-finished");
    scoresInto(document.getElementById("f-scores"), state.players);
    joined = false;
  }
}

function startTimer() {
  stopTimer();
  const bar = document.getElementById("q-bar");
  let left = 20;
  bar.style.width = "100%";
  timerHandle = setInterval(() => {
    left -= 1;
    bar.style.width = Math.max(0, left * 5) + "%";
    if (left <= 0) stopTimer();
  }, 1000);
}
function stopTimer() { if (timerHandle) clearInterval(timerHandle); timerHandle = null; }

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
