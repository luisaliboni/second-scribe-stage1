'use strict';
// Task 27: the evidence drawer (SS.drawer) and the viewer that wires everything (SS.viewer.mount).
// Fixture: tests/fixtures/views/mini.view.json (SYNTHETIC) and tests/fixtures/profiles/profile-synth.json.
// The stub DOM has no real events or focus, so the tests wrap createElement to record listeners and focus calls.
const test = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const { load, ROOT } = require('./harness');

const VIEW = JSON.parse(fs.readFileSync(path.join(ROOT, 'tests', 'fixtures', 'views', 'mini.view.json'), 'utf8'));
const SYNTH = JSON.parse(fs.readFileSync(path.join(ROOT, 'tests', 'fixtures', 'profiles', 'profile-synth.json'), 'utf8'));
const EN = JSON.parse(fs.readFileSync(path.join(ROOT, 'second_scribe', 'app', 'strings', 'en.json'), 'utf8'));
const clone = (o) => JSON.parse(JSON.stringify(o));
const eq = (a, b, msg) => assert.deepStrictEqual(clone(a), b, msg);

const NOT_AVAILABLE = EN['drawer.not_available'];
const NEEDS_JUDGE = EN['common.needs_judge'];
const MODE_B_NOTE = EN['strip.mode_b_note'];
const GLOSS = EN['lane.no_interview_gloss'];

function boot(opts) {
  opts = opts || {};
  const env = load();
  const doc = env.window.document;
  const make = doc.createElement;
  env.focused = [];
  doc.createElement = (tag) => {
    const n = make(tag);
    n.listeners = {};
    n.addEventListener = (type, fn) => { (n.listeners[type] = n.listeners[type] || []).push(fn); };
    n.focus = () => { env.focused.push(n); (n.listeners.focus || []).forEach((f) => f({ target: n })); };
    // a real DOM empties the node when textContent is set to ''; the stub does not
    let text = n.textContent;
    Object.defineProperty(n, 'textContent', {
      get: () => text,
      set: (v) => { text = v; if (v === '') n.children.length = 0; },
    });
    return n;
  };
  env.window.location.hash = opts.hash || '';
  return env;
}
function walk(node, fn) {
  fn(node);
  (node.children || []).forEach((c) => walk(c, fn));
}
function find(root, pred) {
  const out = [];
  walk(root, (n) => { if (pred(n)) out.push(n); });
  return out;
}
const attr = (n, k) => (n.attributes ? n.attributes[k] : undefined);
const byAttr = (root, k, v) => find(root, (n) => attr(n, k) !== undefined && (v === undefined || attr(n, k) === v));
const tag = (root, t) => find(root, (n) => n.tagName === t.toUpperCase());
function allText(root) {
  const out = [];
  walk(root, (n) => { if (n.textContent) out.push(n.textContent); });
  return out.join('\n');
}
function count(text, needle) { return text.split(needle).length - 1; }
function fire(node, type, ev) { (node.listeners[type] || []).forEach((f) => f(ev || {})); }
function stateFor(env, view, patch) {
  env.SS.state.use(clone(view));
  return Object.assign(env.SS.state.get(), patch || {});
}
function mountViewer(env, view, opts) {
  const root = env.window.document.createElement('div');
  const handle = env.SS.viewer.mount(root, clone(view), opts);
  return { root, handle };
}
function section(root, name) {
  const hit = byAttr(root, 'data-section', name);
  assert.strictEqual(hit.length, 1, 'one ' + name + ' section');
  return hit[0];
}

// ---------------------------------------------------------------- chain

test('chain_order: map, quote, judge label, log line, record hash, verify status', () => {
  const env = boot();
  const st = stateFor(env, VIEW, { map: 'MINI-A', rung: 'rung2', item: 'a1' });
  const chain = env.SS.drawer.chain(clone(VIEW), st);
  eq(chain.map((c) => c.step), ['map', 'quote', 'judge', 'log', 'hash', 'verify']);
  const v = Object.fromEntries(chain.map((c) => [c.step, c.value]));
  assert.match(v.map, /MINI-A/);
  assert.match(v.map, /Pre-application meeting/);
  assert.match(v.map, /participant marked it as wished for/);
  assert.match(v.quote, /SYNTHETIC quote: we had a chat with the council/);
  assert.match(v.quote, /participant/);
  assert.match(v.judge, /experienced/);
  assert.match(v.log, /mini-req-0001/);
  assert.match(v.log, /\b0\b/, 'record_index 0 is a recorded value, not a missing one');
  assert.strictEqual(v.hash, '0'.repeat(64));
  assert.strictEqual(v.verify, EN['drawer.verify.not_run'], 'not checked: no check time is printed');
  // a log that was checked says when
  const checked = clone(VIEW);
  checked.run.verify = { status: 'ok', records: 12, checked_at: '2026-10-18T09:30:00Z' };
  const cv = env.SS.drawer.chain(checked, st).filter((c) => c.step === 'verify')[0].value;
  assert.strictEqual(cv, EN['drawer.verify.ok'] + ', checked 2026-10-18T09:30:00Z');
  eq(env.errors, []);
});

test('judge_cached: a label reused from an identical earlier judge request says so and names that record', () => {
  const env = boot();
  const v = clone(VIEW);
  Object.assign(v.maps[0].items[0].evidence.rung2, { request_id: 'judge:modality:OTHER__arm__p0:0:1:ab12', record_index: 487, judge_cached: true });
  const st = stateFor(env, v, { map: 'MINI-A', rung: 'rung2', item: 'a1' });
  const log = env.SS.drawer.chain(v, st).filter((c) => c.step === 'log')[0];
  assert.strictEqual(log.value, EN['drawer.log_value_cached']
    .replace('{request_id}', 'judge:modality:OTHER__arm__p0:0:1:ab12').replace('{record_index}', '487'));
  assert.match(log.value, /reused/);
  assert.strictEqual(log.missing, false);
  // the label of the map's own judge call reads as before
  const own = env.SS.drawer.chain(clone(VIEW), st).filter((c) => c.step === 'log')[0];
  assert.strictEqual(own.value, EN['drawer.log_value'].replace('{request_id}', 'mini-req-0001').replace('{record_index}', '0'));
  eq(env.errors, []);
});

test('chain_follows_the_rung: the same step at another rung shows that rung\'s evidence', () => {
  const env = boot();
  const v4 = (rung) => Object.fromEntries(env.SS.drawer.chain(clone(VIEW), stateFor(env, VIEW, { map: 'MINI-A', rung, item: 'a4' }))
    .map((c) => [c.step, c.value]));
  assert.match(v4('rung1').quote, /fire officer came by/);
  assert.match(v4('rung2').quote, /fire people looked at everything/);
  assert.strictEqual(v4('rung1').hash, '1' + '0'.repeat(63));
  assert.strictEqual(v4('rung2').hash, '2' + '0'.repeat(63));
  // a rung with no evidence for this step
  assert.strictEqual(v4('rung3').quote, NOT_AVAILABLE);
});

test('missing_link_text: every link the data lacks reads "not available", never 0 or "not computed"', () => {
  const env = boot();
  // a4 at rung3 has no evidence at all; the map link is still known
  const st = stateFor(env, VIEW, { map: 'MINI-A', rung: 'rung3', item: 'a4' });
  const chain = env.SS.drawer.chain(clone(VIEW), st);
  eq(chain.map((c) => c.step), ['map', 'quote', 'judge', 'log', 'hash', 'verify']);
  for (const c of chain.filter((x) => ['quote', 'judge', 'log', 'hash'].includes(x.step))) {
    assert.strictEqual(c.value, NOT_AVAILABLE, c.step);
    assert.strictEqual(c.missing, true, c.step);
  }
  assert.notStrictEqual(chain[0].value, NOT_AVAILABLE);
  // partial evidence: a3 at rung2 has a quote and a label but no log line and no hash
  const part = Object.fromEntries(env.SS.drawer.chain(clone(VIEW), stateFor(env, VIEW, { map: 'MINI-A', rung: 'rung2', item: 'a3' }))
    .map((c) => [c.step, c.value]));
  assert.match(part.quote, /health inspector arrived early/);
  assert.match(part.quote, /interviewer/);
  assert.strictEqual(part.log, NOT_AVAILABLE);
  assert.strictEqual(part.hash, NOT_AVAILABLE);
  // the verify link missing from the run
  const noVerify = clone(VIEW);
  delete noVerify.run.verify;
  const nv = env.SS.drawer.chain(noVerify, stateFor(env, noVerify, { map: 'MINI-A', rung: 'rung2', item: 'a1' }));
  assert.strictEqual(nv.filter((c) => c.step === 'verify')[0].value, NOT_AVAILABLE);
  // no item chosen: nothing to chain
  eq(env.SS.drawer.chain(clone(VIEW), stateFor(env, VIEW, { map: 'MINI-A', item: null })), []);
  eq(env.errors, []);
});

test('unattested_reads_not_attested_by_its_own_quote: the quote link says so and prints no quote', () => {
  const env = boot();
  const st = stateFor(env, VIEW, { map: 'MINI-A', rung: 'rung2', item: 'a7' });
  const v = Object.fromEntries(env.SS.drawer.chain(clone(VIEW), st).map((c) => [c.step, c.value]));
  assert.strictEqual(v.quote, 'Not attested by its own quote');
  assert.match(v.judge, /wished/);
  assert.strictEqual(v.hash, '3' + '0'.repeat(63));
});

test('study_public_no_quotes: a study-public view shows only the number and its source key', () => {
  const env = boot();
  // adversarial: even if a maps block with quotes is present, nothing from it may appear
  const pub = clone(VIEW);
  pub.source = 'study-public';
  const st = Object.assign(stateFor(env, pub, { map: 'MINI-A', rung: 'rung2', item: 'a1' }),
    { number: 0.42, source_key: 'ladder.report.rungs.rung2.three_way.pooled.leaked_rate' });
  const chain = env.SS.drawer.chain(pub, st);
  eq(chain.map((c) => c.step), ['number', 'source']);
  assert.strictEqual(chain[0].value, '0.42');
  assert.strictEqual(chain[1].value, 'ladder.report.rungs.rung2.three_way.pooled.leaked_rate');
  const text = JSON.stringify(chain);
  assert.doesNotMatch(text, /SYNTHETIC quote/);
  assert.doesNotMatch(text, /mini-req/);
  // a missing number is "not available", never 0
  const none = env.SS.drawer.chain(pub, { map: 'MINI-A', rung: 'rung2', item: 'a1' });
  assert.strictEqual(none[0].value, NOT_AVAILABLE);
  assert.strictEqual(none[1].value, NOT_AVAILABLE);
  // the rendered drawer too
  const root = env.window.document.createElement('div');
  env.SS.drawer.render(root, pub, st, {});
  const t = allText(root);
  assert.doesNotMatch(t, /SYNTHETIC quote|mini-req|Pre-application/);
});

// ---------------------------------------------------------------- the numbers behind

test('items_behind: reference-missing steps with their fate at the rung, across the maps', () => {
  const env = boot();
  const rows = clone(env.SS.drawer.itemsBehind(clone(VIEW), { rung: 'rung2' }));
  eq(rows.map((r) => [r.map, r.item_id, r.fate]), [
    ['MINI-A', 'a1', 'leaked'], ['MINI-A', 'a4', 'leaked'], ['MINI-A', 'a5', 'missed'],
    ['MINI-A', 'a7', 'unattested'], ['MINI-B', 'b3', 'kept'],
  ]);
  // a rung the run did not do is "not run", not "never drawn"
  const r3 = clone(env.SS.drawer.itemsBehind(clone(VIEW), { rung: 'rung3' }));
  eq(r3.filter((r) => r.map === 'MINI-B').map((r) => r.fate), ['not_run']);
  assert.strictEqual(r3.filter((r) => r.map === 'MINI-B')[0].text, 'Not run');
  // one map only
  eq(env.SS.drawer.itemsBehind(clone(VIEW), { rung: 'rung2', map: 'MINI-B' }).map((r) => r.item_id), ['b3']);
  // unattested reads as in the lanes
  assert.strictEqual(rows.filter((r) => r.fate === 'unattested')[0].text, 'Not attested by its own quote');
});

// ---------------------------------------------------------------- render

test('render: the drawer shows the chain as a list, the hash in mono, and closes with state.item = null', () => {
  const env = boot();
  const view = clone(VIEW);
  const st = stateFor(env, view, { map: 'MINI-A', rung: 'rung2', item: 'a1' });
  const root = env.window.document.createElement('div');
  env.SS.drawer.render(root, view, st, {});
  const t = allText(root);
  assert.ok(t.includes(EN['drawer.title']));
  assert.ok(t.includes('SYNTHETIC quote: we had a chat with the council'));
  const hashNode = find(root, (n) => n.textContent === '0'.repeat(64))[0];
  assert.ok(hashNode && /\bmono\b/.test(hashNode.className || ''), 'record hash is set in mono');
  // closing
  const close = find(root, (n) => n.tagName === 'BUTTON' && n.textContent === EN['drawer.close']);
  assert.strictEqual(close.length, 1);
  fire(close[0], 'click', {});
  assert.strictEqual(env.SS.state.get().item, null);
  // nothing chosen: a plain hint, no list
  const empty = env.window.document.createElement('div');
  env.SS.drawer.render(empty, view, stateFor(env, view, { item: null }), {});
  assert.ok(allText(empty).includes(EN['drawer.empty']));
  eq(tag(empty, 'dl'), []);
  eq(env.errors, []);
});

test('render with a number: lists the steps behind it, each one opens the step', () => {
  const env = boot();
  const view = clone(VIEW);
  env.SS.state.use(clone(view));
  const st = stateFor(env, view, { rung: 'rung2', item: null });
  const root = env.window.document.createElement('div');
  env.SS.drawer.render(root, view, st, { behind: { rung: 'rung2' } });
  const t = allText(root);
  assert.ok(t.includes('Neighbour consultation'));
  assert.ok(t.includes('Appeal a refusal'));
  const heard = [];
  env.SS.bus.on('item:open', (p) => heard.push(clone(p)));
  const b = byAttr(root, 'data-item-id', 'b3');
  assert.strictEqual(b.length, 1);
  fire(b[0], 'click', {});
  eq(heard, [{ item_id: 'b3', map: 'MINI-B', rung: 'rung2', lane: 'ai' }]);
});

// ---------------------------------------------------------------- the viewer

test('mount: sections in order (profile above the lanes), one gloss, state in the hash', () => {
  const env = boot({ hash: '#view=overlay&map=MINI-B&rung=rung1' });
  const { root } = mountViewer(env, VIEW, { profile: clone(SYNTH) });
  const names = byAttr(root, 'data-section').map((n) => attr(n, 'data-section'));
  eq(names, ['bands', 'profile', 'views', 'drawer', 'ledger']);
  const t = allText(root);
  assert.strictEqual(count(t, GLOSS), 1, 'the gloss is printed once per page');
  // the hash on mount chose map and rung; item 20 (23 Oct): the lanes are the one view, so a hash that names another
  // view still draws the lanes, and the viewer shows no view switch
  const s = env.SS.state.get();
  assert.deepStrictEqual([s.view, s.map, s.rung], ['lanes', 'MINI-B', 'rung1']);
  assert.strictEqual(find(root, (n) => /\bss-viewswitch\b/.test(n.className || '')).length, 0);
  assert.strictEqual(find(root, (n) => /\bss-ov\b|\bss-cols\b/.test(n.className || '')).length, 0);
  // the profile is the headline: it comes before the views in document order
  const flat = [];
  walk(root, (n) => { if (attr(n, 'data-section')) flat.push(attr(n, 'data-section')); });
  assert.ok(flat.indexOf('profile') < flat.indexOf('views'));
  eq(env.errors, []);
});

test('the SYNTHETIC band is printed once per screen, with or without the synthetic profile', () => {
  const band = 'SYNTHETIC: corpus and traps written by the authors; test of the instrument';
  const withP = allText(mountViewer(boot(), VIEW, { profile: clone(SYNTH) }).root);
  assert.strictEqual(count(withP, band), 1);
  assert.ok(withP.includes('Test pattern · not a recorded result'), 'the test-pattern label stays');
  // and it comes first, above the test-pattern label and the mode B note
  const b = clone(VIEW);
  b.reference_type = 'B';
  const kids = section(mountViewer(boot(), b, { profile: clone(SYNTH) }).root, 'bands').children;
  eq(kids.map((k) => attr(k, 'data-band') || attr(k, 'data-part')), ['synthetic', 'test_pattern', 'mode_b_note']);
  const without = allText(mountViewer(boot(), VIEW, {}).root);
  assert.strictEqual(count(without, band), 1);
});

test('with a profile the gloss is printed once, by the profile, whatever the view', () => {
  for (const view of ['lanes', 'overlay', 'columns']) {
    const env = boot({ hash: '#view=' + view });
    const { root } = mountViewer(env, VIEW, { profile: clone(SYNTH) });
    assert.strictEqual(count(allText(root), GLOSS), 1, view);
    assert.strictEqual(count(allText(section(root, 'profile')), GLOSS), 1, view + ': the profile prints it');
  }
});

test('mount without a profile still prints the gloss once, in the lanes', () => {
  for (const view of ['lanes', 'columns']) {
    const env = boot({ hash: '#view=' + view });
    const { root } = mountViewer(env, VIEW, {});
    assert.strictEqual(count(allText(root), GLOSS), 1, view);
    eq(byAttr(root, 'data-section').map((n) => attr(n, 'data-section')), ['bands', 'views', 'drawer', 'ledger']);
  }
});

test('item_open_sets_state_and_rung_for_ai_lane, and the drawer follows', () => {
  const env = boot({ hash: '#map=MINI-A&rung=rung2' });
  const { root } = mountViewer(env, VIEW, {});
  env.SS.bus.emit('item:open', { item_id: 'a4', map: 'MINI-A', rung: 'rung1', lane: 'ai' });
  let s = env.SS.state.get();
  assert.deepStrictEqual([s.item, s.rung], ['a4', 'rung1']);
  assert.ok(allText(section(root, 'drawer')).includes('fire officer came by'), 'drawer shows rung 1 evidence');
  // the no-interview lane keeps the rung
  env.SS.bus.emit('item:open', { item_id: 'a1', map: 'MINI-A', rung: 'rung2', lane: 'script' });
  s = env.SS.state.get();
  assert.deepStrictEqual([s.item, s.rung], ['a1', 'rung1']);
  assert.ok(allText(section(root, 'drawer')).includes('Pre-application meeting'));
  // an item of another map brings its map along
  env.SS.bus.emit('item:open', { item_id: 'b3', map: 'MINI-B', rung: 'rung2', lane: 'ai' });
  s = env.SS.state.get();
  assert.deepStrictEqual([s.item, s.map, s.rung], ['b3', 'MINI-B', 'rung2']);
  // the hash follows the state
  assert.match(env.window.location.hash, /item=b3/);
  assert.match(env.window.location.hash, /map=MINI-B/);
  eq(env.errors, []);
});

test('clicking a lane item opens it and puts keyboard focus back on that item after the re-render', () => {
  const env = boot({ hash: '#map=MINI-A&rung=rung2' });
  const { root } = mountViewer(env, VIEW, {});
  const btn = find(section(root, 'views'), (n) => attr(n, 'data-lane') === 'ai' && attr(n, 'data-item-id') === 'a4')[0];
  assert.ok(btn, 'an AI-lane button for a4');
  env.focused.length = 0;
  env.window.document.activeElement = btn;   // a key press on a lane button has focus on it, as in a browser
  fire(btn, 'click', {});
  assert.strictEqual(env.SS.state.get().item, 'a4');
  const last = env.focused[env.focused.length - 1];
  assert.ok(last, 'something took focus');
  assert.strictEqual(attr(last, 'data-item-id'), 'a4');
  assert.strictEqual(attr(last, 'data-lane'), 'ai');
  // it is a new button (the lanes were rebuilt), marked as the selected one
  assert.notStrictEqual(last, btn);
  assert.strictEqual(attr(last, 'aria-current'), 'true');
});

test('a click on a ledger cell does not move focus into the lanes', () => {
  const env = boot({ hash: '#map=MINI-A&rung=rung2' });
  const { root } = mountViewer(env, VIEW, {});
  const cell = find(section(root, 'ledger'), (n) => n.tagName === 'BUTTON' && attr(n, 'data-item-id') === 'a4' && attr(n, 'data-rung') === 'rung1')[0];
  assert.ok(cell, 'a ledger cell');
  env.focused.length = 0;
  env.window.document.activeElement = cell;
  fire(cell, 'click', {});
  assert.strictEqual(env.SS.state.get().item, 'a4');
  assert.strictEqual(env.SS.state.get().rung, 'rung1');
  assert.deepStrictEqual(env.focused.filter((n) => attr(n, 'data-lane') !== null && attr(n, 'data-rung') === null), []);
});

test('hide-list button ends the "steps behind" list; other hash parts survive', () => {
  const env = boot({ hash: '#theme=dark&map=MINI-A&rung=rung2' });
  const { root } = mountViewer(env, VIEW, {});
  assert.match(env.window.location.hash, /theme=dark/);
  env.SS.bus.emit('number:open', { row: 5, rung: 'rung2' });
  const drawer = section(root, 'drawer');
  assert.ok(allText(drawer).includes('Neighbour consultation'));
  const hide = find(drawer, (n) => n.tagName === 'BUTTON' && n.textContent === EN['drawer.behind.close']);
  assert.strictEqual(hide.length, 1);
  fire(hide[0], 'click', {});
  assert.ok(!allText(section(root, 'drawer')).includes('Neighbour consultation'));
  env.SS.state.set({ rung: 'rung1' });
  assert.match(env.window.location.hash, /theme=dark/);
  assert.match(env.window.location.hash, /rung=rung1/);
});

test('number:open lists the steps behind a number and moves the rung', () => {
  const env = boot({ hash: '#map=MINI-A&rung=rung2' });
  const { root } = mountViewer(env, VIEW, {});
  assert.ok(!allText(section(root, 'drawer')).includes('Neighbour consultation'));
  env.SS.bus.emit('number:open', { row: 5, rung: 'rung1' });
  assert.strictEqual(env.SS.state.get().rung, 'rung1');
  const t = allText(section(root, 'drawer'));
  assert.ok(t.includes('Neighbour consultation'));
  assert.ok(t.includes('Appeal a refusal'));
  // a rung the run did not do is ignored
  env.SS.bus.emit('number:open', { row: 5, rung: 'nonsense' });
  assert.strictEqual(env.SS.state.get().rung, 'rung1');
});

test('the drill hook of the profile adds "steps behind these numbers" buttons that emit number:open', () => {
  const env = boot();
  const { root } = mountViewer(env, VIEW, { profile: clone(SYNTH) });
  const heard = [];
  env.SS.bus.on('number:open', (p) => heard.push(clone(p)));
  const rows = byAttr(section(root, 'profile'), 'data-drill');
  eq(rows.map((n) => attr(n, 'data-drill')).sort(), ['5', '8']);
  for (const d of rows) {
    const btns = byAttr(d, 'data-behind');
    assert.ok(btns.length >= 1, 'row ' + attr(d, 'data-drill') + ' has buttons');
    eq(btns.map((b) => attr(b, 'data-behind')), ['rung1', 'rung2', 'placebo', 'rung3'], 'display order, placebo before rung 3');
  }
  fire(byAttr(rows[0], 'data-behind', 'placebo')[0], 'click', {});
  eq(heard, [{ row: Number(attr(rows[0], 'data-drill')), rung: 'placebo' }]);
});

test('test_pattern_label_shown: SYNTHETIC band and the test-pattern label; the own-data band never shares a page with them', () => {
  const env = boot();
  const { root } = mountViewer(env, VIEW, {});
  const t = allText(section(root, 'bands'));
  assert.ok(t.includes('SYNTHETIC: corpus and traps written by the authors; test of the instrument'));
  assert.ok(t.includes('Test pattern · not a recorded result'));
  assert.ok(!t.includes('Your data'));
  // synthetic without the test-pattern flag (the recorded demo): the band only
  const demo = clone(VIEW);
  demo.test_pattern = false;
  const e2 = boot();
  const t2 = allText(section(mountViewer(e2, demo, {}).root, 'bands'));
  assert.ok(t2.includes('SYNTHETIC: corpus'));
  assert.ok(!t2.includes('Test pattern'));
  // any other source gets neither band
  for (const src of ['study-public', undefined]) {
    const other = clone(VIEW);
    other.source = src;
    other.test_pattern = false;
    const bt = allText(section(mountViewer(boot(), other, {}).root, 'bands'));
    assert.ok(!bt.includes('SYNTHETIC') && !bt.includes('Your data'), String(src));
  }
  // own data: "Your data · reference: participant / researcher / none"
  for (const [ref, word] of [['A', 'participant'], ['B', 'researcher'], ['C', 'none']]) {
    const own = clone(VIEW);
    own.source = 'own';
    own.test_pattern = false;
    own.reference_type = ref;
    const e3 = boot();
    const bt = allText(section(mountViewer(e3, own, {}).root, 'bands'));
    assert.ok(bt.includes('Your data · reference: ' + word), ref + ': ' + bt);
    assert.ok(!bt.includes('SYNTHETIC'), ref);
    assert.ok(!bt.includes('Test pattern'), ref);
  }
});

test('mode_b_wording_everywhere: ledger, drawer and strip titles use the mode-B keys; the fixed note appears exactly once', () => {
  const env = boot({ hash: '#map=MINI-A&rung=rung2&item=a1' });
  const b = clone(VIEW);
  b.reference_type = 'B';
  const { root } = mountViewer(env, b, { profile: clone(SYNTH) });
  const t = allText(root);
  assert.ok(t.includes(EN['ledger.title.mode_b']));
  assert.ok(!t.includes(EN['ledger.title']));
  assert.ok(t.includes(EN['drawer.title.mode_b']));
  assert.ok(!t.includes(EN['drawer.title']));
  assert.ok(t.includes(EN['strip.row5.heading_b']));
  assert.ok(!t.includes(EN['strip.row5.heading']), 'the plain row 5 heading must not appear');
  assert.strictEqual(count(t, MODE_B_NOTE), 1, 'the fixed note once per screen');
  // the drawer says who marked the step missing
  assert.ok(allText(section(root, 'drawer')).includes('researcher marked it as wished for'));
  // the same screen with reference A uses none of it
  const a = boot({ hash: '#map=MINI-A&rung=rung2&item=a1' });
  const ta = allText(mountViewer(a, VIEW, { profile: clone(SYNTH) }).root);
  assert.strictEqual(count(ta, MODE_B_NOTE), 0);
  assert.ok(ta.includes(EN['drawer.title']));
  assert.ok(ta.includes(EN['ledger.title']));
  eq(env.errors, []);
});

test('mode B note stays once when the rung changes and the screen re-renders', () => {
  const env = boot({ hash: '#map=MINI-A&rung=rung2' });
  const b = clone(VIEW);
  b.reference_type = 'B';
  const { root } = mountViewer(env, b, { profile: clone(SYNTH) });
  env.SS.state.set({ rung: 'rung1' });
  env.SS.state.set({ view: 'columns' });
  assert.strictEqual(count(allText(root), MODE_B_NOTE), 1);
});

test('one_family_shows_needs_judge: with no judge the comparison, ladder, absence and drawer panels say so', () => {
  const env = boot({ hash: '#map=MINI-A&rung=rung2&item=a1' });
  const one = clone(VIEW);
  delete one.run.models.judge;
  one.maps.forEach((m) => { m.items = []; });
  one.states = { needs_judge: true };
  const { root } = mountViewer(env, one, { profile: clone(SYNTH) });
  const t = allText(root);
  for (const name of ['views', 'ledger', 'drawer']) {
    assert.ok(allText(section(root, name)).includes(NEEDS_JUDGE), name);
  }
  // the profile's absence and instruction drill-downs too
  const drills = byAttr(section(root, 'profile'), 'data-drill');
  assert.strictEqual(drills.length, 2);
  for (const d of drills) assert.ok(allText(d).includes(NEEDS_JUDGE), 'drill ' + attr(d, 'data-drill'));
  // no table, no lane buttons, no rung selector
  eq(tag(section(root, 'ledger'), 'table'), []);
  eq(find(section(root, 'views'), (n) => n.tagName === 'BUTTON' && attr(n, 'data-item-id')), []);
  assert.ok(!t.includes(EN['ledger.selector.legend']));
  // with a judge the sentence is gone
  const e2 = boot();
  assert.ok(!allText(mountViewer(e2, VIEW, {}).root).includes(NEEDS_JUDGE));
  // run.models.judge present but states.needs_judge true is also "needs a judge"
  const e3 = boot();
  const flagged = clone(VIEW);
  flagged.states = { needs_judge: true };
  assert.ok(allText(section(mountViewer(e3, flagged, {}).root, 'ledger')).includes(NEEDS_JUDGE));
});

test('unsupported major version: a plain message, nothing thrown, nothing rendered', () => {
  const env = boot();
  const v2 = clone(VIEW);
  v2.schema = 'view-data/2';
  const { root } = mountViewer(env, v2, {});
  const t = allText(root);
  assert.ok(t.includes(EN['viewer.unsupported']));
  eq(byAttr(root, 'data-section'), []);
});

test('unmount removes the viewer\'s listeners: no re-render, no hash write afterwards', () => {
  const env = boot({ hash: '#map=MINI-A' });
  const { root, handle } = mountViewer(env, VIEW, {});
  handle.unmount();
  const before = allText(root);
  const hash = env.window.location.hash;
  env.SS.state.set({ rung: 'rung1' });
  env.SS.bus.emit('item:open', { item_id: 'a4', map: 'MINI-A', rung: 'rung1', lane: 'ai' });
  assert.strictEqual(allText(root), before);
  assert.strictEqual(env.window.location.hash, hash);
  assert.strictEqual(env.SS.state.get().item, null, 'item:open is no longer answered');
});

test('every string the drawer and the viewer use exists in en.json (no unknown-key errors)', () => {
  const env = boot({ hash: '#map=MINI-A&rung=rung2&item=a7' });
  const { root } = mountViewer(env, VIEW, { profile: clone(SYNTH) });
  env.SS.bus.emit('number:open', { row: 8, rung: 'rung3' });
  env.SS.state.set({ view: 'columns', item: 'a4' });
  eq(env.errors, []);
  assert.ok(allText(root).length > 100);
});

// ---------------------------------------------------------------- the dev page
// static/dev/viewer-full.html is generated from the fixtures and en.json (dev only, never shipped):
//   SS_WRITE_VIEWER_DEV_PAGE=1 node --test tests/js/drawer.test.js
// The test below fails whenever the page drifts from the fixtures, the strings or the script order.
const DEV_PAGE = path.join(ROOT, 'second_scribe', 'app', 'static', 'dev', 'viewer-full.html');
function devPage() {
  const js = (v, o) => 'window.' + v + ' = ' + JSON.stringify(o).replace(/<\//g, '<\\/').replace(/<!--/g, '\\u003c!--') + ';\n';
  return `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Viewer full dev page</title>
<link rel="stylesheet" href="../css/fonts.css">
<link rel="stylesheet" href="../css/tokens.css">
<link rel="stylesheet" href="../css/base.css">
<link rel="stylesheet" href="../css/viewer.css">
<link rel="stylesheet" href="../css/views.css">
<link rel="stylesheet" href="../css/ledger.css">
<link rel="stylesheet" href="../css/strip.css">
<link rel="stylesheet" href="../css/profile.css">
<style>
  /* dev page layout only */
  main { max-width: 1180px; margin: 0 auto; padding: 16px; }
  .devnote { font-family: var(--mono); font-size: 13px; color: var(--muted); margin: 0 0 12px; }
</style>
<script>
// Generated by tests/js/drawer.test.js (SS_WRITE_VIEWER_DEV_PAGE=1). Do not edit by hand.
${js('SS_STRINGS', EN)}window.SS_DATA = window.SS_DATA || {};
window.SS_DATA["mini"] = ${JSON.stringify(VIEW).replace(/<\//g, '<\\/')};
${js('SS_PROFILE_SYNTH', SYNTH)}</script>
<script src="data/tryit.js"></script>
<script>
  // Dev switches in the hash, besides the viewer's own (view, map, rung, item):
  //   theme=dark|light   w=390 (a real 390px frame)   ref=B (reference type B)   nojudge=1 (one model family)   profile=none   behind=<rung> (list the steps behind a number)
  //   data=tryit (the drifting test pattern from data/tryit.js, dev only; the default is the mini fixture)
  if (/(^|[#&])theme=dark($|&)/.test(location.hash)) document.documentElement.setAttribute("data-theme", "dark");
  else if (/(^|[#&])theme=light($|&)/.test(location.hash)) document.documentElement.setAttribute("data-theme", "light");
</script>
</head>
<body>
<main>
  <p class="devnote" id="devnote">Developer page: the full viewer on the mini fixture (or data=tryit) and the synthetic profile. Not shipped.</p>
  <div id="viewer"></div>
</main>
<script src="../js/core.js"></script>
<script src="../js/layout.js"></script>
<script src="../js/encoding.js"></script>
<script src="../js/a11y.js"></script>
<script src="../js/lanes.js"></script>
<script src="../js/state.js"></script>
<script src="../js/views.js"></script>
<script src="../js/ledger.js"></script>
<script src="../js/profile.js"></script>
<script src="../js/strip.js"></script>
<script src="../js/drawer.js"></script>
<script src="../js/viewer.js"></script>
<script>
  (function () {
    "use strict";
    // headless Chrome will not open a window narrower than 500px, so #w=390 shows the page in an iframe that wide
    var w = /(?:^|[#&])w=(\\d+)/.exec(location.hash);
    if (w && window.top === window) {
      document.body.textContent = "";
      document.body.style.margin = "0";
      var f = document.createElement("iframe");
      f.src = location.pathname + location.hash.replace(/(^#|&)w=\\d+/, "$1");
      f.setAttribute("title", "viewer at " + w[1] + "px");
      f.style.border = "0";
      f.style.display = "block";
      f.style.height = "100vh";
      f.style.width = w[1] + "px";
      document.body.appendChild(f);
      return;
    }
    var dev = {};
    location.hash.replace(/^#/, "").split("&").forEach(function (kv) {
      var i = kv.indexOf("=");
      if (i > 0) dev[kv.slice(0, i)] = decodeURIComponent(kv.slice(i + 1));
    });
    var view = JSON.parse(JSON.stringify(window.SS_DATA[dev.data === "tryit" ? "tryit" : "mini"]));
    if (dev.ref) view.reference_type = dev.ref;
    if (dev.nojudge) {
      delete view.run.models.judge;
      view.maps.forEach(function (m) { m.items = []; });
      view.states = { needs_judge: true };
    }
    SS.viewer.mount(document.getElementById("viewer"), view,
      dev.profile === "none" ? {} : { profile: window.SS_PROFILE_SYNTH });
    if (dev.behind) SS.bus.emit("number:open", { row: 5, rung: dev.behind });
    var de = document.documentElement;
    document.getElementById("devnote").textContent = de.scrollWidth > de.clientWidth ?
      "OVERFLOW: page " + de.scrollWidth + "px wide in a " + de.clientWidth + "px viewport" :
      "no horizontal overflow at " + de.clientWidth + "px";
  })();
</script>
</body>
</html>
`;
}
if (process.env.SS_WRITE_VIEWER_DEV_PAGE === '1') fs.writeFileSync(DEV_PAGE, devPage());

test('viewer_full_dev_page_is_current: fixtures, strings and the fixed script order', () => {
  assert.strictEqual(fs.readFileSync(DEV_PAGE, 'utf8'), devPage());
  const html = fs.readFileSync(DEV_PAGE, 'utf8');
  const scripts = [...html.matchAll(/<script src="\.\.\/js\/([a-z0-9]+\.js)">/g)].map((m) => m[1]);
  eq(scripts, ['core.js', 'layout.js', 'encoding.js', 'a11y.js', 'lanes.js', 'state.js', 'views.js', 'ledger.js',
    'profile.js', 'strip.js', 'drawer.js', 'viewer.js']);
  const css = [...html.matchAll(/<link rel="stylesheet" href="\.\.\/css\/([a-z]+\.css)">/g)].map((m) => m[1]);
  eq(css.slice(-5), ['viewer.css', 'views.css', 'ledger.css', 'strip.css', 'profile.css']);
});

// ---------------------------------------------------------------- 2.2 issue 1: "Show the steps at Rung N" shows its effect
function withScrollSpy(env, media) {
  const doc = env.window.document;
  const make = doc.createElement;
  env.scrolled = [];
  doc.createElement = (t) => {
    const n = make(t);
    n.scrollIntoView = (o) => env.scrolled.push({ node: n, opts: o });
    return n;
  };
  env.window.matchMedia = (q) => ({ matches: !!media && /prefers-reduced-motion/.test(q) });
}

test('issue 1 number:open scrolls the views panel into sight and focuses the checked rung radio', () => {
  const env = boot({ hash: '#map=MINI-A&rung=rung2' });
  withScrollSpy(env, false);
  const { root } = mountViewer(env, VIEW, { profile: clone(SYNTH) });
  env.focused.length = 0;
  env.SS.bus.emit('number:open', { row: 5, rung: 'rung1' });
  assert.strictEqual(env.SS.state.get().rung, 'rung1');
  assert.strictEqual(env.scrolled.length, 1, 'one scroll');
  assert.strictEqual(env.scrolled[0].node, section(root, 'views'), 'the views panel, which holds the rung selector');
  assert.strictEqual(env.scrolled[0].opts.block, 'start');
  assert.strictEqual(env.scrolled[0].opts.behavior, 'smooth');
  const radio = env.focused[env.focused.length - 1];
  assert.ok(radio && radio.tagName === 'INPUT' && attr(radio, 'type') === 'radio', 'a radio took focus');
  assert.strictEqual(attr(radio, 'value'), 'rung1');
  assert.strictEqual(radio.checked, true, 'the rung the button named, already applied');
  // pressing the button for the rung that is already chosen still shows the panel
  env.scrolled.length = 0;
  env.SS.bus.emit('number:open', { row: 5, rung: 'rung1' });
  assert.strictEqual(env.scrolled.length, 1);
  eq(env.errors, []);
});

test('issue 1 the scroll is instant when the reader prefers reduced motion', () => {
  const env = boot({ hash: '#map=MINI-A&rung=rung2' });
  withScrollSpy(env, true);
  mountViewer(env, VIEW, { profile: clone(SYNTH) });
  env.SS.bus.emit('number:open', { row: 5, rung: 'rung1' });
  assert.strictEqual(env.scrolled.length, 1);
  assert.strictEqual(env.scrolled[0].opts.behavior, 'auto');
  // a page that sets data-motion="reduce" on <html> (the site's own switch) counts too
  const env2 = boot({ hash: '#map=MINI-A&rung=rung2' });
  withScrollSpy(env2, false);
  env2.window.document.documentElement.setAttribute('data-motion', 'reduce');
  mountViewer(env2, VIEW, { profile: clone(SYNTH) });
  env2.SS.bus.emit('number:open', { row: 5, rung: 'rung1' });
  assert.strictEqual(env2.scrolled[0].opts.behavior, 'auto');
});

test('issue 1 a rung the run did not do neither scrolls nor moves focus', () => {
  const env = boot({ hash: '#map=MINI-A&rung=rung2' });
  withScrollSpy(env, false);
  mountViewer(env, VIEW, { profile: clone(SYNTH) });
  env.focused.length = 0;
  env.SS.bus.emit('number:open', { row: 5, rung: 'nonsense' });
  assert.strictEqual(env.scrolled.length, 0);
  assert.strictEqual(env.focused.length, 0);
});
