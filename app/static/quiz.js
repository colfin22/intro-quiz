let ws, state = {phase: "idle"}, myName = localStorage.getItem("quizName") || "";
let joined = false, myPick = null, timerHandle = null;

function connect() {
  ws = new WebSocket(`ws://${location.host}/ws`);
  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.type === "error") { showErr(msg.message); return; }
    if (msg.type === "state") {
      const prevRound = state.round;
      state = msg;
      if (state.phase !== "question" || state.round !== prevRound) myPick = null;
      render();
    }
  };
  ws.onclose = () => setTimeout(connect, 1500);
}

function send(obj) { ws.send(JSON.stringify(obj)); }
function showErr(m) { const e = document.getElementById("err"); e.textContent = m;
                      setTimeout(() => { e.textContent = ""; }, 4000); }

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
}

function scoresInto(el, players) {
  el.innerHTML = "";
  players.forEach((p, i) => {
    const li = document.createElement("li");
    li.innerHTML = `<span>${["🥇","🥈","🥉"][i] || "&nbsp;&nbsp;"} ${p.name}</span><b>${p.score}</b>`;
    el.appendChild(li);
  });
}

function render() {
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
      document.getElementById("lobby-names").textContent = state.players.map(p => p.name).join(", ");
      show("v-lobby");
    }
    if (!joined) return;
  }
  if (state.phase === "question") {
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

connect();
