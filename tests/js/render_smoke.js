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
global.localStorage = { getItem: () => "Colm", setItem(){} };
global.navigator = {};
global.WebSocket = function(){ return { send(){}, close(){} }; };
global.fetch = () => ({ then: () => ({ then(){}, catch(){} }), catch(){} });
global.setInterval = () => 0; global.clearInterval = () => {}; global.setTimeout = () => 0;

const track = { id: "abc", title: "Song", artist: "Artist", album: "Album", year: 2001 };
const players = [{ name: "Colm", score: 100, ready: true, picked_artists: true },
                 { name: "Oli", score: 200, ready: false, picked_artists: false }];
const snapshots = [
  { phase: "idle", players: [] },
  { phase: "lobby", host: "Colm", players },
  { phase: "question", host: "Colm", round: 3, total_rounds: 10, clip_len: 5, replay: 0,
    options: [{title:"a",artist:"b"},{title:"c",artist:"d"},{title:"e",artist:"f"},{title:"g",artist:"h"}],
    answered: ["Colm"], players },
  { phase: "reveal", host: "Colm", round: 3, total_rounds: 10, track,
    round_answers: { Colm: { points: 120 } }, flagged: false, players },
  { phase: "break", host: "Colm", players },
  { phase: "finished", host: "Colm", players, track },
];
const scenario = `
joined = true;
for (const snap of ${JSON.stringify(snapshots)}) {
  for (const who of ["Colm", "Oli"]) {
    joined = true;  // idle/finished renders reset joined (real play-again behaviour)
    myName = who;
    state = snap;
    try { render(); } catch (e) {
      console.log("RENDER THREW", snap.phase, "as", who, "->", e.message); failures++;
    }
  }
}
// the regression that shipped: host's next-song button must end up with text
joined = true; myName = "Colm"; state = ${JSON.stringify(snapshots[3])}; render();
const btn = document.getElementById("r-next");
if (!btn.textContent) { console.log("r-next has no text on host reveal"); failures++; }
if (btn.hidden) { console.log("r-next hidden on host reveal"); failures++; }
myName = "Oli"; render();
if (!document.getElementById("r-next").hidden) { console.log("r-next visible for non-host"); failures++; }
`;
eval(src.replace(/^connect\(\);?$/m, "") + scenario);
if (failures) { console.log("FAIL:", failures); process.exit(1); }
console.log("render smoke: all phases render clean for host + non-host");
