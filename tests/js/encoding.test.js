'use strict';
// Task 23: shape encoding and screen-reader labels. The fixture is SYNTHETIC (tests/fixtures/views/mini.view.json).
const test = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const { load, ROOT } = require('./harness');

const VIEW = JSON.parse(fs.readFileSync(path.join(ROOT, 'tests', 'fixtures', 'views', 'mini.view.json'), 'utf8'));
const ITEM = VIEW.maps[0].items[0]; // "Pre-application meeting": participant marked missing; rung 2 leaked

function item(over) {
  return Object.assign({ item_id: 'x', label: 'Step', reference_status: 'happened', script_has: false, fate: {} }, over);
}

test('fixture is a SYNTHETIC view-data/1 file', () => {
  assert.strictEqual(VIEW.schema, 'view-data/1');
  assert.strictEqual(VIEW.test_pattern, true);
  assert.match(VIEW.fixture, /^SYNTHETIC/);
});

test('encoding_table', () => {
  const { SS } = load();
  const fates = {
    kept: ['dashed', ''],
    leaked: ['leaked', '▲'],
    recovered: ['normal', ''],
    flipped: ['dashed', '▼'],
    missed: ['never', ''],
    unattested: ['never', ''],
  };
  for (const [fate, [shape, glyph]] of Object.entries(fates)) {
    const e = SS.encoding.forItem('ai', item({ fate: { rung2: fate } }), 'rung2');
    assert.strictEqual(e.shape, shape, 'ai/' + fate);
    assert.strictEqual(e.glyph, glyph, 'ai/' + fate + ' glyph');
  }
  // reference lane: happened -> normal, missing -> dashed
  assert.deepStrictEqual(JSON.parse(JSON.stringify(SS.encoding.forItem('reference', item({ reference_status: 'happened' }), 'rung2'))),
    { shape: 'normal', glyph: '' });
  assert.deepStrictEqual(JSON.parse(JSON.stringify(SS.encoding.forItem('reference', item({ reference_status: 'missing' }), 'rung2'))),
    { shape: 'dashed', glyph: '' });
  // the no-interview lane (code name: script) is the grey fill, and only where it drew the step
  assert.strictEqual(SS.encoding.forItem('script', item({ script_has: true }), 'rung2').shape, 'script');
  assert.strictEqual(SS.encoding.forItem('script', item({ script_has: false }), 'rung2'), null);
  assert.strictEqual(SS.encoding.forItem('script', item({ script_has: null }), 'rung2'), null);
});

test('encoding: unattested carries the quote-not-found tag, nothing else does', () => {
  const { SS } = load();
  const e = SS.encoding.forItem('ai', item({ fate: { rung2: 'unattested' } }), 'rung2');
  assert.strictEqual(e.tag, 'Not attested by its own quote');
  for (const fate of ['kept', 'leaked', 'recovered', 'flipped', 'missed']) {
    assert.strictEqual(SS.encoding.forItem('ai', item({ fate: { rung2: fate } }), 'rung2').tag, undefined, fate);
  }
});

test('encoding: a fate that was not computed is its own shape, never a normal box', () => {
  const { SS } = load();
  for (const fate of [{}, { rung2: null }, { rung2: 'not_a_fate' }]) {
    const e = SS.encoding.forItem('ai', item({ fate }), 'rung2');
    assert.strictEqual(e.shape, 'unknown');
    assert.strictEqual(e.glyph, '');
  }
  // fate of another rung does not leak into this one
  assert.strictEqual(SS.encoding.forItem('ai', item({ fate: { rung1: 'leaked' } }), 'rung2').shape, 'unknown');
  assert.strictEqual(SS.encoding.forItem('reference', item({ reference_status: 'weird' }), 'rung2').shape, 'unknown');
});

test('encoding: shape is never colour alone (each shape has its own class, only leaked/flipped carry a glyph)', () => {
  const { SS } = load();
  const classes = ['normal', 'dashed', 'leaked', 'never', 'script', 'unknown'].map((s) => SS.encoding.shapeClass(s));
  assert.strictEqual(new Set(classes).size, 6);
  for (const c of classes) assert.match(c, /^ss-shape-[a-z]+$/);
  assert.strictEqual(SS.encoding.shapeClass('nonsense'), 'ss-shape-unknown');
});

test('a11y_label_exact', () => {
  const { SS } = load();
  assert.strictEqual(
    SS.a11y.itemLabel(ITEM, 'rung2'),
    'Pre-application meeting: participant marked it as wished for; AI drew it as received at rung 2');
});

test('a11y: every fate has its own sentence, the rung is named, nothing says "script"', () => {
  const { SS } = load();
  const want = {
    kept: 'AI drew it as wished for at rung 1',
    leaked: 'AI drew it as received at rung 1',
    recovered: 'AI drew it as received at rung 1',
    flipped: 'AI drew it as wished for at rung 1',
    missed: 'AI never drew it at rung 1',
    unattested: 'AI drew it, but not attested by its own quote, at rung 1',
  };
  for (const [fate, ai] of Object.entries(want)) {
    const label = SS.a11y.itemLabel(item({ reference_status: 'happened', fate: { rung1: fate } }), 'rung1');
    assert.strictEqual(label, 'Step: participant marked happened; ' + ai, fate);
  }
  assert.strictEqual(SS.a11y.itemLabel(item({ fate: { placebo: 'kept' } }), 'placebo'),
    'Step: participant marked happened; AI drew it as wished for at the placebo');
  assert.strictEqual(SS.a11y.itemLabel(item({ fate: { rung3: 'kept' } }), 'rung3'),
    'Step: participant marked happened; AI drew it as wished for at rung 3');
  // not computed is said, not omitted
  assert.strictEqual(SS.a11y.itemLabel(item({ fate: {} }), 'rung2'),
    'Step: participant marked happened; AI: not computed at rung 2');
  // an unknown rung key is named as given
  assert.match(SS.a11y.itemLabel(item({ fate: { zz: 'kept' } }), 'zz'), /at zz$/);
});

test('a11y: mode B names the researcher, not the participant', () => {
  const { SS } = load();
  assert.strictEqual(
    SS.a11y.itemLabel(ITEM, 'rung2', 'B'),
    'Pre-application meeting: researcher marked it as wished for; AI drew it as received at rung 2');
  assert.match(SS.a11y.itemLabel(item({ reference_status: 'happened' }), 'rung2', 'B'), /researcher marked happened/);
});

test('a11y: lane labels differ per lane and the no-interview lane does not claim a rung', () => {
  const { SS } = load();
  const it = item({ label: 'Fire safety check', reference_status: 'missing', script_has: true, fate: { rung2: 'leaked' } });
  assert.strictEqual(SS.a11y.laneLabel('script', it, 'rung2'), 'Fire safety check: the no-interview map drew it');
  assert.strictEqual(SS.a11y.laneLabel('reference', it, 'rung2'), 'Fire safety check: participant marked it as wished for');
  assert.strictEqual(SS.a11y.laneLabel('reference', it, 'rung2', 'B'), 'Fire safety check: researcher marked it as wished for');
  assert.strictEqual(SS.a11y.laneLabel('ai', it, 'rung2'), SS.a11y.itemLabel(it, 'rung2'));
});

test('a11y and encoding never print a forbidden word', () => {
  const { SS } = load();
  const bad = /\bscript\b|official script|erase|fabrication|ground truth|validated|replicable/i;
  for (const m of VIEW.maps) for (const it of m.items) for (const r of VIEW.rungs) for (const ref of ['A', 'B']) {
    for (const lane of ['script', 'ai', 'reference']) {
      // user-visible text only: the label and the tag (the shape name 'script' is a code identifier)
      const enc = SS.encoding.forItem(lane, it, r);
      const text = SS.a11y.laneLabel(lane, it, r, ref) + ' ' + (enc ? enc.glyph + (enc.tag || '') : '');
      assert.doesNotMatch(text, bad, text);
    }
  }
});
