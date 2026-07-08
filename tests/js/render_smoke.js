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
           appendChild(){}, querySelectorAll(){ return []; } };
}
global.document = {
  getElementById(id) {
    if (!ids.has(id)) { console.log("MISSING ELEMENT:", id); failures++; return mkEl(id); }
    return elems[id] || (elems[id] = mkEl(id));
  },
  querySelector(sel) { return elems[sel] || (elems[sel] = mkEl(sel)); },
  querySelectorAll() { return []; },
  createElement(t) { return mkEl(t); },
};
global.window = { location: { protocol: "http:", host: "x" } };
global.localStorage = { getItem: () => "Alice", setItem(){} };
global.navigator = {};
global.WebSocket = function(){ return { send(){}, close(){} }; };
global.fetch = () => ({ then: () => ({ then(){}, catch(){} }), catch(){} });
global.setInterval = () => 0; global.clearInterval = () => {}; global.setTimeout = () => 0;

const track = { id: "abc", title: "Song", artist: "Artist", album: "Album", year: 2001 };
const players = [{ name: "Alice", score: 100, ready: true, picked_artists: true },
                 { name: "Bob", score: 200, ready: false, picked_artists: false }];
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
// payoff lock: next button disabled with countdown text while the song plays out
joined = true; myName = "Alice"; state = ${JSON.stringify(snapshots[4])}; render();
if (!btn.disabled) { console.log("r-next not locked during payoff"); failures++; }
if (!/9s/.test(btn.textContent)) { console.log("payoff countdown missing:", btn.textContent); failures++; }
state = ${JSON.stringify(snapshots[3])}; render();
if (btn.disabled) { console.log("r-next still locked after payoff"); failures++; }
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
`;
eval(src.replace(/^connect\(\);?$/m, "") + scenario);
if (failures) { console.log("FAIL:", failures); process.exit(1); }
console.log("render smoke: all phases render clean for host + non-host");
