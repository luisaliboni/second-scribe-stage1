'use strict';
// Item 18 (23 Oct): pointing at, focusing or tapping a box of a compared map says in one plain sentence what happened
// to it and lights its partner boxes in the other lanes; the evidence drawer opens with the same sentence. Item 20:
// the lanes are the one view, so the AI lane also draws the boxes only the AI drew. Read from the view data only.
// Fixture: tests/fixtures/views/mini.view.json (SYNTHETIC). The stub DOM records listeners; tests call them directly.
const test = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const { load, ROOT } = require('./harness');

const VIEW = JSON.parse(fs.readFileSync(path.join(ROOT, 'tests', 'fixtures', 'views', 'mini.view.json'), 'utf8'));
const EN = JSON.parse(fs.readFileSync(path.join(ROOT, 'second_scribe', 'app', 'strings', 'en.json'), 'utf8'));
const clone = (o) => JSON.parse(JSON.stringify(o));

function boot(hash) {
  const env = load();
  const doc = env.window.document;
  const make = doc.createElement;
  const focused = [];
  doc.createElement = (tag) => {
    const n = make(tag);
    n.listeners = {};
    n.addEventListener = (type, fn) => { (n.listeners[type] = n.listeners[type] || []).push(fn); };
    n.focus = () => { focused.push(n); (n.listeners.focus || []).forEach((f) => f({ target: n })); };
    let text = n.textContent;
    Object.defineProperty(n, 'textContent', {
      get: () => text,
      set: (v) => { text = v; if (v === '') n.children.length = 0; },
    });
    return n;
  };
  env.focused = focused;
  env.window.location.hash = hash || '';
  return env;
}
function walk(node, fn) { fn(node); (node.children || []).forEach((c) => walk(c, fn)); }
function find(root, pred) { const out = []; walk(root, (n) => { if (pred(n)) out.push(n); }); return out; }
const attr = (n, k) => (n.attributes ? n.attributes[k] : undefined);
const hasClass = (n, c) => new RegExp('(^| )' + c + '( |$)').test(n.className || '');
const fire = (n, type) => (n.listeners[type] || []).forEach((f) => f({ target: n, key: type, preventDefault() {} }));
const key = (n, k) => (n.listeners.keydown || []).forEach((f) => f({ target: n, key: k, preventDefault() {} }));

function render(env, view, mapIdx, rung, opts) {
  const root = env.window.document.createElement('div');
  env.SS.lanes.render(root, view, mapIdx, rung, opts || {});
  return root;
}
const box = (root, lane, id) => find(root, (n) => attr(n, 'data-lane') === lane && attr(n, 'data-item-id') === id && n.tagName === 'BUTTON')[0];
const status = (root) => find(root, (n) => hasClass(n, 'ss-box-status'))[0];
const lit = (root, how) => find(root, (n) => attr(n, 'data-light') === how).map((n) => attr(n, 'data-lane') + ':' + (attr(n, 'data-item-id') || attr(n, 'data-node-id')));
const fill = (k, vars) => EN[k].replace(/\{(\w+)\}/g, (m, v) => vars[v]);

test('box_sentence: one plain sentence per box, by lane, fate and evidence (exactly the item 18 statuses)', () => {
  const { SS } = load();
  const items = Object.fromEntries(VIEW.maps[0].items.map((i) => [i.item_id, i]));
  const s = (lane, id, rung) => SS.lanes.boxSentence(lane, items[id], rung, 'A');
  const ref = EN['box.ref_word'];
  // endorsed steps
  assert.strictEqual(s('reference', 'a2', 'rung2'), EN['box.ref.has']);
  assert.strictEqual(s('reference', 'a3', 'rung1'), EN['box.ref.has_as_never']);
  assert.strictEqual(s('reference', 'a1', 'rung2'), fill('box.ref.wished', { ai: EN['box.wished.leaked'] }));
  assert.strictEqual(s('reference', 'a1', 'rung1'), fill('box.ref.wished', { ai: EN['box.wished.kept'] }));
  assert.strictEqual(s('reference', 'a5', 'rung2'), fill('box.ref.wished', { ai: EN['box.wished.missed'] }));
  assert.strictEqual(s('reference', 'a7', 'rung2'), fill('box.ref.wished', { ai: EN['box.wished.unattested'] }));
  assert.strictEqual(s('reference', 'a1', 'rung2'), 'Wished for, never came; the AI drew it as received.');
  // the AI's boxes: the step matched, how a step that never came was drawn, and whether its own quote backs it
  assert.strictEqual(s('ai', 'a1', 'rung2'),
    'Matches “Pre-application meeting” on the endorsed map, drawn as received; backed by the participant\'s own words.');
  assert.strictEqual(s('ai', 'a3', 'rung2'), fill('box.ai.match', { step: 'Health inspection', ref }) + EN['box.ai.backing.transcript'] + '.');
  assert.strictEqual(s('ai', 'a2', 'rung2'), fill('box.ai.match', { step: 'Submit application', ref }) + '.');
  assert.strictEqual(s('ai', 'a5', 'rung2'), EN['box.ai.lacks']);
  assert.strictEqual(s('ai', 'a7', 'rung2'), fill('box.ai.match', { step: 'Street parking approval', ref }) + EN['box.ai.backing.none'] + '.');
  assert.strictEqual(s('ai', 'a4', 'rung3'), fill('box.ai.match', { step: 'Fire safety check', ref }) + EN['box.ai.how.never'] + '.');
  // the no-interview map, and the boxes that match no step
  assert.strictEqual(s('script', 'a2', 'rung2'), 'Matches “Submit application” on the endorsed map.');
  assert.strictEqual(SS.lanes.boxSentence('ai', null, 'rung2', 'A', true), 'Not on the endorsed map; only the AI drew it.');
  assert.strictEqual(SS.lanes.boxSentence('script', null, 'rung2', 'A', true), 'Not on the endorsed map.');
  // mode B names the researcher's map; a fate the data lacks reads "not computed", never a guess
  assert.strictEqual(SS.lanes.boxSentence('ai', null, 'rung2', 'B', true), "Not on this researcher's map; only the AI drew it.");
  assert.strictEqual(SS.lanes.boxSentence('reference', { label: 'x', reference_status: 'happened', fate: {} }, 'rung2', 'A'), EN['box.unknown']);
});

test('box_strings_follow_the_register', () => {
  const keys = Object.keys(EN).filter((k) => k.startsWith('box.'));
  assert.ok(keys.length >= 20);
  for (const k of keys) {
    assert.ok(!/—|rather than|official script|\b(first|replicable|validated|ground truth|wrong|missed|correct|fix)\b/i.test(EN[k]), k);
  }
  assert.ok(EN['box.hint'].split(/\s+/).length >= 8, 'the hint is a sentence');
});

test('lanes_every_box_has_its_sentence_as_description_and_ai_only_boxes_are_drawn', () => {
  const env = boot();
  const root = render(env, clone(VIEW), 0, 'rung2');
  const st = status(root);
  assert.strictEqual(st.textContent, EN['box.hint']);
  assert.strictEqual(attr(st, 'aria-hidden'), 'true');
  const notes = find(root, (n) => hasClass(n, 'ss-box-notes'))[0];
  assert.strictEqual(notes.hidden, true);
  const boxes = find(root, (n) => hasClass(n, 'ss-item'));
  assert.ok(boxes.length > 0);
  for (const b of boxes) {
    const id = attr(b, 'aria-describedby');
    assert.ok(id, 'every box is described: ' + (attr(b, 'data-item-id') || attr(b, 'data-node-id')));
    const note = find(notes, (n) => attr(n, 'id') === id);
    assert.strictEqual(note.length, 1);
    assert.strictEqual(note[0].textContent, attr(b, 'data-status'));
  }
  // the two AI-only nodes of rung 2 are plain boxes of the AI lane, focusable by arrow keys and tap, never buttons
  const only = find(root, (n) => hasClass(n, 'ss-plain-node') && attr(n, 'data-lane') === 'ai');
  assert.deepStrictEqual(only.map((n) => attr(n, 'data-node-id')), ['rung2-g1', 'rung2-g2']);
  for (const n of only) {
    assert.strictEqual(n.tagName, 'DIV');
    assert.strictEqual(attr(n, 'tabindex'), '-1');
    assert.strictEqual(attr(n, 'data-status'), 'Not on the endorsed map; only the AI drew it.');
  }
  // the AI lane keeps one height at every rung (rows for the most AI-only boxes any rung has)
  const track = (r) => find(render(boot(), clone(VIEW), 0, r), (n) => attr(n, 'data-lane-row') === 'ai')[0].children.find((c) => hasClass(c, 'ss-track'));
  assert.strictEqual(track('rung1').style.height, track('rung2').style.height);
  // a plain map (nothing to compare) has no statuses
  const plain = clone(VIEW);
  plain.maps[0].items = [];
  plain.maps[0].lanes.ai.rung2.nodes = [{ node_id: 'p1', label: 'Alone', phase: 'Before applying' }];
  plain.maps[0].lanes.script.nodes = [];
  const proot = render(boot(), plain, 0, 'rung2');
  assert.strictEqual(find(proot, (n) => hasClass(n, 'ss-box-status')).length, 0);
});

test('pointing_lights_the_box_and_its_partners_and_leaving_restores_the_hint', () => {
  const env = boot();
  const root = render(env, clone(VIEW), 0, 'rung2');
  fire(box(root, 'reference', 'a2'), 'mouseenter');
  assert.strictEqual(status(root).textContent, EN['box.ref.has']);
  assert.deepStrictEqual(lit(root, 'self'), ['reference:a2']);
  assert.deepStrictEqual(lit(root, 'partner').sort(), ['ai:a2', 'script:a2']);
  fire(box(root, 'reference', 'a2'), 'mouseleave');
  assert.strictEqual(status(root).textContent, EN['box.hint']);
  assert.deepStrictEqual(lit(root, 'self').concat(lit(root, 'partner')), []);
  // a step the AI never drew: its dotted AI placeholder is not a partner, and the no-interview map lacks it too
  fire(box(root, 'reference', 'a5'), 'mouseenter');
  assert.deepStrictEqual(lit(root, 'partner'), []);
  // the AI-only box has no partner
  const only = find(root, (n) => hasClass(n, 'ss-plain-node') && attr(n, 'data-lane') === 'ai')[0];
  fire(only, 'mouseenter');
  assert.strictEqual(status(root).textContent, EN['box.ai.only'].replace('{ref}', EN['box.ref_word']));
  assert.deepStrictEqual(lit(root, 'partner'), []);
});

test('focus_and_tap_show_the_sentence_and_arrow_keys_reach_the_extra_boxes', () => {
  const env = boot();
  const root = render(env, clone(VIEW), 0, 'rung2');
  const ai4 = box(root, 'ai', 'a4');
  ai4.focus();
  assert.strictEqual(status(root).textContent, attr(ai4, 'data-status'));
  assert.deepStrictEqual(lit(root, 'partner').sort(), ['reference:a4', 'script:a4']);
  fire(ai4, 'blur');
  assert.strictEqual(status(root).textContent, EN['box.hint']);
  // End goes past the steps to the AI lane's extra boxes; Down from one goes to the nearest no-interview step (council 30 order)
  key(ai4, 'End');
  const last = env.focused[env.focused.length - 1];
  assert.strictEqual(attr(last, 'data-node-id'), 'rung2-g2');
  assert.strictEqual(attr(last, 'tabindex'), '0');
  assert.strictEqual(status(root).textContent, 'Not on the endorsed map; only the AI drew it.');
  key(last, 'ArrowLeft');
  assert.strictEqual(attr(env.focused[env.focused.length - 1], 'data-node-id'), 'rung2-g1');
  key(env.focused[env.focused.length - 1], 'ArrowDown');
  assert.strictEqual(attr(env.focused[env.focused.length - 1], 'data-lane'), 'script');
  // one tab stop only, whatever has focus
  assert.strictEqual(find(root, (n) => attr(n, 'tabindex') === '0').length, 1);
  // a tap on an extra box focuses it, which shows its sentence (touch screens)
  const g1 = find(root, (n) => attr(n, 'data-node-id') === 'rung2-g1')[0];
  fire(g1, 'click');
  assert.strictEqual(env.focused[env.focused.length - 1], g1);
});

test('the_selected_step_keeps_its_sentence_and_the_drawer_opens_with_it', () => {
  const env = boot();
  const root = render(env, clone(VIEW), 0, 'rung2', { item: 'a1', itemLane: 'ai' });
  assert.deepStrictEqual(lit(root, 'self'), ['ai:a1']);
  assert.strictEqual(status(root).textContent, attr(box(root, 'ai', 'a1'), 'data-status'));
  fire(box(root, 'reference', 'a2'), 'mouseenter');
  fire(box(root, 'reference', 'a2'), 'mouseleave');
  assert.deepStrictEqual(lit(root, 'self'), ['ai:a1'], 'leaving another box returns to the selected one');
  // through the viewer: a click on the AI box of a1 opens the drawer, whose body starts with the same sentence
  const env2 = boot('#map=MINI-A&rung=rung2');
  const host = env2.window.document.createElement('div');
  env2.SS.viewer.mount(host, clone(VIEW), {});
  const views = find(host, (n) => attr(n, 'data-section') === 'views')[0];
  fire(box(views, 'ai', 'a1'), 'click');
  const drawer = find(host, (n) => attr(n, 'data-section') === 'drawer')[0];
  const said = find(drawer, (n) => attr(n, 'data-part') === 'status');
  const want = env2.SS.lanes.boxSentence('ai', VIEW.maps[0].items[0], 'rung2', 'A');
  assert.strictEqual(said.length, 1);
  assert.strictEqual(said[0].textContent, want);
  const body = find(drawer, (n) => hasClass(n, 'ss-drawer-body'))[0];
  assert.strictEqual(body.children[0], said[0], 'the sentence leads the drawer');
  assert.strictEqual(attr(body, 'aria-live'), 'polite');
  // and the lanes, re-drawn for the selection, keep that box lit
  assert.deepStrictEqual(lit(find(host, (n) => attr(n, 'data-section') === 'views')[0], 'self'), ['ai:a1']);
  assert.deepStrictEqual(env2.errors, []);
});

test('one_view_only: no view switch, and the selector of the instructions comes after the maps', () => {
  const env = boot('#view=columns&map=MINI-A&rung=rung1');
  const host = env.window.document.createElement('div');
  env.SS.viewer.mount(host, clone(VIEW), {});
  const views = find(host, (n) => attr(n, 'data-section') === 'views')[0];
  assert.strictEqual(find(views, (n) => hasClass(n, 'ss-viewswitch')).length, 0);
  assert.strictEqual(find(views, (n) => hasClass(n, 'ss-cols') || hasClass(n, 'ss-ov')).length, 0);
  const order = [];
  walk(views, (n) => {
    if (hasClass(n, 'ss-viewer-panel')) order.push('panel');
    if (hasClass(n, 'ss-viewer-stamp')) order.push('stamp');
    if (hasClass(n, 'ss-viewer-selector')) order.push('selector');
  });
  // item 42 (Part 5c, re-pinned on purpose): the caption moved to the row above the lanes
  assert.deepStrictEqual(order, ['stamp', 'panel', 'selector']);
  const stamp = find(views, (n) => hasClass(n, 'ss-viewer-stamp'))[0];
  assert.strictEqual(stamp.textContent, 'Map MINI-A · ' + EN['ledger.rung.rung1']);
  const legend = find(views, (n) => n.tagName === 'LEGEND')[0];
  assert.strictEqual(legend.textContent, 'Instructions given to the AI');
  assert.strictEqual(env.SS.state.get().view, 'lanes');
  assert.deepStrictEqual(env.errors, []);
});
