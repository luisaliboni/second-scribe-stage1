'use strict';
// JS-align: the viewer against what export_view.build_view really writes, where the hand-written mini fixture differs:
//   1. AI nodes with neither item_id nor gutter (mode C, AI maps only): plain AI boxes, by phase, in their own order
//   2. run.verify.status passed | failed, verify.errors (codes, in mono)
//   3. run.code_sha256 absent: "not recorded", never zeros
//   4. lanes.ai.<r>.status / lanes.script.status: "no map for this rung" in that lane
//   5. items with phase null: a trailing "no phase" column, not dropped
// The mini fixture is mutated on a copy; it is never edited here. Real exported views are mounted in real_views.test.js.
const test = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const { load, ROOT } = require('./harness');

const VIEW = JSON.parse(fs.readFileSync(path.join(ROOT, 'tests', 'fixtures', 'views', 'mini.view.json'), 'utf8'));
const EN = JSON.parse(fs.readFileSync(path.join(ROOT, 'second_scribe', 'app', 'strings', 'en.json'), 'utf8'));
const clone = (o) => JSON.parse(JSON.stringify(o));

function boot() {
  const env = load();
  const doc = env.window.document;
  const make = doc.createElement;
  doc.createElement = (tag) => {
    const n = make(tag);
    n.listeners = {};
    n.addEventListener = (type, fn) => { (n.listeners[type] = n.listeners[type] || []).push(fn); };
    n.focus = () => {};
    let text = n.textContent;
    Object.defineProperty(n, 'textContent', {
      get: () => text,
      set: (v) => { text = v; if (v === '') n.children.length = 0; },
    });
    return n;
  };
  env.window.location.hash = '';
  return env;
}
function walk(node, fn) { fn(node); (node.children || []).forEach((c) => walk(c, fn)); }
function find(root, pred) { const out = []; walk(root, (n) => { if (pred(n)) out.push(n); }); return out; }
const attr = (n, k) => (n.attributes ? n.attributes[k] : undefined);
const byAttr = (root, k, v) => find(root, (n) => attr(n, k) !== undefined && (v === undefined || attr(n, k) === v));
const dups = (list) => list.filter((x, i) => list.indexOf(x) !== i);
const hasClass = (n, c) => (' ' + (n.className || '') + ' ').includes(' ' + c + ' ');
function allText(root) {
  const out = [];
  walk(root, (n) => { if (n.textContent) out.push(n.textContent); });
  return out.join('\n');
}
function count(text, needle) { return text.split(needle).length - 1; }
function section(root, name) {
  const hit = byAttr(root, 'data-section', name);
  assert.strictEqual(hit.length, 1, 'one ' + name + ' section');
  return hit[0];
}
function mountViewer(env, view, opts) {
  const root = env.window.document.createElement('div');
  env.SS.viewer.mount(root, clone(view), opts);
  return root;
}
function renderLanes(env, view, rung, opts) {
  const root = env.window.document.createElement('div');
  const handle = env.SS.lanes.render(root, view, 0, rung, opts);
  return { root, handle };
}

// A mode C view: no reference, so no items; the AI nodes carry neither item_id nor gutter.
function modeC() {
  const v = clone(VIEW);
  v.reference_type = 'C';
  v.maps = [{
    pid: 'C-1', role: 'owner',
    lanes: {
      script: { nodes: [{ node_id: 's1', label: 'Submit application', phase: 'Before applying' },
                        { node_id: 's2', label: 'Receive permit', phase: 'After approval' }] },
      reference: { nodes: [] },
      ai: {
        rung1: { nodes: [{ node_id: 'x1', label: 'Call the office', phase: 'Before applying' }] },
        rung2: { nodes: [
          { node_id: 'n1', label: 'Meet the planner', phase: 'Before applying' },
          { node_id: 'n2', label: 'Health inspection', phase: 'Inspection' },
          { node_id: 'n3', label: 'Pay the fee', phase: 'Before applying' },
          { node_id: 'n4', label: 'Wait for a letter', phase: null },
        ] },
        placebo: { nodes: [] },
        rung3: { nodes: [{ node_id: 'y1', label: 'Fire safety check', phase: 'Inspection' }] },
      },
    },
    items: [], off_record: [],
  }];
  v.numbers = { per_rung: {}, verdict: { code: 'no_reference' } };
  delete v.states;
  return v;
}

// ---------------------------------------------------------------- 1. plain AI nodes

test('plain_nodes_model: no items -> AI nodes placed by phase, in their own order, no fate', () => {
  const { SS } = load();
  const v = modeC();
  const m = SS.lanes.model(v, 0, 'rung2');
  assert.strictEqual(m.plain, true);
  const ai = m.lanes.filter((l) => l.key === 'ai')[0];
  assert.deepStrictEqual(clone(ai.plain.map((n) => n.node_id)), ['n1', 'n2', 'n3', 'n4']);
  const col = (id) => ai.plain.filter((n) => n.node_id === id)[0];
  // the phase columns follow the Setup order; n1 and n3 share one, n3 after n1 (own order)
  assert.strictEqual(col('n1').col, 0);
  assert.strictEqual(col('n3').col, 0);
  assert.ok(col('n3').x > col('n1').x);
  assert.strictEqual(col('n2').col, 1);
  // the node without a phase goes to the trailing no-phase column
  const cols = m.layout.columns;
  assert.strictEqual(cols[cols.length - 1].noPhase, true);
  assert.strictEqual(col('n4').col, cols.length - 1);
  // no fate on any of them, and the nodes sit inside their own column
  ai.plain.forEach((n) => {
    assert.strictEqual(n.enc.shape, 'normal');
    assert.strictEqual(n.enc.glyph, '');
    assert.strictEqual(n.enc.tag, undefined);
    const c = cols[n.col];
    assert.ok(n.x >= c.x0 && n.x + SS.layout.ITEM_W <= c.x1);
  });
  // a lane with nothing to draw (reference in mode C) is left out; the no-interview lane is there
  assert.deepStrictEqual(clone(m.lanes.map((l) => l.key)), ['script', 'ai']);
});

test('plain_nodes_stable_columns: the columns do not change with the rung', () => {
  const { SS } = load();
  const v = modeC();
  const a = SS.lanes.model(v, 0, 'rung1');
  const b = SS.lanes.model(v, 0, 'rung2');
  assert.deepStrictEqual(clone(a.layout.columns), clone(b.layout.columns));
  assert.strictEqual(a.layout.width, b.layout.width);
});

test('plain_nodes_render: each AI node drawn once as a plain box, no button, no fate, gloss once', () => {
  const env = boot();
  const { root } = renderLanes(env, modeC(), 'rung2');
  const boxes = byAttr(root, 'data-plain', 'true').filter((n) => attr(n, 'data-lane') === 'ai');
  assert.deepStrictEqual(boxes.map((n) => attr(n, 'data-node-id')), ['n1', 'n2', 'n3', 'n4']);
  boxes.forEach((n) => {
    assert.ok(hasClass(n, 'ss-lane-ai') && hasClass(n, 'ss-shape-normal'));
    assert.notStrictEqual(n.tagName, 'BUTTON');
    assert.strictEqual(find(n, (c) => hasClass(c, 'ss-item-glyph') || hasClass(c, 'ss-item-tag')).length, 0);
  });
  const t = allText(root);
  assert.strictEqual(count(t, EN['lane.no_interview_gloss']), 1);
  assert.strictEqual(count(t, EN['lane.ai']), 1);
  assert.ok(!t.includes(EN['views.gutter.title']));
  assert.ok(!t.includes(EN['lane.empty']));
  assert.ok(!t.includes(EN['lane.reference']));
  // the no-interview lane draws its own nodes in the grey shape
  const grey = byAttr(root, 'data-plain', 'true').filter((n) => attr(n, 'data-lane') === 'script');
  assert.deepStrictEqual(grey.map((n) => attr(n, 'data-node-id')), ['s1', 's2']);
  grey.forEach((n) => assert.ok(hasClass(n, 'ss-shape-script')));
});

test('plain_nodes_every_view: overlay and columns fall back to the plain lanes (nothing to compare)', () => {
  for (const view of ['lanes', 'overlay', 'columns']) {
    const env = boot();
    env.window.location.hash = '#view=' + view + '&rung=rung2';
    const root = mountViewer(env, modeC(), {});
    const t = allText(section(root, 'views'));
    const ai = byAttr(root, 'data-plain', 'true').filter((n) => attr(n, 'data-lane') === 'ai');
    assert.strictEqual(ai.length, 4, view);
    assert.ok(!t.includes(EN['views.gutter.title']), view + ': no "only the AI drew" in mode C');
    assert.strictEqual(count(allText(root), EN['lane.no_interview_gloss']), 1, view);
    assert.deepStrictEqual(env.errors, [], view);
  }
});

test('plain_nodes_switch_rung: only the AI lane changes with the rung', () => {
  const env = boot();
  env.window.location.hash = '#rung=rung1';
  const root = mountViewer(env, modeC(), {});
  const ids = () => byAttr(root, 'data-plain', 'true').filter((n) => attr(n, 'data-lane') === 'ai').map((n) => attr(n, 'data-node-id'));
  assert.deepStrictEqual(ids(), ['x1']);
  env.SS.state.set({ rung: 'rung3' });
  assert.deepStrictEqual(ids(), ['y1']);
  assert.deepStrictEqual(byAttr(root, 'data-plain', 'true').filter((n) => attr(n, 'data-lane') === 'script').length, 2);
});

// ---------------------------------------------------------------- 4. missing maps

test('missing_ai_map: the AI lane says there is no map for this rung', () => {
  const env = boot();
  const v = clone(VIEW);
  v.maps[0].lanes.ai.rung1 = { nodes: [], status: 'not_run' };
  const { root, handle } = renderLanes(env, v, 'rung1');
  const lane = byAttr(root, 'data-lane-row', 'ai')[0];
  assert.ok(lane, 'the AI lane section');
  assert.strictEqual(count(allText(lane), EN['lane.no_map_ai']), 1);
  // no item box is drawn in the AI lane, and the reading of the other rungs is untouched
  assert.strictEqual(find(lane, (n) => n.tagName === 'BUTTON').length, 0);
  assert.ok(find(root, (n) => attr(n, 'data-lane') === 'reference').length > 0);
  assert.strictEqual(handle.model.lanes.filter((l) => l.key === 'ai')[0].targets.length, 0);
  // another rung still draws
  const r2 = renderLanes(env, v, 'rung2');
  assert.strictEqual(count(allText(r2.root), EN['lane.no_map_ai']), 0);
  assert.ok(find(r2.root, (n) => attr(n, 'data-lane') === 'ai' && n.tagName === 'BUTTON').length > 0);
});

test('missing_script_map: the no-interview lane says so, in every view', () => {
  for (const view of ['lanes', 'overlay', 'columns']) {
    const env = boot();
    env.window.location.hash = '#view=' + view;
    const v = clone(VIEW);
    v.maps.forEach((m) => { m.lanes.script = { nodes: [], status: 'not_run' }; });
    const root = mountViewer(env, v, {});
    // item 20 (23 Oct): whatever view the hash names, the viewer draws the lanes, which say it once
    assert.strictEqual(count(allText(root), EN['lane.no_map_script']), 1, view);
    assert.deepStrictEqual(env.errors, [], view);
  }
});

test('missing_ai_map_in_columns_and_overlay', () => {
  for (const view of ['columns', 'overlay']) {
    const env = boot();
    env.window.location.hash = '#view=' + view + '&rung=rung1';
    const v = clone(VIEW);
    v.maps[0].lanes.ai.rung1 = { nodes: [], status: 'failed' };
    const root = mountViewer(env, v, {});
    assert.strictEqual(count(allText(section(root, 'views')), EN['lane.no_map_ai']), 1, view);
    assert.deepStrictEqual(env.errors, [], view);
  }
});

// ---------------------------------------------------------------- 5. phase null

test('phase_null_item_gets_a_no_phase_column', () => {
  const { SS } = load();
  const v = clone(VIEW);
  const map = v.maps[0];
  map.items[0].phase = null;                                  // a1 "Pre-application meeting"
  const p = SS.layout.place(map, v.phases);
  const last = p.columns[p.columns.length - 1];
  assert.strictEqual(last.noPhase, true);
  assert.strictEqual(last.phase, null);
  assert.strictEqual(SS.layout.columnName(last), EN['lane.no_phase']);
  assert.deepStrictEqual(clone(p.columns.slice(0, -1).map((c) => c.phase)), v.phases);
  const s = p.slots[map.items[0].item_id];
  assert.ok(s, 'the item has a place');
  assert.strictEqual(s.col, p.columns.length - 1);
  // a map where every item has a phase gets no such column
  assert.ok(!SS.layout.place(VIEW.maps[0], VIEW.phases).columns.some((c) => c.noPhase));
});

test('phase_null_item_is_drawn_in_every_view', () => {
  for (const view of ['lanes', 'overlay', 'columns']) {
    const env = boot();
    env.window.location.hash = '#view=' + view;
    const v = clone(VIEW);
    v.maps[0].items[0].phase = null;
    const id = v.maps[0].items[0].item_id;
    const root = mountViewer(env, v, {});
    const boxes = find(root, (n) => n.tagName === 'BUTTON' && attr(n, 'data-item-id') === id && attr(n, 'data-lane') === 'reference');
    assert.strictEqual(boxes.length, 1, view);
    assert.ok(allText(section(root, 'views')).includes(EN['lane.no_phase']), view);
    assert.deepStrictEqual(env.errors, [], view);
  }
});

test('phase_null_gutter_node_goes_to_the_no_phase_column', () => {
  const env = boot();
  env.window.location.hash = '#view=overlay&rung=rung2';
  const v = clone(VIEW);
  const g = v.maps[0].lanes.ai.rung2.nodes.filter((n) => n.gutter)[0];
  g.phase = null;
  const root = mountViewer(env, v, {});
  // item 20 (23 Oct): the viewer draws the AI-only nodes as plain boxes of the AI lane, the null-phase one in the
  // "No phase" column (the overlay model, kept in the kit, places it the same way)
  const plainAi = find(root, (n) => hasClass(n, 'ss-plain-node') && attr(n, 'data-lane') === 'ai');
  assert.strictEqual(plainAi.length, v.maps[0].lanes.ai.rung2.nodes.filter((n) => n.gutter).length);
  const lm = env.SS.lanes.model(clone(v), 0, 'rung2');
  const lastCol = lm.layout.columns.length - 1;
  assert.strictEqual(lm.lanes.filter((l) => l.key === 'ai')[0].plain.filter((n) => n.node_id === g.node_id)[0].col, lastCol);
  const m = env.SS.views.overlayModel(clone(v), 0, 'rung2');
  const last = m.layout.columns.length - 1;
  assert.strictEqual(m.layout.columns[last].noPhase, true);
  assert.strictEqual(m.gutter.nodes.filter((n) => n.node_id === g.node_id)[0].col, last);
});

// ---------------------------------------------------------------- 2 and 3. the run: verify and the code hash

function drawerRoot(env, view, hash) {
  env.window.location.hash = hash || '';
  return section(mountViewer(env, view, {}), 'drawer');
}

test('verify_failed_shows_error_codes_in_mono', () => {
  const env = boot();
  const v = clone(VIEW);
  v.run.verify = { status: 'failed', records: 12, checked_at: '2026-10-18T10:00:00Z', errors: ['chain_break:7', 'head_mismatch'] };
  const dr = drawerRoot(env, v, '#map=MINI-A&rung=rung2&item=a1');
  const t = allText(dr);
  assert.ok(t.includes(EN['drawer.verify.failed']));
  for (const code of ['chain_break:7', 'head_mismatch']) {
    const hit = find(dr, (n) => n.textContent === code);
    assert.ok(hit.length >= 1, code + ' is shown');
    assert.ok(hit.every((n) => hasClass(n, 'mono')), code + ' is in mono');
  }
  // the model carries them as data too
  const chain = env.SS.drawer.chain(clone(v), { map: 'MINI-A', rung: 'rung2', item: 'a1' });
  const last = chain[chain.length - 1];
  assert.strictEqual(last.step, 'verify');
  assert.deepStrictEqual(clone(last.errors), ['chain_break:7', 'head_mismatch']);
});

test('verify_passed_shows_no_error_codes', () => {
  const env = boot();
  const v = clone(VIEW);
  v.run.verify = { status: 'passed', records: 12, checked_at: '2026-10-18T10:00:00Z', errors: [] };
  const dr = drawerRoot(env, v, '#map=MINI-A&rung=rung2&item=a1');
  assert.ok(allText(dr).includes(EN['drawer.verify.passed']));
  assert.strictEqual(find(dr, (n) => hasClass(n, 'ss-drawer-error-code')).length, 0);
});

test('run_block_without_item_shows_verify_and_code_hash', () => {
  const env = boot();
  const v = clone(VIEW);
  delete v.run.code_sha256;
  v.run.verify = { status: 'failed', records: 3, checked_at: '2026-10-18T10:00:00Z', errors: ['log_missing'] };
  const dr = drawerRoot(env, v, '');
  const t = allText(dr);
  assert.strictEqual(count(t, EN['drawer.run.code_none']), 1);
  assert.ok(t.includes(EN['drawer.verify.failed']));
  assert.ok(find(dr, (n) => n.textContent === 'log_missing' && hasClass(n, 'mono')).length === 1);
  assert.ok(!/0{16}/.test(t), 'no run of zeros');
});

test('code_hash_absent_or_zero_reads_not_recorded_never_zeros', () => {
  const { SS } = load();
  const absent = clone(VIEW);
  delete absent.run.code_sha256;
  const zeros = clone(VIEW);                                   // the hand-written fixture's stand-in
  const real = clone(VIEW);
  real.run.code_sha256 = 'ab12'.repeat(16);
  const code = (v) => SS.drawer.runInfo(v).code;
  assert.deepStrictEqual(clone(code(absent)), { value: EN['drawer.run.code_none'], missing: true });
  assert.deepStrictEqual(clone(code(zeros)), { value: EN['drawer.run.code_none'], missing: true });
  assert.deepStrictEqual(clone(code(real)), { value: 'ab12'.repeat(16), missing: false });
  const none = SS.drawer.runInfo({});
  assert.strictEqual(none.code.missing, true);
  assert.strictEqual(none.verify, null);
});

test('run_block_in_the_one_family_drawer_too', () => {
  const env = boot();
  const v = clone(VIEW);
  delete v.run.models.judge;
  v.states = { needs_judge: true };
  delete v.run.code_sha256;
  const dr = drawerRoot(env, v, '');
  const t = allText(dr);
  assert.ok(t.includes(EN['common.needs_judge']));
  assert.strictEqual(count(t, EN['drawer.run.code_none']), 1);
});

// viewer-fix: the no-interview lane is drawn from its own nodes. A node on a step (item_id) is that step's button at the
// step's x; a second node on the same step, a gutter node and a node with neither field go to the rows below, in their
// phase column (the step's column for the second node; no phase -> the "No phase" column; an unknown phase -> the
// first), filling the column's slots in array order. Columns keep the width of their steps.
function withScriptGutter() {
  const v = clone(VIEW);
  v.maps[0].lanes.script.nodes.push(
    { node_id: 's-dup', label: 'Apply online', phase: 'Before applying', item_id: 'a2' },
    { node_id: 's-g3', label: 'Somewhere else', phase: 'Elsewhere', gutter: true },
    { node_id: 's-g1', label: 'Read the bylaw', phase: 'Inspection', gutter: true },
    { node_id: 's-g2', label: 'Ask a friend', phase: null, gutter: true },
    { node_id: 's-bare', label: 'Open a bank account', phase: 'After approval' },
    { node_id: 's-g6', label: 'Find a kitchen', phase: 'Before applying', gutter: true });
  return v;
}

test('script_nodes_on_steps_and_after_the_steps_of_their_phase', () => {
  const { SS } = load();
  const v = withScriptGutter();
  const map = v.maps[0];
  const L = SS.layout.place(map, v.phases);
  const before = SS.layout.place(VIEW.maps[0], VIEW.phases);
  const col = (name) => L.columns.findIndex((c) => (name === null ? c.noPhase === true : c.phase === name));
  const at = {};
  L.script.forEach((e) => { at[e.node_id] = e; });
  assert.deepStrictEqual(clone(L.script.map((e) => e.node_id)), map.lanes.script.nodes.map((n) => n.node_id), 'array order');
  for (const id of ['s-a2', 's-a3', 's-a4', 's-a6']) {
    assert.strictEqual(at[id].item_id, id.slice(2));
    assert.strictEqual(at[id].x, L.slots[id.slice(2)].x, id + ' over its step');
  }
  const S = SS.layout.SLOT_W;
  const x0 = (c) => L.columns[c].x0 + SS.layout.COL_PAD;
  for (const id of ['s-a2', 's-a3', 's-a4', 's-a6']) assert.strictEqual(at[id].row, 0);
  const pos = (id) => [at[id].col, at[id].row, at[id].x];
  // Before applying has 2 slots (a1, a2): the second node on a2, the unknown-phase node, then a wrap to the next row
  const ba = col('Before applying');
  assert.strictEqual(at['s-dup'].item_id, undefined);
  assert.deepStrictEqual(clone(pos('s-dup')), [ba, 1, x0(ba)]);
  assert.deepStrictEqual(clone(pos('s-g3')), [ba, 1, x0(ba) + S]);
  assert.deepStrictEqual(clone(pos('s-g6')), [ba, 2, x0(ba)]);
  assert.deepStrictEqual(clone(pos('s-g1')), [col('Inspection'), 1, x0(col('Inspection'))]);
  assert.deepStrictEqual(clone(pos('s-bare')), [col('After approval'), 1, x0(col('After approval'))]);
  assert.ok(col(null) >= 0, 'a no-phase column for the node with no phase');
  assert.deepStrictEqual(clone(pos('s-g2')), [col(null), 1, x0(col(null))]);
  assert.strictEqual(L.scriptRows, 3);
  // the columns keep the width of their steps
  assert.deepStrictEqual(clone(L.columns.map((c) => c.slots)), [2, 3, 2, 1, 1]);
  assert.strictEqual(before.scriptRows, 1);
  // the mini fixture's own nodes all sit on steps: its columns keep the width of their steps
  assert.deepStrictEqual(clone(before.columns.map((c) => c.slots)), [2, 3, 2, 1]);
  assert.ok(before.script.every((e) => e.item_id), 'mini: every node on a step');
});

test('script_nodes_each_drawn_once_in_lanes_and_columns', () => {
  const v = withScriptGutter();
  const want = v.maps[0].lanes.script.nodes.map((n) => n.node_id).sort();
  for (const view of ['lanes', 'columns']) {
    for (const rung of ['rung1', 'rung2']) {
      const env = boot();
      const root = env.window.document.createElement('div');
      env.SS.views.render(root, v, { view, map: 'MINI-A', rung });
      const drawn = byAttr(root, 'data-lane', 'script');
      const ids = drawn.map((n) => attr(n, 'data-node-id'));
      assert.deepStrictEqual(ids.slice().sort(), want, view + ' ' + rung);
      const buttons = drawn.filter((n) => n.tagName === 'BUTTON').map((n) => attr(n, 'data-item-id')).sort();
      assert.deepStrictEqual(buttons, ['a2', 'a3', 'a4', 'a6'], view + ': one button per step a node sits on');
      const plain = drawn.filter((n) => n.tagName !== 'BUTTON');
      plain.forEach((n) => assert.strictEqual(attr(n, 'data-plain'), 'true'));
      const label = (id) => drawn.filter((n) => attr(n, 'data-node-id') === id)[0];
      assert.ok(allText(label('s-dup')).includes('Apply online'), 'a node shows its own label');
      if (view === 'lanes') {
        const R = env.SS.lanes.ROW_H;
        const E = env.SS.lanes.EXTRA_ROW_H;
        const T = env.SS.lanes.ROW_TOP;
        // row 0 holds full item boxes; the extra rows of plain nodes stack at the shorter pitch E (T45 spacing)
        assert.ok(E < R && E >= 44 + 12, 'a plain node (44px) plus a gap fits the extra-row pitch');
        assert.strictEqual(label('s-dup').style.top, R + 'px');
        assert.strictEqual(label('s-g6').style.top, (R + E) + 'px');
        const track = find(byAttr(root, 'data-lane-row', 'script')[0], (n) => hasClass(n, 'ss-track'))[0];
        assert.strictEqual(track.style.height, (R + 2 * E) + 'px', 'the lane is three rows tall');
        assert.strictEqual(env.SS.lanes.rowTop(0), T);
        assert.strictEqual(env.SS.lanes.trackHeight(1), R);
        // nothing of the no-interview lane sits over a step it does not match: row 0 holds only buttons
        plain.forEach((n) => assert.ok(n.style.top !== undefined && n.style.top !== (T + 'px'), attr(n, 'data-node-id')));
      }
      if (view === 'columns') {
        // the extra nodes get rows of their own, after the steps of their phase, empty in the other two columns
        const tops = (lane) => byAttr(root, 'data-lane', lane).map((n) => n.style.top);
        const ref = tops('reference');
        plain.forEach((n) => assert.ok(!ref.includes(n.style.top), attr(n, 'data-node-id') + ' has its own row'));
        assert.deepStrictEqual(dups(plain.map((n) => n.style.top)), [], 'one row each');
        // in the no-interview map's own order within a phase (s-dup, s-g3, s-g6 in Before applying)
        const y = (id) => parseFloat(label(id).style.top);
        assert.ok(y('s-dup') < y('s-g3') && y('s-g3') < y('s-g6'), 'array order');
        // plain nodes stack at their own, shorter pitch (T45 spacing): consecutive node rows are NODE_ROW_H apart
        const N = env.SS.views.NODE_ROW_H;
        assert.ok(N < env.SS.views.ROW_H && N >= 44 + 12, 'a plain node (44px) plus a gap fits the node-row pitch');
        assert.strictEqual(y('s-g3') - y('s-dup'), N);
      }
      assert.deepStrictEqual(env.errors, []);
    }
  }
});

// council 30 (seat 02 F3): with opts.runHost the run block (run id, code version, check of the whole log) is printed
// once in the host the page gives (its Technical details); the evidence panel opens on its hint alone and a chosen
// step shows only its own chain
test('run_host_takes_the_run_block_and_the_panel_opens_on_the_hint', () => {
  const env = boot();
  const v = clone(VIEW);
  v.run.verify = { status: 'passed', records: 12, checked_at: '2026-10-18T10:00:00Z', errors: [] };
  const doc = env.window.document;
  const host = doc.createElement('div');
  env.window.location.hash = '';
  const root = mountViewer(env, v, { runHost: host });
  const dr = section(root, 'drawer');
  const dt = allText(dr);
  assert.ok(dt.includes(EN['drawer.empty']), 'the hint');
  assert.ok(!dt.includes(EN['drawer.run.title']), 'no run block in the panel');
  assert.ok(!dt.includes(EN['drawer.verify.passed']), 'no log check in the panel');
  const ht = allText(host);
  assert.strictEqual(count(ht, EN['drawer.run.title']), 1, 'the run block is in the host once');
  assert.ok(ht.includes(EN['drawer.verify.passed']));
  // a chosen step: its chain, still no run block in the panel
  env.window.location.hash = '#map=MINI-A&rung=rung2&item=a1';
  const root2 = mountViewer(env, v, { runHost: doc.createElement('div') });
  const t2 = allText(section(root2, 'drawer'));
  assert.ok(!t2.includes(EN['drawer.run.title']), t2);
});

// council 30 (seat 02 F3), app Results: the run block sits in the run's Technical details; the run-id line is printed
// there only when the view names no run or another one
test('app_results_run_block_in_technical_details', () => {
  const src = fs.readFileSync(path.join(ROOT, 'second_scribe', 'app', 'static', 'js', 'app-results.js'), 'utf8');
  assert.match(src, /vopts\.runHost = runHost;/);
  assert.match(src, /if \(vrun !== o\.run_id\) dBody\.appendChild/);
});
