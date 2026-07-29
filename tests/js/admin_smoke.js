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
           addEventListener(){}, appendChild(){},
           // Parses the inputs back out of whatever innerHTML the render wrote, so
           // groupPicks() reads the real checked state instead of a hand-fed list —
           // that's the bit that decides which speakers get grouped.
           querySelectorAll(sel) {
             if (sel !== "input") return [];
             return [...String(this.innerHTML).matchAll(/<input[^>]*>/g)].map(m => ({
               value: (/value="([^"]*)"/.exec(m[0]) || [, ""])[1],
               checked: /\schecked/.test(m[0]),
             }));
           } };
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
// A /api/admin/speakers payload shaped like the real house: entity PAIRS (native +
// Music Assistant twin), a TV, and a saved target/group already in the DB.
const speakers = {
  configured: true, env_default: "media_player.kitchen_music_assistant",
  target: "media_player.study_music_assistant", overridden: true,
  group: ["media_player.kitchen"],
  players: [
    { entity_id: "media_player.kitchen", name: "Kitchen", state: "playing",
      can_group: true, is_ma: false, group_members: ["media_player.kitchen"], volume: 0.3 },
    { entity_id: "media_player.kitchen_music_assistant", name: "Kitchen MA", state: "idle",
      can_group: false, is_ma: true, group_members: [], volume: 0.3 },
    { entity_id: "media_player.living_room_tv", name: "Living Room TV", state: "idle",
      can_group: false, is_ma: false, group_members: [], volume: null },
    { entity_id: "media_player.study", name: "Study", state: "idle",
      can_group: true, is_ma: false, group_members: ["media_player.study"], volume: null },
    { entity_id: "media_player.study_music_assistant", name: "Study MA", state: "idle",
      can_group: false, is_ma: true, group_members: [], volume: null },
  ],
};
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
for (const t of TABS) {
  try { showTab(t); } catch (e) { console.log("showTab threw:", t, e.message); failures++; }
  for (const o of TABS) {
    if (document.getElementById("sec-" + o).hidden !== (o !== t)) {
      console.log("tab visibility wrong:", t, "->", o); failures++; }
  }
}
// import flow elements exist and preview handles a server reject without throwing
for (const id of ["prompt-region", "prompt-facts", "prompt-tf", "copy-prompt-btn", "import-text", "import-result",
                  "import-preview-btn", "import-commit-btn"]) {
  document.getElementById(id);  // MISSING ELEMENT fires if absent
}

// ---- Speakers tab ----
const sel = document.getElementById("speaker-select");
const picks = document.getElementById("speaker-group");
speakerState = ${JSON.stringify(speakers)};
try { renderSpeakers(); } catch (e) { console.log("renderSpeakers threw:", e.message); failures++; }
// the target is preselected, so Save/Test don't silently retarget a different speaker
if (!/value="media_player.study_music_assistant" selected/.test(sel.innerHTML)) {
  console.log("saved target not selected:", sel.innerHTML); failures++; }
// MA entities are what can actually play a URL — they must be offered first
if (sel.innerHTML.indexOf("Music Assistant<") > sel.innerHTML.indexOf("Other (")) {
  console.log("Music Assistant entities not listed first:", sel.innerHTML); failures++; }
if (!/— use .env default —/.test(sel.innerHTML)) { console.log("no way to clear the override"); failures++; }
// only groupable speakers get a checkbox, and only the NATIVE entity is groupable
if (/kitchen_music_assistant"/.test(picks.innerHTML)) {
  console.log("MA twin offered for grouping — joining it does nothing:", picks.innerHTML); failures++; }
if (!/value="media_player.kitchen"/.test(picks.innerHTML) ||
    !/value="media_player.study"/.test(picks.innerHTML)) {
  console.log("groupable speakers missing from the group list:", picks.innerHTML); failures++; }
if (/living_room_tv/.test(picks.innerHTML)) { console.log("a TV offered for grouping"); failures++; }
// the saved group comes back ticked, and is what groupPicks() would submit
if (JSON.stringify(groupPicks()) !== '["media_player.kitchen"]') {
  console.log("saved group not preselected:", JSON.stringify(groupPicks())); failures++; }
if (!/overrides .env/.test(document.getElementById("speaker-status").textContent)) {
  console.log("override not flagged:", document.getElementById("speaker-status").textContent); failures++; }
// a speaker saved earlier but offline now must stay selectable, or Save silently moves it
speakerState = Object.assign({}, ${JSON.stringify(speakers)}, { target: "media_player.gone" });
renderSpeakers();
if (!/media_player.gone" selected/.test(sel.innerHTML)) {
  console.log("offline saved target dropped from the list:", sel.innerHTML); failures++; }
// HA unreachable: the reason shows and the page still works
speakerState = { configured: true, players: [], target: "", group: [], env_default: "", error: "HA is down" };
renderSpeakers();
if (!/HA is down/.test(document.getElementById("speaker-status").textContent)) {
  console.log("HA error not surfaced:", document.getElementById("speaker-status").textContent); failures++; }
// no HA at all (cast-board-only setup) renders an explanation, not an empty box
speakerState = { configured: false, players: [], target: "", group: [], env_default: "" };
renderSpeakers();
if (!/not configured/.test(sel.innerHTML)) { console.log("unconfigured HA not explained"); failures++; }
if (groupPicks().length) { console.log("group picks non-empty without HA"); failures++; }
`;
eval(src.replace(/^refresh\(\);?$/m, "") + scenario);
if (failures) { console.log("FAIL:", failures); process.exit(1); }
console.log("admin smoke: idle/running/failed render clean");
