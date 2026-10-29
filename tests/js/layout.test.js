'use strict';
// Task 23: phase columns, shared x for every item in every lane and rung, lane DOM, keyboard order.
// The fixture is SYNTHETIC (tests/fixtures/views/mini.view.json). The stub DOM has no real events,
// so the tests wrap createElement to record listeners and call them directly.
const test = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const { load, ROOT } = require('./harness');

const VIEW = JSON.parse(fs.readFileSync(path.join(ROOT, 'tests', 'fixtures', 'views', 'mini.view.json'), 'utf8'));
const clone = (o) => JSON.parse(JSON.stringify(o));

// Load the scripts and make every created element record its listeners and focus() calls.
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
  return env;
}

function walk(node, fn) {
  fn(node);
  (node.children || []).forEach((c) => walk(c, fn));
}
function buttons(root) {
  const out = [];
  walk(root, (n) => { if (n.tagName === 'BUTTON') out.push(n); });
  return out;
}
function allText(root) {
  const out = [];
  walk(root, (n) => { if (n.textContent) out.push(n.textContent); });
  return out.join('\n');
}
function renderOne(env, view, mapIdx, rung, opts) {
  const root = env.window.document.createElement('div');
  const handle = env.SS.lanes.render(root, view, mapIdx, rung, opts);
  return { root, handle };
}

test('place: columns follow Setup order, are contiguous, and an empty phase keeps one slot', () => {
  const { SS } = load();
  const map = VIEW.maps[0];
  const p = SS.layout.place(map, VIEW.phases);
  assert.deepStrictEqual(clone(p.columns.map((c) => c.phase)), VIEW.phases);
  assert.strictEqual(p.columns[0].x0, 0);
  for (let i = 1; i < p.columns.length; i++) assert.strictEqual(p.columns[i].x0, p.columns[i - 1].x1);
  assert.strictEqual(p.width, p.columns[p.columns.length - 1].x1);
  // "Appeals" has no item on map A, still a visible column
  const appeals = p.columns[3];
  assert.ok(appeals.x1 - appeals.x0 >= SS.layout.SLOT_W);
  // every item sits inside its own phase column
  for (const it of map.items) {
    const s = p.slots[it.item_id];
    const col = p.columns[s.col];
    assert.strictEqual(col.phase, it.phase);
    assert.ok(s.x >= col.x0 && s.x + SS.layout.ITEM_W <= col.x1, it.item_id);
  }
});

test('place: x_slot orders items inside a phase, whatever the array order', () => {
  const { SS } = load();
  const map = VIEW.maps[0];
  const shuffled = clone(map);
  shuffled.items.reverse();
  const a = SS.layout.place(map, VIEW.phases);
  const b = SS.layout.place(shuffled, VIEW.phases);
  for (const it of map.items) assert.strictEqual(a.slots[it.item_id].x, b.slots[it.item_id].x, it.item_id);
  const ins = map.items.filter((i) => i.phase === 'Inspection').sort((x, y) => x.x_slot - y.x_slot);
  for (let i = 1; i < ins.length; i++) assert.ok(a.slots[ins[i].item_id].x > a.slots[ins[i - 1].item_id].x);
});

test('place: a clashing x_slot is bumped to the next free slot, deterministically; no two items overlap', () => {
  const { SS } = load();
  const map = clone(VIEW.maps[0]);
  map.items.forEach((i) => { if (i.phase === 'Inspection') i.x_slot = 0; });
  const a = SS.layout.place(map, VIEW.phases);
  const b = SS.layout.place(clone(map), VIEW.phases);
  assert.strictEqual(JSON.stringify(a.slots), JSON.stringify(b.slots));
  const xs = map.items.filter((i) => i.phase === 'Inspection').map((i) => a.slots[i.item_id].x);
  assert.strictEqual(new Set(xs).size, xs.length);
  xs.sort((p, q) => p - q);
  for (let i = 1; i < xs.length; i++) assert.ok(xs[i] - xs[i - 1] >= SS.layout.ITEM_W);
});

test('place: an item whose phase is not in the Setup list gets a trailing column, never dropped', () => {
  const { SS } = load();
  const map = clone(VIEW.maps[0]);
  map.items[0].phase = 'Somewhere else';
  const p = SS.layout.place(map, VIEW.phases);
  assert.strictEqual(p.columns[p.columns.length - 1].phase, 'Somewhere else');
  assert.ok(p.slots[map.items[0].item_id]);
});

test('place: a map with no items still gives the phase columns', () => {
  const { SS } = load();
  const p = SS.layout.place({ pid: 'x', items: [] }, VIEW.phases);
  assert.strictEqual(p.columns.length, VIEW.phases.length);
  assert.deepStrictEqual(clone(Object.keys(p.slots)), []);
  assert.ok(p.width > 0);
});

test('same_x_every_lane_and_rung', () => {
  const env = boot();
  let seen = 0;
  VIEW.maps.forEach((map, mapIdx) => {
    const placed = env.SS.layout.place(map, VIEW.phases);
    for (const rung of VIEW.rungs) {
      const { root } = renderOne(env, VIEW, mapIdx, rung);
      for (const b of buttons(root)) {
        const id = b.getAttribute('data-item-id');
        assert.strictEqual(b.style.left, placed.slots[id].x + 'px',
          map.pid + ' ' + rung + ' ' + b.getAttribute('data-lane') + ' ' + id);
        seen++;
      }
    }
  });
  assert.ok(seen > 40, 'rendered ' + seen + ' item buttons');
  // and each item really appears in more than one lane at one rung (the claim is about lanes, not one lane)
  const { root } = renderOne(env, VIEW, 0, 'rung2');
  const lanesOfA1 = buttons(root).filter((b) => b.getAttribute('data-item-id') === 'a1').map((b) => b.getAttribute('data-lane'));
  assert.deepStrictEqual(clone(lanesOfA1.sort()), ['ai', 'reference']); // a1: the no-interview map did not draw it
});

test('lanes: three lanes in the order reference, AI, no-interview (council 30, the legend order), with the V1 names', () => {
  const env = boot();
  const { root } = renderOne(env, VIEW, 0, 'rung2');
  const text = allText(root);
  const i1 = text.indexOf('The interviewer-drawn, participant-endorsed map');
  const i2 = text.indexOf("The AI's map, from the transcript");
  const i3 = text.indexOf('The same model, without the interview');
  assert.ok(i1 >= 0 && i2 > i1 && i3 > i2, [i1, i2, i3].join(','));
  assert.strictEqual(text.split('a map of the official pathway (prompt quoted)').length - 1, 1);
  assert.doesNotMatch(text, /\bscript\b/i);
});

test('lanes: the gloss prints once per page, so a second render can switch it off', () => {
  const env = boot();
  const { root } = renderOne(env, VIEW, 0, 'rung2', { gloss: false });
  assert.doesNotMatch(allText(root), /official pathway/);
});

test('lanes: mode B names the bottom lane "This researcher\'s reading"', () => {
  const env = boot();
  const v = clone(VIEW);
  v.reference_type = 'B';
  const text = allText(renderOne(env, v, 0, 'rung2').root);
  assert.match(text, /This researcher's reading/);
  assert.doesNotMatch(text, /participant-endorsed/);
  const b = buttons(renderOne(env, v, 0, 'rung2').root).find((x) => x.getAttribute('data-lane') === 'reference');
  assert.match(b.getAttribute('aria-label'), /researcher marked/);
});

test('lanes: every item button has an aria-label, the AI lane uses itemLabel, text comes from data via textContent', () => {
  const env = boot();
  const { root } = renderOne(env, VIEW, 0, 'rung2');
  const a1ai = buttons(root).find((b) => b.getAttribute('data-lane') === 'ai' && b.getAttribute('data-item-id') === 'a1');
  assert.strictEqual(a1ai.getAttribute('aria-label'),
    'Pre-application meeting: participant marked it as wished for; AI drew it as received at rung 2');
  for (const b of buttons(root)) assert.ok(b.getAttribute('aria-label'), 'aria-label on ' + b.getAttribute('data-item-id'));
  // shape classes carry the encoding; the leaked box has its glyph as text
  assert.match(a1ai.className, /ss-shape-leaked/);
  assert.match(allText(a1ai), /▲/);
});

test('lanes: a rung without a recorded fate shows "not computed", not a normal box', () => {
  const env = boot();
  const { root } = renderOne(env, VIEW, 1, 'rung3'); // map B has no rung3 fates
  const ai = buttons(root).filter((b) => b.getAttribute('data-lane') === 'ai');
  assert.strictEqual(ai.length, VIEW.maps[1].items.length);
  for (const b of ai) {
    assert.match(b.className, /ss-shape-unknown/);
    assert.match(allText(b), /not computed/);
  }
});

test('lanes: a bad map index or a map-less file renders an empty state, not an exception', () => {
  const env = boot();
  assert.doesNotThrow(() => renderOne(env, VIEW, 9, 'rung2'));
  assert.doesNotThrow(() => renderOne(env, { schema: 'view-data/1', source: 'study-public', phases: [] }, 0, 'rung2'));
  const t = allText(renderOne(env, { schema: 'view-data/1', source: 'study-public', phases: [] }, 0, 'rung2').root);
  assert.match(t, /No map to show/);
});

test('keyboard_order', () => {
  const env = boot();
  const { SS } = env;
  const model = SS.lanes.model(VIEW, 0, 'rung2');
  const order = SS.lanes.focusOrder(model);
  const rank = { reference: 0, ai: 1, script: 2 };
  for (let i = 1; i < order.length; i++) {
    const a = order[i - 1], b = order[i];
    assert.ok(rank[a.lane] < rank[b.lane] || (a.lane === b.lane && a.x <= b.x),
      'order broke at ' + i + ': ' + a.lane + '@' + a.x + ' then ' + b.lane + '@' + b.x);
  }
  // the DOM (= Tab order) is exactly that order
  const { root } = renderOne(env, VIEW, 0, 'rung2');
  const dom = buttons(root).map((b) => b.getAttribute('data-lane') + ':' + b.getAttribute('data-item-id'));
  assert.deepStrictEqual(clone(dom), clone(order.map((t) => t.lane + ':' + t.item_id)));
  // a roving tabindex: exactly one stop in the tab sequence
  assert.strictEqual(buttons(root).filter((b) => b.getAttribute('tabindex') === '0').length, 1);
  assert.strictEqual(buttons(root)[0].getAttribute('tabindex'), '0');
});

test('keyboard: arrows move along a lane, up/down between lanes, Home/End, and clamp at the ends', () => {
  const { SS } = load();
  const model = SS.lanes.model(VIEW, 0, 'rung2');
  const at = (lane, id) => model.lanes.find((l) => l.key === lane).targets.find((t) => t.item_id === id);
  const aiTargets = model.lanes.find((l) => l.key === 'ai').targets;
  assert.strictEqual(SS.lanes.nextFocus(model, at('ai', 'a1'), 'ArrowRight').item_id, aiTargets[1].item_id);
  assert.strictEqual(SS.lanes.nextFocus(model, at('ai', 'a1'), 'ArrowLeft'), null);
  assert.strictEqual(SS.lanes.nextFocus(model, aiTargets[aiTargets.length - 1], 'ArrowRight'), null);
  assert.strictEqual(SS.lanes.nextFocus(model, at('ai', 'a4'), 'Home').item_id, aiTargets[0].item_id);
  assert.strictEqual(SS.lanes.nextFocus(model, at('ai', 'a4'), 'End').item_id, aiTargets[aiTargets.length - 1].item_id);
  // up from AI goes to the reference lane (on top since council 30), same item
  const upRef = SS.lanes.nextFocus(model, at('ai', 'a4'), 'ArrowUp');
  assert.deepStrictEqual([upRef.lane, upRef.item_id], ['reference', 'a4']);
  // down from AI goes to the no-interview lane; a1 is not there, so the nearest item by x
  const up = SS.lanes.nextFocus(model, at('ai', 'a1'), 'ArrowDown');
  assert.strictEqual(up.lane, 'script');
  const scriptXs = model.lanes[2].targets.map((t) => t.x);
  assert.strictEqual(up.x, scriptXs.reduce((best, x) => (Math.abs(x - at('ai', 'a1').x) < Math.abs(best - at('ai', 'a1').x) ? x : best)));
  // no lane below the last or above the first
  assert.strictEqual(SS.lanes.nextFocus(model, up, 'ArrowDown'), null);
  assert.strictEqual(SS.lanes.nextFocus(model, at('reference', 'a1'), 'ArrowUp'), null);
  assert.strictEqual(SS.lanes.nextFocus(model, at('ai', 'a1'), 'x'), null);
});

test('keyboard and click through the DOM: arrows move focus, Enter/click emit item:open', () => {
  const env = boot();
  const { SS } = env;
  const { root } = renderOne(env, VIEW, 0, 'rung2');
  const bs = buttons(root);
  const heard = [];
  SS.bus.on('item:open', (p) => heard.push(JSON.parse(JSON.stringify(p))));
  const a1ai = bs.find((b) => b.getAttribute('data-lane') === 'ai' && b.getAttribute('data-item-id') === 'a1');
  let prevented = 0;
  const key = (b, k) => (b.listeners.keydown || []).forEach((f) => f({ key: k, target: b, preventDefault() { prevented++; } }));
  key(a1ai, 'ArrowRight');
  assert.strictEqual(prevented, 1);
  const moved = env.focused[env.focused.length - 1];
  assert.deepStrictEqual([moved.getAttribute('data-lane'), moved.getAttribute('data-item-id')], ['ai', 'a2']);
  assert.strictEqual(moved.getAttribute('tabindex'), '0');
  assert.strictEqual(a1ai.getAttribute('tabindex'), '-1');
  // a key that is not ours is left alone
  const before = prevented;
  key(moved, 'a');
  assert.strictEqual(prevented, before);
  (a1ai.listeners.click || []).forEach((f) => f({ target: a1ai }));
  assert.deepStrictEqual(heard, [{ item_id: 'a1', map: 'MINI-A', rung: 'rung2', lane: 'ai' }]);
});

test('dev data file equals the fixture (static/dev/data/mini.view.js is generated from the JSON)', () => {
  const file = path.join(ROOT, 'second_scribe', 'app', 'static', 'dev', 'data', 'mini.view.js');
  const ctx = vm.createContext({ window: {} });
  ctx.window.SS_DATA = {};
  vm.runInContext(fs.readFileSync(file, 'utf8'), ctx);
  assert.strictEqual(JSON.stringify(ctx.window.SS_DATA.mini), JSON.stringify(VIEW));
});

test('dev strings file agrees with en.json for every key it carries, and carries every lane.* and a11y.* key', () => {
  const dir = path.join(ROOT, 'second_scribe', 'app');
  const ctx = vm.createContext({ window: {} });
  vm.runInContext(fs.readFileSync(path.join(dir, 'static', 'dev', 'data', 'strings.dev.js'), 'utf8'), ctx);
  const dev = ctx.window.SS_STRINGS;
  const en = JSON.parse(fs.readFileSync(path.join(dir, 'strings', 'en.json'), 'utf8'));
  for (const k of Object.keys(dev)) assert.strictEqual(dev[k], en[k], k);
  for (const k of Object.keys(en)) if (/^(lane|a11y)\./.test(k)) assert.strictEqual(dev[k], en[k], 'missing in dev file: ' + k);
});

test('viewer.css uses tokens only and never sets text below 13px', () => {
  const css = fs.readFileSync(path.join(ROOT, 'second_scribe', 'app', 'static', 'css', 'viewer.css'), 'utf8')
    .replace(/\/\*[\s\S]*?\*\//g, '');
  assert.doesNotMatch(css, /#[0-9a-fA-F]{3,8}\b/);
  assert.doesNotMatch(css, /\b(rgb|rgba|hsl|hsla)\(/);
  assert.doesNotMatch(css, /https?:\/\/|@import/);
  for (const m of css.matchAll(/font-size:\s*([0-9.]+)(px|rem|em|%)/g)) {
    assert.strictEqual(m[2], 'px');
    assert.ok(Number(m[1]) >= 13, 'font-size ' + m[0]);
  }
  // overflow-x: auto inside the lanes panel, so the page never scrolls sideways
  assert.match(css, /\.ss-lanes-scroll\s*\{[^}]*overflow-x:\s*auto/);
});

test('every source file keeps the classic-script rules (no innerHTML, eval, import/export)', () => {
  for (const f of ['layout.js', 'encoding.js', 'a11y.js', 'lanes.js']) {
    const src = fs.readFileSync(path.join(ROOT, 'second_scribe', 'app', 'static', 'js', f), 'utf8')
      .replace(/\/\*[\s\S]*?\*\//g, '').replace(/\/\/.*$/gm, '');
    assert.doesNotMatch(src, /innerHTML|insertAdjacentHTML|outerHTML|\beval\s*\(|new Function|^\s*(import|export)\b/m, f);
    assert.doesNotMatch(src, /setAttribute\(\s*['"]style['"]/, f + ' sets a style attribute (blocked by a strict CSP)');
  }
});
