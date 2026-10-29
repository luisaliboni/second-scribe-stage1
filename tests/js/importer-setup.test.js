'use strict';
// Task 33: the Setup steps context, import, labels, rule and maps (app-setup-{context,import,labels,rule,maps}.js),
// loaded in the server's order into a vm context with a small stub DOM and a fake fetch.
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
    disabled: false, value: '', checked: false, style: {},
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
const byAttr = (node, k, v) => all(node, (n) => n.attributes[k] === v);

function appScripts() {
  const screens = fs.readdirSync(JS).filter((f) => /^app-[a-z0-9-]+\.js$/.test(f) && f !== 'app-shell.js').sort();
  return ['core.js', 'api.js', 'app-shell.js'].concat(screens);
}
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
      if (url === '/api/project') return Promise.resolve(reply(200, { project: PROJECT }));
      return Promise.resolve(answer(url, init));
    },
  };
  sandbox.window = sandbox;
  vm.createContext(sandbox);
  for (const f of appScripts()) vm.runInContext(fs.readFileSync(path.join(JS, f), 'utf8'), sandbox, { filename: f });
  return { SS: sandbox.SS, calls, errors, sandbox };
}
const tick = () => new Promise((r) => setTimeout(r, 0));
async function settle() { for (let i = 0; i < 20; i++) await tick(); }

const PROJECT = { name: 'Study', root: '/x', blocked: false, sync: { kind: null, acknowledged: false }, files: {},
  changed_files: [], log: { records: 1, head: 'b'.repeat(64), ok: true } };
const PAIRING = {
  rows: [{ pid: 'MAP-A1', transcript: 'MAP-A1.txt', map: 'MAP-A1.vsdx', paired: true, reference_options: ['A', 'B', 'C'] },
    { pid: 'MAP-B1', transcript: 'MAP-B1.txt', map: null, paired: false, reference_options: ['B', 'C'] }],
  table_sha256: 'c'.repeat(64), confirmed: true,
};
const PREVIEW = {
  pid: 'MAP-A1', map: 'MAP-A1.vsdx', suffix: '.vsdx', sha256: 'd'.repeat(64), changed: false,
  rule: { kind: 'red_line_or_text', colors: ['#ff0000'], class_names: ['missing'], inherit: false, default: true },
  phases: ['Planning', 'Permits'],
  nodes: [{ id: 'n2', text: 'Get the permit', status: 'happened', phase: 'Permits', phase_from: 'map', x: 5 },
    { id: 'n1', text: 'Ask at the office', status: 'happened', phase: 'Planning', phase_from: 'position', x: 1 },
    { id: 'n3', text: 'Fire inspection', status: 'happened', phase: 'Elsewhere', phase_from: 'map', x: 9 }],
  edges: [{ source: 'n1', target: 'n2', status: 'happened', label: null }],
  counts: { boxes: 3, missing: 0, missing_arrows: 0, arrows: 1, unresolved: 0 },
  counts_text: '3 boxes, 0 marked missing, 1 arrows',
  warnings: [{ code: 'zero_missing', message: STRINGS['importer.warning.zero_missing'] }],
  offer_inherit: true, confirmed: false,
};

test('the Task 33 steps register after the project step, in Setup order', () => {
  const { SS, errors } = load(() => reply(200, {}));
  assert.deepStrictEqual(errors, []);
  assert.deepStrictEqual(Array.from(SS.setup.list()).slice(0, 6),
    ['project', 'context', 'import', 'labels', 'rule', 'maps']);
});

test('context: roles are parsed as name: description, one per line', () => {
  const { SS } = load(() => reply(200, {}));
  assert.deepStrictEqual(JSON.parse(JSON.stringify(SS.setupContext.parseRoles('owner: owns the site\n\n builder : hired : yes\nlone'))),
    { owner: 'owns the site', builder: 'hired : yes', lone: '' });
  assert.deepStrictEqual(Array.from(SS.setupContext.lines(' a \n\nb\r\n')), ['a', 'b']);
});

test('import: base64 of a data URL and the pairing table with reference types', () => {
  const { SS } = load(() => reply(200, {}));
  assert.strictEqual(SS.setupImport.base64Of('data:text/plain;base64,SGVsbG8='), 'SGVsbG8=');
  assert.strictEqual(SS.setupImport.base64Of('data:application/octet-stream;base64,'), '');
  const box = makeNode('div');
  SS.setupImport.drawTable({ createElement: makeNode }, box, Object.assign({}, PAIRING, { confirmed: false }));
  const t = text(box);
  assert.ok(t.indexOf('A · B · C') >= 0 && t.indexOf('B · C') >= 0, t);
  assert.ok(t.indexOf(STRINGS['setup.import.confirm_pairing']) >= 0);
  assert.strictEqual(byAttr(box, 'data-paired', 'false').length, 1);
});

test('K2 U05: a transcript identical to another is flagged in the pairing table, as a warning only', () => {
  const { SS } = load(() => reply(200, {}));
  const rows = [
    { pid: 'CASE-01', transcript: 'a.docx', map: null, paired: false, reference_options: ['B', 'C'], identical_to: ['b.docx'] },
    { pid: 'CASE-02', transcript: 'b.docx', map: null, paired: false, reference_options: ['B', 'C'], identical_to: ['a.docx'] },
    { pid: 'CASE-03', transcript: 'c.docx', map: null, paired: false, reference_options: ['B', 'C'], identical_to: [] },
  ];
  const box = makeNode('div');
  SS.setupImport.drawTable({ createElement: makeNode }, box, { rows, confirmed: false, table_sha256: 'x' });
  assert.strictEqual(byAttr(box, 'data-identical', 'true').length, 2);
  const t = text(box);
  assert.ok(t.indexOf('Identical to b.docx') >= 0 && t.indexOf('Identical to a.docx') >= 0, t);
  assert.strictEqual(t.split('Identical to').length - 1, 2);
  // nothing is hidden or blocked: all three rows and the confirm button are still there
  assert.ok(t.indexOf('CASE-03') >= 0 && t.indexOf(STRINGS['setup.import.confirm_pairing']) >= 0);
  assert.ok(/exactly the same content as \{name\}/.test(STRINGS['setup.import.identical_warning']));
});

test('rule: the Visio inherited-red choice sends inherit true, other kinds never send it', () => {
  const { SS } = load(() => reply(200, {}));
  assert.strictEqual(SS.setupRule.suffixOf('MAP-A1.mapgraph.json'), '.mapgraph.json');
  assert.strictEqual(SS.setupRule.suffixOf('Map.VSDX'), '.vsdx');
  assert.deepStrictEqual(JSON.parse(JSON.stringify(SS.setupRule.body('.vsdx', 'red_line_or_text', '#FF0000, #c00000', '', true))),
    { suffix: '.vsdx', kind: 'red_line_or_text', colors: ['#FF0000', '#c00000'], inherit: true });
  assert.deepStrictEqual(JSON.parse(JSON.stringify(SS.setupRule.body('.drawio', 'dashed', '#ff0000', '', true))),
    { suffix: '.drawio', kind: 'dashed' });
  assert.deepStrictEqual(JSON.parse(JSON.stringify(SS.setupRule.body('.mmd', 'mermaid_class', '', 'missing gap', false))),
    { suffix: '.mmd', kind: 'mermaid_class', class_names: ['missing', 'gap'] });
});

test('rule step: only formats in use, with the inherited-red option for Visio', () => {
  const { SS } = load(() => reply(200, {}));
  const box = makeNode('div');
  box.ownerDocument = { createElement: makeNode };
  const view = { rules: { '.vsdx': PREVIEW.rule, '.mmd': { kind: 'mermaid_class', class_names: ['missing'] } },
    options: { '.vsdx': ['red_line_or_text', 'red_fill', 'dashed'], '.mmd': ['mermaid_class', 'classdef_style'] } };
  SS.setupRule.draw(box, view, PAIRING);
  assert.strictEqual(byAttr(box, 'data-suffix', '.vsdx').length, 1);
  assert.strictEqual(byAttr(box, 'data-suffix', '.mmd').length, 0);
  assert.ok(text(box).indexOf(STRINGS['setup.rule.kind.red_inherit']) >= 0);
});

test('maps step: boxes on phase columns, missing shown by shape and tag, zero-missing offers inherited red', async () => {
  const { SS, calls } = load((url) => {
    if (url.indexOf('/api/pairing') === 0) return reply(200, PAIRING);
    if (url.indexOf('/api/maps/preview') === 0) return reply(200, PREVIEW);
    return reply(200, {});
  });
  const main = makeNode('main');
  main.ownerDocument = { createElement: makeNode };
  SS.setup.render(main, 'maps');
  await settle();
  const t = text(main);
  assert.ok(t.indexOf('3 boxes, 0 marked missing, 1 arrows') >= 0, t);
  assert.ok(t.indexOf(STRINGS['importer.warning.zero_missing']) >= 0);
  assert.ok(t.indexOf(STRINGS['setup.maps.other_phase']) >= 0);          // a phase that is not a study phase
  const cols = byClass(main, 'ss-setup-mapcol');
  assert.deepStrictEqual(cols.map((c) => c.children[0].textContent), ['Planning', 'Permits', STRINGS['setup.maps.other_phase']]);
  assert.strictEqual(byClass(main, 'is-missing').length, 0);
  const inherit = byAttr(main, 'data-action', 'inherit');
  assert.strictEqual(inherit.length, 1);
  inherit[0].fire('click');
  await settle();
  const post = calls.filter((c) => c.method === 'POST' && c.url === '/api/missing-rule');
  assert.deepStrictEqual(post.map((c) => c.body), [{ suffix: '.vsdx', kind: 'red_line_or_text', colors: ['#ff0000'], inherit: true }]);
  byAttr(main, 'data-action', 'confirm')[0].fire('click');
  await settle();
  const conf = calls.filter((c) => c.url === '/api/maps/confirm');
  assert.deepStrictEqual(conf.map((c) => c.body), [{ pid: 'MAP-A1', sha256: 'd'.repeat(64) }]);
  // only paired rows get a preview
  assert.deepStrictEqual(Array.from(new Set(calls.filter((c) => c.url.indexOf('/api/maps/preview') === 0).map((c) => c.url))),
    ['/api/maps/preview?pid=MAP-A1']);
});

test('maps step: a missing box is dashed and tagged, and columns keep the left-to-right order', () => {
  const { SS } = load(() => reply(200, {}));
  const prev = Object.assign({}, PREVIEW, {
    nodes: [{ id: 'b', text: 'Later', status: 'missing', phase: 'Planning', x: 8 },
      { id: 'a', text: 'Earlier', status: 'happened', phase: 'Planning', x: 2 }],
    warnings: [{ code: 'zero_missing', message: 'x' }], offer_inherit: false,      // e.g. a Mermaid map
  });
  const cols = SS.setupMaps.columns(prev);
  assert.deepStrictEqual(JSON.parse(JSON.stringify(cols[0].nodes.map((n) => n.id))), ['a', 'b']);
  const main = makeNode('div');
  main.ownerDocument = { createElement: makeNode };
  SS.setupMaps.draw(main, PAIRING, [{ pid: 'MAP-A1', preview: prev }]);
  const missing = byClass(main, 'is-missing');
  assert.strictEqual(missing.length, 1);
  assert.strictEqual(missing[0].attributes['data-status'], 'missing');
  assert.ok(text(missing[0]).indexOf(STRINGS['setup.maps.missing_tag']) >= 0);
  assert.strictEqual(byAttr(main, 'data-action', 'inherit').length, 0);
});

test('maps step: boxes flagged "phase heading?" sit apart from the steps, still drawn, and the full count is shown', () => {
  const { SS } = load(() => reply(200, {}));
  const prev = Object.assign({}, PREVIEW, {
    nodes: [{ id: 'h1', text: 'Planning', status: 'happened', phase: 'Planning', x: 0, phase_heading: true },
      { id: 'a', text: 'Ask at the office', status: 'happened', phase: 'Planning', x: 2, phase_heading: false },
      { id: 'h2', text: 'Permits', status: 'happened', phase: 'Permits', x: 4, phase_heading: true },
      { id: 'b', text: 'Get the permit', status: 'happened', phase: 'Permits', x: 6 }],   // no flag field: a step
    counts: { boxes: 4, missing: 0, missing_arrows: 0, arrows: 0, unresolved: 0 },
    phase_headings: 2, edges: [], offer_inherit: false,
    counts_text: '2 boxes, plus 2 that look like phase headings, 0 marked missing, 0 arrows',
  });
  const cols = SS.setupMaps.columns(prev);
  assert.deepStrictEqual(JSON.parse(JSON.stringify(cols.map((c) => [c.nodes.map((n) => n.id), c.headings.map((n) => n.id)]))),
    [[['a'], ['h1']], [['b'], ['h2']]]);
  const main = makeNode('div');
  main.ownerDocument = { createElement: makeNode };
  SS.setupMaps.draw(main, PAIRING, [{ pid: 'MAP-A1', preview: prev }]);
  const t = text(main);
  assert.ok(t.indexOf('2 boxes, plus 2 that look like phase headings, 0 marked missing, 0 arrows') >= 0, t);
  assert.ok(t.indexOf(STRINGS['setup.maps.headings_note']) >= 0);
  const heads = byAttr(main, 'data-phase-heading', 'true');
  assert.deepStrictEqual(heads.map((h) => h.attributes['data-node-id']), ['h1', 'h2']);      // still drawn, 2 of the 4
  heads.forEach((h) => assert.ok(text(h).indexOf(STRINGS['setup.maps.heading_tag']) >= 0));
  assert.strictEqual(byClass(main, 'ss-setup-mapcol-headings').length, 2);
  // the steps' own list holds only the steps
  const stepLists = byClass(main, 'ss-setup-mapcol-list');
  assert.deepStrictEqual(stepLists.map((l) => l.children.map((b) => b.attributes['data-node-id'])), [['a'], ['b']]);
  assert.strictEqual(byAttr(main, 'data-action', 'confirm').length, 1);                      // the user still confirms
  // nothing flagged: no note, no separate list
  const plain = makeNode('div');
  plain.ownerDocument = { createElement: makeNode };
  SS.setupMaps.draw(plain, PAIRING, [{ pid: 'MAP-A1', preview: PREVIEW }]);
  assert.strictEqual(byClass(plain, 'ss-setup-mapcol-headings').length, 0);
  assert.ok(text(plain).indexOf(STRINGS['setup.maps.headings_note']) < 0);
});

test('labels step: a timestamp prefix is shown with the label and its warning; confirm sends the three choices', async () => {
  const LABELS = {
    pid: 'MAP-A1', format: 'none', example_url: '/static/examples/labelled-transcript.txt', confirmed: null,
    candidates: [{ label: 'Interviewer', turns: 3, shown: 'Interviewer:', prefix: null },
      { label: 'Participant', turns: 3, shown: 'Participant 3 (00:04:12):', prefix: '3 (00:04:12):' }],
    warnings: [{ code: 'timestamp_prefix', label: 'Participant', prefix: '3 (00:04:12):', message: 'stays in the text' }],
  };
  const { SS, calls } = load((url) => {
    if (url.indexOf('/api/pairing') === 0) return reply(200, PAIRING);
    if (url.indexOf('/api/context') === 0) return reply(200, { context: { roles: { owner: 'x', builder: 'y' } } });
    if (url.indexOf('/api/labels') === 0) return reply(200, Object.assign({}, LABELS, { pid: url.split('=')[1] }));
    return reply(200, {});
  });
  const main = makeNode('main');
  main.ownerDocument = { createElement: makeNode };
  SS.setup.render(main, 'labels');
  await settle();
  const t = text(main);
  assert.ok(t.indexOf('Participant 3 (00:04:12):') >= 0, t);
  assert.ok(t.indexOf('stays in the text') >= 0);
  assert.ok(t.indexOf(STRINGS['setup.labels.none_help']) >= 0);
  const card = byAttr(main, 'data-pid', 'MAP-A1')[0];
  const selects = all(card, (n) => n.tagName === 'SELECT');
  assert.strictEqual(selects.length, 3);
  selects[0].value = 'Participant';
  selects[1].value = 'Interviewer';
  selects[2].value = 'owner';
  all(card, (n) => n.tagName === 'BUTTON')[0].fire('click');
  await settle();
  const post = calls.filter((c) => c.url === '/api/labels/confirm');
  assert.deepStrictEqual(post.map((c) => c.body), [{ pid: 'MAP-A1', participant: 'Participant', interviewer: 'Interviewer', role: 'owner' }]);
  assert.deepStrictEqual(Array.from(new Set(calls.filter((c) => c.url.indexOf('/api/labels?') === 0).map((c) => c.url))).sort(),
    ['/api/labels?pid=MAP-A1', '/api/labels?pid=MAP-B1']);
});

test('issue 5 labels step: the role dropdown says "Pick a role", the two label dropdowns keep "Pick a label"', async () => {
  const LABELS = {
    pid: 'MAP-A1', format: 'none', example_url: '/static/examples/labelled-transcript.txt', confirmed: null,
    candidates: [{ label: 'Interviewer', turns: 3, shown: 'Interviewer:', prefix: null },
      { label: 'Participant', turns: 3, shown: 'Participant:', prefix: null }], warnings: [],
  };
  const { SS } = load((url) => {
    if (url.indexOf('/api/pairing') === 0) return reply(200, PAIRING);
    if (url.indexOf('/api/context') === 0) return reply(200, { context: { roles: { owner: 'x', builder: 'y' } } });
    if (url.indexOf('/api/labels') === 0) return reply(200, Object.assign({}, LABELS, { pid: url.split('=')[1] }));
    return reply(200, {});
  });
  const main = makeNode('main');
  main.ownerDocument = { createElement: makeNode };
  SS.setup.render(main, 'labels');
  await settle();
  const card = byAttr(main, 'data-pid', 'MAP-A1')[0];
  const selects = all(card, (n) => n.tagName === 'SELECT');
  const firstOption = (sel) => sel.children[0].textContent;
  assert.deepStrictEqual(selects.map(firstOption), ['Pick a label', 'Pick a label', 'Pick a role']);
  assert.ok(!/Pick a label/.test(firstOption(selects[2])));
  assert.strictEqual(STRINGS['setup.labels.pick_role'], 'Pick a role');
});

test('labels step: the confirmed line names the labels as written in the transcript, not their matching keys', () => {
  const prev = {
    pid: 'MAP-B1', format: 'bracket', example_url: '/static/examples/labelled-transcript.txt',
    candidates: [{ label: 'i', turns: 5, shown: '[I]', prefix: null }, { label: 'p', turns: 5, shown: '[P]', prefix: null }],
    warnings: [],
    confirmed: { participant: 'p', interviewer: 'i', role: 'builder', format: 'bracket', turns: { participant: 5, interviewer: 5 } },
  };
  const { SS } = load(() => reply(200, {}));
  const main = makeNode('div');
  main.ownerDocument = { createElement: makeNode };
  SS.setupLabels.draw(main, PAIRING, [{ pid: 'MAP-B1', preview: prev }], { builder: 'x' });
  const ok = byClass(main, 'ss-setup-ok').map(text).join(' ');
  assert.ok(ok.indexOf('participant [P]') >= 0 && ok.indexOf('interviewer [I]') >= 0, ok);
});

// 2.2 follow-up: one turn reads "1 turn", in the candidate list and in the confirmed line.
test('R2 labels step: one turn is singular, in the candidate list and in the confirmed line', () => {
  const prev = {
    pid: 'MAP-C1', format: 'bracket', example_url: '/static/examples/labelled-transcript.txt',
    candidates: [{ label: 'iv', turns: 1, shown: '[IV]', prefix: null }, { label: 'pt', turns: 2, shown: '[PT]', prefix: null }],
    warnings: [],
    confirmed: { participant: 'pt', interviewer: 'iv', role: 'owner', format: 'bracket', turns: { participant: 2, interviewer: 1 } },
  };
  const { SS } = load(() => reply(200, {}));
  const main = makeNode('div');
  main.ownerDocument = { createElement: makeNode };
  SS.setupLabels.draw(main, PAIRING, [{ pid: 'MAP-C1', preview: prev }], { owner: 'x' });
  const t = text(main);
  assert.ok(t.indexOf('(1 turn)') >= 0 && t.indexOf('(2 turns)') >= 0, t);
  assert.ok(t.indexOf('1 turns') < 0, t);
  const ok = byClass(main, 'ss-setup-ok').map(text).join(' ');
  assert.ok(ok.indexOf('participant [PT] (2 turns), interviewer [IV] (1 turn), role owner.') >= 0, ok);
  assert.strictEqual(STRINGS['setup.labels.turns_one'], '({n} turn)');
  assert.strictEqual(STRINGS['setup.labels.n_turns_one'], '{n} turn');
});

// 2.2 follow-up: the stored confirmed label is whatever was sent ("PT"); the server matches labels without case or
// punctuation, so the dropdowns must too. Before, the two label dropdowns rendered blank for such a project.
test('R3 labels step: a confirmed label that differs from the detected one only in case or punctuation is shown as selected', () => {
  const cands = [{ label: 'iv', turns: 11, shown: '[IV]', prefix: null }, { label: 'pt', turns: 10, shown: '[PT]', prefix: null }];
  const draw = (confirmed) => {
    const prev = { pid: 'MAP-C1', format: 'bracket', example_url: '/x', candidates: cands, warnings: [], confirmed };
    const { SS } = load(() => reply(200, {}));
    const main = makeNode('div');
    main.ownerDocument = { createElement: makeNode };
    SS.setupLabels.draw(main, PAIRING, [{ pid: 'MAP-C1', preview: prev }], { owner: 'x' });
    const selects = all(main, (n) => n.tagName === 'SELECT');
    return selects.map((s) => ({
      value: s.value,
      selected: s.children.filter((o) => o.attributes.selected === 'selected').map((o) => o.textContent),
    }));
  };
  const turns = { participant: 10, interviewer: 11 };
  // sent as the label is printed in the transcript, uppercase
  assert.deepStrictEqual(draw({ participant: 'PT', interviewer: 'IV', role: 'owner', format: 'bracket', turns }),
    [{ value: 'pt', selected: ['[PT]'] }, { value: 'iv', selected: ['[IV]'] }, { value: 'owner', selected: ['owner'] }]);
  // sent with its brackets
  assert.deepStrictEqual(draw({ participant: '[PT]', interviewer: '[iv]', role: 'owner', format: 'bracket', turns }).slice(0, 2),
    [{ value: 'pt', selected: ['[PT]'] }, { value: 'iv', selected: ['[IV]'] }]);
  // the exact value still wins, and a label that is none of the options selects nothing (the placeholder shows)
  assert.deepStrictEqual(draw({ participant: 'pt', interviewer: 'iv', role: 'owner', format: 'bracket', turns }).slice(0, 2),
    [{ value: 'pt', selected: ['[PT]'] }, { value: 'iv', selected: ['[IV]'] }]);
  assert.deepStrictEqual(draw({ participant: 'Nobody', interviewer: 'iv', role: 'owner', format: 'bracket', turns }).slice(0, 2),
    [{ value: '', selected: [] }, { value: 'iv', selected: ['[IV]'] }]);
});

test('R3 labels step: with two detected labels that differ only in case, the exact one is chosen', () => {
  const prev = {
    pid: 'MAP-C1', format: 'first_token', example_url: '/x', warnings: [],
    candidates: [{ label: 'Participant', turns: 3, shown: 'Participant:', prefix: null },
      { label: 'participant', turns: 1, shown: 'participant:', prefix: null }, { label: 'Interviewer', turns: 4, shown: 'Interviewer:', prefix: null }],
    confirmed: { participant: 'participant', interviewer: 'INTERVIEWER', role: 'owner', format: 'first_token', turns: { participant: 1, interviewer: 4 } },
  };
  const { SS } = load(() => reply(200, {}));
  const main = makeNode('div');
  main.ownerDocument = { createElement: makeNode };
  SS.setupLabels.draw(main, PAIRING, [{ pid: 'MAP-C1', preview: prev }], { owner: 'x' });
  const selects = all(main, (n) => n.tagName === 'SELECT');
  assert.strictEqual(selects[0].value, 'participant');
  assert.strictEqual(selects[1].value, 'Interviewer');
});

// ------------------------------------------------------------------------------------------- item 24: drag and drop

// A synthetic DataTransfer: files (name, text) and folders, as Chrome and Safari hand them over (items with entries).
function fakeFile(name, body) { return { name, body: body || 'Interviewer: hello\nParticipant: hi\n' }; }
function transfer(files, folders, opts) {
  opts = opts || {};
  const items = files.map((f) => ({ kind: 'file', getAsFile: () => f, webkitGetAsEntry: () => ({ isDirectory: false, name: f.name }) }))
    .concat((folders || []).map((name) => ({ kind: 'file', getAsFile: () => null, webkitGetAsEntry: () => ({ isDirectory: true, name }) })));
  return { types: ['Files'], files, items: opts.noItems ? undefined : items, dropEffect: 'none' };
}
function event(dt) {
  const ev = { dataTransfer: dt, prevented: false, stopped: false };
  ev.preventDefault = () => { ev.prevented = true; };
  ev.stopPropagation = () => { ev.stopped = true; };
  return ev;
}
function send(node, type, ev) { (node.listeners[type] || []).forEach((fn) => fn(ev)); return ev; }

function importApp() {
  const docListeners = {};
  const b = load((url, init) => {
    if (url === '/api/pairing') return reply(200, PAIRING);
    if (url === '/api/import') {
      const body = JSON.parse(init.body);
      if (/\.(pdf|png)$/i.test(body.name)) return reply(422, { error: 'refused_format', message: STRINGS['importer.error.refused_format'] });
      return reply(200, { kind: body.kind, name: body.name });
    }
    return reply(200, {});
  });
  b.sandbox.FileReader = function () {
    const r = this;
    r.readAsDataURL = (f) => setTimeout(() => { r.result = 'data:text/plain;base64,' + Buffer.from(f.body).toString('base64'); r.onload(); }, 0);
  };
  b.sandbox.document.addEventListener = (type, fn) => { (docListeners[type] = docListeners[type] || []).push(fn); };
  b.docListeners = docListeners;
  return b;
}
async function renderImport(b) {
  const main = makeNode('main');
  await b.SS.setup.render(main, 'import');
  await settle();
  return main;
}

test('drop: one drop area per kind, with its sentence, and the file input still inside', async () => {
  const b = importApp();
  const main = await renderImport(b);
  const areas = byClass(main, 'ss-setup-drop');
  assert.deepStrictEqual(areas.map((a) => a.attributes['data-kind']), ['transcript', 'map']);
  areas.forEach((a) => {
    assert.strictEqual(byClass(a, 'ss-setup-drop-text')[0].textContent, STRINGS['setup.import.drop']);
    const input = all(a, (n) => n.tagName === 'INPUT')[0];
    assert.strictEqual(input.attributes.type, 'file');
    assert.ok(input.listeners.change && input.listeners.change.length, 'click and keyboard still choose files');
    assert.strictEqual(a.attributes['data-over'], 'false');
  });
  assert.deepStrictEqual(b.errors, []);
});

test('drop: the area lights while files are over it and goes dark on leave or drop', async () => {
  const b = importApp();
  const area = byClass(await renderImport(b), 'ss-setup-drop')[0];
  const dt = transfer([fakeFile('A1.txt')]);
  assert.ok(send(area, 'dragenter', event(dt)).prevented);
  assert.strictEqual(area.attributes['data-over'], 'true');
  const over = send(area, 'dragover', event(dt));
  assert.ok(over.prevented, 'dragover must be cancelled for the drop to happen');
  assert.strictEqual(dt.dropEffect, 'copy');
  send(area, 'dragenter', event(dt));                 // into a child
  send(area, 'dragleave', event(dt));                 // out of the child: still over the area
  assert.strictEqual(area.attributes['data-over'], 'true');
  send(area, 'dragleave', event(dt));
  assert.strictEqual(area.attributes['data-over'], 'false');
  // text being dragged (not files) is left alone
  const textDrag = send(area, 'dragover', event({ types: ['text/plain'] }));
  assert.strictEqual(textDrag.prevented, false);
  assert.strictEqual(area.attributes['data-over'], 'false');
});

test('drop: dropped transcripts and maps are sent like chosen ones, each to its own kind', async () => {
  const b = importApp();
  const main = await renderImport(b);
  const [tArea, mArea] = byClass(main, 'ss-setup-drop');
  send(tArea, 'drop', event(transfer([fakeFile('MAP-A1.txt'), fakeFile('MAP-B1.docx')])));
  await settle();
  send(mArea, 'drop', event(transfer([fakeFile('MAP-A1.vsdx', 'x')])));
  await settle();
  const posts = b.calls.filter((c) => c.url === '/api/import').map((c) => [c.body.kind, c.body.name]);
  assert.deepStrictEqual(posts, [['transcript', 'MAP-A1.txt'], ['transcript', 'MAP-B1.docx'], ['map', 'MAP-A1.vsdx']]);
  assert.strictEqual(b.calls.find((c) => c.url === '/api/import').body.data_b64,
    Buffer.from('Interviewer: hello\nParticipant: hi\n').toString('base64'));
  // item 25: a file that imported cleanly gets no line of its own; each picker's status line counts them
  assert.strictEqual(byAttr(main, 'data-ok', 'true').length, 0);
  assert.deepStrictEqual(byClass(main, 'ss-setup-import-status').map((p) => p.textContent),
    ['2 of 2 files imported. Transcripts in the project: 2.', '1 of 1 file imported. Maps in the project: 1.']);
  assert.ok(b.calls.filter((c) => c.url === '/api/pairing').length >= 4, 'the pairing table reloads after each file');
  assert.strictEqual(tArea.attributes['data-over'], 'false');
});

test('drop: PDF and images get the same refusal as chosen ones; a folder is named and not opened', async () => {
  const b = importApp();
  const main = await renderImport(b);
  const area = byClass(main, 'ss-setup-drop')[0];
  const ev = send(area, 'drop', event(transfer([fakeFile('scan.pdf'), fakeFile('photo.png'), fakeFile('A2.txt')], ['Interviews'])));
  assert.ok(ev.prevented && ev.stopped);
  await settle();
  assert.deepStrictEqual(b.calls.filter((c) => c.url === '/api/import').map((c) => c.body.name), ['scan.pdf', 'photo.png', 'A2.txt']);
  const bad = byAttr(main, 'data-ok', 'false');
  const said = bad.map((li) => [li.children[0].textContent, li.children[1].textContent]);
  assert.deepStrictEqual(said, [['Interviews', STRINGS['setup.import.drop_folder']],
    ['scan.pdf', STRINGS['importer.error.refused_format']], ['photo.png', STRINGS['importer.error.refused_format']]]);
  // a browser without entries: every dropped file goes to the server, which judges it
  const plain = b.SS.setupImport.dropped(transfer([fakeFile('x.txt')], [], { noItems: true }));
  assert.deepStrictEqual(Array.from(plain.files).map((f) => f.name), ['x.txt']);
  assert.deepStrictEqual(Array.from(plain.folders), []);
});

test('drop: a file dropped beside the areas does not make the browser leave the app', async () => {
  const b = importApp();
  await renderImport(b);
  for (const type of ['dragover', 'drop']) {
    const evFiles = event({ types: ['Files'] });
    (b.docListeners[type] || []).forEach((fn) => fn(evFiles));
    assert.ok(evFiles.prevented, type);
    const evText = event({ types: ['text/plain'] });
    (b.docListeners[type] || []).forEach((fn) => fn(evText));
    assert.strictEqual(evText.prevented, false, type + ' of text is left alone');
  }
});

test('drop: its strings follow the register', () => {
  for (const k of ['setup.import.drop', 'setup.import.drop_folder']) {
    assert.ok(!/—|rather than|second scribe|\b(first|wrong|fail|failed|error|mistake|fix|correct)\b/i.test(STRINGS[k]), k);
    assert.ok(STRINGS[k].split(/\s+/).length >= 8, k);
  }
});

// ------------------------------------------------------------------------------------- item 25: the list refreshes

// A fake server that keeps what was imported, as the real one does, and answers /api/pairing from it.
function statefulImportApp(refuse) {
  const held = {};                                  // pid -> {transcript, map}
  const b = load((url, init) => {
    if (url === '/api/pairing') {
      const rows = Object.keys(held).sort().map((pid) => ({ pid, transcript: held[pid].transcript || null,
        map: held[pid].map || null, paired: !!(held[pid].transcript && held[pid].map),
        reference_options: held[pid].transcript ? ['B', 'C'] : [], identical_to: [] }));
      return reply(200, { rows, table_sha256: 'e'.repeat(64), confirmed: false });
    }
    if (url === '/api/import') {
      const body = JSON.parse(init.body);
      if (refuse && refuse(body)) return reply(422, { error: 'refused_format', message: STRINGS['importer.error.refused_format'] });
      const pid = body.name.replace(/\.[^.]+$/, '');
      const had = held[pid] && held[pid][body.kind];
      held[pid] = Object.assign(held[pid] || {}, { [body.kind]: body.name });
      return reply(200, { file: { kind: body.kind, pid }, replaced: had || null });
    }
    return reply(200, {});
  });
  b.sandbox.FileReader = function () {
    const r = this;
    r.readAsDataURL = (f) => setTimeout(() => { r.result = 'data:text/plain;base64,' + Buffer.from(f.body).toString('base64'); r.onload(); }, 0);
  };
  return b;
}
const TWELVE = ['OF01', 'OF02', 'OF03', 'OF04', 'OF05', 'OW01', 'OW02', 'OW03', 'OW04', 'OW05', 'OW06', 'OW07'].map((x) => 'SYN-' + x);
function choose(main, kind, names) {
  const area = byAttr(main, 'data-kind', kind)[0];
  const input = all(area, (n) => n.tagName === 'INPUT')[0];
  input.files = names.map((n) => fakeFile(n, 'x'));
  input.fire('change');
  return area;
}

test('item 25: 12 transcripts, then 12 maps in one selection: the map picker says so and the table lists all 24', async () => {
  const b = statefulImportApp();
  const main = makeNode('main');
  await b.SS.setup.render(main, 'import');
  await settle();
  const tArea = choose(main, 'transcript', TWELVE.map((p) => p + '.docx'));
  for (let i = 0; i < 6; i++) await settle();
  assert.strictEqual(byClass(tArea, 'ss-setup-import-status')[0].textContent,
    '12 of 12 files imported. Transcripts in the project: 12.');
  const mArea = choose(main, 'map', TWELVE.map((p) => p + '.vsdx'));
  for (let i = 0; i < 6; i++) await settle();
  assert.strictEqual(byClass(mArea, 'ss-setup-import-status')[0].textContent,
    '12 of 12 files imported. Maps in the project: 12.');
  const rows = all(byClass(main, 'ss-setup-pairing')[0], (n) => n.tagName === 'TR' && n.attributes['data-paired'] !== undefined);
  assert.strictEqual(rows.length, 12);
  assert.ok(rows.every((tr) => tr.attributes['data-paired'] === 'true'), 'every transcript sits beside its map');
  assert.ok(text(byClass(main, 'ss-setup-pairing')[0]).indexOf('Transcripts in the project: 12. Maps in the project: 12.') >= 0);
  // no line per clean file: the result stays the size of one status line, inside each picker's own box
  assert.strictEqual(byClass(main, 'ss-setup-import-line').length, 0);
  // the table reloaded after every file (24) on top of the first draw
  assert.ok(b.calls.filter((c) => c.url === '/api/pairing').length >= 25);
  assert.deepStrictEqual(b.errors, []);
});

test('item 25: the table follows the pickers, with no growing list between them', async () => {
  const b = statefulImportApp();
  const main = makeNode('main');
  await b.SS.setup.render(main, 'import');
  await settle();
  const body = byClass(main, 'ss-setup-step-body')[0];
  const classes = body.children.map((c) => (c.attributes.class || c.tagName));
  const forms = classes.indexOf('ss-setup-forms');
  const pairing = classes.indexOf('ss-setup-pairing');
  assert.ok(forms >= 0 && pairing === forms + 2, classes.join(', '));     // pickers, the two download links, table
  byClass(main, 'ss-setup-import-list').forEach((l) => assert.ok(byClass(main, 'ss-setup-picker').some((p) => all(p, (n) => n === l).length)));
});

test('item 25: a refused file and a replaced file each get a line; the table still reloads after them', async () => {
  const b = statefulImportApp((body) => /\.pdf$/.test(body.name));
  const main = makeNode('main');
  await b.SS.setup.render(main, 'import');
  await settle();
  choose(main, 'map', ['SYN-OW01.vsdx']);
  for (let i = 0; i < 3; i++) await settle();
  const before = b.calls.filter((c) => c.url === '/api/pairing').length;
  const area = choose(main, 'map', ['scan.pdf', 'SYN-OW01.vsdx', 'SYN-OW02.vsdx']);
  for (let i = 0; i < 4; i++) await settle();
  assert.strictEqual(b.calls.filter((c) => c.url === '/api/pairing').length - before, 3);
  const lines = byClass(area, 'ss-setup-import-line').map((li) => [li.attributes['data-ok'], text(li).trim()]);
  assert.deepStrictEqual(lines.map((l) => l[0]), ['false', 'true']);
  assert.ok(lines[0][1].indexOf(STRINGS['importer.error.refused_format']) >= 0);
  assert.ok(lines[1][1].indexOf('replaces SYN-OW01.vsdx') >= 0, lines[1][1]);
  assert.strictEqual(byClass(area, 'ss-setup-import-status')[0].textContent,
    '2 of 3 files imported. 1 file was not imported. It is listed below with the reason. Maps in the project: 2.');
  // a new batch clears the last batch's lines
  choose(main, 'map', ['SYN-OW03.vsdx']);
  for (let i = 0; i < 3; i++) await settle();
  assert.strictEqual(byClass(area, 'ss-setup-import-line').length, 0);
});

test('item 25: its strings follow the register', () => {
  for (const k of ['setup.import.progress', 'setup.import.batch_done', 'setup.import.batch_done_one', 'setup.import.batch_failed',
    'setup.import.batch_failed_one', 'setup.import.held_transcript', 'setup.import.held_map', 'setup.import.counts']) {
    assert.ok(typeof STRINGS[k] === 'string', k);
    assert.ok(!/—|rather than|second scribe|\b(first|wrong|fail|failed|error|mistake|fix|correct)\b/i.test(STRINGS[k]), k);
  }
});

// ------------------------------------------------------------------------------------------- item 26: Remove a file

function removeApp(refusal) {
  let rows = [{ pid: 'SYN-OW01', transcript: 'SYN-OW01.docx', map: 'SYN-OW01.vsdx', paired: true, reference_options: ['A', 'B', 'C'], identical_to: [] },
    { pid: 'SYN-OW02', transcript: 'SYN-OW02.docx', map: null, paired: false, reference_options: ['B', 'C'], identical_to: [] }];
  const b = load((url, init) => {
    if (url === '/api/pairing') return reply(200, { rows, table_sha256: 'f'.repeat(64), confirmed: true });
    if (url === '/api/import/remove') {
      if (refusal) return reply(409, { error: 'run_in_progress', message: STRINGS['importer.error.run_in_progress'] });
      const body = JSON.parse(init.body);
      rows = rows.map((r) => (r.pid === body.pid ? Object.assign({}, r, { [body.kind]: null }) : r)).filter((r) => r.transcript || r.map);
      return reply(200, { removed: { kind: body.kind, pid: body.pid, name: body.pid + (body.kind === 'map' ? '.vsdx' : '.docx') },
        reset: { pairing: true, labels: body.kind === 'transcript', map: body.kind === 'map' } });
    }
    return reply(200, {});
  });
  return b;
}
const removes = (main) => byClass(main, 'ss-setup-remove');

test('item 26: one Remove button per imported file, named for that file', async () => {
  const b = removeApp();
  const main = makeNode('main');
  await b.SS.setup.render(main, 'import');
  await settle();
  assert.deepStrictEqual(removes(main).map((x) => [x.attributes['data-kind'], x.attributes['data-pid'], x.attributes['aria-label'], x.attributes.type]),
    [['transcript', 'SYN-OW01', 'Remove SYN-OW01.docx', 'button'], ['map', 'SYN-OW01', 'Remove SYN-OW01.vsdx', 'button'],
      ['transcript', 'SYN-OW02', 'Remove SYN-OW02.docx', 'button']]);
});

test('item 26: Remove asks in the page; "Keep it" closes the question and nothing is sent', async () => {
  const b = removeApp();
  const main = makeNode('main');
  await b.SS.setup.render(main, 'import');
  await settle();
  assert.strictEqual(b.sandbox.confirm, undefined, 'no browser dialog is available, and none is used');
  removes(main)[1].fire('click');
  removes(main)[1].fire('click');                         // a second click does not open a second question
  const asks = byClass(main, 'ss-setup-remove-ask');
  assert.strictEqual(asks.length, 1);
  assert.strictEqual(text(asks[0]).indexOf(STRINGS['setup.import.remove_ask_map'].replace('{name}', 'SYN-OW01.vsdx').replace('{pid}', 'SYN-OW01')) >= 0, true);
  byClass(asks[0], 'ss-setup-remove-no')[0].fire('click');
  assert.strictEqual(byClass(main, 'ss-setup-remove-ask').length, 0);
  assert.strictEqual(b.calls.filter((c) => c.url === '/api/import/remove').length, 0);
  assert.deepStrictEqual(b.errors, []);
});

test('item 26: "Remove the file" posts kind and pid only, then the table reloads and says what was removed', async () => {
  const b = removeApp();
  const main = makeNode('main');
  await b.SS.setup.render(main, 'import');
  await settle();
  const before = b.calls.filter((c) => c.url === '/api/pairing').length;
  removes(main)[0].fire('click');
  byClass(main, 'ss-setup-remove-yes')[0].fire('click');
  await settle();
  assert.deepStrictEqual(b.calls.filter((c) => c.url === '/api/import/remove').map((c) => [c.method, c.body]),
    [['POST', { kind: 'transcript', pid: 'SYN-OW01' }]]);
  assert.strictEqual(b.calls.filter((c) => c.url === '/api/pairing').length, before + 1);
  const note = byClass(main, 'ss-setup-removed');
  assert.strictEqual(note.length, 1);
  assert.strictEqual(note[0].textContent, 'SYN-OW01.docx was removed from the project. The app log records the removal.');
  assert.strictEqual(byClass(main, 'ss-setup-remove-ask').length, 0);
  assert.deepStrictEqual(removes(main).map((x) => x.attributes['aria-label']), ['Remove SYN-OW01.vsdx', 'Remove SYN-OW02.docx']);
  // the note is said once: the next reload does not repeat it
  await b.SS.setupImport.reloadTable();
  await settle();
  assert.strictEqual(byClass(main, 'ss-setup-removed').length, 0);
});

test('item 26: a refusal (a run is going) shows the server sentence in the question and keeps the file', async () => {
  const b = removeApp(true);
  const main = makeNode('main');
  await b.SS.setup.render(main, 'import');
  await settle();
  removes(main)[2].fire('click');
  const ask = byClass(main, 'ss-setup-remove-ask')[0];
  const yes = byClass(ask, 'ss-setup-remove-yes')[0];
  yes.fire('click');
  assert.strictEqual(yes.disabled, true);
  await settle();
  assert.strictEqual(byClass(ask, 'ss-setup-error')[0].textContent, STRINGS['importer.error.run_in_progress']);
  assert.strictEqual(yes.disabled, false);
  assert.strictEqual(removes(main).length, 3);
  assert.strictEqual(byClass(main, 'ss-setup-removed').length, 0);
});

test('item 26: its strings follow the register', () => {
  for (const k of ['setup.import.remove', 'setup.import.remove_label', 'setup.import.remove_ask_transcript', 'setup.import.remove_ask_map',
    'setup.import.remove_yes', 'setup.import.remove_no', 'setup.import.removed', 'importer.error.not_imported',
    'importer.error.bad_remove', 'importer.error.run_in_progress']) {
    assert.ok(typeof STRINGS[k] === 'string', k);
    assert.ok(!/—|rather than|second scribe|\b(first|wrong|fail|failed|error|mistake|fix|correct)\b/i.test(STRINGS[k]), k);
  }
});

// ---------------------------------------------------------------------- item 27: one standard for speaker labels

const STD_PAIRING = { rows: ['SYN-OW01', 'SYN-OF02', 'SYN-XX03', 'SYN-OW04'].map((pid) => ({ pid, transcript: pid + '.docx', map: null,
  paired: false, reference_options: ['B', 'C'], identical_to: [] })), table_sha256: 'a'.repeat(64), confirmed: true };
const STD_DOC = {
  standard: { participant: 'PT', interviewer: 'IV', rules: [{ contains: 'OW', role: 'owner' }, { contains: 'OF', role: 'officer' }] },
  rows: [
    { pid: 'SYN-OW01', participant: 'pt', interviewer: 'iv', role: 'owner', turns: { participant: 12, interviewer: 13, unknown: 0 }, problems: [] },
    { pid: 'SYN-OF02', participant: 'pt', interviewer: 'iv', role: 'officer', turns: { participant: 1, interviewer: 2, unknown: 0 }, problems: [] },
    { pid: 'SYN-XX03', participant: 'pt', interviewer: 'iv', role: null, turns: { participant: 5, interviewer: 5, unknown: 0 }, problems: ['no_rule'] },
    { pid: 'SYN-OW04', participant: 'pt', interviewer: null, role: 'owner', turns: null, problems: ['interviewer_missing'] },
  ],
};
function stdApp(opts) {
  opts = opts || {};
  const b = load((url, init) => {
    if (url === '/api/pairing') return reply(200, STD_PAIRING);
    if (url === '/api/context') return reply(200, { context: { roles: { owner: 'x', officer: 'y' } } });
    if (url === '/api/labels/standard') return reply(200, opts.noStandard ? { standard: null, rows: [] } : STD_DOC);
    if (url.indexOf('/api/labels?') === 0) {
      const pid = decodeURIComponent(url.split('=')[1]);
      const ivLabel = pid === 'SYN-OW04' ? 'q' : 'iv';
      return reply(200, { pid, format: 'bracket', example_url: '/x', warnings: [],
        candidates: [{ label: ivLabel, turns: 13, shown: '[' + ivLabel.toUpperCase() + ']', prefix: null }, { label: 'pt', turns: 12, shown: '[PT]', prefix: null }],
        confirmed: opts.confirmed && opts.confirmed[pid] ? opts.confirmed[pid] : null });
    }
    if (url === '/api/labels/confirm') {
      const body = JSON.parse(init.body);
      if (opts.refuse === body.pid) return reply(422, { error: 'label_not_found', message: STRINGS['importer.error.label_not_found'] });
      return reply(200, { pid: body.pid });
    }
    return reply(200, {});
  });
  return b;
}
async function renderLabels(b) {
  const main = makeNode('main');
  await b.SS.setup.render(main, 'labels');
  await settle();
  return main;
}
const rowOf = (main, pid) => byAttr(main, 'data-pid', pid).filter((n) => n.tagName === 'TR')[0];
const choices = (tr) => all(tr, (n) => n.tagName === 'SELECT').map((s) => s.value);

test('item 27: the standard form sits on top, filled from the saved standard, one rule per role', async () => {
  const main = await renderLabels(stdApp());
  const form = byClass(main, 'ss-setup-standard')[0];
  assert.ok(form, 'the standard form is drawn');
  assert.ok(text(form).indexOf(STRINGS['setup.labels.std.title']) >= 0);
  const inputs = all(form, (n) => n.tagName === 'INPUT');
  assert.deepStrictEqual(inputs.map((i) => i.value), ['PT', 'IV', 'OW', 'OF']);
  assert.deepStrictEqual(byClass(form, 'ss-setup-std-rule').map((l) => l.attributes['data-role']), ['owner', 'officer']);
  // the form comes before the table
  const body = byClass(main, 'ss-setup-step-body')[0];
  const order = body.children.map((c) => c.attributes.class || '');
  assert.ok(order.findIndex((c) => c.indexOf('ss-setup-standard') >= 0) < order.findIndex((c) => c.indexOf('ss-setup-table-wrap') >= 0));
});

test('item 27: every transcript is filled from the standard, with turn counts; rows it cannot fill are flagged', async () => {
  const main = await renderLabels(stdApp());
  assert.deepStrictEqual(['SYN-OW01', 'SYN-OF02', 'SYN-XX03', 'SYN-OW04'].map((p) => rowOf(main, p).attributes['data-state']),
    ['filled', 'filled', 'flagged', 'flagged']);
  assert.deepStrictEqual(choices(rowOf(main, 'SYN-OW01')), ['pt', 'iv', 'owner']);
  assert.deepStrictEqual(choices(rowOf(main, 'SYN-OF02')), ['pt', 'iv', 'officer']);
  assert.deepStrictEqual(choices(rowOf(main, 'SYN-XX03')), ['pt', 'iv', '']);              // no rule: role left to choose
  assert.deepStrictEqual(choices(rowOf(main, 'SYN-OW04')), ['pt', '', 'owner']);
  assert.ok(text(rowOf(main, 'SYN-OW01')).indexOf('Filled from the standard, with 12 turns for the participant and 13 turns for the interviewer. Not confirmed yet.') >= 0);
  assert.ok(text(rowOf(main, 'SYN-OF02')).indexOf('with 1 turn for the participant and 2 turns for the interviewer') >= 0);
  assert.ok(text(rowOf(main, 'SYN-XX03')).indexOf('Choose by hand. No role rule matches this file name.') >= 0);
  assert.ok(text(rowOf(main, 'SYN-OW04')).indexOf('The interviewer label IV is not in this transcript.') >= 0);
  // each row stays editable on its own: three dropdowns and its own confirm button
  all(byClass(main, 'ss-setup-labels-table')[0], (n) => n.tagName === 'TR' && n.attributes['data-pid']).forEach((tr) => {
    assert.strictEqual(all(tr, (n) => n.tagName === 'SELECT').length, 3);
    assert.strictEqual(all(tr, (n) => n.tagName === 'BUTTON').length, 1);
  });
});

test('item 27: without a saved standard the rows stay empty and the form is blank', async () => {
  const main = await renderLabels(stdApp({ noStandard: true }));
  assert.deepStrictEqual(all(byClass(main, 'ss-setup-standard')[0], (n) => n.tagName === 'INPUT').map((i) => i.value), ['', '', '', '']);
  assert.deepStrictEqual(choices(rowOf(main, 'SYN-OW01')), ['', '', '']);
  assert.strictEqual(rowOf(main, 'SYN-OW01').attributes['data-state'], 'open');
});

test('item 27: Apply to all saves the standard (empty rules left out) and redraws', async () => {
  const b = stdApp({ noStandard: true });
  const main = await renderLabels(b);
  const form = byClass(main, 'ss-setup-standard')[0];
  const inputs = all(form, (n) => n.tagName === 'INPUT');
  inputs[0].value = ' PT ';
  inputs[1].value = 'IV';
  inputs[2].value = 'OW';
  inputs[3].value = '  ';
  const projectCalls = b.calls.filter((c) => c.url === '/api/project').length;
  byClass(form, 'ss-setup-std-apply')[0].fire('click');
  await settle();
  assert.deepStrictEqual(b.calls.filter((c) => c.url === '/api/labels/standard' && c.method === 'POST').map((c) => c.body),
    [{ participant: 'PT', interviewer: 'IV', rules: [{ contains: 'OW', role: 'owner' }] }]);
  assert.strictEqual(b.calls.filter((c) => c.url === '/api/project').length, projectCalls + 1, 'the step redraws');
  assert.strictEqual(b.calls.filter((c) => c.url === '/api/labels/confirm').length, 0, 'applying confirms nothing');
});

test('item 27: Confirm all shown sends one confirmation per filled row, with the edits on screen, and skips the rest', async () => {
  const b = stdApp({ confirmed: { 'SYN-OF02': { participant: 'pt', interviewer: 'iv', role: 'officer', format: 'bracket', turns: { participant: 1, interviewer: 2 } } } });
  const main = await renderLabels(b);
  all(rowOf(main, 'SYN-XX03'), (n) => n.tagName === 'SELECT')[2].value = 'owner';    // a flagged row the person completes
  byClass(main, 'ss-setup-labels-all')[0].fire('click');
  await settle();
  // SYN-OF02 is already confirmed with these choices; SYN-OW04 still lacks the interviewer label
  assert.deepStrictEqual(b.calls.filter((c) => c.url === '/api/labels/confirm').map((c) => c.body), [
    { pid: 'SYN-OW01', participant: 'pt', interviewer: 'iv', role: 'owner' },
    { pid: 'SYN-XX03', participant: 'pt', interviewer: 'iv', role: 'owner' }]);
  const report = byClass(main, 'ss-setup-labels-report');
  assert.strictEqual(report.length, 1);
  assert.strictEqual(report[0].textContent, '2 transcripts confirmed.');
});

test('item 27: a confirmation the server refuses is named in its row after Confirm all shown', async () => {
  const b = stdApp({ refuse: 'SYN-OF02' });
  const main = await renderLabels(b);
  byClass(main, 'ss-setup-labels-all')[0].fire('click');
  await settle();
  assert.strictEqual(byClass(main, 'ss-setup-labels-report')[0].textContent,
    '1 transcript confirmed. 1 transcript was not confirmed. The reason is shown in its row.');
  const err = byClass(rowOf(main, 'SYN-OF02'), 'ss-setup-error')[0];
  assert.strictEqual(err.textContent, STRINGS['importer.error.label_not_found']);
});

test('item 27: the body of the standard drops empty rules and trims', () => {
  const { SS } = load(() => reply(200, {}));
  assert.deepStrictEqual(JSON.parse(JSON.stringify(SS.setupLabels.standardBody(' PT', 'IV ', { owner: ' OW ', officer: '' }))),
    { participant: 'PT', interviewer: 'IV', rules: [{ contains: 'OW', role: 'owner' }] });
});

test('item 27: its strings follow the register', () => {
  const keys = Object.keys(STRINGS).filter((k) => /^setup\.labels\.(std\.|flag\.|filled|col_|confirm_all|all_|participant_for|interviewer_for|role_for)/.test(k))
    .concat(['importer.error.bad_standard']);
  assert.ok(keys.length >= 30, String(keys.length));
  for (const k of keys) {
    assert.ok(!/—|rather than|second scribe|\b(first|wrong|fail|failed|error|mistake|fix|correct)\b/i.test(STRINGS[k]), k);
  }
});

// --- item 28: the red-colour list as chips, a colour picker, the colours found in the maps ----------------------
const RULE_VIEW = { rules: { '.vsdx': { kind: 'red_line_or_text', colors: ['#ff0000', '#C00000'], inherit: false } },
  options: { '.vsdx': ['red_line_or_text', 'red_fill', 'dashed'] } };
const FOUND = { max_colors: 8, formats: { '.vsdx': { maps: 12, boxes: 175, unreadable: 1, more: 2, colors: [
  { color: '#cfe2f3', boxes: 48, line: 0, text: 0, fill: 48 },
  { color: '#ff0000', boxes: 7, line: 7, text: 0, fill: 0 },
  { color: '#00ff00', boxes: 1, line: 0, text: 1, fill: 1 }] } } };
function ruleCard(found) {
  const { SS, calls } = load(() => reply(200, {}));
  const box = makeNode('div');
  box.ownerDocument = { createElement: makeNode };
  SS.setupRule.draw(box, RULE_VIEW, PAIRING, found);
  const card = byAttr(box, 'data-suffix', '.vsdx')[0];
  const input = all(card, (n) => n.tagName === 'INPUT' && n.attributes.type === 'text')[0];
  const chips = () => byClass(card, 'ss-setup-chip').filter((c) => (c.attributes.class || '').indexOf('found') < 0)
    .map((c) => c.attributes['data-color']);
  const ticks = () => all(card, (n) => n.attributes.type === 'checkbox');
  return { SS, calls, card, input, chips, ticks };
}

test('item 28: the saved colours show as chips with swatch and hex, and a chip removes its colour', async () => {
  const r = ruleCard(null);
  assert.deepStrictEqual(r.chips(), ['#ff0000', '#c00000']);
  const chip = byClass(r.card, 'ss-setup-chip')[1];
  const sw = byClass(chip, 'ss-setup-swatch')[0];
  assert.strictEqual(sw.style.backgroundColor, '#c00000');
  assert.strictEqual(sw.attributes['aria-hidden'], 'true');
  assert.ok(text(chip).indexOf('#c00000') >= 0);
  const x = byClass(chip, 'ss-setup-chip-remove')[0];
  assert.strictEqual(x.attributes['aria-label'], 'Remove #c00000 from the list');
  x.fire('click');
  assert.strictEqual(r.input.value, '#ff0000');
  assert.deepStrictEqual(r.chips(), ['#ff0000']);
  assert.strictEqual(byClass(r.card, 'ss-setup-found').length, 0);            // nothing found: no found section
  all(r.card, (n) => n.tagName === 'BUTTON' && n.textContent === STRINGS['setup.rule.save'])[0].fire('click');
  await settle();
  assert.deepStrictEqual(r.calls.filter((c) => c.url === '/api/missing-rule').map((c) => c.body),
    [{ suffix: '.vsdx', kind: 'red_line_or_text', colors: ['#ff0000'], inherit: false }]);
});

test('item 28: the colour picker adds a colour once, at most eight, and the hex list stays editable', () => {
  const r = ruleCard(null);
  const picker = all(r.card, (n) => n.attributes.type === 'color')[0];
  const label = all(r.card, (n) => n.tagName === 'LABEL' && n.attributes.for === picker.attributes.id);
  assert.strictEqual(label[0].textContent, STRINGS['setup.rule.pick']);
  picker.value = '#3366FF';
  picker.fire('change');
  assert.strictEqual(r.input.value, '#ff0000, #c00000, #3366ff');
  picker.fire('change');                                                       // same colour again: no repeat
  assert.deepStrictEqual(r.chips(), ['#ff0000', '#c00000', '#3366ff']);
  r.input.value = '#ff0000, #111111, #222222, #333333, #444444, #555555, #666666, #777777';
  r.input.fire('input');                                                       // typed codes become chips
  assert.strictEqual(r.chips().length, 8);
  picker.value = '#888888';
  picker.fire('change');
  assert.strictEqual(r.chips().length, 8);
  assert.strictEqual(byClass(r.card, 'ss-setup-colors-full')[0].textContent,
    'The list holds at most 8 colours. Remove one to add another.');
  r.input.value = '#ff0000, nothex';
  r.input.fire('input');
  assert.deepStrictEqual(r.chips(), ['#ff0000', 'nothex']);                    // shown as typed; the server says no
  assert.strictEqual(byClass(r.card, 'ss-setup-colors-full')[0].textContent, '');
});

test('item 28: colours found in the maps are chips to tick, with how many boxes use each', () => {
  const r = ruleCard(FOUND);
  const found = byClass(r.card, 'ss-setup-found')[0];
  assert.strictEqual(found.children[0].textContent, 'Colours found in your maps');
  const chips = byClass(found, 'ss-setup-found-chip');
  assert.deepStrictEqual(chips.map((c) => byClass(c, 'ss-setup-found-use')[0].textContent),
    ['48 boxes: fill 48', '7 boxes: outline 7', '1 box: text 1, fill 1']);
  assert.strictEqual(byClass(chips[2], 'ss-setup-swatch')[0].style.backgroundColor, '#00ff00');
  assert.deepStrictEqual(r.ticks().map((t) => [t.attributes['data-color'], t.checked]),
    [['#cfe2f3', false], ['#ff0000', true], ['#00ff00', false]]);
  const t = text(found);
  for (const s of ['2 more colours are used less often and are not shown.', '1 map could not be read for its colours.',
    STRINGS['setup.rule.found_vsdx']]) assert.ok(t.indexOf(s) >= 0, s);
  const blue = r.ticks()[0];
  blue.checked = true;
  blue.fire('change');
  assert.strictEqual(r.input.value, '#ff0000, #c00000, #cfe2f3');
  const red = r.ticks()[1];
  red.checked = false;
  red.fire('change');
  assert.strictEqual(r.input.value, '#c00000, #cfe2f3');
  byClass(r.card, 'ss-setup-chip-remove')[1].fire('click');                    // removing the chip unticks it
  assert.strictEqual(r.ticks()[0].checked, false);
  r.input.value = '#00FF00';
  r.input.fire('input');
  assert.deepStrictEqual(r.ticks().map((x) => x.checked), [false, false, true]);
});

test('item 28: maps with no colour say so; the step reads the colours and works without them', async () => {
  const none = ruleCard({ formats: { '.vsdx': { maps: 1, boxes: 3, unreadable: 0, more: 0, colors: [] } } });
  assert.ok(text(none.card).indexOf(STRINGS['setup.rule.found_none']) >= 0);
  for (const status of [200, 500]) {
    const { SS, calls } = load((url) => {
      if (url === '/api/missing-rule') return reply(200, RULE_VIEW);
      if (url.indexOf('/api/pairing') === 0) return reply(200, PAIRING);
      if (url === '/api/missing-rule/colors') return reply(status, status === 200 ? FOUND : { error: 'x', message: 'x' });
      return reply(200, {});
    });
    const main = makeNode('main');
    main.ownerDocument = { createElement: makeNode };
    SS.setup.render(main, 'rule');
    await settle();
    assert.ok(calls.some((c) => c.url === '/api/missing-rule/colors' && c.method === 'GET'));
    assert.strictEqual(byAttr(main, 'data-suffix', '.vsdx').length, 1, String(status));
    assert.strictEqual(byClass(main, 'ss-setup-found-chip').length, status === 200 ? 3 : 0);
  }
});

test('item 28: its strings follow the register', () => {
  const keys = Object.keys(STRINGS).filter((k) => /^setup\.rule\.(chip|pick|colors_full|found|place\.)/.test(k));
  assert.ok(keys.length >= 16, String(keys.length));
  for (const k of keys) {
    assert.ok(!/—|rather than|second scribe|\b(first|wrong|fail|failed|error|mistake|fix|correct)\b/i.test(STRINGS[k]), k);
  }
});

// council 30 (seat 02 F7): "phase from its place on the map" is said once above the drawing, and a box says where its
// phase came from only when that is somewhere else
test('maps step: the phase-position note is printed once, and boxes name only another source', () => {
  const { SS } = load(() => reply(200, {}));
  const count = (t, s) => t.split(s).length - 1;
  const draw = (prev) => {
    const main = makeNode('div');
    main.ownerDocument = { createElement: makeNode };
    SS.setupMaps.draw(main, PAIRING, [{ pid: 'MAP-A1', preview: prev }]);
    return main;
  };
  const all = Object.assign({}, PREVIEW, { nodes: [1, 2, 3, 4, 5].map((i) => ({ id: 'p' + i, text: 'Step ' + i, status: 'happened', phase: 'Planning', phase_from: 'position', x: i })) });
  let main = draw(all);
  let t = text(main);
  assert.strictEqual(count(t, STRINGS['setup.maps.phase_position_note']), 1, t);
  assert.strictEqual(byClass(main, 'ss-setup-mapbox-from').length, 0);
  // mixed: the map-file boxes say so, the one placed by position says nothing; a box set by you always says so
  const mixed = Object.assign({}, PREVIEW, { nodes: PREVIEW.nodes.concat([{ id: 'n4', text: 'Moved', status: 'happened', phase: 'Permits', phase_from: 'you', x: 3 }]) });
  main = draw(mixed);
  t = text(main);
  assert.strictEqual(count(t, STRINGS['setup.maps.phase_position_note']), 1);
  assert.strictEqual(count(t, STRINGS['setup.maps.phase_map']), 2);
  assert.strictEqual(count(t, STRINGS['setup.maps.phase_you']), 1);
  // no box placed by position: no note, and the map-file boxes need no tag
  main = draw(Object.assign({}, PREVIEW, { nodes: PREVIEW.nodes.filter((n) => n.phase_from !== 'position') }));
  t = text(main);
  assert.strictEqual(count(t, STRINGS['setup.maps.phase_position_note']), 0);
  assert.strictEqual(byClass(main, 'ss-setup-mapbox-from').length, 0);
});
