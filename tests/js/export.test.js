'use strict';
// Task 42: the Export screen (app-export.js) and the report page's script (report.js).
// V13: the report is the main action, at the top; V14: the profile export is "Profile for comparison", and no text of
// this screen says "pool". The scripts load in the server's order into a vm context with a stub DOM and a fake fetch.
const test = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ROOT = path.resolve(__dirname, '..', '..');
const JS = path.join(ROOT, 'second_scribe', 'app', 'static', 'js');
const STRINGS = JSON.parse(fs.readFileSync(path.join(ROOT, 'second_scribe', 'app', 'strings', 'en.json'), 'utf8'));
const RUN = 'A-0a1b2c3d';

function makeNode(tag) {
  const node = {
    tagName: String(tag).toUpperCase(), children: [], attributes: {}, listeners: {}, textContent: '',
    disabled: false, value: '', checked: false,
    get firstChild() { return node.children[0] || null; },
    setAttribute(k, v) { node.attributes[k] = String(v); },
    getAttribute(k) { return Object.prototype.hasOwnProperty.call(node.attributes, k) ? node.attributes[k] : null; },
    appendChild(c) { node.children.push(c); return c; },
    removeChild(c) { node.children.splice(node.children.indexOf(c), 1); return c; },
    addEventListener(type, fn) { (node.listeners[type] = node.listeners[type] || []).push(fn); },
    fire(type) { return (node.listeners[type] || []).map((fn) => fn({ preventDefault() {} })); },
  };
  return node;
}

function all(node, pred, out) {
  out = out || [];
  if (pred(node)) out.push(node);
  node.children.forEach((c) => all(c, pred, out));
  return out;
}
const text = (node) => [node.textContent].concat(node.children.map(text)).join(' ');
const byClass = (node, cls) => all(node, (n) => (n.attributes.class || '').split(' ').indexOf(cls) >= 0);
const byTag = (node, tag) => all(node, (n) => n.tagName === tag.toUpperCase());

function reply(status, doc) {
  return { ok: status >= 200 && status < 300, status, text: () => Promise.resolve(JSON.stringify(doc)) };
}

function load(answer) {
  const calls = [];
  const errors = [];
  const sandbox = {
    console: { error: (...a) => errors.push(a.map(String).join(' ')), log() {}, warn() {} },
    location: { hash: '', search: '' },
    SS_STRINGS: STRINGS,
    document: { createElement: makeNode, addEventListener() {} },
    fetch: (url, init) => {
      calls.push({ url, method: init.method, body: init.body ? JSON.parse(init.body) : undefined });
      if (url === '/api/session') return Promise.resolve(reply(200, { csrf: 'tok' }));
      return Promise.resolve(answer(url, init, calls));
    },
  };
  sandbox.window = sandbox;
  const ctx = vm.createContext(sandbox);
  for (const f of ['core.js', 'api.js', 'app-shell.js', 'app-place.js', 'app-export.js']) {
    vm.runInContext(fs.readFileSync(path.join(JS, f), 'utf8'), ctx, { filename: f });
  }
  return { SS: sandbox.SS, calls, errors };
}

const tick = () => new Promise((r) => setTimeout(r, 0));
async function settle() { for (let i = 0; i < 10; i++) await tick(); }

const RUNS = { runs: [{ run_id: RUN, group: 'A', finished: true }], k_min: 10, gate: { open: true, waiting: 0 } };
const OK_PREVIEW = {
  run_id: RUN, k: 10, n_people: 12, state: 'pending', exportable: true, sha256: 'c'.repeat(64),
  preview: ['row,name,x,value,ci_lo,ci_hi,ci_level,n,unit,state', 'lead,ai_recall,primary,0.5,,,,12,people,'],
  refusals: [], warnings: [], declaration: [], json_text: '{}',
};

function answerWith(map) {
  return (url, init) => {
    const key = (init.method || 'GET') + ' ' + url.split('?')[0];
    const v = map[key];
    if (typeof v === 'function') return v(url, init);
    if (v) return reply(200, v);
    return reply(404, { error: 'not_found', message: 'no' });
  };
}

async function screen(map) {
  const env = load(answerWith(Object.assign({ 'GET /api/export/runs': RUNS,
    'GET /api/export/list': { run_id: RUN, outputs: [] } }, map || {})));
  const main = makeNode('main');
  env.SS.exportScreen.render(main, '');
  await settle();
  return Object.assign(env, { main });
}

test('registers the export screen at order 60', () => {
  const { SS, errors } = load(() => reply(200, {}));
  assert.deepStrictEqual(errors, []);
  assert.ok(SS.shell.list().indexOf('export') >= 0);
});

test('the report comes before the profile for comparison, the methods paragraph and the run record', async () => {
  const { main } = await screen();
  const cards = all(main, (n) => n.getAttribute('data-export') !== null).map((n) => n.getAttribute('data-export'));
  assert.deepStrictEqual(cards, ['report', 'profile', 'methods', 'record']);
  const primary = byClass(main, 'primary');
  assert.strictEqual(primary.length, 1);
  assert.strictEqual(primary[0].textContent, STRINGS['export.report.make']);
  assert.ok(text(main).indexOf('Profile for comparison') > text(main).indexOf(STRINGS['export.report.title']));
});

test('no text on the screen says pool or pooling (V14)', async () => {
  const { main, SS } = await screen({ 'POST /api/export/profile/preview': OK_PREVIEW });
  byClass(main, 'ss-export-profile-preview')[0].fire('click');
  await settle();
  assert.doesNotMatch(text(main), /\bpool/i);
  assert.match(text(main), /Profile for comparison/);
  assert.strictEqual(byClass(main, 'ss-export-profile-save')[0].textContent, 'Save the profile for comparison');
  assert.ok(SS);
});

test('preview shows every line, then the save sends the previewed sha256 and k', async () => {
  let saved = null;
  const { main, calls } = await screen({
    'POST /api/export/profile/preview': OK_PREVIEW,
    'POST /api/export/profile/save': (url, init) => { saved = JSON.parse(init.body); return reply(200, { path: 'exports/profile-A-0a1b2c3d.json', sha256: OK_PREVIEW.sha256 }); },
  });
  byClass(main, 'ss-export-profile-preview')[0].fire('click');
  await settle();
  const pre = byClass(main, 'ss-export-pre')[0];
  assert.strictEqual(pre.textContent, OK_PREVIEW.preview.join('\n'));
  assert.match(text(main), /12 people in this profile/);
  byClass(main, 'ss-export-profile-save')[0].fire('click');
  await settle();
  assert.deepStrictEqual(saved, { run_id: RUN, k: 10, sha256: OK_PREVIEW.sha256 });
  assert.match(text(main), /exports\/profile-A-0a1b2c3d\.json/);
  assert.ok(calls.some((c) => c.url.indexOf('/api/export/list') === 0));
});

test('a refused profile lists the reasons and offers no save button', async () => {
  const pv = Object.assign({}, OK_PREVIEW, { exportable: false,
    refusals: [{ code: 'group_below_k', message: 'group below k: not exportable' }],
    warnings: [{ code: 'differencing', message: 'An earlier saved profile counted 9 people' }] });
  const { main } = await screen({ 'POST /api/export/profile/preview': pv });
  byClass(main, 'ss-export-profile-preview')[0].fire('click');
  await settle();
  assert.match(text(byClass(main, 'ss-export-refusals')[0]), /group below k: not exportable/);
  assert.match(text(byClass(main, 'ss-export-warnings')[0]), /counted 9 people/);
  assert.strictEqual(byClass(main, 'ss-export-profile-save').length, 0);
});

test('with a mode-B coding open the screen explains and shows no export', async () => {
  const { main, calls } = await screen({ 'GET /api/export/runs': Object.assign({}, RUNS, { gate: { open: false, waiting: 2 } }) });
  assert.strictEqual(all(main, (n) => n.getAttribute('data-export') !== null).length, 0);
  assert.match(text(main), /Still open: 2/);
  assert.ok(!calls.some((c) => c.url.indexOf('/api/export/list') === 0), 'no result route is called');
});

test('no finished run: says where runs start', async () => {
  const { main } = await screen({ 'GET /api/export/runs': { runs: [{ run_id: RUN, group: 'A', finished: false }], gate: { open: true } } });
  assert.match(text(main), new RegExp(STRINGS['export.no_runs'].slice(0, 20)));
});

test('the report button shows the written file', async () => {
  const file = '/Users/x/proj/exports/report-' + RUN + '/index.html';
  const { main } = await screen({ 'POST /api/export/report': { path: 'exports/report-' + RUN + '/index.html', file, sha256: 'd'.repeat(64) } });
  byClass(main, 'ss-export-report')[0].fire('click');
  await settle();
  assert.strictEqual(byClass(main, 'ss-export-file')[0].textContent, file);
});

test('pickRun prefers the hash run, else the last finished one', () => {
  const { SS } = load(() => reply(200, {}));
  const runs = [{ run_id: 'A-1', finished: true }, { run_id: 'B-2', finished: true }, { run_id: 'C-3', finished: false }];
  assert.strictEqual(SS.exportScreen.pickRun(runs, 'A-1').run_id, 'A-1');
  assert.strictEqual(SS.exportScreen.pickRun(runs, 'C-3').run_id, 'B-2');
  assert.strictEqual(SS.exportScreen.pickRun([], ''), null);
});

test('report.js: every map prints at rung 2 when the run has it', () => {
  const sandbox = { console, location: { hash: '' }, SS_STRINGS: STRINGS };
  sandbox.window = sandbox;
  const ctx = vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(path.join(JS, 'core.js'), 'utf8'), ctx);
  vm.runInContext(fs.readFileSync(path.join(JS, 'report.js'), 'utf8'), ctx);
  const r = sandbox.SS.report;
  assert.strictEqual(r.printRung({ rungs: ['rung1', 'rung2', 'placebo', 'rung3'] }), 'rung2');
  assert.strictEqual(r.printRung({ rungs: ['rung1'] }), 'rung1');
  assert.strictEqual(r.printRung({ rungs: [] }), 'rung2');
});

test('report.js: a wide map shrinks to the printed line, a narrow one keeps the default', () => {
  const sandbox = { console, location: { hash: '' }, SS_STRINGS: STRINGS };
  sandbox.window = sandbox;
  const ctx = vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(path.join(JS, 'core.js'), 'utf8'), ctx);
  vm.runInContext(fs.readFileSync(path.join(JS, 'report.js'), 'utf8'), ctx);
  const body = (w) => {
    const props = {};
    return { props, querySelector: (s) => (s === '.ss-lanes-inner' && w ? { scrollWidth: w } : null),
      style: { setProperty: (k, v) => { props[k] = v; } } };
  };
  const wide = body(2000);
  assert.strictEqual(sandbox.SS.report.printZoom(wide), 0.33);
  assert.strictEqual(wide.props['--report-print-zoom'], '0.33');
  const narrow = body(500);
  assert.strictEqual(sandbox.SS.report.printZoom(narrow), 0.62);
  assert.strictEqual(sandbox.SS.report.printZoom(body(0)), null);
});

test('issue 4 with no project open the Export screen offers the way back to Setup, like the other tabs', async () => {
  const env = load(answerWith({
    'GET /api/export/runs': () => reply(409, { error: 'no_project', message: STRINGS['project.error.no_project'] }),
  }));
  const main = makeNode('main');
  env.SS.exportScreen.render(main, '');
  await settle();
  assert.ok(text(main).indexOf(STRINGS['project.error.no_project']) >= 0, 'the plain sentence');
  const back = byClass(main, 'ss-setup-back');
  assert.strictEqual(back.length, 1, 'one Go to Setup link');
  assert.strictEqual(back[0].tagName, 'A');
  assert.strictEqual(back[0].textContent, 'Go to Setup');
  assert.strictEqual(back[0].getAttribute('href'), '#setup');
  assert.deepStrictEqual(env.errors, []);
});

test('issue 4 another failure on the Export screen shows its message without the Setup link', async () => {
  const env = load(answerWith({
    'GET /api/export/runs': () => reply(500, { error: 'boom', message: 'Something broke.' }),
  }));
  const main = makeNode('main');
  env.SS.exportScreen.render(main, '');
  await settle();
  assert.ok(text(main).indexOf('Something broke.') >= 0);
  assert.strictEqual(byClass(main, 'ss-setup-back').length, 0);
});

test('issue 5 Export names the tab as the tab is called: "Run" (Part 5b; was "Compare instructions"), never "the Instructions screen"', async () => {
  const { main } = await screen({ 'GET /api/export/runs': { runs: [], k_min: 10, gate: { open: true, waiting: 0 } } });
  const t = text(main);
  assert.ok(t.indexOf('Runs start on the Run screen.') >= 0, t);
  assert.ok(!/Instructions screen/.test(t));
  assert.ok(STRINGS['export.no_runs'].indexOf(STRINGS['instructions.title']) >= 0, 'the sentence uses the tab\'s own title string');
});

test('K2 U08: the report card says the report holds participant quotes and map steps, not to share it like the profile', async () => {
  const { main } = await screen();
  const card = all(main, (n) => n.getAttribute('data-export') === 'report')[0];
  assert.ok(card, 'the report card exists');
  const t = text(card).replace(/\s+/g, ' ');
  assert.ok(/quotes/.test(t) && /map/.test(t), 'names quotes and map steps: ' + t);
  assert.ok(/participant text/.test(t), t);
  assert.ok(/do not share it like the profile/.test(t), t);
  // the report page's own lede carries the same warning
  assert.ok(/participant text/.test(STRINGS['report.lede']) && /do not share/.test(STRINGS['report.lede']));
  assert.ok(/profile/.test(STRINGS['report.lede']));
  // the profile card still says it is the file to share
  assert.ok(/share/.test(STRINGS['export.profile.body']));
});

// ---- Part 5a (scout F11, F12, F9) ----
test('F12: three plain checks sit above the raw lines, which stay closed; no "state" on the people line', async () => {
  const pv = Object.assign({}, OK_PREVIEW, { checks: { smallest_group: 12, not_run: 3, cells: 40 } });
  const { main } = await screen({ 'POST /api/export/profile/preview': pv });
  byClass(main, 'ss-export-profile-preview')[0].fire('click');
  await settle();
  const checks = all(main, (n) => n.getAttribute('data-part') === 'checks')[0];
  assert.ok(checks, 'the checks box exists');
  const items = all(checks, (n) => n.getAttribute('data-check') !== null);
  assert.deepStrictEqual(items.map((n) => n.getAttribute('data-check')), ['groups', 'smallest', 'not_run']);
  assert.deepStrictEqual(items.map((n) => n.getAttribute('data-status')), ['ok', 'ok', 'note']);
  assert.match(text(items[1]), /smallest group in the file has 12 people/);
  assert.match(text(items[1]), /k = 10/);
  assert.match(text(items[2]), /3 of the 40 numbers/);
  const lines = byClass(main, 'ss-export-lines')[0];
  assert.strictEqual(lines.getAttribute('open'), null, 'the raw lines are closed');
  assert.match(text(lines), /"pending"/);
  // the checks come before the raw lines
  const host = byClass(main, 'ss-export-result').filter((n) => byClass(n, 'ss-export-lines').length)[0];
  const order = host.children.map((c) => c.attributes.class || '');
  assert.ok(order.indexOf('ss-export-checks') < order.indexOf('ss-export-lines'), order.join(','));
  assert.doesNotMatch(text(byClass(main, 'ss-export-people')[0]), /state/);
});

test('F12: a smallest group below k is marked as not passing; no empty numbers says so', async () => {
  const pv = Object.assign({}, OK_PREVIEW, { k: 12, exportable: false, checks: { smallest_group: 9, not_run: 0, cells: 40 } });
  const { main } = await screen({ 'POST /api/export/profile/preview': pv });
  byClass(main, 'ss-export-profile-preview')[0].fire('click');
  await settle();
  const items = all(main, (n) => n.getAttribute('data-check') !== null);
  assert.strictEqual(items[1].getAttribute('data-status'), 'fail');
  assert.match(text(items[1]), /fewer than k = 12/);
  assert.strictEqual(items[2].getAttribute('data-status'), 'ok');
  assert.match(text(items[2]), /Every number in the file was computed in this run/);
});

test('F11: Open and Show in folder beside saved files, only when the computer can open files', async () => {
  const opened = [];
  const map = {
    'GET /api/export/runs': Object.assign({}, RUNS, { can_open: true }),
    'GET /api/export/list': { run_id: RUN, outputs: [
      { kind: 'report', saved: true, path: 'exports/report-' + RUN + '/index.html' },
      { kind: 'methods', saved: true, path: 'exports/methods-' + RUN + '.txt' },
      { kind: 'profile', saved: false, path: null }] },
    'POST /api/export/open': (url, init) => { opened.push(JSON.parse(init.body)); return reply(200, { opened: true }); },
  };
  const { main } = await screen(map);
  const rows = byClass(main, 'ss-export-list-row');
  assert.strictEqual(rows.length, 2);
  rows.forEach((r) => {
    assert.strictEqual(byClass(r, 'ss-export-open-file')[0].textContent, 'Open');
    assert.strictEqual(byClass(r, 'ss-export-open-folder')[0].textContent, 'Show in folder');
  });
  byClass(rows[1], 'ss-export-open-file')[0].fire('click');
  byClass(rows[0], 'ss-export-open-folder')[0].fire('click');
  await settle();
  assert.deepStrictEqual(opened, [{ path: 'exports/methods-' + RUN + '.txt', reveal: false },
    { path: 'exports/report-' + RUN + '/index.html', reveal: true }]);
  // without an opener on this computer, no buttons
  const none = await screen(Object.assign({}, map, { 'GET /api/export/runs': RUNS }));
  assert.strictEqual(byClass(none.main, 'ss-export-open-btn').length, 0);
});

test('F11: an opener failure shows the app\'s message beside the buttons', async () => {
  const { main } = await screen({
    'GET /api/export/runs': Object.assign({}, RUNS, { can_open: true }),
    'POST /api/export/report': { path: 'exports/report-' + RUN + '/index.html', file: '/x/index.html', sha256: 'd'.repeat(64) },
    'POST /api/export/open': () => reply(502, { error: 'open_failed', message: STRINGS['export.error.open_failed'] }),
  });
  byClass(main, 'ss-export-report')[0].fire('click');
  await settle();
  byClass(main, 'ss-export-open-file')[0].fire('click');
  await settle();
  assert.strictEqual(byClass(main, 'ss-export-open-error')[0].textContent, STRINGS['export.error.open_failed']);
});

test('F9 report.js: the plain sentences go into #report-lead; judgeless and no-reference runs get one sentence', () => {
  const sandbox = { console, location: { hash: '' }, SS_STRINGS: STRINGS };
  sandbox.window = sandbox;
  const ctx = vm.createContext(sandbox);
  for (const f of ['core.js', 'profile.js', 'report.js']) vm.runInContext(fs.readFileSync(path.join(JS, f), 'utf8'), ctx);
  const SS = sandbox.SS;
  SS.viewer = { noJudge: (v) => !!v.nojudge };
  const host = makeNode('div');
  const doc = { getElementById: (id) => (id === 'report-lead' ? host : null), createElement: makeNode };
  sandbox.document = doc;
  const prof = { rows: [{ row: 'lead', cells: [
    { name: 'listening_added', x: 'primary', value: 0.24 }, { name: 'ai_recall', x: 'primary', value: 0.394 },
    { name: 'no_interview_recall', x: 'primary', value: 0.152 }] }] };
  const said = SS.report.lead(doc, { reference_type: 'A' }, prof);
  assert.strictEqual(said[0].id, 'added');
  assert.match(text(host), /24(\.0)? percentage points/);
  SS.report.lead(doc, { reference_type: 'A', nojudge: true }, null);
  assert.strictEqual(host.children.length, 1);
  assert.strictEqual(host.children[0].textContent, STRINGS['results.lead.no_judge']);
  SS.report.lead(doc, { reference_type: 'C' }, null);
  assert.strictEqual(host.children[0].textContent, STRINGS['results.lead.no_reference']);
  // no profile, judge set, reference A: nothing is written
  assert.strictEqual(SS.report.lead(doc, { reference_type: 'A' }, null), null);
});
