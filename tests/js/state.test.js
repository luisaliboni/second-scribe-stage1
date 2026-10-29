'use strict';
// Task 24: shared state with hash round-trip, and views B (overlay) and C (columns) beside the lanes.
// The fixture is SYNTHETIC (tests/fixtures/views/mini.view.json). The stub DOM has no real events,
// so the tests wrap createElement to record listeners and call them directly (as layout.test.js does).
const test = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const { load, ROOT } = require('./harness');

const VIEW = JSON.parse(fs.readFileSync(path.join(ROOT, 'tests', 'fixtures', 'views', 'mini.view.json'), 'utf8'));
const clone = (o) => JSON.parse(JSON.stringify(o));
const RUNGS = ['rung1', 'rung2', 'placebo', 'rung3'];
// Arrays and objects built inside the vm context belong to another realm: compare after a JSON round-trip.
const eq = (a, b, msg) => assert.deepStrictEqual(clone(a), b, msg);

function boot() {
  const env = load();
  const doc = env.window.document;
  const make = doc.createElement;
  const focused = [];
  doc.createElement = (tag) => {
    const n = make(tag);
    n.listeners = {};
    n.addEventListener = (type, fn) => { (n.listeners[type] = n.listeners[type] || []).push(fn); };
    n.focus = () => { focused.push(n); (n.listeners.focus || []).forEach((f) => f({ target: n })); };
    return n;
  };
  env.focused = focused;
  env.SS.state.use(clone(VIEW));
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
function buttons(root) { return find(root, (n) => n.tagName === 'BUTTON'); }
function allText(root) {
  const out = [];
  walk(root, (n) => { if (n.textContent) out.push(n.textContent); });
  return out.join('\n');
}
function hasClass(n, c) { return (' ' + (n.className || '') + ' ').indexOf(' ' + c + ' ') >= 0; }
function renderView(env, st, opts) {
  const root = env.window.document.createElement('div');
  const handle = env.SS.views.render(root, clone(VIEW), st, opts);
  return { root, handle };
}
function key(btn, k) {
  let prevented = false;
  (btn.listeners.keydown || []).forEach((f) => f({ key: k, preventDefault() { prevented = true; } }));
  return prevented;
}

// ---------------------------------------------------------------- state

test('hash_roundtrip', () => {
  const { SS } = boot();
  assert.strictEqual(SS.state.toHash({ view: 'overlay', map: 'MINI-A', rung: 'rung3', item: 'a1' }),
    '#view=overlay&map=MINI-A&rung=rung3&item=a1');
  assert.strictEqual(SS.state.toHash({ view: 'lanes', map: 'MINI-A', rung: 'rung2', item: null }),
    '#view=lanes&map=MINI-A&rung=rung2');
  const cases = [];
  for (const v of ['lanes', 'overlay', 'columns']) {
    for (const rung of RUNGS) {
      cases.push({ view: v, map: 'MINI-A', rung, item: null });
      cases.push({ view: v, map: 'MINI-A', rung, item: 'a4' });
      cases.push({ view: v, map: 'MINI-B', rung, item: 'b3' });
    }
  }
  for (const s of cases) {
    assert.deepStrictEqual(JSON.parse(JSON.stringify(SS.state.fromHash(SS.state.toHash(s)))), s);
  }
  // the leading # is optional on the way in
  assert.deepStrictEqual(JSON.parse(JSON.stringify(SS.state.fromHash('view=columns&map=MINI-B&rung=placebo&item=b1'))),
    { view: 'columns', map: 'MINI-B', rung: 'placebo', item: 'b1' });
});

test('hash_roundtrip with ids that need escaping (no view bound)', () => {
  const { SS } = load();
  const s = { view: 'overlay', map: 'MAP 1&x=2#y', rung: 'rung2', item: 'a/b?c' };
  const h = SS.state.toHash(s);
  assert.doesNotMatch(h.slice(1), /[ #?\/]/);
  assert.strictEqual(h.split('&').length, 4);
  assert.deepStrictEqual(JSON.parse(JSON.stringify(SS.state.fromHash(h))), s);
});

test('bad_hash_falls_back', () => {
  const { SS } = boot();
  const DEFAULT = { view: 'lanes', map: 'MINI-A', rung: 'rung2', item: null };
  const bad = ['', '#', '#&&&', '#view=zzz&map=NOPE&rung=nope&item=zz', '#view=&map=&rung=&item=',
    '#view=%E0%A4%A&map=%E0%A4%A&rung=%ZZ&item=%', '#__proto__=1&constructor=2&toString=3',
    '#view=lanes=overlay', '#no-equals-sign'];
  for (const h of bad) {
    assert.deepStrictEqual(JSON.parse(JSON.stringify(SS.state.fromHash(h))), DEFAULT, JSON.stringify(h));
  }
  assert.deepStrictEqual(JSON.parse(JSON.stringify(SS.state.fromHash(undefined))), DEFAULT);
  assert.deepStrictEqual(JSON.parse(JSON.stringify(SS.state.fromHash(null))), DEFAULT);
  // one bad part does not spoil the good ones
  assert.deepStrictEqual(JSON.parse(JSON.stringify(SS.state.fromHash('#view=columns&map=NOPE&rung=placebo&item=zz'))),
    { view: 'columns', map: 'MINI-A', rung: 'placebo', item: null });
  // an item that belongs to the other map is dropped
  assert.strictEqual(SS.state.fromHash('#map=MINI-A&item=b1').item, null);
  assert.strictEqual(SS.state.fromHash('#map=MINI-B&item=b1').item, 'b1');
  // a rung the data does not carry falls back to rung2
  assert.strictEqual(SS.state.fromHash('#rung=rung9').rung, 'rung2');
});

test('state starts at the fallbacks and get() hands out a copy', () => {
  const { SS } = boot();
  assert.deepStrictEqual(JSON.parse(JSON.stringify(SS.state.get())),
    { view: 'lanes', map: 'MINI-A', rung: 'rung2', item: null });
  const s = SS.state.get();
  s.view = 'columns';
  s.item = 'a1';
  assert.strictEqual(SS.state.get().view, 'lanes');
  assert.strictEqual(SS.state.get().item, null);
});

test('switching_view_keeps_map_rung_item', () => {
  const { SS } = boot();
  const heard = [];
  SS.bus.on('state:change', (p) => heard.push(p));
  SS.state.set({ map: 'MINI-A', rung: 'rung3', item: 'a4' });
  heard.length = 0;
  for (const v of ['overlay', 'columns', 'lanes', 'columns']) {
    SS.state.set({ view: v });
    assert.deepStrictEqual(JSON.parse(JSON.stringify(SS.state.get())),
      { view: v, map: 'MINI-A', rung: 'rung3', item: 'a4' });
  }
  assert.strictEqual(heard.length, 4);
  assert.deepStrictEqual(JSON.parse(JSON.stringify(heard[0].changed)), ['view']);
  assert.strictEqual(heard[0].prev.view, 'lanes');
  assert.strictEqual(heard[0].state.view, 'overlay');
  // the hash the viewer writes for the new view still names the same map, rung and item
  assert.strictEqual(SS.state.toHash(SS.state.get()), '#view=columns&map=MINI-A&rung=rung3&item=a4');
});

test('state:change fires on a real change only, with what changed', () => {
  const { SS } = boot();
  const heard = [];
  SS.bus.on('state:change', (p) => heard.push(JSON.parse(JSON.stringify(p))));
  SS.state.set({ view: 'lanes' });          // already lanes
  SS.state.set({});                          // nothing
  assert.strictEqual(heard.length, 0);
  SS.state.set({ rung: 'placebo', item: 'a1' });
  assert.strictEqual(heard.length, 1);
  eq(heard[0].changed.sort(), ['item', 'rung']);
  assert.strictEqual(heard[0].state.rung, 'placebo');
  assert.strictEqual(heard[0].prev.rung, 'rung2');
});

test('changing the map keeps view and rung and drops an item the new map does not have', () => {
  const { SS } = boot();
  SS.state.set({ view: 'columns', rung: 'placebo', item: 'a1' });
  SS.state.set({ map: 'MINI-B' });
  assert.deepStrictEqual(JSON.parse(JSON.stringify(SS.state.get())),
    { view: 'columns', map: 'MINI-B', rung: 'placebo', item: null });
  // set with unknown values lands on the same fallbacks as a hash
  SS.state.set({ view: 'nope', rung: 'nope', map: 'nope', item: 'nope', extra: 1 });
  assert.deepStrictEqual(JSON.parse(JSON.stringify(SS.state.get())),
    { view: 'lanes', map: 'MINI-A', rung: 'rung2', item: null });
});

test('use(view) rebinds: a new view data set re-checks the current state', () => {
  const { SS } = boot();
  SS.state.set({ map: 'MINI-B', item: 'b3' });
  const only = clone(VIEW);
  only.maps = [only.maps[0]];
  SS.state.use(only);
  assert.deepStrictEqual(JSON.parse(JSON.stringify(SS.state.get())),
    { view: 'lanes', map: 'MINI-A', rung: 'rung2', item: null });
});

// ---------------------------------------------------------------- stamp

test('stamp_text', () => {
  // item 20 (23 Oct): one view, so the stamp names the map and the instructions in plain words, never a view
  const { SS } = boot();
  assert.strictEqual(SS.views.stamp({ view: 'lanes', map: 'SYN-OW03', rung: 'rung2', item: null }),
    "Map SYN-OW03 · The study's instructions (rung 2)");
  assert.strictEqual(SS.views.stamp({ view: 'columns', map: 'MINI-A', rung: 'rung3' }),
    'Map MINI-A · A remedy with two prompting strategies of Zhou et al. 2023 (rung 3)');
  assert.strictEqual(SS.views.stamp({ view: 'overlay', map: 'MINI-A', rung: 'placebo' }),
    'Map MINI-A · A placebo matched to the remedy in length and format');
  assert.strictEqual(SS.views.stamp({ view: 'overlay', map: 'MINI-A', rung: 'rung1' }),
    "Map MINI-A · The authors' original draft (rung 1)");
  // the stamp is the same for any selected item and any view: it names the map and the instructions, not the click
  assert.strictEqual(SS.views.stamp({ view: 'lanes', map: 'MINI-A', rung: 'rung2', item: 'a4' }),
    SS.views.stamp({ view: 'columns', map: 'MINI-A', rung: 'rung2', item: null }));
  // no map, unknown rung: still words, never "undefined"
  assert.strictEqual(SS.views.stamp({ view: 'lanes', rung: 'rung2' }), "The study's instructions (rung 2)");
  assert.strictEqual(SS.views.stamp({ view: 'lanes', map: 'MINI-A', rung: 'my_rung' }), 'Map MINI-A · my_rung');
  assert.strictEqual(SS.views.stamp({ view: 'bogus', map: 'MINI-A', rung: 'rung2' }), "Map MINI-A · The study's instructions (rung 2)");
  assert.strictEqual(SS.views.stamp(), "The study's instructions (rung 2)");
  assert.ok(!/View:|lanes|columns/.test(SS.views.stamp({ map: 'M', rung: 'rung1' })));
});

// ---------------------------------------------------------------- overlay model (view B)

test('overlay: one box per reference item, AI mark by fate at the rung, same x as the lanes at every rung', () => {
  const { SS } = boot();
  VIEW.maps.forEach((map, mi) => {
    const lane = SS.layout.place(map, VIEW.phases);
    let firstXs = null;
    for (const rung of RUNGS) {
      const m = SS.views.overlayModel(clone(VIEW), mi, rung);
      assert.strictEqual(m.lanes.length, 1);
      assert.strictEqual(m.lanes[0].key, 'reference');
      const targets = m.lanes[0].targets;
      eq(targets.map((t) => t.item_id).sort(), map.items.map((i) => i.item_id).sort());
      const xs = {};
      for (const t of targets) {
        xs[t.item_id] = t.x;
        assert.strictEqual(t.x, lane.slots[t.item_id].x, t.item_id);
        const item = map.items.find((i) => i.item_id === t.item_id);
        assert.strictEqual(t.enc.shape, item.reference_status === 'missing' ? 'dashed' : 'normal');
        const expectAi = SS.encoding.forItem('ai', item, rung);
        assert.strictEqual(t.ai.enc.shape, expectAi.shape, t.item_id + ' ' + rung);
        assert.strictEqual(t.ai.enc.glyph, expectAi.glyph);
        assert.strictEqual(t.label, SS.a11y.itemLabel(item, rung, 'A'));
      }
      if (firstXs === null) firstXs = xs; else eq(xs, firstXs);
    }
  });
});

test('overlay marks: words for each fate, unattested uses the fixed tag', () => {
  const { SS } = boot();
  const t = (m, id) => m.lanes[0].targets.find((x) => x.item_id === id);
  const r2 = SS.views.overlayModel(clone(VIEW), 0, 'rung2');
  assert.strictEqual(t(r2, 'a1').ai.text, 'AI: drawn as received');          // leaked
  assert.strictEqual(t(r2, 'a1').ai.enc.glyph, '▲');
  assert.strictEqual(t(r2, 'a2').ai.text, 'AI: drawn as received');          // recovered
  assert.strictEqual(t(r2, 'a5').ai.text, 'AI: never drawn');                // missed
  assert.strictEqual(t(r2, 'a7').ai.text, 'Not attested by its own quote');  // unattested
  const r1 = SS.views.overlayModel(clone(VIEW), 0, 'rung1');
  assert.strictEqual(t(r1, 'a1').ai.text, 'AI: drawn as wished for');           // kept
  assert.strictEqual(t(r1, 'a3').ai.text, 'AI: drawn as wished for');           // flipped
  assert.strictEqual(t(r1, 'a3').ai.enc.glyph, '▼');
  // no recorded fate for that rung: "not computed", never a normal mark
  const b3 = SS.views.overlayModel(clone(VIEW), 1, 'rung3');
  assert.strictEqual(t(b3, 'b1').ai.text, 'AI: not computed');
  assert.strictEqual(t(b3, 'b1').ai.enc.shape, 'unknown');
});

test('overlay gutter: AI-only nodes of the rung, each in its phase column; the gutter has the same size at every rung', () => {
  const { SS } = boot();
  const sizes = [];
  const byRung = {};
  for (const rung of RUNGS) {
    const m = SS.views.overlayModel(clone(VIEW), 0, rung);
    sizes.push([m.gutter.rows, m.layout.width]);
    byRung[rung] = m.gutter.nodes.map((n) => n.label);
    for (const n of m.gutter.nodes) {
      const col = m.layout.columns[n.col];
      assert.strictEqual(col.phase, n.phase, n.label);
      assert.ok(n.x >= col.x0 && n.x + SS.layout.ITEM_W <= col.x1, n.label);
    }
  }
  eq(byRung, {
    rung1: [], rung2: ['Ask a friend for advice', 'Call the supplier twice'],
    placebo: [], rung3: ['Ask a neighbour']
  });
  assert.ok(sizes.every((s) => s[0] === sizes[0][0] && s[1] === sizes[0][1]), JSON.stringify(sizes));
  // two nodes in one phase stack in rows; they never share a place
  const r2 = SS.views.overlayModel(clone(VIEW), 0, 'rung2');
  const [g1, g2] = r2.gutter.nodes;
  assert.strictEqual(g1.col, g2.col);
  assert.strictEqual(g1.x, g2.x);
  eq([g1.row, g2.row], [0, 1]);
  assert.strictEqual(r2.gutter.rows, 2);
  // and no gutter node is ever an item
  assert.ok(r2.lanes[0].targets.every((t) => t.item_id.indexOf('g') !== 0));
});

test('overlay gutter: an unknown phase goes in the first column; a map without gutter nodes has an empty gutter', () => {
  const { SS } = boot();
  const v = clone(VIEW);
  v.maps[0].lanes.ai.rung2.nodes.push({ node_id: 'rung2-g9', label: 'Odd one', phase: 'Not a phase', gutter: true });
  const m = SS.views.overlayModel(v, 0, 'rung2');
  const odd = m.gutter.nodes.find((n) => n.label === 'Odd one');
  assert.strictEqual(odd.col, 0);
  const b = SS.views.overlayModel(clone(VIEW), 1, 'rung2');
  eq(b.gutter.nodes, []);
  assert.strictEqual(b.gutter.rows, 1);
  // missing lanes, missing map, missing nodes: nothing throws
  assert.doesNotThrow(() => SS.views.overlayModel({ phases: [], maps: [{ pid: 'X', items: [] }] }, 0, 'rung2'));
  assert.doesNotThrow(() => SS.views.overlayModel({ phases: [], maps: [] }, 0, 'rung2'));
  assert.doesNotThrow(() => SS.views.overlayModel(null, 0, 'rung2'));
});

// ---------------------------------------------------------------- columns model (view C)

test('columns: the same item is on the same row in every column and at every rung', () => {
  const { SS } = boot();
  VIEW.maps.forEach((map, mi) => {
    let first = null;
    for (const rung of RUNGS) {
      const m = SS.views.columnsModel(clone(VIEW), mi, rung);
      eq(m.lanes.map((l) => l.key), ['reference', 'ai', 'script']);  // council 30: the legend order
      const rows = {};
      for (const lane of m.lanes) {
        for (const t of lane.targets) {
          if (rows[t.item_id] === undefined) rows[t.item_id] = { row: t.row, y: t.y };
          eq({ row: t.row, y: t.y }, rows[t.item_id], lane.key + ' ' + t.item_id);
        }
      }
      eq(Object.keys(rows).sort(), map.items.map((i) => i.item_id).sort());
      if (first === null) first = rows; else eq(rows, first, rung);
    }
  });
});

test('columns: rows follow phase order then x_slot; phases with no item get no heading; heights are fixed', () => {
  const { SS } = boot();
  const m = SS.views.columnsModel(clone(VIEW), 0, 'rung2');
  const ref = m.lanes.find((l) => l.key === 'reference').targets;
  eq(ref.map((t) => t.item_id), ['a1', 'a2', 'a3', 'a4', 'a5', 'a6', 'a7']);
  eq(ref.map((t) => t.row), [0, 1, 2, 3, 4, 5, 6]);
  eq(m.rows.filter((r) => r.type === 'phase').map((r) => r.phase),
    ['Before applying', 'Inspection', 'After approval']);   // "Appeals" has no item on map A
  const ys = ref.map((t) => t.y);
  for (let i = 1; i < ys.length; i++) assert.ok(ys[i] > ys[i - 1]);
  assert.ok(m.height >= ys[ys.length - 1] + SS.views.ROW_H);
  // the same height at every rung
  for (const rung of RUNGS) assert.strictEqual(SS.views.columnsModel(clone(VIEW), 0, rung).height, m.height);
  // the no-interview column holds only the steps that map drew, on their shared rows
  const script = m.lanes.find((l) => l.key === 'script').targets;
  eq(script.map((t) => t.item_id), ['a2', 'a3', 'a4', 'a6']);
  eq(script.map((t) => t.row), [1, 2, 3, 5]);
  // the AI-only nodes are not in this view (they belong to view B)
  const ai = m.lanes.find((l) => l.key === 'ai').targets;
  assert.strictEqual(ai.length, 7);
});

test('columns: arrow keys run down a column and across to the same item', () => {
  const { SS } = boot();
  const m = SS.views.columnsModel(clone(VIEW), 0, 'rung2');
  const at = (lane, id) => m.lanes.find((l) => l.key === lane).targets.find((t) => t.item_id === id);
  const nf = (lane, id, k) => { const r = SS.views.nextFocus(m, at(lane, id), k); return r && (r.lane + ':' + r.item_id); };
  assert.strictEqual(nf('ai', 'a2', 'ArrowDown'), 'ai:a3');
  assert.strictEqual(nf('ai', 'a2', 'ArrowUp'), 'ai:a1');
  assert.strictEqual(nf('ai', 'a1', 'ArrowUp'), null);
  assert.strictEqual(nf('ai', 'a7', 'ArrowDown'), null);
  assert.strictEqual(nf('ai', 'a4', 'Home'), 'ai:a1');
  assert.strictEqual(nf('ai', 'a4', 'End'), 'ai:a7');
  assert.strictEqual(nf('ai', 'a4', 'ArrowRight'), 'script:a4');
  assert.strictEqual(nf('ai', 'a4', 'ArrowLeft'), 'reference:a4');
  // the no-interview column lacks a1: the nearest row (the earlier one on a tie)... a1 has row 0, a2 row 1
  assert.strictEqual(nf('ai', 'a1', 'ArrowRight'), 'script:a2');
  assert.strictEqual(nf('script', 'a2', 'ArrowRight'), null);
  assert.strictEqual(nf('reference', 'a2', 'ArrowLeft'), null);
  assert.strictEqual(nf('ai', 'a4', 'x'), null);
});

test('overlay: arrow keys move along the one row and stop at the ends', () => {
  const { SS } = boot();
  const m = SS.views.overlayModel(clone(VIEW), 0, 'rung2');
  const order = m.lanes[0].targets.map((t) => t.item_id);
  const at = (id) => m.lanes[0].targets.find((t) => t.item_id === id);
  const nf = (id, k) => { const r = SS.views.nextFocus(m, at(id), k); return r && r.item_id; };
  assert.strictEqual(nf(order[0], 'ArrowRight'), order[1]);
  assert.strictEqual(nf(order[0], 'ArrowLeft'), null);
  assert.strictEqual(nf(order[1], 'ArrowLeft'), order[0]);
  assert.strictEqual(nf(order[0], 'End'), order[order.length - 1]);
  assert.strictEqual(nf(order[3], 'Home'), order[0]);
  assert.strictEqual(nf(order[3], 'ArrowUp'), null);
  assert.strictEqual(nf(order[3], 'ArrowDown'), null);
});

// ---------------------------------------------------------------- render

test('render lanes: state.view lanes is the Task 23 renderer, with the selected item marked', () => {
  const env = boot();
  const { root } = renderView(env, { view: 'lanes', map: 'MINI-A', rung: 'rung2', item: 'a4' });
  const sel = find(root, (n) => n.attributes && n.attributes['aria-current'] === 'true');
  assert.deepStrictEqual(sel.map((n) => n.getAttribute('data-item-id')), ['a4', 'a4', 'a4']);   // all three lanes draw a4
  assert.strictEqual(buttons(root).length, 4 + 7 + 7);
  assert.ok(find(root, (n) => hasClass(n, 'ss-lanes')).length === 1);
});

test('render overlay: one reference lane of boxes with AI marks, a gutter list of plain text, click emits item:open', () => {
  const env = boot();
  const { SS } = env;
  const { root, handle } = renderView(env, { view: 'overlay', map: 'MINI-A', rung: 'rung2', item: 'a1' });
  const btns = buttons(root);
  assert.strictEqual(btns.length, 7);
  for (const b of btns) {
    assert.ok(hasClass(b, 'ss-lane-reference'));
    assert.ok(b.getAttribute('aria-label').length > 0);
    assert.strictEqual(b.attributes.style, undefined);                 // positions go through element.style
    assert.ok(b.style.left.endsWith('px'));
    assert.ok(find(b, (n) => hasClass(n, 'ss-mark')).length === 1);
  }
  const a1 = btns.find((b) => b.getAttribute('data-item-id') === 'a1');
  assert.strictEqual(a1.getAttribute('aria-label'),
    'Pre-application meeting: participant marked it as wished for; AI drew it as received at rung 2');
  assert.ok(hasClass(a1, 'ss-shape-dashed'));
  assert.strictEqual(a1.getAttribute('aria-current'), 'true');
  assert.ok(find(a1, (n) => hasClass(n, 'ss-mk-leaked')).length === 1);
  assert.match(allText(a1), /▲/);
  // exactly one tab stop
  assert.strictEqual(btns.filter((b) => b.getAttribute('tabindex') === '0').length, 1);
  // gutter: text, not buttons
  const gutter = find(root, (n) => hasClass(n, 'ss-gutter'))[0];
  assert.ok(gutter);
  assert.match(allText(gutter), /Only the AI drew/);
  assert.match(allText(gutter), /Ask a friend for advice/);
  assert.match(allText(gutter), /Call the supplier twice/);
  assert.strictEqual(buttons(gutter).length, 0);
  // click
  const heard = [];
  SS.bus.on('item:open', (p) => heard.push(JSON.parse(JSON.stringify(p))));
  (a1.listeners.click || []).forEach((f) => f({ target: a1 }));
  assert.deepStrictEqual(heard, [{ item_id: 'a1', map: 'MINI-A', rung: 'rung2', lane: 'reference' }]);
  // keyboard: right arrow moves focus to the next box and takes the tab stop with it
  const order = handle.model.lanes[0].targets.map((t) => t.item_id);
  const first = btns.find((b) => b.getAttribute('data-item-id') === order[0]);
  const second = btns.find((b) => b.getAttribute('data-item-id') === order[1]);
  assert.strictEqual(key(first, 'ArrowRight'), true);
  assert.strictEqual(env.focused[env.focused.length - 1], second);
  assert.strictEqual(second.getAttribute('tabindex'), '0');
  assert.strictEqual(first.getAttribute('tabindex'), '-1');
});

test('render overlay: empty gutter says so, and the gutter of another rung is empty text, not a hole', () => {
  const env = boot();
  const { root } = renderView(env, { view: 'overlay', map: 'MINI-A', rung: 'rung1' });
  const gutter = find(root, (n) => hasClass(n, 'ss-gutter'))[0];
  assert.match(allText(gutter), /Only the AI drew/);
  assert.match(allText(gutter), /Nothing here at this rung/);
  const r2 = renderView(env, { view: 'overlay', map: 'MINI-A', rung: 'rung2' }).root;
  const g1 = find(root, (n) => hasClass(n, 'ss-gutter-track'))[0];
  const g2 = find(r2, (n) => hasClass(n, 'ss-gutter-track'))[0];
  assert.strictEqual(g1.style.height, g2.style.height);
  assert.strictEqual(find(root, (n) => hasClass(n, 'ss-ov-inner'))[0].style.width,
    find(r2, (n) => hasClass(n, 'ss-ov-inner'))[0].style.width);
});

test('render columns: three columns on shared rows, an item in the script column only where the model drew it', () => {
  const env = boot();
  const { SS } = env;
  const { root } = renderView(env, { view: 'columns', map: 'MINI-A', rung: 'rung3', item: 'a4' });
  const cols = find(root, (n) => hasClass(n, 'ss-colv'));
  assert.strictEqual(cols.length, 3);
  assert.deepStrictEqual(cols.map((c) => buttons(c).length), [7, 7, 4]);
  const tops = (c) => buttons(c).map((b) => b.getAttribute('data-item-id') + '@' + b.style.top);
  const ai = tops(cols[1]);
  for (const t of tops(cols[2])) assert.ok(ai.includes(t), t);
  for (const t of tops(cols[0])) assert.ok(ai.includes(t), t);
  for (const b of buttons(root)) assert.strictEqual(b.attributes.style, undefined);
  const heads = find(root, (n) => hasClass(n, 'ss-colv-title')).map((n) => n.textContent);
  assert.deepStrictEqual(heads, [SS.t('lane.reference'), SS.t('lane.ai'), SS.t('lane.no_interview')]);
  // the selected item is marked in every column that draws it
  assert.strictEqual(find(root, (n) => n.attributes && n.attributes['aria-current'] === 'true').length, 3);
  // click
  const heard = [];
  SS.bus.on('item:open', (p) => heard.push(JSON.parse(JSON.stringify(p))));
  const b = buttons(cols[1]).find((x) => x.getAttribute('data-item-id') === 'a3');
  (b.listeners.click || []).forEach((f) => f({ target: b }));
  assert.deepStrictEqual(heard, [{ item_id: 'a3', map: 'MINI-A', rung: 'rung3', lane: 'ai' }]);
  // an AI-only node never shows up here
  assert.doesNotMatch(allText(root), /Ask a neighbour|Ask a friend/);
});

test('render columns: the unattested tag and the shapes are the same as in the lanes', () => {
  const env = boot();
  const { root } = renderView(env, { view: 'columns', map: 'MINI-A', rung: 'rung2' });
  const ai = find(root, (n) => hasClass(n, 'ss-colv-ai'))[0];
  const a7 = buttons(ai).find((b) => b.getAttribute('data-item-id') === 'a7');
  assert.ok(hasClass(a7, 'ss-shape-never'));
  assert.match(allText(a7), /Not attested by its own quote/);
  const a1 = buttons(ai).find((b) => b.getAttribute('data-item-id') === 'a1');
  assert.ok(hasClass(a1, 'ss-shape-leaked'));
  assert.match(allText(a1), /▲/);
});

test('gloss: "a map of the official pathway (prompt quoted)" once per page unless asked for again', () => {
  const env = boot();
  const count = (root) => allText(root).split('a map of the official pathway (prompt quoted)').length - 1;
  for (const v of ['lanes', 'columns']) {
    assert.strictEqual(count(renderView(env, { view: v, map: 'MINI-A', rung: 'rung2' }).root), 1, v);
    assert.strictEqual(count(renderView(env, { view: v, map: 'MINI-A', rung: 'rung2' }, { gloss: false }).root), 0, v);
  }
  assert.strictEqual(count(renderView(env, { view: 'overlay', map: 'MINI-A', rung: 'rung2' }).root), 0);
});

test('mode B: the reference lane is named "This researcher\'s reading" in all three views', () => {
  const env = boot();
  const root = env.window.document.createElement('div');
  const v = clone(VIEW);
  v.reference_type = 'B';
  for (const view of ['lanes', 'overlay', 'columns']) {
    env.SS.views.render(root, v, { view, map: 'MINI-A', rung: 'rung2', item: null });
    const txt = allText(root);
    assert.match(txt, /This researcher's reading/, view);
    assert.doesNotMatch(txt, /participant-endorsed/, view);
  }
});

test('render: a map that is not in the data, or an empty map, shows the empty text; unknown view renders lanes', () => {
  const env = boot();
  const root = env.window.document.createElement('div');
  const v = clone(VIEW);
  v.maps = [{ pid: 'EMPTY', role: 'x', lanes: {}, items: [], off_record: [] }];
  for (const view of ['lanes', 'overlay', 'columns']) {
    assert.doesNotThrow(() => env.SS.views.render(root, v, { view, map: 'EMPTY', rung: 'rung2', item: null }), view);
    assert.match(allText(root), /No map to show\./, view);
    assert.doesNotThrow(() => env.SS.views.render(root, v, { view, map: 'NOPE', rung: 'rung2', item: null }), view);
  }
  const lanes = env.window.document.createElement('div');
  env.SS.views.render(lanes, clone(VIEW), { view: 'bogus', map: 'MINI-A', rung: 'rung2' });
  assert.ok(find(lanes, (n) => hasClass(n, 'ss-lanes')).length === 1);
});

test('data stays out of markup: a label with angle brackets is text, never an attribute-built tag', () => {
  const env = boot();
  const v = clone(VIEW);
  v.maps[0].items[0].label = '<img src=x onerror=alert(1)>';
  v.maps[0].lanes.ai.rung2.nodes.push({ node_id: 'rung2-g3', label: '<b>bold</b>', phase: 'Inspection', gutter: true });
  const root = env.window.document.createElement('div');
  for (const view of ['overlay', 'columns']) {
    env.SS.views.render(root, v, { view, map: 'MINI-A', rung: 'rung2' });
    assert.match(allText(root), /<img src=x onerror=alert\(1\)>/);
    assert.strictEqual(find(root, (n) => n.tagName === 'IMG' || n.tagName === 'B').length, 0);
  }
});

// ---------------------------------------------------------------- switch and strings

test('switch: three toggle buttons in the fixed order; the current one is pressed; a click changes only the view', () => {
  const env = boot();
  const { SS } = env;
  SS.state.set({ map: 'MINI-A', rung: 'rung3', item: 'a4' });
  const root = env.window.document.createElement('div');
  SS.views.renderSwitch(root, SS.state.get());
  const btns = buttons(root);
  assert.deepStrictEqual(btns.map((b) => b.textContent), ['Three lanes', "One map, AI's marks", 'Three columns']);
  assert.deepStrictEqual(btns.map((b) => b.getAttribute('aria-pressed')), ['true', 'false', 'false']);
  assert.deepStrictEqual(btns.map((b) => b.getAttribute('data-view')), ['lanes', 'overlay', 'columns']);
  (btns[2].listeners.click || []).forEach((f) => f({ target: btns[2] }));
  assert.deepStrictEqual(JSON.parse(JSON.stringify(SS.state.get())),
    { view: 'columns', map: 'MINI-A', rung: 'rung3', item: 'a4' });
});

test('every views.* string exists, has no placeholder gaps, and never says "script"', () => {
  const { SS, window } = load();
  const en = JSON.parse(fs.readFileSync(path.join(ROOT, 'second_scribe', 'app', 'strings', 'en.json'), 'utf8'));
  const keys = Object.keys(en).filter((k) => k.indexOf('views.') === 0);
  assert.ok(keys.length >= 15, 'views.* keys: ' + keys.length);
  for (const k of keys) {
    assert.doesNotMatch(en[k], /script/i, k);
    assert.doesNotMatch(en[k], /\b(erase|fabricat|neither map|interviewer erred)/i, k);
  }
  // the code only asks for keys that exist (no console error during a full tour of the views)
  const env = boot();
  const root = env.window.document.createElement('div');
  for (const view of ['lanes', 'overlay', 'columns']) {
    for (const rung of RUNGS) for (const map of ['MINI-A', 'MINI-B']) {
      env.SS.views.render(root, clone(VIEW), { view, map, rung, item: null });
      env.SS.views.stamp({ view, map, rung });
    }
  }
  env.SS.views.renderSwitch(root, env.SS.state.get());
  assert.deepStrictEqual(env.errors, []);
  void SS; void window;
});

test('views.css uses tokens only and never sets text below 13px', () => {
  const css = fs.readFileSync(path.join(ROOT, 'second_scribe', 'app', 'static', 'css', 'views.css'), 'utf8')
    .replace(/\/\*[\s\S]*?\*\//g, '');
  assert.doesNotMatch(css, /#[0-9a-fA-F]{3,8}\b/);
  assert.doesNotMatch(css, /\b(rgb|rgba|hsl|hsla)\(/);
  assert.doesNotMatch(css, /https?:\/\/|@import/);
  for (const m of css.matchAll(/font-size:\s*([0-9.]+)(px|rem|em|%)/g)) {
    assert.strictEqual(m[2], 'px');
    assert.ok(Number(m[1]) >= 13, 'font-size ' + m[0]);
  }
});
