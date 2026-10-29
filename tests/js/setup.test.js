'use strict';
// Task 32: the Setup screen's step registry (app-setup.js) and the project step (app-setup-project.js).
// The app's scripts load in the server's order (core, api, shell, then app-*.js sorted by name), into a vm context
// with a small stub DOM and a fake fetch.
const test = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ROOT = path.resolve(__dirname, '..', '..');
const JS = path.join(ROOT, 'second_scribe', 'app', 'static', 'js');
const STRINGS = JSON.parse(fs.readFileSync(path.join(ROOT, 'second_scribe', 'app', 'strings', 'en.json'), 'utf8'));

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
    fire(type) { (node.listeners[type] || []).forEach((fn) => fn({ preventDefault() {} })); },
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

function appScripts() {
  const screens = fs.readdirSync(JS).filter((f) => /^app-[a-z0-9-]+\.js$/.test(f) && f !== 'app-shell.js').sort();
  return ['core.js', 'api.js', 'app-shell.js'].concat(screens);
}

// The registry and the project step alone: tests that count or name steps must not depend on later steps' files.
const BASE = ['core.js', 'api.js', 'app-shell.js', 'app-place.js', 'app-setup-project.js', 'app-setup.js'];

function reply(status, doc) {
  return { ok: status >= 200 && status < 300, status, text: () => Promise.resolve(JSON.stringify(doc)) };
}

function load(opts) {
  opts = opts || {};
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
      return Promise.resolve((opts.answer || (() => reply(200, { project: null })))(url, init, calls));
    },
  };
  sandbox.window = sandbox;
  const ctx = vm.createContext(sandbox);
  if (opts.before) vm.runInContext(opts.before, ctx, { filename: 'before.js' });
  for (const f of opts.files || appScripts()) {
    vm.runInContext(fs.readFileSync(path.join(JS, f), 'utf8'), ctx, { filename: f });
  }
  return { SS: sandbox.SS, calls, errors, ctx };
}

const tick = () => new Promise((r) => setTimeout(r, 0));
async function settle() { for (let i = 0; i < 10; i++) await tick(); }

function summary(extra) {
  return Object.assign({
    schema: 'project/1', project_id: 'a'.repeat(32), name: 'Court study', root: '/Users/ana/Studies/court',
    sync: { kind: null, acknowledged: false }, blocked: false, files: {}, changed_files: [],
    counts: { files: 0, maps: 0, references: 0, runs: 0 }, log: { records: 1, head: 'b'.repeat(64), ok: true },
  }, extra || {});
}

test('the server order loads step files before app-setup.js, and the queue still registers them', () => {
  const order = appScripts();
  assert.ok(order.indexOf('app-setup-project.js') < order.indexOf('app-setup.js'), order.join(','));
  const { SS, errors } = load();
  assert.deepStrictEqual(errors, []);
  assert.ok(SS.setup.list().indexOf('project') >= 0);
  assert.strictEqual(SS.setup.list()[0], 'project');
  assert.ok(SS.shell.list().indexOf('setup') >= 0);
});

test('steps sort by order then id; later pushes register at once; duplicates and bad defs throw', () => {
  const before = "(window.SS = window.SS || {}); (SS.setupQueue = SS.setupQueue || []).push(['zeta', " +
    "{ order: 50, titleKey: 'setup.title', render: function () {} }]);" +
    "SS.setupQueue.push(['alpha', { order: 50, titleKey: 'setup.title', render: function () {} }]);" +
    "SS.setupQueue.push(['early', { order: 5, titleKey: 'setup.title', render: function () {} }]);";
  const { SS } = load({ before, files: BASE });
  assert.deepStrictEqual(Array.from(SS.setup.list()), ['early', 'project', 'alpha', 'zeta']);
  SS.setupQueue.push(['models', { order: 60, titleKey: 'setup.title', render() {} }]);
  assert.deepStrictEqual(Array.from(SS.setup.list()), ['early', 'project', 'alpha', 'zeta', 'models']);
  assert.throws(() => SS.setupQueue.push(['models', { titleKey: 'setup.title', render() {} }]), /already registered/);
  assert.throws(() => SS.setup.step('Bad id', { titleKey: 'x', render() {} }), /bad step id/);
  assert.throws(() => SS.setup.step('norender', { titleKey: 'x' }), /needs titleKey and render/);
  assert.strictEqual(SS.setup.pick('models'), 'models');
  assert.strictEqual(SS.setup.pick('models/extra'), 'models');
  assert.strictEqual(SS.setup.pick('nope'), 'early');
  assert.strictEqual(SS.setup.pick(''), 'early');
});

test('a step that needs a project shows a note until a usable project is open', () => {
  const before = "(window.SS = window.SS || {}); (SS.setupQueue = SS.setupQueue || []).push(['context', " +
    "{ order: 20, titleKey: 'setup.title', render: function (c, p) { window.__seen.push(p && p.name); } }]);";
  const { SS, ctx } = load({ before: 'window.__seen = [];' + before, files: BASE });
  const main = makeNode('main');
  SS.setup.draw(main, 'context', null);
  assert.ok(text(main).indexOf(STRINGS['setup.needs_project']) >= 0);
  SS.setup.draw(main, 'context', summary({ blocked: true, sync: { kind: 'dropbox', acknowledged: false } }));
  assert.ok(text(main).indexOf(STRINGS['setup.needs_ack']) >= 0);
  const locked = byClass(main, 'ss-setup-step').filter((li) => li.attributes['data-locked'] === 'true');
  assert.strictEqual(locked.length, 1);
  SS.setup.draw(main, 'context', summary());
  assert.deepStrictEqual(Array.from(vm.runInContext('window.__seen', ctx)), ['Court study']);
  assert.strictEqual(byClass(main, 'ss-setup-step').filter((li) => li.attributes['data-locked']).length, 0);
  const current = byTag(main, 'a').filter((a) => a.attributes['aria-current'] === 'step');
  assert.strictEqual(current.length, 1);
  assert.strictEqual(current[0].attributes.href, '#setup/context');
});

test('a throwing step is contained and the page still draws', () => {
  const before = "(window.SS = window.SS || {}); (SS.setupQueue = SS.setupQueue || []).push(['broken', " +
    "{ order: 20, titleKey: 'setup.title', needsProject: false, render: function () { throw new Error('boom'); } }]);";
  const { SS, errors } = load({ before });
  const main = makeNode('main');
  SS.setup.draw(main, 'broken', null);
  assert.ok(text(main).indexOf(STRINGS['shell.screen_failed']) >= 0);
  assert.strictEqual(errors.length, 1);
});

test('render fetches GET /api/project and draws the project step with both forms', async () => {
  const { SS, calls } = load();
  const main = makeNode('main');
  await SS.setup.render(main, '');
  // item 22: the project step also asks whether this computer has a folder window
  assert.deepStrictEqual(calls.map((c) => c.method + ' ' + c.url), ['GET /api/project', 'GET /api/project/folder-dialog']);
  const forms = byTag(main, 'form');
  assert.strictEqual(forms.length, 2);
  const t = text(main);
  assert.ok(t.indexOf(STRINGS['setup.project.create_title']) >= 0);
  assert.ok(t.indexOf(STRINGS['setup.project.open_title']) >= 0);
  assert.ok(t.indexOf(STRINGS['setup.project.folder_hint']) >= 0);
  // every input has a label pointing at it
  byTag(main, 'input').forEach((inp) => {
    const labels = byTag(main, 'label').filter((l) => l.attributes.for === inp.attributes.id);
    assert.strictEqual(labels.length, 1, inp.attributes.id);
  });
});

test('create posts the typed folder and name, then refreshes; a refusal shows the server message', async () => {
  let refuse = true;
  const { SS, calls } = load({
    answer: (url, init) => {
      if (url === '/api/project/create') {
        return refuse ? reply(409, { error: 'project_exists', message: 'This folder already holds a project.' })
          : reply(200, { project: summary() });
      }
      return reply(200, { project: refuse ? null : summary() });
    },
  });
  const main = makeNode('main');
  await SS.setup.render(main, 'project');
  const form = byTag(main, 'form')[0];
  const inputs = byTag(form, 'input');
  inputs[0].value = '/Users/ana/Studies/court';
  inputs[1].value = 'Court study';
  form.fire('submit');
  await settle();
  const post = calls.filter((c) => c.method === 'POST');
  assert.strictEqual(post.length, 1);
  assert.strictEqual(post[0].url, '/api/project/create');
  assert.deepStrictEqual(post[0].body, { path: '/Users/ana/Studies/court', name: 'Court study' });
  assert.ok(text(form).indexOf('This folder already holds a project.') >= 0);
  refuse = false;
  form.fire('submit');
  await settle();
  assert.strictEqual(calls.filter((c) => c.url === '/api/project').length, 2);    // refreshed after success
  assert.ok(text(main).indexOf('Open project: Court study') >= 0);
  assert.ok(text(main).indexOf('/Users/ana/Studies/court') >= 0);
});

test('open posts the typed folder', async () => {
  const { SS, calls } = load({ answer: () => reply(200, { project: null }) });
  const main = makeNode('main');
  await SS.setup.render(main, 'project');
  const form = byTag(main, 'form')[1];
  byTag(form, 'input')[0].value = '/Users/ana/Studies/old';
  form.fire('submit');
  await settle();
  const post = calls.filter((c) => c.method === 'POST');
  assert.deepStrictEqual(post.map((c) => [c.url, c.body]), [['/api/project/open', { path: '/Users/ana/Studies/old' }]]);
});

test('sync warning: the button waits for the box, then posts understood: true', async () => {
  let acked = false;
  const blocked = () => summary({ blocked: !acked, sync: { kind: 'icloud', acknowledged: acked } });
  const { SS, calls } = load({
    answer: (url) => {
      if (url === '/api/project/ack-sync') { acked = true; return reply(200, { project: blocked() }); }
      return reply(200, { project: blocked() });
    },
  });
  const main = makeNode('main');
  await SS.setup.render(main, 'project');
  const warning = byClass(main, 'ss-setup-warning');
  assert.strictEqual(warning.length, 1);
  assert.strictEqual(warning[0].attributes.role, 'alert');
  const t = text(warning[0]);
  assert.ok(t.indexOf('iCloud Drive') >= 0, t);
  assert.ok(t.indexOf("participants' words would leave this computer") >= 0, t);
  const box = byTag(warning[0], 'input')[0];
  const button = byTag(warning[0], 'button')[0];
  assert.strictEqual(button.disabled, true);
  button.fire('click');
  await settle();
  assert.strictEqual(calls.filter((c) => c.method === 'POST').length, 0);
  box.checked = true;
  box.fire('change');
  assert.strictEqual(button.disabled, false);
  button.fire('click');
  await settle();
  const post = calls.filter((c) => c.method === 'POST');
  assert.deepStrictEqual(post.map((c) => [c.url, c.body]), [['/api/project/ack-sync', { understood: true }]]);
  assert.strictEqual(byClass(main, 'ss-setup-warning').length, 0);
  assert.ok(text(main).indexOf('You confirmed that you understand') >= 0);
});

test('changed files are named; a broken log is said', async () => {
  const { SS } = load({
    answer: () => reply(200, { project: summary({
      changed_files: ['inputs/transcripts/case-a.txt'], log: { records: 4, head: 'c'.repeat(64), ok: false } }) }),
  });
  const main = makeNode('main');
  await SS.setup.render(main, 'project');
  const t = text(main);
  assert.ok(t.indexOf(STRINGS['setup.project.changed']) >= 0);
  assert.ok(t.indexOf('inputs/transcripts/case-a.txt') >= 0);
  assert.ok(t.indexOf('Records: 4. The chain does not verify') >= 0);
});

test('every setup.* and project.* key the scripts name exists in en.json', () => {
  for (const f of ['app-setup.js', 'app-setup-project.js']) {
    const src = fs.readFileSync(path.join(JS, f), 'utf8').replace(/\/\*[\s\S]*?\*\//g, '');   // code, not the doc comment
    const keys = src.match(/'(setup|project)\.[a-z_.]+'/g) || [];
    keys.map((k) => k.slice(1, -1)).filter((k) => !k.endsWith('.')).forEach((k) => {
      assert.ok(Object.prototype.hasOwnProperty.call(STRINGS, k), f + ': ' + k);
    });
  }
  ['icloud', 'cloudstorage', 'dropbox', 'onedrive', 'google_drive', 'box', 'pcloud', 'nextcloud', 'mega',
    'sync_com', 'sharepoint', 'unknown'].forEach((k) => {
    assert.ok(STRINGS['project.sync.kind.' + k], k);
  });
});

test('sync warning names each added service, not "a syncing service"', async () => {
  const names = { box: 'Box', pcloud: 'pCloud', nextcloud: 'Nextcloud', mega: 'MEGA', sync_com: 'Sync.com',
    sharepoint: 'SharePoint' };
  for (const kind of Object.keys(names)) {
    const { SS } = load({ answer: () => reply(200, { project: summary({ blocked: true, sync: { kind, acknowledged: false } }) }) });
    const main = makeNode('main');
    await SS.setup.render(main, 'project');
    const t = text(byClass(main, 'ss-setup-warning')[0]);
    assert.ok(t.indexOf('This folder is inside ' + names[kind] + '.') >= 0, kind + ': ' + t);
  }
});

test('the folder hint says the check is best-effort and the researcher confirms', async () => {
  assert.ok(/confirm/i.test(STRINGS['setup.project.folder_hint']));
  assert.ok(!/guarantee/i.test(STRINGS['setup.project.folder_hint'] + STRINGS['setup.project.sync_warning']));
});

// ------------------------------------------------------------------------------------------- item 22: Choose folder…

function chooserApp(available, chooseReply) {
  return load({
    answer: (url) => {
      if (url === '/api/project/folder-dialog') return reply(200, { available });
      if (url === '/api/project/choose-folder') return chooseReply();
      return reply(200, { project: null });
    },
  });
}

test('choose folder: one button beside each folder field, shown only when the app has a folder window', async () => {
  const { SS } = chooserApp(true, () => reply(200, { path: null }));
  const main = makeNode('main');
  await SS.setup.render(main, 'project');
  await settle();
  const forms = byTag(main, 'form');
  forms.forEach((form, i) => {
    const buttons = byClass(form, 'ss-setup-choose');
    assert.strictEqual(buttons.length, 1, 'form ' + i);
    assert.strictEqual(buttons[0].textContent, 'Choose folder…');
    assert.strictEqual(buttons[0].attributes.type, 'button', 'never submits the form');
    assert.strictEqual(buttons[0].hidden, false);
    const row = byClass(form, 'ss-setup-pathrow')[0];
    assert.strictEqual(row.children[0].attributes.name, 'path');
    assert.strictEqual(row.children[1], buttons[0]);
  });
  // the name field has no button
  assert.strictEqual(byClass(main, 'ss-setup-choose').length, 2);
});

test('choose folder: no folder window hides the buttons and asks for the full path', async () => {
  const { SS } = chooserApp(false, () => reply(500, {}));
  const main = makeNode('main');
  await SS.setup.render(main, 'project');
  await settle();
  byClass(main, 'ss-setup-choose').forEach((b) => assert.strictEqual(b.hidden, true));
  const notes = byClass(main, 'ss-setup-choose-note');
  assert.strictEqual(notes.length, 2);
  notes.forEach((n) => assert.strictEqual(n.textContent, STRINGS['setup.project.choose_none']));
});

test('choose folder: the chosen path only fills the field; cancel leaves it; a refusal is said', async () => {
  let answer = () => reply(200, { path: '/Users/ana/Studies/court' });
  const { SS, calls } = chooserApp(true, () => answer());
  const main = makeNode('main');
  await SS.setup.render(main, 'project');
  await settle();
  const form = byTag(main, 'form')[0];
  const input = byTag(form, 'input')[0];
  const button = byClass(form, 'ss-setup-choose')[0];
  button.fire('click');
  await settle();
  const posts = calls.filter((c) => c.method === 'POST');
  assert.deepStrictEqual(posts.map((c) => c.url), ['/api/project/choose-folder']);
  assert.deepStrictEqual(posts[0].body, {}, 'the request carries no text');
  assert.strictEqual(input.value, '/Users/ana/Studies/court');
  assert.strictEqual(calls.filter((c) => c.url === '/api/project/create').length, 0, 'nothing is created');
  assert.strictEqual(button.disabled, false);
  answer = () => reply(200, { path: null });
  button.fire('click');
  await settle();
  assert.strictEqual(input.value, '/Users/ana/Studies/court', 'cancel keeps what was there');
  answer = () => reply(409, { error: 'folder_dialog_open', message: STRINGS['setup.project.choose_open'] });
  button.fire('click');
  await settle();
  assert.strictEqual(byClass(form, 'ss-setup-choose-note')[0].textContent, STRINGS['setup.project.choose_open']);
  // the open form's button fills the open form's field only
  answer = () => reply(200, { path: '/Users/ana/Studies/old' });
  const openForm = byTag(main, 'form')[1];
  byClass(openForm, 'ss-setup-choose')[0].fire('click');
  await settle();
  assert.strictEqual(byTag(openForm, 'input')[0].value, '/Users/ana/Studies/old');
  assert.strictEqual(input.value, '/Users/ana/Studies/court');
});

test('choose folder: its strings follow the register', () => {
  const keys = Object.keys(STRINGS).filter((k) => k.startsWith('setup.project.choose'));
  assert.deepStrictEqual(keys.sort(), ['setup.project.choose', 'setup.project.choose_failed', 'setup.project.choose_none',
    'setup.project.choose_open', 'setup.project.choose_prompt', 'setup.project.choose_timeout']);
  for (const k of keys) {
    assert.ok(!/—|rather than|second scribe|\b(first|wrong|fail|failed|error|mistake|fix|correct)\b/i.test(STRINGS[k]), k);
    if (!/choose$|choose_prompt$/.test(k)) assert.ok(STRINGS[k].split(/\s+/).length >= 8, k + ' is a sentence');
  }
});

// --- item 30: a refresh keeps the scroll position, the focus and shows the result next to the control ----------
// A DOM stub that knows parents, focus and insertBefore, and a window that scrolls.
function makeLiveNode(tag, world) {
  const node = makeNode(tag);
  node.parentNode = null;
  node.style = {};
  node.offsetHeight = 0;
  node.appendChild = (c) => { c.parentNode = node; node.children.push(c); return c; };
  node.removeChild = (c) => { node.children.splice(node.children.indexOf(c), 1); c.parentNode = null; return c; };
  node.insertBefore = (c, ref) => { c.parentNode = node; node.children.splice(node.children.indexOf(ref), 0, c); return c; };
  node.focus = (opts) => { world.focused = node; world.focusOpts = opts; };
  return node;
}
function keepPlace(draws) {
  const world = { focused: null, scrolls: [], minHeights: [] };
  const make = (tag) => makeLiveNode(tag, world);
  const { SS, ctx } = load({ files: BASE, answer: () => reply(200, { project: summary() }) });
  ctx.scrollY = 640;
  ctx.scrollTo = (x, y) => world.scrolls.push([x, y]);
  let n = 0;
  SS.setup.step('demo', { order: 50, titleKey: 'setup.title', needsProject: true, render(container) {
    const doc = { createElement: make };
    const draw = draws[Math.min(n, draws.length - 1)];
    n += 1;
    while (container.firstChild) container.removeChild(container.firstChild);
    container.appendChild(doc.createElement('p')).textContent = 'Loading';
    return Promise.resolve().then(() => {
      while (container.firstChild) container.removeChild(container.firstChild);
      draw(doc, container);
    });
  } });
  const main = make('main');
  main.ownerDocument = { createElement: make };
  return { SS, main, world, make };
}
function card(doc, container, role, buttons) {
  const c = container.appendChild(doc.createElement('section'));
  c.setAttribute('data-role', role);
  const row = c.appendChild(doc.createElement('div'));
  row.setAttribute('class', 'ss-models-actions');
  return buttons.map((label, i) => {
    const b = row.appendChild(doc.createElement('button'));
    b.setAttribute('type', 'button');
    b.setAttribute('id', 'gen-' + Math.random());                  // generated ids differ on every draw
    b.textContent = label;
    return b;
  });
}

test('item 30: refresh keeps the page while it redraws, then the scroll, the focus and a result line', async () => {
  const draw = (doc, c) => { card(doc, c, 'recon', ['Check', 'Save']); card(doc, c, 'judge', ['Check', 'Save']); };
  const { SS, main, world } = keepPlace([draw]);
  await SS.setup.render(main, 'demo');
  await settle();
  const judgeSave = byTag(main, 'button')[3];
  SS.setup.remember(judgeSave);
  main.offsetHeight = 2400;
  const p = SS.setup.refresh({ message: 'Saved.' });
  assert.strictEqual(main.style.minHeight, '2400px');                      // the page keeps its height
  assert.ok(byTag(main, 'button').indexOf(judgeSave) >= 0, 'no Loading page in between');
  await p;
  await settle();
  const now = byTag(main, 'button');
  assert.ok(now.indexOf(judgeSave) < 0, 'the step was drawn again');
  assert.strictEqual(world.focused, now[3]);                                // the same control, found again
  assert.deepStrictEqual(JSON.parse(JSON.stringify(world.focusOpts)), { preventScroll: true });
  assert.deepStrictEqual(world.scrolls, [[0, 640]]);
  assert.strictEqual(main.style.minHeight, '');
  const note = byClass(main, 'ss-setup-after');
  assert.strictEqual(note.length, 1);
  assert.strictEqual(note[0].textContent, 'Saved.');
  assert.strictEqual(note[0].attributes.role, 'status');
  const judge = byTag(main, 'section').find((x) => x.attributes['data-role'] === 'judge');
  assert.strictEqual(judge.children[1], note[0], 'right after the row of buttons in the judge card');
});

test('item 30: a control that is gone hands the focus to its card, at the same place or at the top', async () => {
  const before = (doc, c) => { card(doc, c, 'recon', ['Check']); card(doc, c, 'judge', ['A', 'B', 'Use this model']); };
  const after = (doc, c) => { card(doc, c, 'recon', ['Check']); card(doc, c, 'judge', ['Test', 'Change', 'Remove']); };
  const r = keepPlace([before, after]);
  await r.SS.setup.render(r.main, 'demo');
  await settle();
  r.SS.setup.remember(byTag(r.main, 'button')[3]);                        // "Use this model", third in the judge card
  await r.SS.setup.refresh();
  await settle();
  assert.strictEqual(r.world.focused.textContent, 'Remove');              // same place in the card
  const shorter = (doc, c) => { card(doc, c, 'recon', ['Check']); card(doc, c, 'judge', ['Test']); };
  const r2 = keepPlace([before, shorter]);
  await r2.SS.setup.render(r2.main, 'demo');
  await settle();
  r2.SS.setup.remember(byTag(r2.main, 'button')[3]);
  await r2.SS.setup.refresh({ message: 'Done.' });
  await settle();
  assert.strictEqual(r2.world.focused.textContent, 'Test');               // the card's next step
  assert.strictEqual(byClass(r2.main, 'ss-setup-after').length, 1);
});

test('item 30: generated ids and numbers do not change what a control is', () => {
  const { SS } = load({ files: BASE });
  const a = makeNode('input');
  a.setAttribute('type', 'radio'); a.setAttribute('name', 'ss-models-kind-12'); a.setAttribute('value', 'local');
  a.setAttribute('id', 'x-1');
  const b = makeNode('input');
  b.setAttribute('type', 'radio'); b.setAttribute('name', 'ss-models-kind-31'); b.setAttribute('value', 'local');
  b.setAttribute('id', 'x-2');
  assert.strictEqual(SS.setup.keyOf(a), SS.setup.keyOf(b));
  b.setAttribute('value', 'anthropic');
  assert.notStrictEqual(SS.setup.keyOf(a), SS.setup.keyOf(b));
});

test('item 30: opening another step does not keep the old place', async () => {
  const draw = (doc, c) => { card(doc, c, 'recon', ['Check', 'Save']); };
  const { SS, main, world } = keepPlace([draw]);
  await SS.setup.render(main, 'demo');
  await settle();
  SS.setup.remember(byTag(main, 'button')[1]);
  await SS.setup.render(main, 'demo');                                      // a fresh visit, not a refresh
  await settle();
  await SS.setup.refresh();
  await settle();
  assert.strictEqual(world.focused, null);
});
