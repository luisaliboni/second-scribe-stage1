'use strict';
// Part 5c, item 38: every app screen keeps the reader's place after a save or a choice (SS.place, app-place.js):
// the scroll position, the focus on the control just used, and the result line beside it. One test per screen:
// Setup (the Models step's Quick setup), Reference, Coding, Run, Results, Export.
// The stub DOM knows parents, focus (document.activeElement falls back to the body when the focused node leaves the
// page) and scrolling: when the main element is emptied, the page "jumps" to the top (scrollY = 0) unless it keeps a
// min-height, as a browser does when the page gets shorter than the scroll position.
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
const clone = (o) => JSON.parse(JSON.stringify(o));

function textNode(s) {
  return { nodeType: 3, nodeValue: String(s), get textContent() { return this.nodeValue; }, parentNode: null };
}

function makeWorld() {
  const world = { focused: null, scrollY: 0, scrolls: [], body: null, main: null };
  function makeNode(tag) {
    const node = {
      nodeType: 1, tagName: String(tag).toUpperCase(), childNodes: [], attributes: {}, listeners: {}, style: {},
      dataset: {}, parentNode: null, disabled: false, value: '', checked: false, hidden: false, _text: '',
      offsetHeight: 0,
      classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
      get children() { return node.childNodes.filter((c) => c.nodeType === 1); },
      get firstChild() { return node.childNodes[0] || null; },
      get className() { return node.attributes.class || ''; },
      set className(v) { node.attributes.class = String(v); },
      get textContent() { return node._text + node.childNodes.map((c) => c.textContent).join(''); },
      set textContent(v) { node.childNodes.forEach((c) => { c.parentNode = null; }); node.childNodes = []; node._text = String(v); jump(node); },
      setAttribute(k, v) { node.attributes[k] = String(v); },
      getAttribute(k) { return Object.prototype.hasOwnProperty.call(node.attributes, k) ? node.attributes[k] : null; },
      removeAttribute(k) { delete node.attributes[k]; },
      appendChild(c) { if (c.parentNode) c.parentNode.removeChild(c); c.parentNode = node; node.childNodes.push(c); return c; },
      insertBefore(c, ref) {
        if (!ref) return node.appendChild(c);
        if (c.parentNode) c.parentNode.removeChild(c);
        c.parentNode = node; node.childNodes.splice(node.childNodes.indexOf(ref), 0, c); return c;
      },
      removeChild(c) { node.childNodes.splice(node.childNodes.indexOf(c), 1); c.parentNode = null; jump(node); return c; },
      addEventListener(type, fn) { (node.listeners[type] = node.listeners[type] || []).push(fn); },
      removeEventListener() {},
      // events bubble to the ancestors (capture listeners of SS.place.watch included)
      fire(type, extra) {
        const ev = Object.assign({ preventDefault() {}, target: node, key: '' }, extra || {});
        for (let n = node; n; n = n.parentNode) (n.listeners[type] || []).slice().forEach((fn) => fn(ev));
      },
      focus() { world.focused = node; },
      scrollIntoView() {},
    };
    return node;
  }
  // emptying the main element shortens the page: the browser clamps the scroll to the top
  function jump(node) {
    if (node === world.main && node.childNodes.length === 0 && !node.style.minHeight) world.scrollY = 0;
  }
  function connected(n) {
    while (n && n.parentNode) n = n.parentNode;
    return n === world.body;
  }
  world.makeNode = makeNode;
  world.connected = connected;
  return world;
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

function appScripts() {
  const screens = fs.readdirSync(JS).filter((f) => /^app-[a-z0-9-]+\.js$/.test(f) && f !== 'app-shell.js').sort();
  return ORDER.filter((f) => fs.existsSync(path.join(JS, f))).concat(['api.js', 'app-shell.js'], screens);
}
function reply(status, doc) {
  return { ok: status >= 200 && status < 300, status, text: () => Promise.resolve(JSON.stringify(doc)) };
}

function load(answer) {
  const world = makeWorld();
  const calls = [];
  const errors = [];
  const body = world.makeNode('body');
  world.body = body;
  const document = {
    createElement: world.makeNode, createElementNS: (ns, t) => world.makeNode(t), createTextNode: textNode,
    addEventListener() {}, body, hidden: false,
    get activeElement() { return world.focused && world.connected(world.focused) ? world.focused : body; },
  };
  const sandbox = {
    console: { error: (...a) => errors.push(a.map(String).join(' ')), log() {}, warn() {} },
    location: { hash: '', search: '' },
    SS_STRINGS: Object.assign({}, EN),
    document,
    setTimeout: (fn) => { fn(); return 0; }, clearTimeout() {},
    setInterval: () => 1, clearInterval() {},
    addEventListener() {}, removeEventListener() {},
    get scrollY() { return world.scrollY; },
    scrollTo: (x, y) => { world.scrolls.push([x, y]); world.scrollY = y; },
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
  const main = world.makeNode('main');
  main.ownerDocument = document;
  body.appendChild(main);
  world.main = main;
  return { SS: sandbox.SS, win: sandbox, world, calls, errors, main };
}
const tick = () => new Promise((r) => setImmediate(r));
async function settle() { for (let i = 0; i < 30; i++) await tick(); }

// The reader scrolled down and used `control`: focus on it, page at 640 px, page taller than the window.
function use(env, control) {
  env.world.scrollY = 640;
  env.main.offsetHeight = 2400;
  control.focus();
}
// After the action: same scroll, the focus on a control like `before` (same tag and attributes), connected.
function assertKept(env, before, label) {
  assert.strictEqual(env.world.scrollY, 640, label + ': the scroll position is kept');
  const f = env.world.focused;
  assert.ok(f && env.world.connected(f), label + ': the focus is on a control of the page');
  assert.strictEqual(f.tagName, before.tag, label + ': same kind of control');
  assert.strictEqual(f.getAttribute('value'), before.value, label + ': same control');
  if (before.text !== undefined) assert.strictEqual(f.textContent, before.text, label + ': same control');
  assert.strictEqual(env.main.style.minHeight || '', '', label + ': the page lets go of its height');
}
const what = (n) => ({ tag: n.tagName, value: n.getAttribute('value'), text: n.tagName === 'BUTTON' ? n.textContent : undefined });
// The result line right after the control's row (a status line, or an alert for a refusal).
function noteAfter(env) {
  return byClass(env.main, 'ss-place-after');
}

// ------------------------------------------------------------------------------------------------- Setup
test('Setup (Models, Quick setup): choosing where a model runs keeps the scroll, the focus and the place', async () => {
  const models = { roles: {}, keys: { recon: false, judge: false }, problems: [], optins: [], declarations: [],
    defaults: { local: 'loop' + 'back-default', openai_compat: '', anthropic: 'api-default-from-server' } };
  const summary = { schema: 'project/1', project_id: 'a'.repeat(32), name: 'Court', root: '/x', sync: { kind: null, acknowledged: false },
    blocked: false, files: {}, changed_files: [], counts: {}, log: { records: 1, head: 'b'.repeat(64), ok: true } };
  const env = load((u) => {
    const p = u.split('?')[0];
    if (p === '/api/project') return reply(200, { project: summary });
    if (p === '/api/models') return reply(200, { models });
    if (p === '/api/runs/ready') return reply(200, { items: [{ key: 'models', ok: false }], groups: {} });
    return reply(404, { error: 'not_found', message: 'no route' });
  });
  env.SS.setupModels.reset();
  await env.SS.setup.render(env.main, 'models');
  await settle();
  const col = byClass(env.main, 'ss-quick-role').filter((c) => c.getAttribute('data-quick') === 'judge')[0];
  const net = byTag(col, 'input').filter((i) => i.getAttribute('value') === 'network')[0];
  use(env, net);
  net.fire('click');
  net.checked = true;
  net.fire('change');
  await settle();
  assert.deepStrictEqual(env.errors, []);
  assertKept(env, what(net), 'Setup');
  const judge = byClass(env.main, 'ss-quick-role').filter((c) => c.getAttribute('data-quick') === 'judge')[0];
  assert.ok(byTag(judge, 'input').indexOf(env.world.focused) >= 0, 'the focus is in the judge column again');
  assert.strictEqual(env.world.focused.checked, true);
});

// ------------------------------------------------------------------------------------------------- Reference
function refs(types) {
  return { references: ['SYN-OF01', 'SYN-OF02', 'SYN-OW03'].map((pid, i) => ({ pid, type: types[i], map_confirmed: i !== 2,
    coding: { n_marks: 0, locked: false } })), gate: { open: true, waiting: [], revealed: false } };
}
test('Reference: a choice keeps the scroll, the focus on that choice, and says the result beside it', async () => {
  const types = ['A', null, null];
  const env = load((u, init) => {
    if (u === '/api/references' && init.method === 'POST') {
      const b = JSON.parse(init.body);
      types[['SYN-OF01', 'SYN-OF02', 'SYN-OW03'].indexOf(b.pid)] = b.type;
      return reply(200, { pid: b.pid, type: b.type, changed: true });
    }
    if (u === '/api/references') return reply(200, refs(types));
    return reply(404, { error: 'not_found', message: 'no route' });
  });
  await env.SS.reference.render(env.main, '');
  await settle();
  const row = byTag(env.main, 'fieldset').filter((f) => f.getAttribute('data-pid') === 'SYN-OF02')[0];
  const c = byTag(row, 'input').filter((i) => i.getAttribute('value') === 'C')[0];
  use(env, c);
  c.fire('click');
  c.checked = true;
  c.fire('change');
  await settle();
  assert.deepStrictEqual(env.errors, []);
  assertKept(env, what(c), 'Reference');
  const now = byTag(env.main, 'fieldset').filter((f) => f.getAttribute('data-pid') === 'SYN-OF02')[0];
  assert.ok(byTag(now, 'input').indexOf(env.world.focused) >= 0, 'the focus is in the same transcript row');
  const note = noteAfter(env);
  assert.strictEqual(note.length, 1);
  assert.strictEqual(note[0].textContent, EN['reference.chosen'].replace('{pid}', 'SYN-OF02').replace('{type}', 'C'));
  assert.strictEqual(note[0].parentNode, now, 'the line is in the row that was used');
  // item 37: A of the row without a confirmed map says why and links to Check the maps
  const r3 = byTag(env.main, 'fieldset').filter((f) => f.getAttribute('data-pid') === 'SYN-OW03')[0];
  const why = byClass(r3, 'ss-ref-why')[0];
  assert.ok(why && why.textContent.indexOf(EN['reference.no_map']) === 0, 'the reason is said');
  assert.ok(why.textContent.indexOf(EN['reference.maps_count'].replace('{done}', '2').replace('{total}', '3')) >= 0);
  assert.strictEqual(byTag(why, 'a')[0].getAttribute('href'), '#setup/maps');
  const a3 = byTag(r3, 'input').filter((i) => i.getAttribute('value') === 'A')[0];
  assert.strictEqual(a3.disabled, true);
  assert.strictEqual(a3.getAttribute('aria-describedby'), why.getAttribute('id'));
});

// ------------------------------------------------------------------------------------------------- Coding
test('Coding: removing a mark keeps the scroll and moves the focus to the next mark, with the result beside it', async () => {
  const turns = [{ idx: 0, speaker: 'interviewer', text: 'Could you start from the beginning of it all?' },
    { idx: 1, speaker: 'participant', text: 'After that came the how-to pages on the city website, which helped a lot.' }];
  let marks = [
    { mark_id: 'm1', turn: 1, start: 0, end: 14, text: 'After that came', status: 'happened', step: 'Pages', phase: '' },
    { mark_id: 'm2', turn: 1, start: 15, end: 40, text: 'the how-to pages on the city', status: 'missing', step: 'Website', phase: '' },
  ];
  const env = load((u, init) => {
    const p = u.split('?')[0];
    if (p === '/api/references') return reply(200, { references: [{ pid: 'SYN-OW03', type: 'B', map_confirmed: false,
      coding: { n_marks: marks.length, locked: false } }], gate: { open: false, waiting: ['SYN-OW03'], revealed: false } });
    if (p === '/api/coding' && init.method === 'GET') return reply(200, { pid: 'SYN-OW03', type: 'B', turns, marks,
      seconds: 0, locked: false, phases: [], missing_kinds: ['never_came'], min_words: 3, limit: 'x' });
    if (p === '/api/coding/unmark') { const b = JSON.parse(init.body); marks = marks.filter((m) => m.mark_id !== b.mark_id); return reply(200, {}); }
    if (p === '/api/coding/tick') return reply(200, {});
    return reply(404, { error: 'not_found', message: 'no route' });
  });
  await env.SS.coding.render(env.main, 'SYN-OW03');
  await settle();
  const rm = byClass(env.main, 'ss-coding-remove')[0];
  assert.strictEqual(rm.getAttribute('aria-label'), EN['coding.remove_label'].replace('{step}', 'Pages'));
  use(env, rm);
  rm.fire('click');
  await settle();
  assert.deepStrictEqual(env.errors, []);
  assert.strictEqual(env.world.scrollY, 640, 'Coding: the scroll position is kept');
  const left = byClass(env.main, 'ss-coding-remove');
  assert.strictEqual(left.length, 1);
  assert.strictEqual(env.world.focused, left[0], 'the focus moves to the next mark');
  const note = noteAfter(env);
  assert.strictEqual(note.length, 1);
  assert.strictEqual(note[0].textContent, EN['coding.removed'].replace('{step}', 'Pages'));
});

// ------------------------------------------------------------------------------------------------- Run
const PROMPTS = { rungs: ['rung1', 'rung2', 'rung3', 'placebo'].map((r, i) => ({ rung: r, file: r + '.txt', text: r.toUpperCase(),
  sha256: String(i + 1).repeat(64), system: null, definitions: null, requires: r === 'rung3' ? ['placebo'] : [] })),
wrapper: { text: 'WRAPPER', sha256: '5'.repeat(64) }, reading3: { file: 'p.txt', sha256: '9'.repeat(64) } };
test('Run: ticking the remedy keeps the scroll, the focus on the box and says "Choice saved." beside it', async () => {
  let ins = { rung1: 'basic', rung1_user: null, rungs: ['rung2'], preset: 'standard' };
  const env = load((u, init) => {
    const p = u.split('?')[0];
    if (p === '/api/prompts') return reply(200, PROMPTS);
    if (p === '/api/references') return reply(200, refs(['A', 'A', 'C']));
    if (p === '/api/instructions') {
      if (init.method === 'POST') { const b = JSON.parse(init.body); ins = Object.assign({}, ins, { rungs: b.rungs, preset: b.preset }); }
      return reply(200, { instructions: ins });
    }
    if (p === '/api/runs/status') return reply(200, { job: null });
    if (p === '/api/runs/last') return reply(200, { last: [] });
    if (p === '/api/runs/ready') return reply(200, { items: [{ key: 'labels', ok: true, done: 3, total: 3 }], groups: { A: { ready: true } } });
    return reply(404, { error: 'not_found', message: 'no route' });
  });
  await env.SS.instructions.render(env.main);
  await settle();
  const box = byTag(env.main, 'input').filter((i) => i.getAttribute('id') === 'ss-ins-rung3')[0];
  use(env, box);
  box.fire('click');
  box.checked = true;
  box.fire('change');
  await settle();
  assert.deepStrictEqual(env.errors, []);
  assertKept(env, what(box), 'Run');
  assert.strictEqual(env.world.focused.getAttribute('id'), 'ss-ins-rung3');
  assert.strictEqual(env.world.focused.checked, true);
  const note = noteAfter(env);
  assert.strictEqual(note.length, 1);
  assert.strictEqual(note[0].textContent, EN['instructions.saved']);
  // a poll redraw with the focus elsewhere (outside the screen) never pulls the focus back
  const outside = env.world.makeNode('a');
  env.world.body.appendChild(outside);
  outside.focus();
  env.SS.instructions.render(env.main);
  await settle();
  assert.strictEqual(env.world.focused, outside, 'focus outside the screen is left alone');
});

// ------------------------------------------------------------------------------------------------- Results
test('Results: changing the reference tab keeps the scroll and the focus on the tab', async () => {
  const env = load((u) => {
    const p = u.split('?')[0];
    if (p === '/api/results/gate') return reply(200, { open: true, waiting: [], revealed: false });
    if (p === '/api/results/runs') return reply(200, { runs: [{ run_id: 'A-0a1b2c3d', group: 'A', ready: true },
      { run_id: 'C-1b2c3d4e', group: 'C', ready: true }] });
    if (p === '/api/view') return reply(200, { view: clone(VIEW) });
    return reply(404, { error: 'not_found', message: 'no route' });
  });
  await env.SS.results.render(env.main, '');
  await settle();
  const tab = byClass(env.main, 'ss-res-tab').filter((t) => t.getAttribute('data-group') === 'C')[0];
  use(env, tab);
  tab.fire('click');
  await settle();
  assert.deepStrictEqual(env.errors, []);
  assertKept(env, what(tab), 'Results');
  assert.strictEqual(env.world.focused.getAttribute('data-group'), 'C');
  assert.strictEqual(env.world.focused.getAttribute('aria-selected'), 'true');
});

test('Results (item 37): with a B coding open, the Show button says why and links to Coding', async () => {
  const env = load((u) => {
    const p = u.split('?')[0];
    if (p === '/api/results/gate') return reply(200, { open: false, waiting: ['SYN-OW03'], revealed: false });
    if (p === '/api/results/runs') return reply(200, { runs: [{ run_id: 'A-0a1b2c3d', group: 'A', ready: true }] });
    return reply(404, { error: 'not_found', message: 'no route' });
  });
  await env.SS.results.render(env.main, '');
  await settle();
  const b = byClass(env.main, 'ss-res-show')[0];
  assert.strictEqual(b.disabled, true);
  const why = byClass(env.main, 'ss-res-why')[0];
  assert.ok(why.textContent.indexOf(EN['results.run.closed_why']) === 0);
  assert.strictEqual(byTag(why, 'a')[0].getAttribute('href'), '#coding');
  assert.strictEqual(b.getAttribute('aria-describedby'), why.getAttribute('id'));
});

// ------------------------------------------------------------------------------------------------- Export
test('Export: picking another run keeps the scroll and the focus on the picker; an unfinished run says why', async () => {
  const env = load((u) => {
    const p = u.split('?')[0];
    if (p === '/api/export/runs') return reply(200, { runs: [{ run_id: 'A-0a1b2c3d', group: 'A', finished: true },
      { run_id: 'A-1b2c3d4e', group: 'A', finished: true }, { run_id: 'A-2c3d4e5f', group: 'A', finished: false }],
    gate: { open: true }, k_min: 10, can_open: false });
    if (p === '/api/export/list') return reply(200, { outputs: [] });
    return reply(404, { error: 'not_found', message: 'no route' });
  });
  await env.SS.exportScreen.render(env.main, '');
  await settle();
  const sel = byTag(env.main, 'select')[0];
  const why = byClass(env.main, 'ss-export-why')[0];
  assert.ok(why && why.textContent.indexOf(EN['export.unfinished_why']) === 0, 'the disabled run says why');
  assert.strictEqual(byTag(why, 'a')[0].getAttribute('href'), '#instructions');
  assert.strictEqual(sel.getAttribute('aria-describedby'), why.getAttribute('id'));
  use(env, sel);
  sel.value = 'A-0a1b2c3d';
  sel.fire('change');
  await settle();
  assert.deepStrictEqual(env.errors, []);
  assert.strictEqual(env.win.location.hash, '#export/A-0a1b2c3d');
  assertKept(env, what(sel), 'Export');
  assert.strictEqual(env.world.focused.getAttribute('id'), 'ss-export-run');
  const picked = byTag(env.world.focused, 'option').filter((o) => o.getAttribute('selected') === 'selected')[0];
  assert.strictEqual(picked.getAttribute('value'), 'A-0a1b2c3d', 'the run picked is the one drawn');
});

// ------------------------------------------------------------------------------------------------- the helper itself
test('SS.place keeps opened folded parts open and never closes one', async () => {
  const env = load(() => reply(404, { error: 'not_found', message: 'no route' }));
  const { SS, world, main } = env;
  const draw = (open) => {
    while (main.firstChild) main.removeChild(main.firstChild);
    const page = main.appendChild(world.makeNode('div'));
    ['One', 'Two'].forEach((t) => {
      const d = page.appendChild(world.makeNode('details'));
      d.setAttribute('class', 'ss-x');
      d.appendChild(world.makeNode('summary')).textContent = t;
      if (open && t === 'Two') d.setAttribute('open', '');
    });
  };
  draw(false);
  byTag(main, 'details')[0].setAttribute('open', '');                 // the reader opened "One"
  SS.place.redraw(main, () => draw(true));                             // the new draw opens "Two" by itself
  const ds = byTag(main, 'details');
  assert.strictEqual(ds[0].getAttribute('open'), '', 'the part the reader opened is open again');
  assert.strictEqual(ds[1].getAttribute('open'), '', 'a part the screen opens stays open');
});

// ------------------------------------------------------------------------------------------------- F15: step marks
test('F15: the Setup step list marks each step in words, with counts, and updates after every accepted action', async () => {
  const summary = { schema: 'project/1', project_id: 'a'.repeat(32), name: 'Court', root: '/x', sync: { kind: null, acknowledged: false },
    blocked: false, files: {}, changed_files: [], counts: {}, log: { records: 1, head: 'b'.repeat(64), ok: true } };
  let labels = { key: 'labels', ok: false, done: 1, total: 3 };
  const env = load((u, init) => {
    const p = u.split('?')[0];
    if (p === '/api/project') return reply(200, { project: summary });
    if (p === '/api/runs/ready') return reply(200, { items: [{ key: 'context', ok: true }, { key: 'pairing', ok: true, done: 3, total: 3 },
      labels, { key: 'models', ok: false }, { key: 'model_check', ok: false, state: 'running' }], groups: {} });
    if (p === '/api/labels/confirm' && init.method === 'POST') { labels = { key: 'labels', ok: true, done: 3, total: 3 }; return reply(200, {}); }
    if (p === '/api/project/context') return reply(200, { context: null });
    return reply(200, {});
  });
  await env.SS.setup.render(env.main, 'project');
  await settle();
  const steps = () => byClass(env.main, 'ss-setup-step');
  const state = () => Object.fromEntries(steps().map((li) => [li.getAttribute('data-step'), li.getAttribute('data-state')]));
  const mark = (id) => byClass(steps().find((li) => li.getAttribute('data-step') === id), 'ss-setup-step-mark')[0];
  const ids = clone(env.SS.setup.list());
  assert.deepStrictEqual(ids.filter((id) => ['project', 'context', 'import', 'labels', 'maps', 'rule', 'models', 'check'].indexOf(id) < 0), [],
    'every step has a mark rule');
  const want = { project: 'done', context: 'done', import: 'done', labels: 'needs', maps: 'optional', rule: 'optional', models: 'needs', check: 'running' };
  for (const id of ids) assert.strictEqual(state()[id], want[id], id);
  // in words, never by colour alone; the count where it helps
  assert.strictEqual(mark('labels').textContent, EN['setup.mark.needs'] + ' · ' + EN['setup.mark.count'].replace('{done}', '1').replace('{total}', '3'));
  assert.strictEqual(mark('import').textContent, EN['setup.mark.done'] + ' · 3 of 3');
  assert.strictEqual(mark('check').textContent, EN['setup.mark.running']);
  // a screen reader reads "Speaker labels, Needs you · 1 of 3": the separator is in the link, hidden from sight only
  const link = byTag(steps().find((li) => li.getAttribute('data-step') === 'labels'), 'a')[0];
  const sep = byClass(link, 'ss-setup-step-sep')[0];
  assert.ok(sep && cls(sep, 'ss-visually-hidden') && sep.textContent === ', ');
  assert.strictEqual(link.childNodes.indexOf(sep) + 1, link.childNodes.indexOf(mark('labels')));
  // an action the server accepts updates the marks without a new visit
  await env.SS.api.post('/api/labels/confirm', { pid: 'SYN-OF01' });
  await settle();
  assert.strictEqual(state().labels, 'done');
  assert.strictEqual(mark('labels').textContent, EN['setup.mark.done'] + ' · 3 of 3');
  assert.strictEqual(byClass(env.main, 'ss-setup-step-mark').length, ids.length, 'one mark per step, never two');
});

test('F15: with no project open, only the project step says it needs you; the others are not guessed', async () => {
  const env = load((u) => (u.split('?')[0] === '/api/project' ? reply(200, { project: null }) : reply(404, { error: 'not_found', message: 'x' })));
  await env.SS.setup.render(env.main, 'project');
  await settle();
  const st = byClass(env.main, 'ss-setup-step').map((li) => [li.getAttribute('data-step'), li.getAttribute('data-state')]);
  assert.deepStrictEqual(st.filter(([id]) => id === 'project'), [['project', 'needs']]);
  assert.ok(st.filter(([id]) => id !== 'project').every(([, s]) => s === 'unknown'));
  assert.ok(env.calls.every((c) => c.url !== '/api/runs/ready'), 'no readiness read without a project');
});

test('item 37: the project step says why "continue" waits, and the reason goes once the sync box is ticked', async () => {
  const summary = { schema: 'project/1', project_id: 'a'.repeat(32), name: 'Court', root: '/x/Dropbox/c',
    sync: { kind: 'dropbox', kinds: ['dropbox'], acknowledged: false }, blocked: true, files: {}, changed_files: [], counts: {},
    log: { records: 1, head: 'b'.repeat(64), ok: true } };
  const env = load((u) => (u.split('?')[0] === '/api/project' ? reply(200, { project: summary }) : reply(200, {})));
  await env.SS.setup.render(env.main, 'project');
  await settle();
  const why = byTag(env.main, 'p').find((p) => p.getAttribute('id') === 'ss-setup-sync-why');
  assert.ok(why && why.textContent.indexOf(EN['setup.project.sync_why']) === 0);
  const btn = byTag(env.main, 'button').find((b) => b.getAttribute('aria-describedby') === 'ss-setup-sync-why');
  assert.ok(btn && btn.disabled, 'the button names its reason');
  const box = byTag(why.parentNode, 'input').find((i) => i.getAttribute('type') === 'checkbox');
  box.checked = true;
  box.fire('change');
  assert.strictEqual(why.hidden, true);
  assert.strictEqual(btn.disabled, false);
});
