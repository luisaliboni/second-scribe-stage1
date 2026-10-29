'use strict';
// Task 40: the Instructions screen ("Compare instructions", app-instructions.js; spec 6.8) and the app's Results
// screen (app-results.js; spec 6.3, 6.7 gate; plan V2, V13, V14).
// The app's scripts load in the server's order: core, the viewer's scripts (harness ORDER), api, the shell, then every
// app-*.js by name, into a vm context with a small stub DOM and a fake fetch that answers like the Task 36/40 routes.
// Fixtures: tests/fixtures/views/mini.view.json and tests/fixtures/profiles/profile-synth.json.
const test = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const { ORDER } = require('./harness');

const ROOT = path.resolve(__dirname, '..', '..');
const JS = path.join(ROOT, 'second_scribe', 'app', 'static', 'js');
const EN = JSON.parse(fs.readFileSync(path.join(ROOT, 'second_scribe', 'app', 'strings', 'en.json'), 'utf8'));
const VIEW = JSON.parse(fs.readFileSync(path.join(ROOT, 'tests', 'fixtures', 'views', 'mini.view.json'), 'utf8'));
const PROFILE = JSON.parse(fs.readFileSync(path.join(ROOT, 'tests', 'fixtures', 'profiles', 'profile-synth.json'), 'utf8'));
const PROMPT3 = EN['study.reading3.prompt'].replace(/\{\{/g, '{').replace(/\}\}/g, '}');
const clone = (o) => JSON.parse(JSON.stringify(o));

// --- a small DOM -------------------------------------------------------------------------------------------------
function textNode(s) {
  return { nodeType: 3, nodeValue: String(s), get textContent() { return this.nodeValue; }, parentNode: null };
}
function makeNode(tag) {
  const node = {
    nodeType: 1, tagName: String(tag).toUpperCase(), childNodes: [], attributes: {}, listeners: {}, style: {},
    dataset: {}, parentNode: null, disabled: false, value: '', checked: false, hidden: false, _text: '',
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    get children() { return node.childNodes.filter((c) => c.nodeType === 1); },
    get firstChild() { return node.childNodes[0] || null; },
    get className() { return node.attributes.class || ''; },
    set className(v) { node.attributes.class = String(v); },
    get textContent() { return node._text + node.childNodes.map((c) => c.textContent).join(''); },
    set textContent(v) { node.childNodes.forEach((c) => { c.parentNode = null; }); node.childNodes = []; node._text = String(v); },
    setAttribute(k, v) { node.attributes[k] = String(v); },
    getAttribute(k) { return Object.prototype.hasOwnProperty.call(node.attributes, k) ? node.attributes[k] : null; },
    removeAttribute(k) { delete node.attributes[k]; },
    appendChild(c) { if (c.parentNode) c.parentNode.removeChild(c); c.parentNode = node; node.childNodes.push(c); return c; },
    insertBefore(c, ref) {
      if (!ref) return node.appendChild(c);
      if (c.parentNode) c.parentNode.removeChild(c);
      c.parentNode = node; node.childNodes.splice(node.childNodes.indexOf(ref), 0, c); return c;
    },
    removeChild(c) { node.childNodes.splice(node.childNodes.indexOf(c), 1); c.parentNode = null; return c; },
    addEventListener(type, fn) { (node.listeners[type] = node.listeners[type] || []).push(fn); },
    removeEventListener() {},
    fire(type, extra) { (node.listeners[type] || []).forEach((fn) => fn(Object.assign({ preventDefault() {}, target: node }, extra || {}))); },
    focus() {},
    scrollIntoView() {},
  };
  return node;
}
function all(node, pred, out) {
  out = out || [];
  if (node.nodeType === 1) {
    if (pred(node)) out.push(node);
    node.childNodes.forEach((c) => all(c, pred, out));
  }
  return out;
}
const cls = (n, c) => (n.attributes.class || '').split(' ').indexOf(c) >= 0;
const byClass = (node, c) => all(node, (n) => cls(n, c));
const byTag = (node, tag) => all(node, (n) => n.tagName === tag.toUpperCase());
const byAttr = (node, k, v) => all(node, (n) => n.getAttribute(k) === (v === undefined ? n.getAttribute(k) : v) && n.getAttribute(k) !== null);
const count = (text, needle) => text.split(needle).length - 1;
function button(node, label) {
  const hits = byTag(node, 'button').filter((b) => b.textContent === label);
  assert.ok(hits.length, 'no button ' + label);
  return hits[0];
}

// --- loading the app ---------------------------------------------------------------------------------------------
function appScripts() {
  const screens = fs.readdirSync(JS).filter((f) => /^app-[a-z0-9-]+\.js$/.test(f) && f !== 'app-shell.js').sort();
  return ORDER.filter((f) => fs.existsSync(path.join(JS, f))).concat(['api.js', 'app-shell.js'], screens);
}
function reply(status, doc) {
  return { ok: status >= 200 && status < 300, status, text: () => Promise.resolve(JSON.stringify(doc)) };
}

function load(answer) {
  const calls = [];
  const errors = [];
  const intervals = [];
  const winListeners = {};
  const body = makeNode('body');
  const sandbox = {
    console: { error: (...a) => errors.push(a.map(String).join(' ')), log() {}, warn() {} },
    location: { hash: '', search: '' },
    SS_STRINGS: Object.assign({}, EN),
    document: { createElement: makeNode, createElementNS: (ns, t) => makeNode(t), createTextNode: textNode,
      addEventListener() {}, body, hidden: false, activeElement: null },
    setTimeout: (fn) => { fn(); return 0; }, clearTimeout() {},
    setInterval: (fn) => { intervals.push(fn); return intervals.length; },
    clearInterval: (id) => { intervals[id - 1] = null; },
    addEventListener: (t, fn) => { (winListeners[t] = winListeners[t] || []).push(fn); },
    removeEventListener: (t, fn) => { const l = winListeners[t] || []; const i = l.indexOf(fn); if (i >= 0) l.splice(i, 1); },
    fetch: (url, init) => {
      const u = String(url);
      calls.push({ url: u, method: init.method, body: init.body ? JSON.parse(init.body) : undefined });
      if (u === '/api/session') return Promise.resolve(reply(200, { csrf: 'tok' }));
      return Promise.resolve(answer(u, init, calls));
    },
  };
  sandbox.window = sandbox;
  sandbox.history = { replaceState: (a, b, h) => { sandbox.location.hash = h; } };
  const ctx = vm.createContext(sandbox);
  for (const f of appScripts()) vm.runInContext(fs.readFileSync(path.join(JS, f), 'utf8'), ctx, { filename: f });
  const main = makeNode('main');
  body.appendChild(main);
  return { SS: sandbox.SS, win: sandbox, calls, errors, main, intervals, winListeners };
}
const tick = () => new Promise((r) => setImmediate(r));
async function settle() { for (let i = 0; i < 30; i++) await tick(); }

// --- a fake server -----------------------------------------------------------------------------------------------
const PROMPTS = {
  rungs: [
    { rung: 'rung1', file: 'reconstruct.p0.v1.txt', text: 'RUNG ONE TEXT', sha256: '1'.repeat(64), system: null, definitions: null, requires: [] },
    { rung: 'rung2', file: 'reconstruct.p0.v2.txt', text: 'RUNG TWO TEXT', sha256: '2'.repeat(64),
      system: { file: 'system.reconstruct.v2.txt', text: 'SYSTEM TEXT', sha256: 'a'.repeat(64) },
      definitions: { file: 'definitions.v2.txt', text: 'DEFINITIONS TEXT', sha256: 'b'.repeat(64) }, requires: [] },
    { rung: 'rung3', file: 'reconstruct.p0.remedy.v1.txt', text: 'REMEDY TEXT', sha256: '3'.repeat(64), system: null, definitions: null, requires: ['placebo'] },
    { rung: 'placebo', file: 'reconstruct.p0.placebo.v1.txt', text: 'PLACEBO TEXT', sha256: '4'.repeat(64), system: null, definitions: null, requires: [] },
  ],
  wrapper: { text: 'WRAPPER TEXT\nTRANSCRIPT:\n{transcript}\n', sha256: '5'.repeat(64) },
  reading3: { file: 'prior_only.p0.v2.txt', sha256: '9b48f863' + '0'.repeat(56) },
};
function runRungs(picked) {
  const k = new Set(picked.concat(['rung2']));
  if (k.has('rung3')) k.add('placebo');
  return ['rung1', 'rung2', 'placebo', 'rung3'].filter((r) => k.has(r));
}
function refsDoc(extra) {
  return Object.assign({
    references: [
      { pid: 'MAP-A', type: 'A', map_confirmed: true, coding: { n_marks: 0, locked: false } },
      { pid: 'MAP-B', type: 'B', map_confirmed: false, coding: { n_marks: 2, locked: false } },
      { pid: 'MAP-C', type: 'C', map_confirmed: false, coding: { n_marks: 0, locked: false } },
    ],
    gate: { open: false, waiting: ['MAP-B'], revealed: false },
  }, extra || {});
}

function server(state) {
  state = state || {};
  state.ins = state.ins || { rung1: 'basic', rung1_user: null, rungs: ['rung2'], preset: 'standard' };
  return (url, init) => {
    const p = url.split('?')[0];
    const m = init.method;
    if (m === 'GET' && p === '/api/prompts') return reply(200, PROMPTS);
    if (m === 'GET' && p === '/api/references') return reply(200, state.refs || refsDoc());
    if (p === '/api/instructions') {
      if (m === 'POST') {
        const b = JSON.parse(init.body);
        const ins = { rung1: b.rung1, rung1_user: state.ins.rung1_user, rungs: ['rung1', 'rung2', 'rung3'].filter((r) => r === 'rung2' || b.rungs.indexOf(r) >= 0), preset: b.preset };
        if (typeof b.rung1_text === 'string') ins.rung1_user = { text: b.rung1_text, text_sha256: 'c'.repeat(64), wrapper: PROMPTS.wrapper.text, wrapper_sha256: '5'.repeat(64) };
        state.ins = ins;
      }
      return reply(200, { instructions: state.ins, run_rungs: runRungs(state.ins.rungs) });
    }
    if (m === 'POST' && p === '/api/estimate') return reply(200, { estimate: { low_s: 600, high_s: 5400, calls: { Q: 40, J: 90, M: 0 },
      tokens: { recon: 1000, judge: 2000 }, words: { median: 900, demo: 301 }, probe_s: 2.5, remote_roles: [], includes_check: false } });
    if (m === 'GET' && p === '/api/runs/ready') {
      if (!state.ready) return reply(404, { error: 'not_found', message: 'no route' });
      return reply(200, typeof state.ready === 'function' ? state.ready() : state.ready);
    }
    if (m === 'POST' && p === '/api/runs/start' && state.startRefused) return reply(409, state.startRefused);
    if (m === 'POST' && p === '/api/runs/resume') return reply(200, { job: { run_id: 'A-0a1b2c3d', state: 'running', stage: 'Q', done: 10, total: 40 } });
    if (m === 'POST' && p === '/api/runs/start') return reply(200, { job: { run_id: 'A-0a1b2c3d', state: 'running', stage: 'Q', done: 0, total: 40 } });
    if (m === 'GET' && p === '/api/runs/status') {
      // no ?run=: the run in progress when the screen opens (Task 39), null by default
      if (url.indexOf('?run=') < 0) return reply(200, { job: state.current || null });
      return reply(200, { job: state.job || { run_id: 'A-0a1b2c3d', state: 'running', stage: 'J', done: 3, total: 10 } });
    }
    if (m === 'GET' && p === '/api/runs/last') {
      if (state.lastFails) return reply(500, { error: 'boom', message: 'boom' });
      return reply(200, { last: state.last || [] });
    }
    if (m === 'GET' && p === '/api/results/gate') return reply(200, state.gate || { open: true, waiting: [], revealed: false });
    if (m === 'GET' && p === '/api/results/runs') return reply(200, { runs: state.runs || [{ run_id: 'A-0a1b2c3d', group: 'A', finished: true, ready: true }] });
    if (m === 'GET' && p === '/api/results/profile') {
      if (state.refused) return reply(200, Object.assign({ run_id: 'A-0a1b2c3d', group: 'A', refused: state.refused }, state.refusals ? { refusals: state.refusals } : {}));
      return reply(200, { run_id: 'A-0a1b2c3d', group: 'A', profile: clone(PROFILE) });
    }
    if (m === 'GET' && p === '/api/view') return reply(200, { run_id: 'A-0a1b2c3d', group: 'A', view: clone(VIEW) });
    return reply(404, { error: 'not_found', message: 'no route' });
  };
}

async function openInstructions(state) {
  const env = load(server(state));
  env.SS.instructions.render(env.main);
  await settle();
  return env;
}
async function openResults(state, sub) {
  const env = load(server(state));
  env.SS.results.render(env.main, sub || '');
  await settle();
  return env;
}
const routesCalled = (calls) => calls.map((c) => c.method + ' ' + c.url.split('?')[0]).filter((r) => r !== 'GET /api/session');

// --- Instructions ------------------------------------------------------------------------------------------------

test('both screens register with the shell after Coding: Instructions 40, Results 50', () => {
  const { SS, errors } = load(server());
  assert.deepStrictEqual(errors, []);
  const ids = Array.from(SS.shell.list());
  assert.ok(ids.indexOf('coding') < ids.indexOf('instructions'), ids.join(','));
  assert.strictEqual(ids.indexOf('instructions') + 1, ids.indexOf('results'), ids.join(','));
});

test('step_named_run (Part 5b, scout F5; was "Compare instructions")', async () => {
  const { main, errors } = await openInstructions();
  assert.deepStrictEqual(errors, []);
  assert.strictEqual(EN['instructions.title'], 'Run');
  assert.strictEqual(byTag(main, 'h1')[0].textContent, 'Run');
  const t = main.textContent.toLowerCase();
  for (const bad of [/\bcorrect/, /\bfix\b/, /\bfirst\b/, /\bpool/, /\bscript/]) assert.ok(!bad.test(t), String(bad));
});

test('rung3_toggles_placebo', async () => {
  const env = await openInstructions();
  const { SS, main, calls } = env;
  assert.deepStrictEqual(Array.from(SS.instructions.selected()), ['rung2']);
  const state = () => byAttr(main, 'data-part', 'placebo_state')[0];
  assert.strictEqual(state().getAttribute('data-placebo'), 'false');
  const box = () => all(main, (n) => n.getAttribute('id') === 'ss-ins-rung3')[0];
  box().checked = true;
  box().fire('change');
  await settle();
  const post = calls.filter((c) => c.method === 'POST' && c.url === '/api/instructions').pop();
  assert.deepStrictEqual(post.body.rungs, ['rung2', 'rung3']);
  assert.deepStrictEqual(Array.from(SS.instructions.selected()), ['rung2', 'placebo', 'rung3']);
  assert.strictEqual(state().getAttribute('data-placebo'), 'true');
  assert.strictEqual(state().textContent, EN['instructions.rung3.placebo_on']);
  box().checked = false;
  box().fire('change');
  await settle();
  assert.deepStrictEqual(Array.from(SS.instructions.selected()), ['rung2']);
  assert.strictEqual(state().getAttribute('data-placebo'), 'false');
  // the remedy and the placebo are shown together, in full, with the harms note and the null text
  const r3 = byAttr(main, 'data-part', 'rung3')[0];
  const txt = r3.textContent;
  assert.ok(txt.indexOf('REMEDY TEXT') >= 0 && txt.indexOf('PLACEBO TEXT') >= 0);
  assert.ok(txt.indexOf(EN['instructions.rung3.harms']) >= 0);
  assert.ok(txt.indexOf(EN['instructions.rung3.null_text']) >= 0);
});

test('the three readings are named once; the no-interview gloss and its prompt appear once', async () => {
  const { main } = await openInstructions();
  const t = main.textContent;
  assert.strictEqual(count(t, EN['lane.no_interview']), 1);
  assert.strictEqual(count(t, EN['lane.no_interview_gloss']), 1);
  assert.strictEqual(count(t, PROMPT3), 1);
  assert.strictEqual(count(t, EN['lane.ai']), 1);
  assert.ok(t.indexOf('9b48f863') >= 0, 'the as-run file hash');
});

test('R7: the readings heading says "up to three" and mode C is named as having no third reading', async () => {
  const { main } = await openInstructions();
  assert.strictEqual(EN['instructions.readings.heading'], 'Each run gives up to three readings of every interview');
  assert.strictEqual(EN['instructions.readings.no_reference_c'], 'Reference type C has no third reading, because it uses no reference map.');  // council 30: one name
  const sec = byAttr(main, 'data-part', 'readings')[0];
  assert.ok(sec.textContent.indexOf(EN['instructions.readings.heading']) >= 0);
  const ref = byAttr(main, 'data-lane', 'reference')[0];
  const note = byAttr(ref, 'data-part', 'reading_c_note')[0];
  assert.ok(note, 'the note sits under the third reading');
  assert.strictEqual(note.textContent.trim(), EN['instructions.readings.no_reference_c']);
  assert.strictEqual(count(main.textContent, EN['instructions.readings.no_reference_c']), 1);
});

test('rung 2 always runs; its full prompt shows system and definitions', async () => {
  const { main } = await openInstructions();
  const box = all(main, (n) => n.getAttribute('id') === 'ss-ins-rung2')[0];
  assert.strictEqual(box.checked, true);
  assert.strictEqual(box.disabled, true);
  const r2 = byAttr(main, 'data-part', 'rung2')[0].textContent;
  for (const s of ['RUNG TWO TEXT', 'SYSTEM TEXT', 'DEFINITIONS TEXT']) assert.ok(r2.indexOf(s) >= 0, s);
});

test('rung 1 own prompt: the wrapper is shown below the text, and the text is saved', async () => {
  const { main, calls } = await openInstructions();
  const own = all(main, (n) => n.getAttribute('id') === 'ss-ins-rung1-mode-own')[0];
  own.checked = true;
  own.fire('change');
  await settle();
  const r1 = byAttr(main, 'data-part', 'rung1')[0];
  const ta = byTag(r1, 'textarea')[0];
  assert.ok(ta, 'textarea shown');
  const flat = all(r1, () => true);
  const wrapper = byAttr(r1, 'data-part', 'wrapper')[0];
  assert.ok(flat.indexOf(ta) < flat.indexOf(wrapper), 'wrapper below the text');
  assert.ok(wrapper.textContent.indexOf('WRAPPER TEXT') >= 0);
  ta.value = 'My own way of asking for a map.';
  ta.fire('input');
  button(r1, EN['instructions.rung1.save']).fire('click');
  await settle();
  const post = calls.filter((c) => c.method === 'POST' && c.url === '/api/instructions').pop();
  assert.strictEqual(post.body.rung1, 'own');
  assert.strictEqual(post.body.rung1_text, 'My own way of asking for a map.');
  assert.ok(main.textContent.indexOf('c'.repeat(64)) >= 0, 'saved sha shown');
});

test('rung 1: back to the basic prompt hides the own text and checks basic', async () => {
  const { main, calls } = await openInstructions();
  const pick = (id) => all(main, (n) => n.getAttribute('id') === id)[0];
  const own = pick('ss-ins-rung1-mode-own');
  own.checked = true;
  own.fire('change');
  await settle();
  assert.ok(byAttr(main, 'data-part', 'own_prompt')[0], 'own box shown');
  assert.strictEqual(pick('ss-ins-rung1-mode-own').checked, true, 'own radio follows the shown box');
  const basic = pick('ss-ins-rung1-mode-basic');
  basic.checked = true;
  basic.fire('change');
  await settle();
  const post = calls.filter((c) => c.method === 'POST' && c.url === '/api/instructions').pop();
  assert.strictEqual(post.body.rung1, 'basic');
  assert.strictEqual(byAttr(main, 'data-part', 'own_prompt').length, 0, 'own box gone');
  assert.strictEqual(pick('ss-ins-rung1-mode-basic').checked, true);
  assert.strictEqual(pick('ss-ins-rung1-mode-own').checked, false);
});

test('instructions: the estimate, Start per group (B waits for its codings) and progress', async () => {
  const env = await openInstructions();
  const { main, calls, intervals } = env;
  button(main, EN['instructions.estimate.run']).fire('click');
  await settle();
  const est = calls.filter((c) => c.url === '/api/estimate').pop();
  assert.deepStrictEqual(est.body, { rungs: ['rung2'] });
  assert.ok(main.textContent.indexOf('About 10 min to 1 h 30 min on this setup') >= 0, main.textContent.slice(0, 200));
  assert.ok(main.textContent.indexOf(EN['setup.estimate.calls'].replace('{q}', '40').replace('{j}', '90')) >= 0, 'the calls, as Setup showed them');
  const starts = all(main, (n) => cls(n, 'ss-ins-start'));
  assert.deepStrictEqual(starts.map((b) => b.getAttribute('data-group')), ['A', 'B', 'C']);
  assert.deepStrictEqual(starts.map((b) => b.disabled), [false, true, false]);
  starts[0].fire('click');
  await settle();
  const st = calls.filter((c) => c.url === '/api/runs/start').pop();
  assert.deepStrictEqual(st.body, { group: 'A', rungs: ['rung2'], preset: 'standard' });
  intervals.forEach((fn) => fn && fn());
  await settle();
  const stages = all(main, (n) => cls(n, 'ss-ins-stage'));
  assert.deepStrictEqual(stages.map((s) => s.getAttribute('data-status')), ['done', 'current', 'waiting']);
  assert.ok(stages[1].textContent.indexOf('3 of 10 calls') >= 0);
  assert.ok(byAttr(main, 'data-action', 'pause').length === 1);
  // the screen never asks for a result
  assert.deepStrictEqual(routesCalled(calls).filter((r) => /\/api\/(view|results)/.test(r)), []);
});

test('instructions: a run in progress when the screen opens is shown and polled (Task 39)', async () => {
  const current = { run_id: 'B-1a2b3c4d', group: 'B', state: 'running', stages: ['Q', 'J', 'M'], stage: 'Q', done: 2,
    total: 40, last_record: { record_type: 'call_response', stage: 'recon:removed', seq: 9 } };
  const env = await openInstructions({ current: current, job: Object.assign({}, current, { done: 5 }) });
  const { main, calls, intervals } = env;
  assert.ok(calls.some((c) => c.method === 'GET' && c.url === '/api/runs/status'), 'asked for the run in progress');
  assert.strictEqual(byClass(main, 'ss-ins-run')[0].textContent, 'B-1a2b3c4d');
  assert.ok(main.textContent.indexOf('2 of 40 calls') >= 0);
  assert.deepStrictEqual(all(main, (n) => cls(n, 'ss-ins-start')).map((b) => b.disabled), [true, true, true]);
  intervals.forEach((fn) => fn && fn());
  await settle();
  assert.ok(calls.some((c) => c.url === '/api/runs/status?run=B-1a2b3c4d'), 'polls the run by id');
  assert.ok(main.textContent.indexOf('5 of 40 calls') >= 0);
  // nothing in progress: the screen shows none and Start stays available
  const idle = await openInstructions();
  assert.ok(idle.main.textContent.indexOf(EN['instructions.progress.none']) >= 0);
  assert.strictEqual(idle.intervals.filter(Boolean).length, 0);
});

test('renderProgress: a failed run offers Resume only when it can go on (Task 39)', () => {
  const { SS } = load(server());
  const c = makeNode('div');
  SS.instructions.renderProgress(c, { run_id: 'A-0a1b2c3d', state: 'failed', stage: 'Q', resumable: true,
    message: 'the run ended before it finished. Press Resume to go on item by item.' });
  assert.deepStrictEqual(byTag(c, 'button').map((b) => b.getAttribute('data-action')), ['resume']);
  assert.ok(c.textContent.indexOf('Stopped: the run ended before it finished.') >= 0, c.textContent);
  SS.instructions.renderProgress(c, { run_id: 'A-0a1b2c3d', state: 'failed', stage: 'M', resumable: false, message: 'x' });
  assert.deepStrictEqual(byTag(c, 'button').map((b) => b.getAttribute('data-action')), []);
});

test('renderProgress: paused offers resume; needs_swap offers both switch choices; done links to Results', () => {
  const { SS } = load(server());
  const c = makeNode('div');
  SS.instructions.renderProgress(c, { run_id: 'A-0a1b2c3d', state: 'paused', stage: 'Q', done: 4, total: 9 });
  assert.deepStrictEqual(byTag(c, 'button').map((b) => b.getAttribute('data-action')), ['resume']);
  SS.instructions.renderProgress(c, { run_id: 'A-0a1b2c3d', state: 'needs_swap', stage: 'J', done: 0, total: 9 });
  assert.deepStrictEqual(byTag(c, 'button').map((b) => b.getAttribute('data-action')), ['swap_auto', 'swap_done']);
  SS.instructions.renderProgress(c, { run_id: 'A-0a1b2c3d', state: 'done', stages: ['Q', 'M'] });
  assert.deepStrictEqual(all(c, (n) => cls(n, 'ss-ins-stage')).map((s) => s.getAttribute('data-stage') + ':' + s.getAttribute('data-status')), ['Q:done', 'M:done']);
  assert.strictEqual(byTag(c, 'a')[0].getAttribute('href'), '#results/A-0a1b2c3d');
  assert.strictEqual(SS.instructions.fmtDuration(30), 'under a minute');
  assert.strictEqual(SS.instructions.fmtDuration(undefined), EN['common.not_computed']);
});

// --- Results -----------------------------------------------------------------------------------------------------

test('results: never asks for a view or a profile on load', async () => {
  const { calls, main, errors } = await openResults();
  assert.deepStrictEqual(errors, []);
  assert.deepStrictEqual(routesCalled(calls), ['GET /api/results/gate', 'GET /api/results/runs']);
  assert.ok(main.textContent.indexOf(EN['results.gate.before_open']) >= 0);
  assert.strictEqual(byAttr(main, 'data-section').length, 0, 'no viewer yet');
});

test('results: a run in the hash picks its tab and still waits for the button', async () => {
  const runs = [{ run_id: 'A-0a1b2c3d', group: 'A', ready: true }, { run_id: 'C-1b2c3d4e', group: 'C', ready: true }];
  const { calls, main } = await openResults({ runs }, 'C-1b2c3d4e&view=lanes');
  assert.deepStrictEqual(routesCalled(calls), ['GET /api/results/gate', 'GET /api/results/runs']);
  const tabs = all(main, (n) => cls(n, 'ss-res-tab'));
  assert.deepStrictEqual(tabs.map((t) => t.getAttribute('data-group') + ':' + t.getAttribute('aria-selected')), ['A:false', 'C:true']);
  assert.deepStrictEqual(all(main, (n) => cls(n, 'ss-res-run')).map((r) => r.getAttribute('data-run')), ['C-1b2c3d4e']);
  tabs[0].fire('click');
  assert.deepStrictEqual(all(main, (n) => cls(n, 'ss-res-run')).map((r) => r.getAttribute('data-run')), ['A-0a1b2c3d']);
});

test('results (item 40): the button opens the maps, then plain sentences, then the absent steps, then a closed Technical details', async () => {
  // Part 5a (scout F9; the author's items 9 to 11): the open page is the plain lead and the maps; every profile panel,
  // interval and ledger moves into one closed <details>; the "yield per box" lines are left out.
  // Item 40 (the author, after a real run): the maps come first, with the "point at a box" line and the instructions
  // selector, then the plain sentences, then the absent-steps table, then Technical details. The order is pinned.
  const env = await openResults();
  const { calls, main, errors, win } = env;
  button(main, EN['results.run.show']).fire('click');
  await settle();
  assert.deepStrictEqual(errors, []);
  assert.deepStrictEqual(routesCalled(calls), ['GET /api/results/gate', 'GET /api/results/runs',
    'GET /api/results/profile', 'GET /api/view']);
  assert.ok(calls.some((c) => c.url === '/api/view?run=A-0a1b2c3d'));
  const body = byAttr(main, 'data-part', 'results')[0];
  const heading = byClass(body, 'ss-res-heading')[0];
  assert.strictEqual(heading.textContent, 'What this run shows');
  const flat = all(body, () => true);
  const lead = byAttr(body, 'data-part', 'lead')[0];
  const views = byAttr(body, 'data-section', 'views')[0];
  const details = byAttr(body, 'data-part', 'technical')[0];
  const hint = byClass(body, 'ss-res-next')[0];
  const selector = byClass(views, 'ss-viewer-selector')[0];
  const ledger = byAttr(body, 'data-section', 'ledger')[0];
  assert.ok(lead && views && details && hint && selector && ledger, 'hint, lanes, selector, lead, ledger and details drawn');
  assert.strictEqual(hint.textContent, EN['results.lead.next']);
  const at = (n) => flat.indexOf(n);
  assert.ok(at(heading) < at(hint) && at(hint) < at(views) && at(views) < at(selector) && at(selector) < at(lead)
    && at(lead) < at(ledger) && at(ledger) < at(details),
    'heading, the point-at-a-box line, the lanes with the instructions selector, the plain sentences, the absent steps, then Technical details');
  // the lead: what listening added in points with both percentages, the share left out, size, flattened ideals
  const said = byAttr(lead, 'data-lead').map((p) => p.getAttribute('data-lead'));
  assert.deepStrictEqual(said, ['added', 'omitted', 'size', 'flattened']);
  const t = lead.textContent;
  assert.ok(t.indexOf('added 58.7 percentage points') >= 0 && t.indexOf('recovered 90.9%') >= 0 && t.indexOf('recovered 32.2%') >= 0, t);
  assert.ok(t.indexOf('left out 9.1%') >= 0, t);
  assert.ok(t.indexOf(EN['results.lead.size.not_run']) >= 0, 'no size cell in the fixture: said in words');
  assert.ok(t.indexOf(EN['results.lead.flattened.none'].replace('{ideals}', '7')) >= 0, t);
  assert.ok(!/(^|[^\d])\.\d{3}/.test(t), 'no ".587" style proportion in the open page');
  // the whole profile sits in the closed details, without the yield-per-box group; nothing of it above
  assert.strictEqual(details.tagName, 'DETAILS');
  assert.strictEqual(details.getAttribute('open'), null, 'closed by default');
  assert.strictEqual(byTag(details, 'summary')[0].textContent, EN['results.details.summary']);
  const prof = byAttr(details, 'data-section', 'profile')[0];
  assert.ok(prof && byClass(prof, 'pf-lead')[0], 'the profile, lead block included, is inside the details');
  assert.strictEqual(byAttr(body, 'data-section', 'profile').length, 1, 'drawn once');
  assert.strictEqual(byAttr(body, 'data-group', 'lead-derived').length, 0, 'no yield per box lines');
  assert.strictEqual(body.textContent.indexOf(EN['profile.lead.group.derived']), -1);
  assert.ok(details.textContent.indexOf('A-0a1b2c3d') >= 0, 'the run id is in the details');
  assert.strictEqual(count(body.textContent, EN['try.profile.full']), 0, 'one details only');
  // saving for comparison is named after the results, never before them
  const sec = byAttr(body, 'data-part', 'secondary')[0];
  assert.ok(flat.indexOf(sec) > flat.indexOf(views));
  assert.ok(sec.textContent.indexOf('Profile for comparison') >= 0);
  // no copy of this screen's own says pool / pooling (the profile's statistical qualifier "a pooled point" is the
  // renderer's, plan V3); and nothing before the viewer names other labs
  Object.keys(EN).filter((k) => /^(results|instructions)\./.test(k)).forEach((k) => {
    assert.ok(!/\bpool/i.test(EN[k]) && !/\blabs?\b|network/i.test(EN[k]), k);
  });
  assert.ok(win.location.hash.indexOf('#results/A-0a1b2c3d') === 0, win.location.hash);
  // a later viewer state change keeps the screen in the hash
  env.SS.state.set({ view: 'columns' });
  assert.ok(win.location.hash.indexOf('#results/A-0a1b2c3d&') === 0, win.location.hash);
  assert.ok(win.location.hash.indexOf('view=columns') > 0, win.location.hash);
});

test('results: the lead reads the size cell, flattened ideals with quotes, and mode B in its own words', async () => {
  const lead = (doc, o) => { const env = load(server()); return clone(env.SS.profile.lead(doc, o)).map((f) => f.text); };
  const doc = clone(PROFILE);
  doc.rows.push({ row: 2, cells: [{ name: 'ai_per_endorsed', value: 0.627 }] });
  doc.rows.forEach((r) => { if (r.row === 5) r.cells.forEach((c) => { if (c.x === 'primary' && c.name === 'flattened') c.value = 2; if (c.x === 'primary' && c.name === 'counterfactual') c.value = 0; }); });
  const a = lead(doc, { referenceType: 'A' });
  assert.strictEqual(a[2], 'The AI maps hold about 63% as many boxes as the endorsed maps.');
  assert.strictEqual(a[3], 'Of the 7 services that participants marked as wished for, the AI drew 2 as received, which the study calls a flattened ideal. In none of these cases did the AI\'s own quote say that the service never came.');
  const b = lead(doc, { referenceType: 'B' });
  assert.ok(b[0].indexOf("this researcher's steps") >= 0 && b[2].indexOf("this researcher's maps") >= 0, b.join(' | '));
  assert.ok(b.every((x) => x.indexOf('endorsed') < 0), 'mode B never says endorsed');
  // a negative difference is said the other way round, still in points
  doc.rows.forEach((r) => { if (r.row === 'lead') r.cells.forEach((c) => { if (c.name === 'listening_added' && c.x === 'primary') c.value = -0.031; }); });
  assert.ok(lead(doc, {})[0].indexOf('more of the endorsed steps than the AI map did, by 3.1 percentage points') >= 0);
  // a cell that was not run reads as a plain sentence, never "not computed" or a blank
  const bare = { rows: [] };
  assert.deepStrictEqual(lead(bare, {}), [EN['results.lead.added.not_run'], EN['results.lead.omitted.not_run'],
    EN['results.lead.size.not_run'], EN['results.lead.flattened.not_run']]);
  // no judge, or no reference map: one plain sentence instead
  assert.deepStrictEqual(lead(doc, { judgeless: true }), [EN['results.lead.no_judge']]);
  assert.deepStrictEqual(lead(doc, { referenceType: 'C' }), [EN['results.lead.no_reference']]);
});

test('results: a run with no judge opens with the plain no-judge sentence above the AI maps', async () => {
  const env = load(server({}));
  const view = clone(VIEW);
  delete view.run.models.judge;
  env.SS.results.render(env.main, '');
  await settle();
  // answer the view without a judge
  env.win.fetch = ((orig) => (url, init) => {
    if (String(url).indexOf('/api/view') === 0) return Promise.resolve(reply(200, { run_id: 'A-0a1b2c3d', group: 'A', view }));
    return orig(url, init);
  })(env.win.fetch);
  button(env.main, EN['results.run.show']).fire('click');
  await settle();
  const lead = byAttr(env.main, 'data-part', 'lead')[0];
  assert.ok(lead, 'lead drawn');
  assert.strictEqual(lead.textContent, EN['results.lead.no_judge']);
});

test('results: the runs list puts the newest run first and says date, instructions, people and models', async () => {
  const runs = [
    { run_id: 'A-0a1b2c3d', group: 'A', finished: true, ready: true, state: 'finished', started: '2026-10-20T10:00:00+00:00',
      rungs: ['rung2'], rung1_own: false, n_people: 11, models: { recon: 'qwen-mock:7b', judge: 'gemma-mock:9b' } },
    { run_id: 'A-1b2c3d4e', group: 'A', finished: true, ready: true, state: 'finished', started: '2026-10-23T19:27:58+00:00',
      rungs: ['rung1', 'rung2', 'placebo', 'rung3'], rung1_own: true, n_people: 1, models: { recon: 'qwen-mock:7b' } },
    { run_id: 'B-2b2c3d4e', group: 'B', finished: true, ready: true, state: 'finished', started: '2026-10-22T08:00:00+00:00',
      rungs: ['rung2'], n_people: 12, models: { recon: 'qwen-mock:7b', judge: 'gemma-mock:9b' } },
  ];
  const { main, calls } = await openResults({ runs });
  // the tab of the newest run is the one shown, and the newest run is its first row
  const tabs = all(main, (n) => cls(n, 'ss-res-tab'));
  assert.deepStrictEqual(tabs.map((t) => t.getAttribute('data-group') + ':' + t.getAttribute('aria-selected')), ['A:true', 'B:false']);
  const rows = all(main, (n) => cls(n, 'ss-res-run'));
  assert.deepStrictEqual(rows.map((r) => r.getAttribute('data-run')), ['A-1b2c3d4e', 'A-0a1b2c3d']);
  const d = new Date('2026-10-23T19:27:58+00:00');
  const two = (n) => (n < 10 ? '0' : '') + n;
  const date = d.getDate() + ' ' + ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'][d.getMonth()] + ' ' + d.getFullYear() + ', ' + two(d.getHours()) + ':' + two(d.getMinutes());
  const facts = byAttr(rows[0], 'data-part', 'facts')[0].textContent;
  assert.strictEqual(facts, EN['results.run.newest'] + ' ' + [date, [EN['results.run.instructions_own'], EN['ledger.rung.rung2'], EN['ledger.rung.rung3'], EN['ledger.rung.placebo']].join('; '),
    EN['results.run.people_one'], 'qwen-mock:7b, with no judge'].join(' · '));
  assert.ok(byAttr(rows[1], 'data-part', 'facts')[0].textContent.indexOf("The study's instructions (rung 2) · 11 people · qwen-mock:7b and gemma-mock:9b") >= 0);
  // the deliberate click guard stays: nothing AI-derived is asked for until a button is pressed
  assert.deepStrictEqual(routesCalled(calls), ['GET /api/results/gate', 'GET /api/results/runs']);
  assert.strictEqual(byAttr(main, 'data-section').length, 0);
});

test('R12: a refused profile says the profile was not made, in plain sentences, and the lanes still come', async () => {
  const dev = ['n_analysed 4 is below the k floor of 10 people: nothing is exported', 'forbidden key $.rows[0].pid'];
  const refusals = [
    { code: 'k_floor', message: 'Too few participants were analysed to share a profile: 4, and at least 10 are needed.' },
    { code: 'privacy_check', message: EN['results.refused.reason.privacy_check'] },
  ];
  const { main } = await openResults({ refused: dev, refusals });
  button(main, EN['results.run.show']).fire('click');
  await settle();
  const body = byAttr(main, 'data-part', 'results')[0];
  // the heading no longer promises the table
  const heading = byClass(body, 'ss-res-heading')[0];
  assert.strictEqual(heading.textContent, EN['results.heading.refused']);
  assert.strictEqual(EN['results.heading.refused'], 'The profile was not made for this run');
  assert.ok(body.textContent.indexOf(EN['results.heading']) < 0, 'no promise of the table');
  // plain sentences, not mono developer text
  const box = byAttr(main, 'data-part', 'refused')[0];
  const lis = byTag(box, 'li');
  assert.deepStrictEqual(lis.map((li) => li.textContent), refusals.map((r) => r.message));
  lis.forEach((li) => assert.ok(!cls(li, 'mono'), 'not set in mono'));
  const t = body.textContent;
  for (const bad of ['n_analysed', 'k floor', 'forbidden key', 'exporter refused']) assert.strictEqual(t.indexOf(bad), -1, bad);
  // the footer offers no profile to save (nothing can be saved), but still points to the report
  const sec = byAttr(body, 'data-part', 'secondary')[0];
  assert.strictEqual(sec.textContent.indexOf('Profile for comparison'), -1);
  assert.ok(sec.textContent.indexOf(EN['results.secondary.refused']) >= 0);
  const sections = byAttr(main, 'data-section').map((n) => n.getAttribute('data-section'));
  assert.ok(sections.indexOf('views') >= 0 && sections.indexOf('profile') < 0, sections.join(','));
});

test('R12: an older answer with only the verbatim reasons still shows plain words, never the developer text', async () => {
  const { main } = await openResults({ refused: ['n_analysed 4 is below the k floor of 10 people: nothing is exported'] });
  button(main, EN['results.run.show']).fire('click');
  await settle();
  const box = byAttr(main, 'data-part', 'refused')[0];
  assert.deepStrictEqual(byTag(box, 'li').map((li) => li.textContent), [EN['results.refused.reason.other']]);
  assert.strictEqual(main.textContent.indexOf('n_analysed'), -1);
});

test('R12: an opened run that was not refused keeps the table heading and the save-a-profile footer', async () => {
  const { main } = await openResults();
  button(main, EN['results.run.show']).fire('click');
  await settle();
  const body = byAttr(main, 'data-part', 'results')[0];
  assert.strictEqual(byClass(body, 'ss-res-heading')[0].textContent, EN['results.heading']);
  assert.ok(byAttr(body, 'data-part', 'secondary')[0].textContent.indexOf(EN['results.secondary']) >= 0);
  assert.strictEqual(byAttr(main, 'data-part', 'refused').length, 0);
});

test('R14: a stopped run says it stopped, with no button; a run still going says not ready', async () => {
  const runs = [
    { run_id: 'A-0a1b2c3d', group: 'A', finished: true, ready: true, state: 'finished' },
    { run_id: 'A-1b2c3d4e', group: 'A', finished: false, ready: false, state: 'stopped' },
    { run_id: 'A-2b2c3d4e', group: 'A', finished: false, ready: false, state: 'running' },
  ];
  const { main } = await openResults({ runs });
  const row = (id) => all(main, (n) => cls(n, 'ss-res-run') && n.getAttribute('data-run') === id)[0];
  assert.strictEqual(EN['results.run.stopped'], "This run stopped before it finished, so it has no results. Its progress and log are on the Run screen.");
  assert.ok(row('A-1b2c3d4e').textContent.indexOf(EN['results.run.stopped']) >= 0);
  assert.strictEqual(row('A-1b2c3d4e').textContent.indexOf(EN['results.run.not_ready']), -1, 'never told to wait');
  assert.strictEqual(all(row('A-1b2c3d4e'), (n) => n.tagName === 'BUTTON').length, 0, 'no results button');
  assert.ok(row('A-2b2c3d4e').textContent.indexOf(EN['results.run.not_ready']) >= 0);
  assert.strictEqual(all(row('A-2b2c3d4e'), (n) => n.tagName === 'BUTTON').length, 0);
  assert.strictEqual(all(row('A-0a1b2c3d'), (n) => n.tagName === 'BUTTON').length, 1);
  // an older answer with no state keeps the wait wording
  const old = await openResults({ runs: [{ run_id: 'A-3b2c3d4e', group: 'A', finished: false, ready: false }] });
  assert.ok(old.main.textContent.indexOf(EN['results.run.not_ready']) >= 0);
});

test('results: while a B coding is open the button is off and the open codings are named', async () => {
  const { main, calls } = await openResults({ gate: { open: false, waiting: ['MAP-B'], revealed: false } });
  const b = button(main, EN['results.run.show']);
  assert.strictEqual(b.disabled, true);
  assert.ok(main.textContent.indexOf(EN['results.gate.closed'].replace('{pids}', 'MAP-B')) >= 0);
  assert.deepStrictEqual(routesCalled(calls), ['GET /api/results/gate', 'GET /api/results/runs']);
});

test('results: leaving the screen unmounts the viewer before its own hash listener runs', async () => {
  const env = await openResults();
  button(env.main, EN['results.run.show']).fire('click');
  await settle();
  const before = (env.winListeners.hashchange || []).length;
  assert.ok(before >= 2, 'ours and the viewer\'s');
  env.win.location.hash = '#instructions';
  (env.winListeners.hashchange || []).slice().forEach((fn) => {
    if ((env.winListeners.hashchange || []).indexOf(fn) >= 0) fn();
  });
  assert.strictEqual((env.winListeners.hashchange || []).length, before - 2);
  assert.strictEqual(env.win.location.hash, '#instructions');
});

// --- review R8: the last run of each group is shown beside its Start button ---------------------------------------
test('R8: each group row shows its last run, its state and a link to Results', async () => {
  const last = [{ group: 'A', run_id: 'A-49dfffa2', finished: true }, { group: 'C', run_id: 'C-0a1b2c3d', finished: false }];
  const env = await openInstructions({ last });
  const { main, calls } = env;
  assert.ok(calls.some((c) => c.method === 'GET' && c.url === '/api/runs/last'), 'asked for the last runs');
  const rowA = byAttr(main, 'data-group', 'A').filter((n) => cls(n, 'ss-ins-start-row'))[0];
  const rowB = byAttr(main, 'data-group', 'B').filter((n) => cls(n, 'ss-ins-start-row'))[0];
  const rowC = byAttr(main, 'data-group', 'C').filter((n) => cls(n, 'ss-ins-start-row'))[0];
  const a = byClass(rowA, 'ss-ins-last')[0];
  assert.ok(a.textContent.indexOf(EN['instructions.last.finished'].replace('{run_id}', 'A-49dfffa2')) >= 0, a.textContent);
  const link = byTag(a, 'a')[0];
  assert.strictEqual(link.textContent, EN['instructions.last.show']);
  assert.strictEqual(link.getAttribute('href'), '#results/A-49dfffa2');
  assert.ok(rowA.textContent.indexOf(EN['instructions.last.again']) >= 0, 'says Start makes a new run');
  assert.strictEqual(byClass(rowB, 'ss-ins-last').length, 0, 'no run for B: nothing said');
  const c = byClass(rowC, 'ss-ins-last')[0];
  assert.ok(c.textContent.indexOf(EN['instructions.last.stopped'].replace('{run_id}', 'C-0a1b2c3d')) >= 0, c.textContent);
  assert.strictEqual(byTag(c, 'a').length, 0, 'a run that did not finish has no results to open');
  assert.deepStrictEqual(routesCalled(calls).filter((r) => /\/api\/(view|results)/.test(r)), [], 'still no result asked for');
});

test('R8: empty Progress says no run is in progress; a failing listing is not shown', async () => {
  assert.strictEqual(EN['instructions.progress.none'], 'No run is in progress.');
  const env = await openInstructions({ lastFails: true });
  assert.ok(env.main.textContent.indexOf(EN['instructions.progress.none']) >= 0);
  assert.strictEqual(byClass(env.main, 'ss-ins-last').length, 0);
  assert.deepStrictEqual(all(env.main, (n) => cls(n, 'ss-ins-start')).map((b) => b.disabled), [false, true, false]);
  assert.strictEqual(byClass(env.main, 'ss-setup-error')[0].textContent, '', 'no message from a failed listing');
});

test('R8: a run in progress is not also listed as the last run of its group', async () => {
  const current = { run_id: 'A-49dfffa2', group: 'A', state: 'running', stages: ['Q', 'J', 'M'], stage: 'Q', done: 2, total: 40 };
  const env = await openInstructions({ current, last: [{ group: 'A', run_id: 'A-49dfffa2', finished: false }] });
  assert.strictEqual(byClass(env.main, 'ss-ins-last').length, 0);
});

// --- Part 5b: the Run screen (scout F5 to F8, F14, F16; item 39) -----------------------------------------------------
const READY_ITEMS = [
  { key: 'context', ok: true }, { key: 'pairing', ok: true }, { key: 'labels', ok: false, done: 0, total: 12 },
  { key: 'maps', ok: true, done: 12, total: 12 }, { key: 'references', ok: true, done: 12, total: 12 },
  { key: 'models', ok: true }, { key: 'model_check', ok: true, state: 'passed' },
];
const fillT = (str, kw) => str.replace(/\{(\w+)\}/g, (m, k) => (k in kw ? String(kw[k]) : m));

test('Run: the instructions come first, then Start (readiness, the estimate, one line on A and B), then progress, then the readings', async () => {
  const { main } = await openInstructions();
  const parts = byClass(main, 'ss-ins-section').map((n) => n.getAttribute('data-part'));
  assert.deepStrictEqual(parts, ['rung1', 'rung2', 'rung3', 'preset', 'start', 'progress', 'readings']);
  const start = byAttr(main, 'data-part', 'start')[0];
  const inStart = all(start, (n) => ['ready', 'estimate'].indexOf(n.getAttribute('data-part')) >= 0).map((n) => n.getAttribute('data-part'));
  assert.deepStrictEqual(inStart, ['ready', 'estimate']);
  assert.ok(start.textContent.indexOf(EN['instructions.start.groups_note']) >= 0);
  assert.ok(start.textContent.indexOf(fillT(EN['instructions.start.rungs'], { rungs: EN['methods.rung.rung2'] })) >= 0, start.textContent);
  // item 19 names on the headings; the remedy notes and the readings sit in closed details
  assert.strictEqual(byAttr(main, 'data-part', 'rung1')[0].childNodes[0].textContent, "The authors' original draft (rung 1)");
  assert.strictEqual(byAttr(main, 'data-part', 'rung2')[0].childNodes[0].textContent, "The study's instructions (rung 2)");
  for (const part of ['remedy_notes', 'readings_more']) {
    const d = byAttr(main, 'data-part', part)[0];
    assert.strictEqual(d.tagName, 'DETAILS', part);
    assert.strictEqual(d.getAttribute('open'), null, part + ' is closed');
  }
  assert.ok(byAttr(main, 'data-part', 'remedy_notes')[0].textContent.indexOf(EN['instructions.rung3.null_text']) >= 0);
  // only one estimate on the page, and the hashes wrap on a phone (F16)
  assert.strictEqual(byTag(main, 'button').filter((b) => b.textContent === EN['instructions.estimate.run']).length, 1);
  const css = fs.readFileSync(path.join(ROOT, 'second_scribe', 'app', 'static', 'css', 'app.css'), 'utf8');
  assert.ok(/\.ss-ins code, \.ss-ins \.mono \{ overflow-wrap: anywhere; \}/.test(css));
  assert.ok(byClass(main, 'ss-ins-hash').length === 1);
});

test('Run: the readiness list gives counts and links; a group that cannot start says why beside its Start (item 39)', async () => {
  const ready = { items: READY_ITEMS, groups: {
    A: { ready: false, code: 'labels_unconfirmed', item: 'labels', message: 'Confirm the speaker labels of MAP-A in Setup before you run this group.' },
    B: { ready: false, code: 'coding_unlocked', item: 'coding', message: 'Finish and lock your reading of MAP-B before you run reference B.' },
    C: { ready: true } } };
  const { main, calls } = await openInstructions({ ready });
  const items = byClass(main, 'ss-ins-ready-item');
  assert.deepStrictEqual(items.map((i) => i.getAttribute('data-key')), READY_ITEMS.map((i) => i.key));
  const labels = items[2];
  assert.strictEqual(labels.getAttribute('data-ok'), 'false');
  assert.ok(labels.textContent.indexOf('Speaker labels: 0 of 12 confirmed') >= 0, labels.textContent);
  assert.strictEqual(byTag(labels, 'a')[0].getAttribute('href'), '#setup/labels');
  assert.strictEqual(byTag(labels, 'a')[0].textContent, fillT(EN['instructions.start.go_step'], { step: EN['setup.labels.title'] }));
  assert.strictEqual(byTag(items[3], 'a').length, 0, 'a ready line has no link');
  const starts = all(main, (n) => cls(n, 'ss-ins-start'));
  assert.deepStrictEqual(starts.map((b) => b.disabled), [true, true, false]);
  const reasonA = byClass(main, 'ss-ins-reason').filter((n) => n.getAttribute('data-group') === 'A')[0];
  assert.strictEqual(reasonA.getAttribute('role'), 'alert');
  assert.ok(reasonA.textContent.indexOf(EN['instructions.start.blocked'] + ' Speaker labels: 0 of 12 confirmed.') >= 0, reasonA.textContent);
  assert.strictEqual(byTag(reasonA, 'a')[0].getAttribute('href'), '#setup/labels');
  const reasonB = byClass(main, 'ss-ins-reason').filter((n) => n.getAttribute('data-group') === 'B')[0];
  assert.ok(reasonB.textContent.indexOf('Finish and lock your reading of MAP-B') >= 0, 'no count known: the refusal itself');
  assert.strictEqual(byTag(reasonB, 'a')[0].getAttribute('href'), '#coding');
  assert.ok(calls.some((c) => c.url === '/api/runs/ready'));
});

test('Run: a refused Start says why beside that Start, never only at the top, and reads the list again (item 39)', async () => {
  let n = 0;
  const state = { startRefused: { error: 'run_busy', message: EN['runs.error.run_busy'] },
    ready: () => { n += 1; return { items: READY_ITEMS.map((i) => Object.assign({}, i, { ok: true })), groups: { A: { ready: true }, B: { ready: true }, C: { ready: true } } }; } };
  const { main, calls } = await openInstructions(state);
  all(main, (x) => cls(x, 'ss-ins-start'))[0].fire('click');
  await settle();
  const reason = byClass(main, 'ss-ins-reason').filter((x) => x.getAttribute('data-group') === 'A')[0];
  assert.ok(reason && reason.textContent.indexOf(EN['runs.error.run_busy']) >= 0, 'the message sits in the A row');
  assert.strictEqual(byClass(main, 'ss-setup-error')[0].textContent, '', 'not at the top of the page');
  assert.strictEqual(n, 2, 'the readiness list was read again');
  assert.strictEqual(all(main, (x) => cls(x, 'ss-ins-start'))[0].disabled, false, 'it can be pressed again');
  assert.ok(calls.some((c) => c.url === '/api/runs/start'));
});

test('Run progress: plain stage names, the time left, the server warning, items left out and the next step', () => {
  const { SS } = load(server());
  const c = makeNode('div');
  SS.instructions.renderProgress(c, { run_id: 'A-0a1b2c3d', state: 'running', stages: ['Q', 'J', 'M'], stage: 'Q', done: 33,
    total: 56, left_s: 1500, server: { code: 'http_503', seconds: 40, errors: 3 } });
  const t = c.textContent;
  assert.ok(!/\b[QJM] ·/.test(t), 'no letter codes');
  assert.ok(t.indexOf(EN['instructions.progress.stage.q']) >= 0);
  assert.ok(t.indexOf(fillT(EN['instructions.progress.left'], { time: '25 min' })) >= 0, t);
  SS.instructions.renderProgress(c, { run_id: 'A-0a1b2c3d', state: 'running', stage: 'J', done: 40, total: 48, left_s: 30 });
  assert.ok(c.textContent.indexOf(EN['instructions.progress.left_minute']) >= 0, 'the last minute reads plainly');
  assert.ok(c.textContent.indexOf('About under') < 0);
  SS.instructions.renderProgress(c, { run_id: 'A-0a1b2c3d', state: 'running', stages: ['Q', 'J', 'M'], stage: 'Q', done: 33,
    total: 56, left_s: 1500, server: { code: 'http_503', seconds: 40, errors: 3 } });
  const warn = byClass(c, 'ss-ins-server')[0];
  assert.strictEqual(warn.getAttribute('role'), 'alert');
  assert.strictEqual(warn.textContent, fillT(EN['jobs.server.http'], { status: '503', time: 'under a minute' }));
  SS.instructions.renderProgress(c, { run_id: 'A-0a1b2c3d', state: 'paused', stage: 'Q', done: 33, total: 56, left_s: 9,
    server: { code: 'timeout', seconds: 9 } });
  assert.strictEqual(byClass(c, 'ss-ins-server').length, 0, 'only while it runs');
  assert.ok(c.textContent.indexOf(fillT(EN['instructions.progress.left'], { time: 'under a minute' })) < 0);
  SS.instructions.renderProgress(c, { run_id: 'A-0a1b2c3d', state: 'done', stages: ['Q', 'J', 'M'], left_out: 1 });
  const line = byClass(c, 'ss-ins-state')[0];
  assert.strictEqual(line.textContent, fillT(EN['instructions.progress.state.done_left_out_one'], { n: 1 }));
  assert.strictEqual(line.getAttribute('data-left-out'), '1');
  assert.ok(c.textContent.indexOf(EN['instructions.progress.next']) >= 0);
  SS.instructions.renderProgress(c, { run_id: 'A-0a1b2c3d', state: 'done', stages: ['Q', 'J', 'M'], left_out: 3 });
  assert.strictEqual(byClass(c, 'ss-ins-state')[0].textContent, fillT(EN['instructions.progress.state.done_left_out'], { n: 3 }));
  SS.instructions.renderProgress(c, { run_id: 'A-0a1b2c3d', state: 'done', stages: ['Q', 'J', 'M'], left_out: 0 });
  assert.strictEqual(byClass(c, 'ss-ins-state')[0].textContent, EN['instructions.progress.state.done']);
});

test('Run: while a run is open, Start says why it is off', async () => {
  const current = { run_id: 'A-49dfffa2', group: 'A', state: 'running', stages: ['Q', 'J', 'M'], stage: 'Q', done: 2, total: 40 };
  const env = await openInstructions({ current });
  assert.ok(byClass(env.main, 'ss-ins-busy')[0].textContent === EN['instructions.start.busy']);
});


test('Resume on a stopped run starts watching the run again (the page never freezes on "Running")', async () => {
  const stopped = { run_id: 'A-0a1b2c3d', group: 'A', state: 'failed', resumable: true, stages: ['Q', 'J', 'M'], stage: 'Q',
    done: 10, total: 40, message: 'the run ended before it finished.' };
  const state = { current: stopped, job: { run_id: 'A-0a1b2c3d', state: 'running', stage: 'Q', done: 12, total: 40 } };
  const env = await openInstructions(state);
  const { main, intervals } = env;
  assert.strictEqual(intervals.filter(Boolean).length, 0, 'a stopped run is not polled');
  byAttr(main, 'data-action', 'resume')[0].fire('click');
  await settle();
  assert.strictEqual(intervals.filter(Boolean).length, 1, 'polling again after Resume');
  state.job = { run_id: 'A-0a1b2c3d', state: 'done', stages: ['Q', 'J', 'M'], stage: 'M', done: 1, total: 1 };
  intervals.forEach((fn) => fn && fn());
  await settle();
  assert.ok(byClass(main, 'ss-ins-next').length === 1, 'the finish shows without a reload');
  assert.strictEqual(intervals.filter(Boolean).length, 0, 'and the polling stops');
});
