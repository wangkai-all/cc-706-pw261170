/* Node-only smoke tests for the browser helpers.
 * Loads app.js in a vm with tiny DOM stubs, cuts the boot IIFE, and checks
 * pure rendering helpers (escaping, range painting, meter, group diagram).
 * Run: node tests/ui_smoke.js
 */
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const assert = require('assert');

const source = fs.readFileSync(path.join(__dirname, '..', 'corpuslake', 'static', 'app.js'), 'utf8');
const cut = source.indexOf('(async function boot()');
assert(cut > 0, 'boot function found');
const body = source.slice(0, cut);

function stubEl() {
  return {
    innerHTML: '', textContent: '', className: '', dataset: {}, style: {},
    classList: { add() {}, remove() {}, toggle() {} },
    addEventListener() {}, appendChild() {}, remove() {}, querySelector() { return null; },
    querySelectorAll() { return []; }, focus() {}, scrollIntoView() {},
  };
}

const sandbox = {
  console,
  document: {
    querySelector: () => stubEl(),
    querySelectorAll: () => [],
    createElement: () => stubEl(),
    addEventListener() {}, removeEventListener() {}, body: stubEl(),
  },
  window: { addEventListener() {}, location: { hash: '' } },
  location: { hash: '' },
  fetch: async () => ({ ok: true, status: 200, headers: { get: () => 'application/json' }, json: async () => ({}) }),
  FormData: class { get() { return ''; } },
  setTimeout, clearTimeout, setInterval, clearInterval,
  CSS: { escape: s => s },
  URLSearchParams,
  Set, Map, Number, Math, Date, JSON, Array, Object, String,
  Error,
};
sandbox.globalThis = sandbox;
vm.createContext(sandbox);
vm.runInContext(body + '\n;globalThis.__exports = { esc, paintRanges, meter, edgeDiagram, fmtScore, truncate };', sandbox);
const fn = sandbox.__exports;

let checks = 0;
function check(name, cond, extra) {
  checks += 1;
  if (!cond) { console.error('FAIL ' + name + (extra ? '\n  ' + extra : '')); process.exitCode = 1; }
  else console.log('ok   ' + name);
}

check('esc escapes all five entities',
  fn.esc('<a href="x">&\'') === '&lt;a href=&quot;x&quot;&gt;&amp;&#39;');

check('fmtScore rounding', fn.fmtScore(0.6034) === '60.3%');

const text = 'Hello brave world';
const html = fn.paintRanges(text, [
  { start: 6, end: 11, classes: ['shared'], seg: 0 },
  { start: 6, end: 8, classes: ['active'], seg: 0 },
]);
check('paintRanges keeps text intact',
  html.replace(/<[^>]+>/g, '') === text, html);
check('paintRanges merges overlapping classes deterministically',
  /class="active shared"/.test(html), html);
check('paintRanges escapes markup in the text',
  fn.paintRanges('<script>', [{ start: 0, end: 8, classes: ['shared'], seg: 0 }])
    .indexOf('&lt;script&gt;') >= 0);
check('paintRanges ignores out-of-bounds ranges',
  fn.paintRanges('abc', [{ start: 1, end: 99, classes: ['shared'] }]).replace(/<[^>]+>/g, '') === 'abc');
check('paintRanges empty ranges is plain escaped text',
  fn.paintRanges('a<b', []) === 'a&lt;b');

const m = fn.meter(0.5, 0.6);
check('meter shows score and threshold position',
  m.indexOf('50.0%') >= 0 && m.indexOf('--threshold-pos:60.0%') >= 0, m);

const group = {
  members: [{ id: 'doc_a', title: 'Alpha' }, { id: 'doc_b', title: 'Beta' },
            { id: 'doc_c', title: 'Gamma' }],
  pairs: [
    { doc_a: 'doc_a', doc_b: 'doc_b', edge: { jaccard: 0.8, edge_id: 1 } },
    { doc_a: 'doc_a', doc_b: 'doc_c', edge: null },
    { doc_a: 'doc_b', doc_b: 'doc_c', edge: { jaccard: 0.7, edge_id: 2 } },
  ],
  edges: [],
};
const svg = fn.edgeDiagram(group);
check('diagram draws a line per pair', (svg.match(/<line/g) || []).length === 3, svg);
check('diagram marks the missing pair explicitly', svg.indexOf('无直接边') >= 0);
check('diagram labels scores for small groups', svg.indexOf('80.0%') >= 0 && svg.indexOf('70.0%') >= 0);
check('diagram does not draw an A-C solid edge',
  (svg.match(/class="link"/g) || []).length === 2,
  'solid links=' + (svg.match(/class="link"/g) || []).length);
check('diagram escapes titles', fn.edgeDiagram({
  members: [{ id: 'doc_x', title: '<img src=x>' }], pairs: [], edges: [],
}).indexOf('&lt;img') >= 0);

const big = fn.edgeDiagram({
  members: Array.from({ length: 12 }, (_, i) => ({ id: 'd' + i, title: 'T' + i })),
  pairs: [], edges: [],
});
check('diagram degrades gracefully past 9 members', big.indexOf('图略') >= 0);

console.log('\n' + checks + ' UI checks run');
