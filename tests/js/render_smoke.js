// Executes the phone UI's render() against every phase snapshot in a stub DOM.
// Exists because three separate UI regressions shipped despite passing python tests:
// a thrown render leaves half-built screens (e.g. a blank next-song button).
const fs = require("fs");
const path = require("path");
const base = path.join(__dirname, "..", "..", "app", "static");
const src = fs.readFileSync(path.join(base, "quiz.js"), "utf8");
const html = fs.readFileSync(path.join(base, "index.html"), "utf8");
const ids = new Set([...html.matchAll(/id="([^"]+)"/g)].map(m => m[1]));
const elems = {};
let failures = 0;
function mkEl(id) {
  return { id, hidden: false, disabled: false, textContent: "", innerHTML: "", value: "", src: "",
           style: {}, classList: { add(){}, remove(){} }, parentElement: { hidden: false },
           // Appended children accumulate into innerHTML, so lists built with
           // createElement + appendChild (the scoreboards, the rosters, the
           // all-time table) are actually inspectable. A no-op appendChild made
           // every such assertion vacuously pass against an empty string.
           appendChild(c) { this.innerHTML += c && c.innerHTML ? c.innerHTML : ""; },
           querySelectorAll(){ return []; } };
}
global.document = {
  getElementById(id) {
    if (!ids.has(id)) { console.log("MISSING ELEMENT:", id); failures++; return mkEl(id); }
    return elems[id] || (elems[id] = mkEl(id));
  },
  querySelector(sel) { return elems[sel] || (elems[sel] = mkEl(sel)); },
  querySelectorAll() { return []; },
  createElement(t) { return mkEl(t); },
  addEventListener() {},
  hidden: false,
};
// A minimal Web Audio stub, so the remote-player audio path is exercised rather
// than short-circuited: ensureCtx() returns null when AudioContext is absent.
const fetched = [];
global.window = {
  location: { protocol: "http:", host: "x" },
  AudioContext: function () {
    return { state: "running", currentTime: 0, destination: {},
             resume: () => Promise.resolve(),
             createBufferSource: () => ({ buffer: null, connect(){}, disconnect(){}, start(){}, stop(){} }),
             decodeAudioData: () => Promise.resolve({ duration: 5 }) };
  },
};
global.localStorage = { getItem: () => "Alice", setItem(){} };
global.navigator = {};
// an "open" socket, so client->server messages are captured rather than
// triggering the reconnect path
const sent = [];
global.WebSocket = function(){ return { readyState: 1, send(m){ sent.push(JSON.parse(m)); }, close(){} }; };
global.WebSocket.OPEN = 1;
// Clip and leaderboard fetches resolve for real (a thenable stub would never
// reach decode, so the audio_started report would go untested, and never reach
// the leaderboard rows); everything else keeps the inert stub the other call
// sites expect.
const LEADERBOARD = [
  { player: "Alice", games: 12, total_score: 14320, total_correct: 88, fastest_ms: 1204 },
  { player: "Bob", games: 9, total_score: 11050, total_correct: 71, fastest_ms: 1610 },
  { player: "Carol", games: 1, total_score: 980, total_correct: 6, fastest_ms: 2400 },
];
let leaderboardRows = LEADERBOARD;
global.fetch = (url) => {
  fetched.push(url);
  if (typeof url === "string" && /audio|\.mp3|\/clips\//.test(url))
    return Promise.resolve({ ok: true, arrayBuffer: () => Promise.resolve(new ArrayBuffer(8)) });
  if (typeof url === "string" && url.startsWith("/api/leaderboard"))
    return Promise.resolve({ ok: true, json: () => Promise.resolve(leaderboardRows) });
  return { then: () => ({ then(){}, catch(){} }), catch(){} };
};
// Controllable clock + timers, so the payoff lock (which counts down against a
// wall-clock deadline) can be tested at whatever "time" we like instead of slept
// through. advance() fires due intervals; sleepThrough() jumps time WITHOUT
// firing them, which is what a backgrounded phone tab does.
let now = 1_000_000;
const intervals = [];
global.setInterval = (fn, ms) => { const h = { fn, ms, next: now + ms, dead: false };
                                   intervals.push(h); return h; };
global.clearInterval = (h) => { if (h) h.dead = true; };
global.setTimeout = () => 0; global.clearTimeout = () => {};
const RealDate = global.Date;
global.Date = class extends RealDate { static now() { return now; } };
const advance = (ms) => {
  const end = now + ms;
  for (;;) {
    const due = intervals.filter(h => !h.dead && h.next <= end).sort((a, b) => a.next - b.next)[0];
    if (!due) break;
    now = due.next; due.next += due.ms; due.fn();
  }
  now = end;
};
const sleepThrough = (ms) => { now += ms; };

const track = { id: "abc", title: "Song", artist: "Artist", album: "Album", year: 2001 };
const players = [{ name: "Alice", score: 100, ready: true, picked_artists: true, remote: false },
                 { name: "Bob", score: 200, ready: false, picked_artists: false, remote: true }];
const snapshots = [
  { phase: "idle", players: [] },
  { phase: "lobby", host: "Alice", players },
  { phase: "question", host: "Alice", round: 3, total_rounds: 10, clip_len: 5, replay: 0,
    options: [{title:"a",artist:"b"},{title:"c",artist:"d"},{title:"e",artist:"f"},{title:"g",artist:"h"}],
    answered: ["Alice"], players },
  { phase: "reveal", host: "Alice", round: 3, total_rounds: 10, track,
    round_answers: { Alice: { points: 120 } }, flagged: false, players, payoff_wait: 0 },
  { phase: "reveal", host: "Alice", round: 3, total_rounds: 10, track,
    round_answers: { Alice: { points: 120 } }, flagged: false, players, payoff_wait: 8.4 },
  { phase: "break", host: "Alice", players },
  { phase: "break", host: "Alice", break_stage: "facts",
    facts: { Alice: "A fact to read", Bob: "Another fact" }, players },
  { phase: "break", host: "Alice", break_stage: "tf", facts: {},
    tf: { num: 1, total: 3, text: "T or F?", answered: ["Alice"], revealed: false, last: false }, players },
  { phase: "break", host: "Alice", break_stage: "tf", facts: {},
    tf: { num: 3, total: 3, text: "T or F?", answered: ["Alice", "Bob"], revealed: true,
          last: true, answer: true, results: { Alice: 50, Bob: 0 } }, players },
  { phase: "finished", host: "Alice", players, track, next_host: "Bob" },
  { phase: "lobby", host: "Carol",  // rotated master hasn't joined, everyone ready
    players: [{ name: "Alice", score: 0, ready: true, picked_artists: true },
              { name: "Bob", score: 0, ready: true, picked_artists: true }] },
];
const scenario = `
joined = true;
for (const snap of ${JSON.stringify(snapshots)}) {
  for (const who of ["Alice", "Bob"]) {
    joined = true;  // idle/finished renders reset joined (real play-again behaviour)
    myName = who;
    state = snap;
    try { render(); } catch (e) {
      console.log("RENDER THREW", snap.phase, "as", who, "->", e.message); failures++;
    }
  }
}
// the regression that shipped: host's next-song button must end up with text
joined = true; myName = "Alice"; state = ${JSON.stringify(snapshots[3])}; render();
const btn = document.getElementById("r-next");
if (!btn.textContent) { console.log("r-next has no text on host reveal"); failures++; }
if (btn.hidden) { console.log("r-next hidden on host reveal"); failures++; }
myName = "Bob"; render();
if (!document.getElementById("r-next").hidden) { console.log("r-next visible for non-host"); failures++; }
// Enters a reveal as if it were a brand-new round: the lock deadline is pinned
// per round (see startPayoffLock), so replaying the SAME round with a different
// payoff_wait keeps the first deadline. That's right in production — within one
// round payoff_wait only ever counts down — but these checks drive the value up
// and down out of order, so each one needs a fresh round to land on.
const relock = (payoff_wait, round) => {
  payoffUntil = 0; payoffUntilKey = "";
  state = Object.assign({}, ${JSON.stringify(snapshots[4])}, { payoff_wait, round: round || 3 });
  render();
};
// payoff lock: next button disabled with countdown text while the song plays out
joined = true; myName = "Alice"; relock(8.4);
if (!btn.disabled) { console.log("r-next not locked during payoff"); failures++; }
if (!/9s/.test(btn.textContent)) { console.log("payoff countdown missing:", btn.textContent); failures++; }
relock(0);
if (btn.disabled) { console.log("r-next still locked after payoff"); failures++; }

// ---- the payoff lock must always let go -----------------------------------
// It shipped able to disable the next button FOREVER while CSS still gave it a
// pointer cursor: a dead button that looks clickable, and the game cannot move
// on. Three ways in, all reproduced below. The button unlocking is the only way
// a round ever ends, so every one of these is a stuck game.
// 1. a sub-second remainder (any re-broadcast in the payoff's last second:
//    a flag, a set_remote, a reconnect re-join). Counted down before deciding
//    whether to schedule a tick, so it scheduled none and stuck on "1s".
for (const pw of [1, 0.4]) {
  relock(pw);
  if (!btn.disabled) { console.log("payoff_wait=" + pw + " did not lock at all"); failures++; }
  advance(1600);
  if (btn.disabled) {
    console.log("payoff lock STUCK with payoff_wait=" + pw + ": " + btn.textContent); failures++; }
}
// 2. local re-renders (flag tap, remote toggle) each restarted a full countdown
//    from the original payoff_wait, so taps pushed the unlock ever further out
relock(12);
for (let i = 0; i < 3; i++) { advance(3000); render(); }   // renders at 3s, 6s, 9s
advance(3600);                                             // t = 12.6s > 12s deadline
if (btn.disabled) {
  console.log("payoff lock extended by re-renders: " + btn.textContent); failures++; }
// 3. a backgrounded phone has its intervals throttled; the countdown must be
//    re-derived from the clock on the next render, not resumed where it froze
relock(12);
sleepThrough(30000);
render();
if (btn.disabled) {
  console.log("payoff lock still held after the phone slept: " + btn.textContent); failures++; }
// and a genuinely new round still locks on its own deadline
relock(12, 4);
if (!btn.disabled) { console.log("a new round's payoff did not lock"); failures++; }
advance(12600);
if (btn.disabled) { console.log("new round's payoff lock never released"); failures++; }
if (btn.textContent !== "▶ Round 5") {
  console.log("label not restored after the lock:", btn.textContent); failures++; }
// half-time facts: my fact shows, someone else's doesn't leak into my card
state = ${JSON.stringify(snapshots[6])}; render();
if (document.getElementById("bk-fact").hidden) { console.log("fact card hidden for fact-holder"); failures++; }
if (document.getElementById("bk-fact-text").textContent !== "A fact to read") {
  console.log("wrong fact shown:", document.getElementById("bk-fact-text").textContent); failures++; }
// T/F question: buttons live before answering, status shows verdict after reveal
state = ${JSON.stringify(snapshots[7])}; myTf = null; render();
if (document.getElementById("bk-tf").hidden) { console.log("tf box hidden during tf stage"); failures++; }
if (document.getElementById("bk-tf-true").disabled) { console.log("tf buttons dead before answering"); failures++; }
state = ${JSON.stringify(snapshots[8])}; render();
if (!document.getElementById("bk-tf-true").disabled) { console.log("tf buttons live after reveal"); failures++; }
if (!/TRUE/.test(document.getElementById("bk-tf-status").textContent)) {
  console.log("tf verdict missing:", document.getElementById("bk-tf-status").textContent); failures++; }
if (!/Second half/.test(document.getElementById("bk-next").textContent)) {
  console.log("bk-next label wrong on last reveal:", document.getElementById("bk-next").textContent); failures++; }
// master banner: named for non-hosts, 'You' for the host, during play
state = ${JSON.stringify(snapshots[2])}; myName = "Bob"; render();
const mb = document.getElementById("master-banner");
if (mb.hidden || !/Alice/.test(mb.innerHTML)) { console.log("master banner missing for non-host:", mb.innerHTML); failures++; }
myName = "Alice"; render();
if (!/game master/.test(mb.innerHTML)) { console.log("master banner missing for host"); failures++; }
// finished screen announces the next master
state = ${JSON.stringify(snapshots[9])}; myName = "Alice"; render();
const nm = document.getElementById("f-next-master");
if (nm.hidden || !/Bob/.test(nm.innerHTML)) { console.log("next-master missing on finished:", nm.innerHTML); failures++; }
// absent rotated master: everyone gets a take-over start button
joined = true; state = ${JSON.stringify(snapshots[10])}; myName = "Alice"; render();
const sb2 = document.querySelector("#v-lobby > button.primary");
if (sb2.hidden) { console.log("no take-over button when master absent"); failures++; }
if (!/take over/.test(sb2.textContent)) { console.log("take-over label wrong:", sb2.textContent); failures++; }

// ---- remote players: the clip must reach a phone that isn't in the room ----
// Fetch+decode is async, so these steps are awaited (settle() drains the
// microtask queue) — a synchronous check would pass before playback happened.
// setImmediate, not process.nextTick: it yields to the check phase, by which
// point EVERY queued promise continuation has run. nextTick jumps the microtask
// queue instead, so a multi-step chain (fetch -> .json() -> render rows) was
// only part-way through when the assertions ran.
const settle = () => new Promise(r => setImmediate(r));
const clipsOf = () => fetched.filter(u => typeof u === "string" && /audio|\\.mp3|\\/clips\\//.test(u));
global.__remoteChecks = async () => {
  ws = new WebSocket();  // connect() is stripped below, so stand a live socket up

  // a local player's phone stays silent — otherwise the room echoes itself
  joined = true; myName = "Alice"; iAmRemote = false;
  fetched.length = 0; sent.length = 0;
  state = ${JSON.stringify(snapshots[2])}; render(); remoteAudioTick(); await settle();
  if (clipsOf().length) { console.log("local player fetched clip audio:", clipsOf()); failures++; }
  if (sent.some(m => m.type === "audio_started")) { console.log("local player reported audio_started"); failures++; }
  if (!/silent/.test(document.getElementById("where-hint").textContent)) {
    console.log("local hint wrong:", document.getElementById("where-hint").textContent); failures++; }

  // a remote player DOES fetch it, from the phase-gated endpoint only, and
  // reports the start so the server can measure their speed bonus fairly
  fetched.length = 0; sent.length = 0; iAmRemote = true; audioKey = ""; audioReported = "";
  render(); remoteAudioTick(); await settle();
  const urls = clipsOf();
  if (!urls.length) { console.log("remote player never fetched the clip"); failures++; }
  if (urls.some(u => u.startsWith("/clips/"))) {
    console.log("remote fetched /clips — that URL leaks the track id:", urls); failures++; }
  if (!urls.every(u => u.startsWith("/api/round/audio?kind="))) {
    console.log("remote clip url is not the phase-gated endpoint:", urls); failures++; }
  if (!sent.some(m => m.type === "audio_started")) {
    console.log("remote player did not report audio_started:", JSON.stringify(sent)); failures++; }

  // the same round re-broadcast must not restart the clip mid-listen, nor
  // re-report the start (which would push the scoring baseline out)
  fetched.length = 0; sent.length = 0; render(); remoteAudioTick(); await settle();
  if (clipsOf().length) { console.log("re-broadcast restarted the clip"); failures++; }
  if (sent.some(m => m.type === "audio_started")) { console.log("re-broadcast re-reported audio_started"); failures++; }

  // ...nor does replaying the SAME buffer (what the tap-unlock overlay does when
  // autoplay swallowed the first attempt) re-report the start. The server ignores
  // repeats too, but a second report here would be asking for a bigger bonus.
  sent.length = 0; startBuffer(curBuffer);
  if (sent.some(m => m.type === "audio_started")) {
    console.log("replaying the same clip re-reported audio_started"); failures++; }

  // ...but the next round does re-fetch
  fetched.length = 0;
  state = Object.assign({}, ${JSON.stringify(snapshots[2])}, {round: 4}); render(); remoteAudioTick(); await settle();
  if (!clipsOf().length) { console.log("new round did not fetch a clip"); failures++; }

  // leaving the question phase stops the audio and clears the status line
  state = ${JSON.stringify(snapshots[5])}; render(); remoteAudioTick(); await settle();
  if (!document.getElementById("q-audio").hidden) { console.log("audio status left showing outside a round"); failures++; }

  // the lobby toggle tells the server, and the roster marks who's away
  joined = true; sent.length = 0; iAmRemote = false;
  state = ${JSON.stringify(snapshots[1])}; render();
  if (!/somewhere else/.test(document.getElementById("lobby-where-link").textContent)) {
    console.log("lobby where-link wrong when local:", document.getElementById("lobby-where-link").textContent); failures++; }
  toggleWhere();
  if (!sent.some(m => m.type === "set_remote" && m.remote === true)) {
    console.log("toggleWhere did not send set_remote:", JSON.stringify(sent)); failures++; }
  if (!/in the room/.test(document.getElementById("lobby-where-link").textContent)) {
    console.log("lobby where-link wrong when remote"); failures++; }

  // ---- all-time top scores: persistent, and shown at the two useful moments --
  const atCard = document.getElementById("alltime-card");
  const atList = document.getElementById("alltime-list");
  const lbFetches = () => fetched.filter(u => typeof u === "string" && u.startsWith("/api/leaderboard")).length;

  // 1. on the join screen, before joining: walk up and see who you're chasing
  alltimeKey = ""; fetched.length = 0; joined = false; myName = "Bob";
  state = { phase: "idle", players: [] }; render(); await settle();
  if (atCard.hidden) { console.log("all-time hidden on the join screen"); failures++; }
  if (!/Alice/.test(atList.innerHTML) || !/14320/.test(atList.innerHTML)) {
    console.log("all-time rows missing on join:", atList.innerHTML); failures++; }
  if (!/12 games/.test(atList.innerHTML)) {
    console.log("all-time game count missing:", atList.innerHTML); failures++; }
  // your own row stands out — it's the only place a phone shows where you stand.
  // myName is Bob here, so Bob's row carries the accent and Alice's must not.
  const rows = atList.innerHTML.split("<span").filter(r => /Alice|Bob|Carol/.test(r));
  const rowFor = (who) => rows.find(r => r.includes(who)) || "";
  if (!/accent/.test(rowFor("Bob"))) {
    console.log("own row not highlighted:", rowFor("Bob")); failures++; }
  if (/accent/.test(rowFor("Alice"))) {
    console.log("someone else's row highlighted as mine:", rowFor("Alice")); failures++; }

  // 2. it does NOT nag mid-game — a leaderboard over the answer buttons is noise
  state = ${JSON.stringify(snapshots[2])}; joined = true; render(); await settle();
  if (!atCard.hidden) { console.log("all-time showing during a question"); failures++; }
  state = ${JSON.stringify(snapshots[3])}; render(); await settle();
  if (!atCard.hidden) { console.log("all-time showing during a reveal"); failures++; }

  // 3. and it comes back with the final scores, refetched so tonight is included
  //    (finish() writes results before broadcasting "finished")
  fetched.length = 0;
  state = ${JSON.stringify(snapshots[9])}; render(); await settle();
  if (atCard.hidden) { console.log("all-time hidden on the finished screen"); failures++; }
  if (lbFetches() !== 1) {
    console.log("finished screen should refetch the leaderboard exactly once, got", lbFetches()); failures++; }

  // 4. re-renders of the same screen must not re-request it every state message
  fetched.length = 0; render(); render(); await settle();
  if (lbFetches()) { console.log("leaderboard refetched on every render:", lbFetches()); failures++; }

  // 5. a brand-new install says so rather than showing an empty box
  leaderboardRows = []; alltimeKey = "";
  joined = false; state = { phase: "idle", players: [] }; render(); await settle();
  if (!/no games finished yet/.test(atList.innerHTML)) {
    console.log("empty leaderboard not explained:", atList.innerHTML); failures++; }
  leaderboardRows = LEADERBOARD;
};
`;
eval(src.replace(/^connect\(\);?$/m, "") + scenario);
global.__remoteChecks().then(() => {
  if (failures) { console.log("FAIL:", failures); process.exit(1); }
  console.log("render smoke: all phases render clean for host + non-host, remote audio routed");
});
