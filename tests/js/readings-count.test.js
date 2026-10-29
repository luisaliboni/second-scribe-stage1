'use strict';
// R7 / R11 (browser-pass review): the number of readings the page names follows what is drawn.
// Mode C has no reference map, so two readings are drawn (no-interview and AI); A and B draw three.
// The stub DOM has no real events, so the viewer is mounted and its text and aria-labels are read back.
const test = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const { load, ROOT } = require('./harness');

const VIEW = JSON.parse(fs.readFileSync(path.join(ROOT, 'tests', 'fixtures', 'views', 'mini.view.json'), 'utf8'));
const EN = JSON.parse(fs.readFileSync(path.join(ROOT, 'second_scribe', 'app', 'strings', 'en.json'), 'utf8'));
const clone = (o) => JSON.parse(JSON.stringify(o));

function walk(node, fn) {
  fn(node);
  (node.children || []).forEach((c) => walk(c, fn));
}
// Everything a reader or a screen reader gets: node text and the aria-label / title attributes.
function surface(root) {
  const out = [];
  walk(root, (n) => {
    if (n.textContent) out.push(n.textContent);
    if (n.attributes && n.attributes['aria-label']) out.push(n.attributes['aria-label']);
  });
  return out.join('\n');
}
function mount(refType, hash) {
  const env = load();
  env.window.location.hash = hash || '';
  const view = clone(VIEW);
  view.reference_type = refType;
  const root = env.window.document.createElement('div');
  env.SS.viewer.mount(root, view, {});
  return { env, root, text: surface(root) };
}

test('R11: mode C names two readings, never three, and says why', () => {
  const { text } = mount('C');
  assert.strictEqual(EN['viewer.panel.views.two'], 'The two readings of one map (this reference type has no reference map)');  // council 30
  assert.ok(text.indexOf(EN['viewer.panel.views.two']) >= 0, 'the heading counts two');
  // item 20 (23 Oct): the lanes are the one view, so there is no view switch to count
  assert.ok(text.indexOf(EN['views.switch.lanes.two']) < 0, 'no view switch');
  assert.ok(!/three/i.test(text), 'no "three" anywhere on a mode C viewer: ' + (text.match(/.{0,40}three.{0,40}/i) || [''])[0]);
});

test('R11: mode C aria labels and the rung note follow the two lanes (a hash naming the columns view still draws lanes)', () => {
  ['lanes', 'columns'].forEach((v) => {
    const { text } = mount('C', '#view=' + v);
    assert.ok(!/three/i.test(text), v);
    assert.ok(text.indexOf(EN['lane.panel_label.two']) >= 0, v + ' is drawn as lanes with the two-reading label');
  });
  const { env } = mount('C');
  assert.strictEqual(env.SS.lanes.readingCount({ reference_type: 'C' }), 2);
  assert.strictEqual(EN['ledger.selector.note.two'], 'Changing only the instructions given to the AI already changes its map, while the other map stays the same.');
  assert.strictEqual(EN['lane.panel_label.two'], 'The two readings of one map, by phase');
  assert.strictEqual(EN['views.columns.panel_label.two'], 'The two readings of one map, side by side');
});

test('R11: modes A and B keep the three-reading wording', () => {
  ['A', 'B'].forEach((t) => {
    const { text, env } = mount(t);
    assert.strictEqual(env.SS.lanes.readingCount({ reference_type: t }), 3);
    assert.ok(text.indexOf(EN['viewer.panel.views']) >= 0, t);
    assert.ok(text.indexOf(EN['views.switch.lanes']) < 0, t + ': no view switch (item 20)');
    assert.ok(text.indexOf(EN['ledger.selector.note']) >= 0, t);
    assert.ok(text.indexOf(EN['viewer.panel.views.two']) < 0, t);
  });
});

test('R11: the stamp names the map and the instructions only, so it carries no count in any mode (item 20)', () => {
  const { env } = mount('C');
  const S = env.SS.views;
  const want = "Map M · The study's instructions (rung 2)";
  assert.strictEqual(S.stamp({ view: 'lanes', map: 'M', rung: 'rung2' }, { reference_type: 'C' }), want);
  assert.strictEqual(S.stamp({ view: 'columns', rung: 'rung2' }, { reference_type: 'C' }), "The study's instructions (rung 2)");
  assert.strictEqual(S.stamp({ view: 'lanes', map: 'M', rung: 'rung2' }, { reference_type: 'A' }), want);
  assert.strictEqual(S.stamp({ view: 'lanes', map: 'M', rung: 'rung2' }), want);
});

test('R11: the report and export text give no count (it is the same page for every mode)', () => {
  ['export.report.body', 'report.stamp.view_value', 'report.maps.title'].forEach((k) => {
    assert.ok(EN[k] && !/\bthree\b/i.test(EN[k]), k + ': ' + EN[k]);
    assert.ok(/readings/.test(EN[k]), k + ' still names the readings');
  });
});
