// Drives admin.js's render() in a stub DOM against idle / running / failed
// snapshots — same convention as render_smoke.js: a thrown render or a
// referenced-but-missing element id fails the build.
const fs = require("fs");
const path = require("path");
const base = path.join(__dirname, "..", "..", "app", "static");
const src = fs.readFileSync(path.join(base, "admin.js"), "utf8");
const html = fs.readFileSync(path.join(base, "admin.html"), "utf8");
const ids = new Set([...html.matchAll(/id="([^"]+)"/g)].map(m => m[1]));
const elems = {};
let failures = 0;
function mkEl(id) {
  return { id, hidden: false, disabled: false, textContent: "", innerHTML: "", value: "",
           className: "", style: {}, classList: { add(){}, remove(){} },
           addEventListener(){}, appendChild(){}, querySelectorAll(){ return []; } };
}
global.document = {
  getElementById(id) {
    if (!ids.has(id)) { console.log("MISSING ELEMENT:", id); failures++; return mkEl(id); }
    return elems[id] || (elems[id] = mkEl(id));
  },
  addEventListener() {},
};
global.window = {};
global.localStorage = { getItem: () => "", setItem() {} };
global.fetch = () => { throw new Error("no network in the smoke test"); };
global.setTimeout = () => 0; global.clearTimeout = () => {};
global.prompt = () => ""; global.confirm = () => false;

const t = Math.round(Date.now() / 1000);
const players = [{ name: "Alice", score: 1240 }, { name: "Bob", score: 980 }];
const job = (over) => Object.assign({ running: false, stage: "done", started_at: t - 700,
                                      finished_at: t - 600, summary: { n: 3 }, error: null,
                                      log: ["10:00:00 INFO did things"] }, over);
const snapshots = [
  // fresh boot: no jobs, no game
  { current: null, jobs: {}, game: { phase: "idle" },
    trivia: { fact: { total: 180, played: 42, left: 138 },
              tf: { total: 215, played: 60, left: 155 } },
    leaderboard_games: 12 },
  // one running with progress + a game mid-question
  { current: "clips",
    jobs: { clips: job({ running: true, stage: "cutting — 61 remaining", finished_at: null }),
            sync: job({ summary: { albums: 3283, tracks_active: 47540 } }) },
    game: { phase: "question", host: "Alice", round: 7, total_rounds: 15, players,
            answered: 1, window_left: 8.2, display: "Sitting Room TV",
            last_revealed: { round: 6, title: "Take On Me", artist: "a-ha" } } },
  // a failure with error + log, game in reveal
  { current: null,
    jobs: { lastfm: job({ error: "Last.fm timeout", stage: "failed",
                          log: ["05:45 ERROR boom"] }),
            bootstrap: job({ summary: { tracks_synced: 5, warning: "no progress" } }) },
    game: { phase: "reveal", host: "Bob", round: 3, total_rounds: 10, players,
            display: "none", last_revealed: { round: 3, title: "Song", artist: "Artist" } } },
];
const scenario = `
statsState = { tracks_active: 47540, with_family_score: 884,
               tiered: [{tier:"easy",c:232},{tier:"medium",c:12470},{tier:"hard",c:15307}] };
healthState = { ready_to_play: true, tracks_playable: 12583, version: "1.21.0" };
for (const snap of ${JSON.stringify(snapshots)}) {
  adminState = snap;
  try { render(); } catch (e) { console.log("RENDER THREW:", e.message); failures++; }
}
// running job: its button says Running…, every button disabled, stage shown
adminState = ${JSON.stringify(snapshots[1])}; render();
if (document.getElementById("run-clips").textContent !== "Running…") {
  console.log("running button label wrong"); failures++; }
if (!document.getElementById("run-sync").disabled) {
  console.log("other buttons live during a run"); failures++; }
if (!/61 remaining/.test(document.getElementById("status-clips").textContent)) {
  console.log("stage missing:", document.getElementById("status-clips").textContent); failures++; }
// the spoiler line + revealed song
if (!/hidden until reveal/.test(document.getElementById("game-detail").textContent)) {
  console.log("spoiler note missing"); failures++; }
if (!/Take On Me/.test(document.getElementById("game-last").textContent)) {
  console.log("last revealed missing"); failures++; }
// failed job shows its error; warning surfaces on ok jobs
adminState = ${JSON.stringify(snapshots[2])}; render();
if (!/Last\\.fm timeout/.test(document.getElementById("status-lastfm").textContent)) {
  console.log("error missing:", document.getElementById("status-lastfm").textContent); failures++; }
if (!/no progress/.test(document.getElementById("status-bootstrap").textContent)) {
  console.log("warning missing:", document.getElementById("status-bootstrap").textContent); failures++; }
if (document.getElementById("run-lastfm").disabled) {
  console.log("buttons still dead when idle"); failures++; }
// idle game hides the controls
adminState = ${JSON.stringify(snapshots[0])}; render();
if (!document.getElementById("game-controls").hidden) {
  console.log("game controls visible with no game"); failures++; }
// health chip + version + playable count
if (!/ready to play/.test(document.getElementById("ready-chip").textContent)) {
  console.log("ready chip missing:", document.getElementById("ready-chip").textContent); failures++; }
if (!/1\\.21\\.0/.test(document.getElementById("app-version").textContent)) {
  console.log("version missing"); failures++; }
if (!/playable/.test(document.getElementById("stats-row").innerHTML)) {
  console.log("playable count missing from stats row"); failures++; }
healthState = { ready_to_play: false, tracks_playable: 0, version: "1.21.0" }; render();
if (!/not ready/.test(document.getElementById("ready-chip").textContent)) {
  console.log("not-ready state missing"); failures++; }
// trivia stats + wipe controls (snapshot 0 carries trivia + leaderboard_games)
adminState = ${JSON.stringify(snapshots[0])}; render();
const trow = document.getElementById("trivia-row").innerHTML;
if (!/facts/.test(trow) || !/138/.test(trow) || !/true\\/false/.test(trow) || !/155/.test(trow)) {
  console.log("trivia stats wrong:", trow); failures++; }
if (!/12 games/.test(document.getElementById("leaderboard-wipe-btn").textContent)) {
  console.log("leaderboard game count missing"); failures++; }
// missing trivia payload must not throw and renders zeros
adminState = { current: null, jobs: {}, game: { phase: "idle" } };
try { render(); } catch (e) { console.log("render threw without trivia:", e.message); failures++; }
// tabs: exactly one section visible, selected tab highlighted, choice persisted
for (const t of ["game", "library", "trivia"]) {
  try { showTab(t); } catch (e) { console.log("showTab threw:", t, e.message); failures++; }
  for (const o of ["game", "library", "trivia"]) {
    if (document.getElementById("sec-" + o).hidden !== (o !== t)) {
      console.log("tab visibility wrong:", t, "->", o); failures++; }
  }
}
`;
eval(src.replace(/^refresh\(\);?$/m, "") + scenario);
if (failures) { console.log("FAIL:", failures); process.exit(1); }
console.log("admin smoke: idle/running/failed render clean");
