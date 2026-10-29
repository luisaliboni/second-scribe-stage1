'use strict';
// Plan RERUN-PLAN-PROFILE-ZIP Task 4 (code only): the v4-shaped study file = profile-v3.json with reading 3 at
// temperature 0 and two extra cells in the lead row (rarefied_ai_recall, rarefied_ai_minus_prior_only_recall).
// The v4 document is built here, at test time, from the v3 fixture. Nothing is added to tests/fixtures/ (package.py
// ships that folder). The values below are placeholders for shape only; they are not results.
// The two new cell labels and the matching_one_to_one qualifier sentence come from en.json (looked up below, so a
// missing key fails here instead of printing the renderer's fallback).
const test = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const { load, ROOT, JS_DIR } = require('./harness');

const FIX = path.join(ROOT, 'tests', 'fixtures', 'profiles');
const EN = JSON.parse(fs.readFileSync(path.join(ROOT, 'second_scribe', 'app', 'strings', 'en.json'), 'utf8'));
const V3 = JSON.parse(fs.readFileSync(path.join(FIX, 'profile-v3.json'), 'utf8'));
const clone = (o) => JSON.parse(JSON.stringify(o));

const RARE_AI = 'rarefied_ai_recall';
const RARE_MINUS = 'rarefied_ai_minus_prior_only_recall';
const LABEL_AI = EN['profile.lead.cell.' + RARE_AI];
const LABEL_MINUS = EN['profile.lead.cell.' + RARE_MINUS];
const ONE_TO_ONE = EN['profile.qualifier.matching_one_to_one'];

// A v4-shaped document: temperature 0, reading-3 values changed, the two rarefied cells appended to the lead row.
function makeV4(opts) {
  opts = opts || {};
  const doc = clone(V3);
  doc.run.settings.reading3_temperature = 0;
  const lead = doc.rows.find((r) => r.row === 'lead');
  const row7 = doc.rows.find((r) => r.row === 7);
  const put = (row, name, x, v) => {
    const c = row.cells.find((k) => k.name === name && (k.x === x || (x === undefined && k.x === undefined)));
    assert.ok(c, name + '/' + x);
    c.value = v;
  };
  [lead, row7].forEach((row) => {
    put(row, 'no_interview_recall', 'primary', 0.123);
    put(row, 'listening_added', 'primary', 0.271);
    put(row, 'no_interview_recall', 'lenient', 0.201);
    put(row, 'listening_added', 'lenient', 0.26);
  });
  put(lead, 'no_interview_nodes_per_map', undefined, 12.5);
  doc.reference.map_size.reading3_map_size.nodes_per_map = 12.5;
  const q = opts.oneToOne ? ['size_beside_lead', 'matching_one_to_one'] : ['size_beside_lead'];
  const mk = (name, value, lo, hi, kind) => ({
    name, x: 'primary', n: 43, unit: 'node', kind, value, ci_lo: lo, ci_hi: hi, ci_level: 0.95,
    qualifiers: q.slice(), source: 'gate chain entry 022',
  });
  lead.cells.push(mk(RARE_AI, 0.31, 0.27, 0.35, 'share'), mk(RARE_MINUS, 0.19, 0.15, 0.23, 'difference'));
  return doc;
}

function walk(node, fn, parents) {
  parents = parents || [];
  fn(node, parents);
  (node.children || []).forEach((c) => walk(c, fn, parents.concat([node])));
}
function textOf(node) {
  const out = [];
  walk(node, (n) => { if (n.textContent) out.push(n.textContent); });
  return out.join(' ');
}
function attr(n, k) { return n.attributes ? n.attributes[k] : undefined; }
function find(root, pred) {
  const out = [];
  walk(root, (n, parents) => { if (pred(n)) out.push({ node: n, parents }); });
  return out;
}
function byAttr(root, k, v) { return find(root, (n) => attr(n, k) !== undefined && (v === undefined || attr(n, k) === v)); }

function render(doc) {
  const env = load({ files: ['core.js', 'profile.js'] });
  const root = env.window.document.createElement('div');
  env.SS.profile.render(root, doc);
  return { root, env };
}
// the cell keys ("lead|name|x") inside one data-group of the lead
function cellsIn(root, group) {
  const g = byAttr(root, 'data-group', group);
  assert.strictEqual(g.length, 1, 'one ' + group + ' group');
  return byAttr(g[0].node, 'data-cell').map((h) => attr(h.node, 'data-cell'));
}
const KEY_AI = 'lead|' + RARE_AI + '|primary';
const KEY_MINUS = 'lead|' + RARE_MINUS + '|primary';

test('v4_fixture_is_built_not_stored', () => {
  assert.ok(!fs.readdirSync(FIX).some((f) => /v4/i.test(f)), 'no v4 file under tests/fixtures/profiles');
  const doc = makeV4();
  assert.strictEqual(doc.run.settings.reading3_temperature, 0);
  const onDisk = JSON.parse(fs.readFileSync(path.join(FIX, 'profile-v3.json'), 'utf8'));
  assert.deepStrictEqual(V3, onDisk, 'building v4 leaves the v3 fixture object untouched');
  assert.strictEqual(doc.rows.find((r) => r.row === 'lead').cells.length, V3.rows[0].cells.length + 2);
});

test('rarefied_cells_render_beside_the_lead_not_under_settings', () => {
  const { root, env } = render(makeV4());
  assert.deepStrictEqual(env.errors, []);
  const sizes = cellsIn(root, 'lead-sizes');
  assert.ok(sizes.includes(KEY_AI), 'rarefied ai recall in the sizes block');
  assert.ok(sizes.includes(KEY_MINUS), 'rarefied difference in the sizes block');
  const settings = cellsIn(root, 'lead-settings');
  assert.ok(!settings.includes(KEY_AI) && !settings.includes(KEY_MINUS), 'not under settings: ' + settings.join(', '));
  // each exactly once in the whole page
  assert.strictEqual(byAttr(root, 'data-cell', KEY_AI).length, 1);
  assert.strictEqual(byAttr(root, 'data-cell', KEY_MINUS).length, 1);
  // the settings group is what it was in v3 (nothing else moved)
  const v3settings = cellsIn(render(V3).root, 'lead-settings');
  assert.deepStrictEqual(settings, v3settings);
  // the new cells print as numbers, with their interval
  const ai = byAttr(root, 'data-cell', KEY_AI)[0].node;
  const pv = (n, p) => textOf(find(n, (k) => attr(k, 'data-part') === p)[0].node);
  assert.strictEqual(pv(ai, 'value'), '31.0%');
  assert.strictEqual(pv(ai, 'ci'), '[27.0%, 35.0%]');
  assert.strictEqual(pv(byAttr(root, 'data-cell', KEY_MINUS)[0].node, 'value'), '+19.0 points');
});

test('rarefied_cells_routing_is_by_the_size_beside_lead_qualifier', () => {
  // The renderer routes by qualifier (profile.js renderLead), not by name. Without it the same cells land under
  // settings: this pins that the builder must keep writing the qualifier (build_profile_v4.LEAD_TAG).
  const doc = makeV4();
  doc.rows.find((r) => r.row === 'lead').cells.forEach((c) => {
    if (c.name === RARE_AI || c.name === RARE_MINUS) c.qualifiers = [];
  });
  const { root } = render(doc);
  assert.ok(cellsIn(root, 'lead-settings').includes(KEY_AI));
  assert.ok(!cellsIn(root, 'lead-sizes').includes(KEY_AI));
});

test('rarefied_cells_print_their_en_labels', () => {
  for (const [label, name] of [[LABEL_AI, RARE_AI], [LABEL_MINUS, RARE_MINUS]]) {
    assert.strictEqual(typeof label, 'string', 'en.json has profile.lead.cell.' + name);
    assert.ok(label.length > 0 && label !== name.replace(/_/g, ' '), name + ': not the fallback');
  }
  const { root } = render(makeV4());
  const t = textOf(byAttr(root, 'data-cell', KEY_AI)[0].node);
  assert.ok(t.includes(LABEL_AI), t);
  assert.ok(!t.includes(RARE_AI.replace(/_/g, ' ')), 'no fallback label: ' + t);
  const t2 = textOf(byAttr(root, 'data-cell', KEY_MINUS)[0].node);
  assert.ok(t2.includes(LABEL_MINUS), t2);
  assert.ok(!t2.includes(RARE_MINUS.replace(/_/g, ' ')), 'no fallback label: ' + t2);
});

test('matching_one_to_one_qualifier_prints_its_en_sentence', () => {
  assert.strictEqual(typeof ONE_TO_ONE, 'string', 'en.json has profile.qualifier.matching_one_to_one');
  const { root, env } = render(makeV4({ oneToOne: true }));
  assert.deepStrictEqual(env.errors, []);
  for (const key of [KEY_AI, KEY_MINUS]) {
    const cell = byAttr(root, 'data-cell', key);
    assert.strictEqual(cell.length, 1);
    const chips = byAttr(cell[0].node, 'data-qualifier');
    const codes = chips.map((h) => attr(h.node, 'data-qualifier'));
    assert.ok(codes.includes('matching_one_to_one'), key + ': ' + codes.join(', '));
    // the chip prints the fixed sentence, never the raw code, undefined or an object
    const chip = chips.find((h) => attr(h.node, 'data-qualifier') === 'matching_one_to_one');
    assert.strictEqual(textOf(chip.node), ONE_TO_ONE);
  }
  assert.ok(cellsIn(root, 'lead-sizes').includes(KEY_AI), 'still beside the lead');
  assert.ok(!/undefined|\[object Object\]|NaN/.test(textOf(root)));
});

test('row_7_keeps_its_cells_and_gets_no_rarefied_cells', () => {
  // the builder adds the two cells to the lead row only (build_profile_v4 row7_new); row 7 repeats the floors
  const { root, env } = render(makeV4());
  assert.deepStrictEqual(env.errors, []);
  const names = byAttr(root, 'data-cell').map((h) => attr(h.node, 'data-cell'));
  assert.ok(names.includes('7|listening_added|primary'));
  assert.ok(!names.some((k) => k.startsWith('7|rarefied')));
  assert.strictEqual(names.filter((k) => k.includes(RARE_AI)).length, 1);
});

// ------------------------------------------------------------------------------------------- the study page
function renderStudy(doc) {
  const e = load({ files: ['core.js', 'strip.js', 'profile.js'] });
  for (const f of ['site-study.js', 'site-install.js']) {
    vm.runInContext(fs.readFileSync(path.join(JS_DIR, f), 'utf8'), e.window, { filename: f });
  }
  const root = e.window.document.createElement('div');
  e.SS.study.render(root, doc);
  return { root, errors: e.errors };
}
function runValue(r, key) {
  const p11 = byAttr(r.root, 'data-study-panel', '11')[0].node;
  return textOf(byAttr(p11, 'data-key', key)[0].node);
}

test('study_page_shows_reading3_temperature_0', () => {
  const r = renderStudy(makeV4());
  assert.deepStrictEqual(r.errors, []);
  const t = runValue(r, 'run.settings.reading3_temperature');
  assert.strictEqual(t, '0', 'a zero is printed as 0, not as "not computed"');
  // a nonzero temperature prints with the kit's digits (built here, so the check does not depend on the fixture)
  const warm = makeV4();
  warm.run.settings.reading3_temperature = 0.7;
  assert.strictEqual(runValue(renderStudy(warm), 'run.settings.reading3_temperature'), '.7');
});

test('study_page_renders_the_v4_document_without_errors_and_with_the_new_cells_once', () => {
  const r = renderStudy(makeV4({ oneToOne: true }));
  assert.deepStrictEqual(r.errors, []);
  assert.strictEqual(byAttr(r.root, 'data-cell', KEY_AI).length, 1);
  assert.strictEqual(byAttr(r.root, 'data-cell', KEY_MINUS).length, 1);
  const sizes = byAttr(r.root, 'data-group', 'lead-sizes');
  assert.strictEqual(sizes.length, 1);
  assert.ok(byAttr(sizes[0].node, 'data-cell', KEY_AI).length === 1);
});
