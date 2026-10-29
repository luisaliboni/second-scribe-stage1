'use strict';
// Plan V3: the profile renderer (SS.profile.render) for files of schema orgsci-profile/v2.
// Fixtures: tests/fixtures/profiles/profile-v2.json (state reported, the study's gate-cleared file) and
// profile-synth.json (state synthetic, the parity pack run on the demo corpus).
// Number printing is pinned to analysis/RESULTS-PHASE1-2026-10-18.md (profile table): no leading zero,
// U+2212 minus, a sign on differences, values with the digits the file carries, counts grouped "1,268".
// 2.6 (part 5c): shares and rates print as percentages ("39.4%"), differences between shares in percentage points
// ("+24.1 points"), one decimal, as SS.profile.lead does; every other kind keeps the table's digits.
const test = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const { load, ROOT } = require('./harness');

const FIX = path.join(ROOT, 'tests', 'fixtures', 'profiles');
const V2 = JSON.parse(fs.readFileSync(path.join(FIX, 'profile-v2.json'), 'utf8'));
// the study's file in its public form (run block as PROFILE-SCHEMA section 14): same cells and values as V2
const V3 = JSON.parse(fs.readFileSync(path.join(FIX, 'profile-v3.json'), 'utf8'));
const SYNTH = JSON.parse(fs.readFileSync(path.join(FIX, 'profile-synth.json'), 'utf8'));
// The kit exporter's sample export on the demo (E2 milestone M1): codes, never sentences (PROFILE-SCHEMA §13).
const SAMPLE = JSON.parse(fs.readFileSync(path.join(FIX, 'e2-m1-sample-demo.json'), 'utf8'));
const EN = JSON.parse(fs.readFileSync(path.join(ROOT, 'second_scribe', 'app', 'strings', 'en.json'), 'utf8'));
// The kit's sentences for those codes, merged as strings.load_strings merges them (never a hand copy).
const KIT = JSON.parse(fs.readFileSync(path.join(ROOT, 'second_scribe', 'profile_strings.json'), 'utf8'));
const clone = (o) => JSON.parse(JSON.stringify(o));

const DERIVED_TEXT = 'arithmetic on reported numbers, with no statistical test';  // council 30: no "X, not Y"
const GLOSS = 'a map of the official pathway (prompt quoted)';

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
function cellKey(row, c) { return row + '|' + c.name + '|' + (c.x === undefined || c.x === null ? '' : c.x); }
function part(cellNode, name) {
  const hit = find(cellNode, (n) => attr(n, 'data-part') === name);
  return hit.length ? textOf(hit[0].node) : undefined;
}

function render(doc, opts) {
  const env = load({ files: ['core.js', 'profile.js'] });
  const root = env.window.document.createElement('div');
  env.SS.profile.render(root, doc, opts);
  return { root, env };
}
function cellNode(root, key) {
  const hits = byAttr(root, 'data-cell', key);
  assert.strictEqual(hits.length, 1, 'exactly one element for ' + key);
  return hits[0];
}
// The file's cells, once each. The two map sizes printed again beside +.241 in row 7 (V8) are repeats of the lead's.
function ownCells(root) {
  return byAttr(root, 'data-cell')
    .filter((h) => !h.parents.some((p) => attr(p, 'data-group') === 'beside-sizes'))
    .map((h) => attr(h.node, 'data-cell'));
}
function rowOf(doc, key) { return doc.rows.find((r) => r.row === key); }

// ------------------------------------------------------------------------------------------- number printing
test('number_format_pinned_to_results_table', () => {
  const { SS } = load({ files: ['core.js', 'profile.js'] });
  const f = SS.profile.fmt;
  const cases = [
    [0.394, 'share', '39.4%'], [0.152, 'share', '15.2%'], [0.27, 'share', '27.0%'], [0.2, 'share', '20.0%'], [0.15, 'difference', '+15.0 points'], [0.07, 'upper_bound', '0.070'],
    [0.241, 'difference', '+24.1 points'], [-0.069, 'difference', '−6.9 points'], [0.0, 'difference', '0.0 points'],
    [0.0, 'share', '0.0%'], [0.014, 'difference', '+1.4 points'], [0.044, 'rate', '4.4%'], [1, 'share', '100.0%'],
    [29.488, 'mean', '29.488'], [15.465, 'mean', '15.465'], [15.488, 'mean', '15.488'], [15.48, 'mean', '15.480'],
    [14.023, 'count_difference', '+14.023'], [1.093, 'rank', '1.093'], [22.0, 'rank', '22'], [7.897, 'ratio', '7.897'],
    [1268, 'count', '1,268'], [304, 'count', '304'], [0, 'count', '0'], [39540, 'count', '39,540'],
    [250.304, 'count', '250'], [354.474, 'count', '354'], [1234.5, 'mean', '1,234.500'],
  ];
  for (const [v, kind, want] of cases) assert.strictEqual(f.num(v, kind), want, `${v} ${kind}`);
  // interval ends never carry a plus sign; negatives use U+2212
  assert.strictEqual(f.ci(0.205, 0.277), '[.205, .277]');
  assert.strictEqual(f.ci(-0.127, -0.013), '[−.127, −.013]');
  assert.strictEqual(f.ci(-0.047, 0.072), '[−.047, .072]');
  assert.strictEqual(f.ci(0, 0.068), '[0, .068]');
  assert.strictEqual(f.ci(250.304, 354.474, 'count'), '[250, 354]');   // 2.2 issue 3: a count prints whole, ends included
  // 2.6 part 5c: a share interval in percentages, a difference interval in points; ends never carry a plus sign
  assert.strictEqual(f.ci(0.361, 0.427, 'share'), '[36.1%, 42.7%]');
  assert.strictEqual(f.ci(0.205, 0.277, 'difference'), '[20.5, 27.7] points');
  assert.strictEqual(f.ci(-0.047, 0.072, 'difference'), '[−4.7, 7.2] points');
  assert.strictEqual(f.range(0.073, 0.461, 'share'), '7.3%–46.1%');
  assert.strictEqual(f.range(0.225, 0.241, 'difference'), '22.5–24.1 points');
  // the display kind of a cell: decided from its kind and name (PROFILE-SCHEMA sections 3, 11)
  const kinds = [
    [{ name: 'ai_recall', kind: 'share' }, 'share'], [{ name: 'flattening_rate', kind: 'rate' }, 'rate'],
    [{ name: 'listening_added', kind: 'difference' }, 'difference'], [{ name: 'pull', kind: 'difference' }, 'difference'],
    [{ name: 'ai_minus_no_interview_nodes_per_map', kind: 'difference' }, 'count_difference'],
    [{ name: 'ai_nodes_remedy_minus_placebo', kind: 'difference' }, 'count_difference'],
    [{ name: 'counterfactual_upper_bound', kind: 'upper_bound' }, 'share'],
    [{ name: 'largest_excess_ruled_out', kind: 'upper_bound' }, 'difference'],
    [{ name: 'r_star', kind: 'ratio' }, 'share'], [{ name: 'ai_per_endorsed', kind: 'ratio' }, 'share'],
    [{ name: 'no_interview_over_ai_nodes', kind: 'ratio' }, 'share'],   // Part 6, re-pinned on purpose: percentages
    [{ name: 'yield_per_box', kind: 'ratio' }, 'ratio'], [{ name: 'canonical_ari', kind: 'index' }, 'index'],
    [{ name: 'ai_per_map', kind: 'count' }, 'per_map'], [{ name: 'own_map_mean_rank', kind: 'rank' }, 'rank'],
  ];
  for (const [c, want] of kinds) assert.strictEqual(f.kind(c), want, c.name);
  // missing and placeholders
  assert.strictEqual(f.num(undefined, 'share'), 'not computed');
  assert.strictEqual(f.num(null, 'share'), 'not computed');
  assert.strictEqual(f.num('[SOURCE]', 'share'), 'not yet available');
  assert.strictEqual(f.ci(undefined, 0.3), 'not computed');
});

test('lead_number_prints_like_results', () => {
  const { root } = render(V2);
  const lead = cellNode(root, 'lead|listening_added|primary').node;
  assert.strictEqual(part(lead, 'value'), '+24.1 points');
  assert.strictEqual(part(lead, 'ci'), '[20.5, 27.7] points');
  assert.strictEqual(part(lead, 'ci_level'), '95% interval');
  const ai = cellNode(root, 'lead|ai_recall|primary').node;
  assert.strictEqual(part(ai, 'value'), '39.4%');
  assert.strictEqual(part(ai, 'ci'), '[36.1%, 42.7%]');
  assert.match(part(ai, 'n'), /1,977/);
  const size = cellNode(root, 'lead|no_interview_nodes_per_map|').node;
  assert.strictEqual(part(size, 'value'), '15.465');
  const box = cellNode(root, 'ledger|ai_boxes_total|').node;
  assert.strictEqual(part(box, 'value'), '1,268');
});

// ------------------------------------------------------------------------------------------- every cell, once
test('renders_profile_v2_every_cell', () => {
  const { root, env } = render(V2);
  const want = [];
  V2.rows.forEach((r) => r.cells.forEach((c) => want.push(cellKey(r.row, c))));
  const got = ownCells(root);
  assert.strictEqual(new Set(want).size, want.length, 'fixture triples are unique');
  assert.deepStrictEqual(got.slice().sort(), want.slice().sort());
  for (const k of want) assert.strictEqual(got.filter((g) => g === k).length, 1, k);
  assert.deepStrictEqual(env.errors, [], 'no unknown string key and no error');
  // rows render in file order
  const rows = byAttr(root, 'data-row').map((h) => attr(h.node, 'data-row'));
  assert.deepStrictEqual(rows, V2.rows.map((r) => String(r.row)));
  // a reported file shows no SYNTHETIC band
  assert.ok(!textOf(root).includes(EN['band.synthetic']));
});

test('renders_parity_file_without_error', () => {
  let out;
  assert.doesNotThrow(() => { out = render(SYNTH); });
  const { root, env } = out;
  assert.deepStrictEqual(env.errors, []);
  const want = [];
  SYNTH.rows.forEach((r) => r.cells.forEach((c) => want.push(cellKey(r.row, c))));
  const got = ownCells(root);
  assert.deepStrictEqual(got.slice().sort(), want.slice().sort());
  // the SYNTHETIC band, once, before the first row
  const bands = find(root, (n) => n.textContent === EN['band.synthetic']);
  assert.strictEqual(bands.length, 1);
  assert.strictEqual(attr(root.children[0], 'data-band'), 'synthetic');
});

// ------------------------------------------------------------------------------------------- lead and sizes
function leadParts(root) {
  const lead = byAttr(root, 'data-row', 'lead')[0].node;
  const block = byAttr(lead, 'data-group', 'lead')[0].node;
  const kids = block.children.map((k) => attr(k, 'data-group'));
  return { lead, block, kids };
}

test('lead_always_shows_size', () => {
  const check = (doc) => {
    const { root } = render(doc);
    const { block, kids } = leadParts(root);
    const iHead = kids.indexOf('lead-number');
    assert.ok(iHead >= 0, 'lead number group');
    assert.strictEqual(kids[iHead + 1], 'lead-sizes', 'sizes sit right beside the lead number');
    const head = block.children[iHead];
    const sizes = block.children[iHead + 1];
    assert.strictEqual(byAttr(head, 'data-cell', 'lead|listening_added|primary').length, 1);
    const lead = rowOf(doc, 'lead');
    const tagged = lead.cells.filter((c) => (c.qualifiers || []).includes('size_beside_lead'));
    assert.ok(tagged.length >= 8);
    for (const c of tagged) assert.strictEqual(byAttr(sizes, 'data-cell', cellKey('lead', c)).length, 1, c.name);
  };
  check(V2);
  check(SYNTH);
  // the file lists the sizes before the lead number, and the lead number is not computed: sizes still beside it
  const odd = clone(V2);
  const lead = rowOf(odd, 'lead');
  const sizes = lead.cells.filter((c) => (c.qualifiers || []).includes('size_beside_lead'));
  lead.cells = sizes.concat(lead.cells.filter((c) => !sizes.includes(c)));
  delete lead.cells.find((c) => c.name === 'listening_added' && c.x === 'primary').value;
  check(odd);
  const { root } = render(odd);
  const head = cellNode(root, 'lead|listening_added|primary').node;
  assert.strictEqual(part(head, 'value'), 'not computed');
});

test('derived_label', () => {
  const { root } = render(V2);
  V2.rows.forEach((r) => r.cells.forEach((c) => {
    const t = textOf(cellNode(root, cellKey(r.row, c)).node);
    // V1: the "not a test" line belongs to derived: true / not_a_test cells (the lead's four lines) only
    if (c.derived === true) assert.ok(t.includes(DERIVED_TEXT), 'derived label on ' + cellKey(r.row, c));
    else assert.ok(!t.includes(DERIVED_TEXT), 'no derived label on ' + cellKey(r.row, c));
  }));
});

test('derived_only_beside_lead', () => {
  const insideSizes = (parents) => parents.some((p) => attr(p, 'data-group') === 'lead-sizes');
  const check = (doc) => {
    const { root } = render(doc);
    const derived = find(root, (n) => attr(n, 'data-derived') === 'beside_lead');
    assert.ok(derived.length >= 4);
    for (const d of derived) assert.ok(insideSizes(d.parents), attr(d.node, 'data-cell') + ' sits beside the lead');
    return root;
  };
  check(V2);
  check(SYNTH);
  // a derived line placed in another row still prints beside the lead, never on its own
  const odd = clone(V2);
  const lead = rowOf(odd, 'lead');
  const moved = lead.cells.filter((c) => c.derived === true);
  lead.cells = lead.cells.filter((c) => c.derived !== true);
  rowOf(odd, 3).cells.push(...moved);
  const root = check(odd);
  const row3 = byAttr(root, 'data-row', '3')[0].node;
  for (const c of moved) {
    assert.strictEqual(byAttr(row3, 'data-cell', cellKey(3, c)).length, 0, 'not inside row 3');
    assert.ok(insideSizes(cellNode(root, cellKey(3, c)).parents));
  }
});

test('gloss_once_and_no_forbidden_words', () => {
  for (const doc of [V2, SYNTH]) {
    const { root } = render(doc);
    const t = textOf(root);
    assert.strictEqual(t.split(GLOSS).length - 1, 1, 'gloss once per page');
    assert.ok(t.includes(EN['lane.no_interview']));
    const low = t.toLowerCase();
    for (const bad of ['official script', 'validated', 'ground truth', 'replicable', 'erase', 'fabrication',
      'no support in the transcript', 'found what the interviewers missed']) assert.ok(!low.includes(bad), bad);
    // DECISIONS L67 (council 23 D8): "no-interview script(s)" is the one allowed phrase (same allowance as test_wording.py)
    assert.ok(!/\bscript\b/.test(low.replace(/no-interview scripts?\b/g, ' ')), 'no "script"');
    assert.ok(!/\bfirst\b/.test(low), 'no "first"');
  }
  // a page that already shows the gloss elsewhere can turn it off
  const { root } = render(V2, { gloss: false });
  assert.ok(!textOf(root).includes(GLOSS));
});

// ------------------------------------------------------------------------------------------- ledger
test('ledger_order', () => {
  const { root } = render(V2);
  const ledger = byAttr(root, 'data-row', 'ledger')[0].node;
  const got = byAttr(ledger, 'data-cell').map((h) => attr(h.node, 'data-cell'));
  const want = rowOf(V2, 'ledger').cells.map((c) => cellKey('ledger', c));
  assert.deepStrictEqual(got, want);
  assert.strictEqual(got[0], 'ledger|omitted_share|');
  // array order is the render order, whatever it is
  const odd = clone(V2);
  rowOf(odd, 'ledger').cells.reverse();
  const r2 = render(odd).root;
  const got2 = byAttr(byAttr(r2, 'data-row', 'ledger')[0].node, 'data-cell').map((h) => attr(h.node, 'data-cell'));
  assert.deepStrictEqual(got2, rowOf(odd, 'ledger').cells.map((c) => cellKey('ledger', c)));
});

test('no_ratio_between_ledger_cells', () => {
  const { SS } = load({ files: ['core.js', 'profile.js'] });
  const { root } = render(V2);
  const row = rowOf(V2, 'ledger');
  const ledger = byAttr(root, 'data-row', 'ledger')[0].node;
  const vals = row.cells.filter((c) => typeof c.value === 'number').map((c) => c.value);
  const fmtv = (v) => SS.profile.fmt.num(v, 'share');
  // no number the renderer could only have made by dividing one ledger cell by another
  const inFile = new Set();
  JSON.stringify(row).replace(/-?\d+(?:\.\d+)?/g, (m) => { inFile.add(fmtv(Number(m))); return m; });
  const text = textOf(ledger);
  for (const a of vals) for (const b of vals) {
    if (a === b || b === 0) continue;
    for (const digits of [2, 3]) {
      const r = Number((a / b).toFixed(digits));
      const s = fmtv(r);
      if (r === 0 || inFile.has(s)) continue;
      assert.ok(!new RegExp('(^|[^\\d.,])' + s.replace(/[.+]/g, '\\$&') + '(?![\\d])').test(text), `ratio ${a}/${b} = ${s}`);
    }
  }
  assert.ok(!/[÷×]|\bratio\b|\btimes\b/.test(text));
  // no ledger cell's text carries another ledger cell's value (other than its own denominator n)
  // (the band members and the cell's own n are the file's own numbers for that cell)
  row.cells.forEach((c) => {
    const node = cellNode(root, cellKey('ledger', c)).node;
    const shown = (part(node, 'value') || '') + ' ' + (part(node, 'ci') || '');
    row.cells.forEach((o) => {
      if (o === c || typeof o.value !== 'number' || o.value === c.value || o.value === c.n) return;
      const s = SS.profile.fmt.num(o.value, o.kind);
      assert.ok(!new RegExp('(^|[^\\d.,])' + s.replace(/[.+]/g, '\\$&') + '(?![\\d,])').test(shown),
        `${c.name} set against ${o.name}`);
    });
  });
  // the row's no_ratio qualifier is printed as its fixed phrase
  assert.ok(text.includes(EN['profile.qualifier.no_ratio']));
});

// ------------------------------------------------------------------------------------------- unavailable cells
test('not_run_text', () => {
  // the parity file: not_run cells carry a reason and no needs
  const { root } = render(SYNTH);
  const r = cellNode(root, '1|r_star|').node;
  assert.strictEqual(part(r, 'value'), 'not run in this profile');
  assert.strictEqual(attr(r, 'data-state'), 'not_run');
  assert.ok(!/(^|\s)0(\s|$)/.test(part(r, 'value')));
  // an exporter cell with needs names them
  const doc = clone(SYNTH);
  const c = rowOf(doc, 1).cells.find((x) => x.name === 'r_star');
  delete c.reason;
  c.needs = ['J2', 'sidejudge'];
  const r2 = cellNode(render(doc).root, '1|r_star|').node;
  assert.strictEqual(part(r2, 'value'),
    'not run in this profile (needs: ' + EN['profile.needs.j2'] + ', ' + EN['profile.needs.sidejudge'] + ')');
  // a band that was not run says so
  const row5 = byAttr(render(SYNTH).root, 'data-row', '5')[0].node;
  assert.ok(textOf(row5).includes(EN['profile.band.not_run']));
});

test('source_placeholder_text', () => {
  const doc = clone(V2);
  const c = rowOf(doc, 1).cells.find((x) => x.name === 'kept');
  c.value = '[SOURCE]';
  c.ci_lo = '[SOURCE]';
  c.ci_hi = '[SOURCE]';
  c.n = '[SOURCE]';
  const m = rowOf(doc, 1).cells.find((x) => x.name === 'coarsened');
  delete m.value;
  const { root, env } = render(doc);
  const k = cellNode(root, '1|kept|').node;
  assert.strictEqual(part(k, 'value'), 'not yet available');
  assert.strictEqual(part(k, 'ci'), 'not yet available');
  assert.ok(!textOf(k).includes('[SOURCE]'));
  const n = cellNode(root, '1|coarsened|').node;
  assert.strictEqual(part(n, 'value'), 'not computed');
  assert.deepStrictEqual(env.errors, []);
});

test('zero_count_prints_bound', () => {
  const { root } = render(V2);
  const p = cellNode(root, '5|counterfactual|primary').node;
  assert.strictEqual(part(p, 'value'), '0');
  assert.strictEqual(attr(p, 'data-zero'), 'true');
  const pb = part(p, 'bound');
  assert.ok(pb.includes('3.3%') && pb.includes('8.9%'), pb);
  assert.ok(!pb.includes('7.0%'), 'the lenient bound goes with the lenient zero');
  const l = cellNode(root, '5|counterfactual|lenient').node;
  assert.ok(part(l, 'bound').includes('7.0%'));
  // the bound cells themselves are still drawn once, in place
  cellNode(root, '5|counterfactual_upper_bound|all_ideals');
  // a zero with no bound cell in the file says so, and never invents one
  const doc = clone(V2);
  const row5 = rowOf(doc, 5);
  row5.cells = row5.cells.filter((c) => c.name !== 'counterfactual_upper_bound');
  const z = cellNode(render(doc).root, '5|counterfactual|primary').node;
  assert.strictEqual(part(z, 'bound'), EN['profile.zero.no_bound']);
});

// ------------------------------------------------------------------------------------------- the rest of a cell
test('qualifiers_band_rests_on_pooled_only', () => {
  const { root } = render(V2);
  // qualifier codes become fixed phrases
  assert.ok(EN['profile.qualifier.ceiling'].includes('assumes correct accepted matches and attestation'));
  const c = cellNode(root, 'ledger|attested_absent_from_map|primary').node;
  assert.ok(textOf(c).includes(EN['profile.qualifier.ceiling']));
  // band with its range, what it is over and its members
  const t = textOf(c);
  assert.ok(t.includes('200–809'), t);
  assert.ok(t.includes(EN['profile.band.over.8_settings']));
  assert.ok(t.includes('607–608'));
  // rests_on codes, expandable
  const ro = find(c, (n) => attr(n, 'data-part') === 'rests_on');
  assert.strictEqual(ro.length, 1);
  assert.strictEqual(ro[0].node.tagName, 'DETAILS');
  assert.ok(textOf(ro[0].node).includes(EN['profile.rests_on.lx']));
  // pooled_only: a point with no interval, even if the file had one
  const doc = clone(V2);
  const po = rowOf(doc, 'lead').cells.find((x) => x.name === 'no_interview_nodes_per_map');
  po.ci_lo = 9; po.ci_hi = 12; po.ci_level = 0.95;
  const pn = cellNode(render(doc).root, 'lead|no_interview_nodes_per_map|').node;
  assert.strictEqual(part(pn, 'ci'), undefined);
  assert.ok(textOf(pn).includes(EN['profile.qualifier.pooled_only']));
  // every code of PROFILE-SCHEMA sections 6 and 7 has its phrase
  for (const q of ['ceiling', 'not_bounded_below', 'descriptive', 'one_draw', 'package_contrast',
    'ci_excludes_draw_variance', 'paired_by_map', 'attested_line_one_sided', 'derived_arithmetic', 'no_interval',
    'zero_count', 'labeller_dependent', 'partly_lexical', 'no_structural_claim', 'seam_withheld_k',
    'recall_is_a_band', 'no_ratio', 'not_reproducible_to_the_digit', 'size_beside_lead', 'pooled_only',
    'not_a_test', 'floor_not_estimate']) assert.ok(EN['profile.qualifier.' + q], q);
  for (const r of ['R', 'P', 'JS', 'IT', 'AL', 'AS', 'CN', 'MO', 'LD', 'J2', 'Q', 'LX']) assert.ok(EN['profile.rests_on.' + r.toLowerCase()], r);
  for (const n of ['J2', 'sidejudge', 'ladder', 'judge_script']) assert.ok(EN['profile.needs.' + n.toLowerCase()], n);
});

// §13 (coordinator, 18 Oct): lab exports carry reason_code / formula_code / missing / p_holm_lt. Their
// sentences come from the kit's profile_strings.json (merged by strings.load_strings), never from a hand copy in
// en.json. A code with no sentence prints as the code itself, in mono. The study and parity files keep the free
// `formula` / `reason`, so both forms render.
function codedParity() {
  const doc = clone(SYNTH);
  const rs = rowOf(doc, 1).cells.find((x) => x.name === 'r_star');
  delete rs.reason;
  rs.reason_code = 'needs_J2:017';
  const y = rowOf(doc, 'lead').cells.find((x) => x.name === 'yield_per_box' && x.x === 'ai');
  delete y.formula;
  y.formula_code = 'yield_per_box:ai';
  const u = rowOf(doc, 'ledger').cells.find((x) => x.name === 'unanchored');
  delete u.partial_not_run;
  u.state = 'partial_not_run';
  u.missing = ['band', 'n'];
  const d = rowOf(doc, 8).cells.find((x) => x.name === 'size_adjusted_recall_2_minus_1');
  delete d.p_holm;
  d.p_holm_lt = 0.001;
  return doc;
}

test('parity_cell_with_formula_code_and_reason_code_renders', () => {
  const doc = codedParity();
  assert.ok(!Object.keys(EN).some((k) => /^profile\.(reason|formula|missing)\./.test(k)), 'no hand copy in en.json');
  // with the kit's sentences merged in
  const kit = {
    'profile.reason.needs_J2': 'needs a second judge family',
    'profile.formula.yield_per_box': 'endorsed steps recovered per box drawn',
    'profile.missing.band': 'the band', 'profile.missing.n': 'the denominator',
  };
  const env = load({ files: ['core.js', 'profile.js'], strings: kit });
  const root = env.window.document.createElement('div');
  assert.doesNotThrow(() => env.SS.profile.render(root, doc));
  assert.deepStrictEqual(env.errors, []);
  assert.ok(textOf(cellNode(root, '1|r_star|').node).includes('needs a second judge family'));
  assert.ok(textOf(cellNode(root, 'lead|yield_per_box|ai').node).includes('endorsed steps recovered per box drawn'));
  const ut = textOf(cellNode(root, 'ledger|unanchored|primary').node);
  assert.ok(ut.includes('the band') && ut.includes('the denominator'), ut);
  assert.ok(textOf(cellNode(root, '8|size_adjusted_recall_2_minus_1|rung2-rung1').node).includes('Holm-adjusted p < .001'));
  // without them: the code itself, in mono, and still no error
  const { root: r2, env: e2 } = render(doc);
  assert.deepStrictEqual(e2.errors, []);
  const mono = (key, code) => {
    const hits = find(cellNode(r2, key).node, (n) => n.textContent === code);
    assert.strictEqual(hits.length, 1, code);
    assert.match(attr(hits[0].node, 'class') || '', /\bmono\b/);
  };
  mono('1|r_star|', 'needs_J2:017');
  mono('lead|yield_per_box|ai', 'yield_per_box:ai');
  mono('ledger|unanchored|primary', 'band');
  // the free forms of the study and parity files still print
  const { root: r3 } = render(V2);
  const f = rowOf(V2, 'lead').cells.find((x) => x.name === 'yield_per_box' && x.x === 'ai').formula;
  assert.ok(textOf(cellNode(r3, 'lead|yield_per_box|ai').node).includes(f));
  // a free reason (pack diagnostics) stays in the DOM as data, not as printed copy: it carries kit words
  const reason = rowOf(SYNTH, 1).cells.find((x) => x.name === 'r_star').reason;
  const rs = cellNode(render(SYNTH).root, '1|r_star|').node;
  assert.strictEqual(attr(rs, 'data-reason'), reason);
  assert.ok(!textOf(rs).includes(reason));
});

// ------------------------------------------------------------------------------------------- E2 M1 sample export
// Renders with the merged strings and records every string key the renderer looked for and did not find: each such
// miss is a fallback (a raw code in mono, a cell name with spaces for underscores, a label key as a title).
function renderTracked(doc) {
  const env = load({ files: ['core.js', 'profile.js'], strings: KIT });
  const misses = new Set();
  const table = env.window.SS_STRINGS;
  env.window.SS_STRINGS = new Proxy(table, {
    getOwnPropertyDescriptor(t, k) {
      const d = Object.getOwnPropertyDescriptor(t, k);
      if (!d) misses.add(String(k));
      return d;
    },
  });
  const root = env.window.document.createElement('div');
  env.SS.profile.render(root, doc);
  return { root, env, misses: Array.from(misses).sort() };
}

test('renders_e2_sample_export_without_fallbacks', () => {
  const { root, env, misses } = renderTracked(SAMPLE);
  assert.deepStrictEqual(env.errors, []);
  assert.deepStrictEqual(misses, [], 'string keys the renderer fell back on');
  // every cell exactly once
  for (const r of SAMPLE.rows) for (const c of r.cells) cellNode(root, cellKey(r.row, c));
  assert.strictEqual(ownCells(root).length, 147);
  // no raw code anywhere: no mono code fallback, and no code the file carries printed as text
  assert.deepStrictEqual(find(root, (n) => /\bpf-code\b/.test(attr(n, 'class') || '')).map((h) => h.node.textContent), []);
  const text = textOf(root);
  const codes = new Set();
  for (const r of SAMPLE.rows) {
    for (const c of r.cells) {
      if (c.reason_code) codes.add(c.reason_code);
      if (c.formula_code) codes.add(c.formula_code);
      (c.needs || []).forEach((n) => codes.add(n));
      (c.missing || []).forEach((m) => codes.add(m));
      (c.qualifiers || []).forEach((q) => codes.add(q));
    }
    (r.qualifiers || []).forEach((q) => codes.add(q));
    assert.ok(!text.includes(r.label_key), r.label_key);
  }
  assert.ok(codes.size > 10, 'the sample carries codes');
  // identifier-like codes never reach the text (one-word codes such as "descriptive" are plain English inside
  // the sentences; the pf-code and misses checks above already prove they were looked up)
  for (const code of codes) {
    if (code === 'J2') continue;        // also a rests_on code, which the panel prints by design (V3); see below
    if (/[_:]/.test(code)) assert.ok(!text.includes(code), 'raw code shown: ' + code);
  }
  // the coded sentences are the kit's: whole formula codes ("yield_per_box:ai") and reason codes
  assert.ok(text.includes(KIT['profile.formula.yield_per_box:ai']));
  assert.ok(text.includes(KIT['profile.formula.yield_per_box:no_interview']));
  assert.ok(text.includes(KIT['profile.reason.group_empty']));
  // needs codes read as words in the not-run line, never as the code ("J2", "sidejudge", "judge_script")
  for (const r of SAMPLE.rows) {
    for (const c of r.cells.filter((x) => (x.needs || []).length)) {
      const v = part(cellNode(root, cellKey(r.row, c)).node, 'value');
      for (const n of c.needs) assert.ok(!new RegExp('\\b' + n + '\\b').test(v), n + ' raw in ' + v);
    }
  }
  const notRun = SAMPLE.rows.flatMap((r) => r.cells.filter((c) => c.state === 'not_run').map((c) => cellKey(r.row, c)));
  assert.strictEqual(notRun.length, 14);
  for (const key of notRun) assert.match(part(cellNode(root, key).node, 'value'), /not run in this profile/);
});

test('cell_note_codes_render_as_kit_sentences', () => {
  // the kit writes note codes on computed cells: one code, "code:count", or several joined with ";"
  const doc = clone(SAMPLE);
  const cs = rowOf(doc, 8).cells.filter((c) => c.state !== 'not_run');
  cs[0].note = 'ci_undefined;holm_family_lt_2';
  cs[1].note = 'leak_split_indeterminate:3';
  const { root, env, misses } = renderTracked(doc);
  assert.deepStrictEqual(env.errors, []);
  assert.deepStrictEqual(misses, []);
  const t0 = textOf(cellNode(root, cellKey(8, cs[0])).node);
  assert.ok(t0.includes(KIT['profile.reason.ci_undefined']) && t0.includes(KIT['profile.reason.holm_family_lt_2']), t0);
  assert.ok(textOf(cellNode(root, cellKey(8, cs[1])).node).includes(KIT['profile.reason.leak_split_indeterminate']));
  // pooled_only stays a qualifier phrase, not a note sentence (the study and parity files are unchanged)
  const pooled = rowOf(doc, 'lead').cells.find((c) => c.note === 'pooled_only');
  assert.ok(!byAttr(cellNode(root, cellKey('lead', pooled)).node, 'data-part', 'note').length);
});

test('dev_page_inlines_current_fixtures_and_strings', () => {
  const html = fs.readFileSync(path.join(ROOT, 'second_scribe', 'app', 'static', 'dev', 'profile.html'), 'utf8');
  const grab = (v) => {
    const m = html.match(new RegExp('window\\.' + v + ' = (\\{.*?\\});\\n'));
    assert.ok(m, v);
    return JSON.parse(m[1].replace(/<\\\//g, '</'));
  };
  assert.deepStrictEqual(grab('SS_PROFILE_V3'), V3);
  assert.deepStrictEqual(grab('SS_PROFILE_V2'), V2);
  assert.deepStrictEqual(grab('SS_PROFILE_SYNTH'), SYNTH);
  assert.deepStrictEqual(grab('SS_PROFILE_SAMPLE'), SAMPLE);
  const strings = grab('SS_STRINGS');
  const merged = Object.assign({}, EN, KIT);
  for (const [k, v] of Object.entries(strings)) assert.strictEqual(v, merged[k], k);
  for (const k of Object.keys(merged)) {
    if (/^(profile|common|band|lane)\./.test(k)) assert.ok(k in strings, 'dev page lacks ' + k);
  }
});

// Writes static/dev/profile.html from the fixtures and en.json (dev only, never shipped):
//   SS_WRITE_PROFILE_DEV_PAGE=1 node --test tests/js/profile.test.js
// The test above fails whenever the page drifts from the fixtures or the strings.
function devPage() {
  const js = (v, o) => 'window.' + v + ' = ' + JSON.stringify(o).replace(/<\//g, '<\\/').replace(/<!--/g, '\\u003c!--') + ';\n';
  const strings = {};
  for (const [k, v] of Object.entries(Object.assign({}, EN, KIT))) {
    if (/^(profile|common|band|lane)\./.test(k)) strings[k] = v;
  }
  return `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Profile dev page</title>
<link rel="stylesheet" href="../css/fonts.css">
<link rel="stylesheet" href="../css/tokens.css">
<link rel="stylesheet" href="../css/base.css">
<link rel="stylesheet" href="../css/profile.css">
<style>
  /* dev page layout only */
  main { max-width: 1180px; margin: 0 auto; padding: 24px 16px; }
</style>
<script>
// Generated by tests/js/profile.test.js (SS_WRITE_PROFILE_DEV_PAGE=1). Do not edit by hand.
${js('SS_STRINGS', strings)}${js('SS_PROFILE_V3', V3)}${js('SS_PROFILE_V2', V2)}${js('SS_PROFILE_SYNTH', SYNTH)}${js('SS_PROFILE_SAMPLE', SAMPLE)}</script>
<script>
  // default: the study's public file (v3); #doc=v2 for its older per-phase form, #doc=synth for the parity file,
  // #doc=sample for the kit exporter's sample export; #theme=dark or #theme=light
  if (/(^|[#&])theme=dark($|&)/.test(location.hash)) document.documentElement.setAttribute("data-theme", "dark");
  else if (/(^|[#&])theme=light($|&)/.test(location.hash)) document.documentElement.setAttribute("data-theme", "light");
</script>
</head>
<body>
<main>
  <h1>Profile dev page</h1>
  <p class="muted">Developer page for the profile panel. Fixtures from tests/fixtures/profiles (#doc=v2, #doc=synth, #doc=sample). Not shipped.</p>
  <div id="profile"></div>
</main>
<script src="../js/core.js"></script>
<script src="../js/profile.js"></script>
<script>
  (function () {
    "use strict";
    // #w=390: headless Chrome will not open a window narrower than 500px, so the page shows itself in an
    // iframe that wide (a real 390px viewport, media queries included). Inside, a horizontal overflow is flagged.
    var w = /(?:^|[#&])w=(\\d+)/.exec(location.hash);
    if (w && window.top === window) {
      document.body.textContent = "";
      document.body.style.margin = "0";
      var f = document.createElement("iframe");
      f.src = location.pathname + location.hash.replace(/(^#|&)w=\\d+/, "$1");
      f.setAttribute("title", "profile at " + w[1] + "px");
      f.style.cssText = "border:0;display:block;height:100vh;width:" + w[1] + "px";
      document.body.appendChild(f);
      return;
    }
    var synth = /(^|[#&])doc=synth($|&)/.test(location.hash);
    var sample = /(^|[#&])doc=sample($|&)/.test(location.hash);
    var v2 = /(^|[#&])doc=v2($|&)/.test(location.hash);
    SS.profile.render(document.getElementById("profile"),
      sample ? window.SS_PROFILE_SAMPLE : synth ? window.SS_PROFILE_SYNTH : v2 ? window.SS_PROFILE_V2 : window.SS_PROFILE_V3);
    var de = document.documentElement;
    var flag = document.createElement("p");
    flag.setAttribute("id", "overflow-check");
    flag.textContent = de.scrollWidth > de.clientWidth ?
      "OVERFLOW: page " + de.scrollWidth + "px wide in a " + de.clientWidth + "px viewport" :
      "no horizontal overflow at " + de.clientWidth + "px";
    document.querySelector("main").insertBefore(flag, document.querySelector("main").children[1]);
  })();
</script>
</body>
</html>
`;
}
if (process.env.SS_WRITE_PROFILE_DEV_PAGE === '1') {
  fs.writeFileSync(path.join(ROOT, 'second_scribe', 'app', 'static', 'dev', 'profile.html'), devPage());
}


// ------------------------------------------------------------------------------------------- audit fix C
test('V1_not_a_test_only_on_the_four_lead_lines', () => {
  const { root } = render(V2);
  const T = 'with no statistical test';
  const hits = find(root, (n) => n.textContent && n.textContent.indexOf(T) >= 0 && !(n.children || []).length);
  const four = V2.rows.reduce((k, r) => k + r.cells.filter((c) => c.derived === true).length, 0);
  assert.strictEqual(four, 4);
  assert.strictEqual(hits.length, four, 'one "not a test" line per derived: true cell');
  for (const h of hits) assert.ok(h.parents.some((p) => attr(p, 'data-group') === 'lead-derived'), 'beside the lead');
  // a string-derived cell keeps the plain qualifier chip, not the test line
  const band = textOf(cellNode(root, 'ledger|attested_absent_from_map|primary').node);
  assert.ok(band.includes('arithmetic on reported numbers'));
  assert.ok(!band.includes(T));
  for (const key of ['2|ai_per_map|', '5|unanchored|primary', '9|rejudge_changed|lived_steps']) {
    assert.ok(!textOf(cellNode(root, key).node).includes(T), key);
  }
});

test('V2_upper_bound_with_interval_reads_one_sided_from_ci_level', () => {
  const { root } = render(V2);
  const t = textOf(cellNode(root, '6|largest_excess_ruled_out|other_family_script').node);
  assert.ok(t.includes('one-sided 95% (upper end of the 90% interval [−1.7, 2.6] points)'), t);
  // a bound is an end, so it carries no plus sign (2.6 part 5c: in points, as the difference it bounds)
  assert.strictEqual(part(cellNode(root, '6|largest_excess_ruled_out|other_family_script').node, 'value'), '2.6 points');
  assert.ok(!/90% one-sided/.test(textOf(root)), 'no "90% one-sided" anywhere');
  // computed from the file's level, never typed: a 95% interval gives a one-sided 97.5% bound
  const odd = clone(V2);
  rowOf(odd, 6).cells.find((c) => c.name === 'largest_excess_ruled_out').ci_level = 0.95;
  const t2 = textOf(cellNode(render(odd).root, '6|largest_excess_ruled_out|other_family_script').node);
  assert.ok(t2.includes('one-sided 97.5% (upper end of the 95% interval'), t2);
  // without an interval the level is the one-sided level itself
  const t3 = textOf(cellNode(render(V2).root, '5|counterfactual_upper_bound|all_ideals').node);
  assert.match(t3, /95% one-sided bound/);
});

test('V3_numbers_keep_the_article_digits', () => {
  const { root } = render(V2);
  // 2.6 part 5c: shares in percentages and differences between shares in points, one decimal
  assert.strictEqual(part(cellNode(root, '7|listening_added_attested_line|primary').node, 'value'), '+15.0 points');
  assert.strictEqual(part(cellNode(root, '7|listening_added_attested_line|primary').node, 'ci'), '[11.3, 18.4] points');
  const rec = cellNode(root, '8|size_adjusted_recall|rung2').node;
  assert.strictEqual(part(rec, 'value'), '54.0%');
  assert.strictEqual(part(cellNode(root, '8|ai_per_map|rung1').node, 'value'), '19.000');   // 2.2 issue 3: one precision per panel
  assert.strictEqual(part(cellNode(root, '8|leak_rate|placebo').node, 'value'), '10.0%');
  assert.strictEqual(part(cellNode(root, '8|leak_rate|placebo').node, 'ci'), '[2.5%, 18.9%]');
  assert.strictEqual(part(cellNode(root, 'lead|no_interview_edges_per_map|').node, 'value'), '15.488');
  // never a number the file does not carry: a whole-number rank or count stays whole
  assert.strictEqual(part(cellNode(root, 'lead|rank_null_expectation|primary').node, 'value'), '22');
  assert.strictEqual(part(cellNode(root, 'lead|real_recall|primary').node, 'value'), '8.7%');
  // a difference between counts per map keeps its digits; ratios, ranks and indexes too
  assert.strictEqual(part(cellNode(root, 'lead|ai_minus_no_interview_nodes_per_map|').node, 'value'), '+14.023');
  assert.strictEqual(part(cellNode(root, '8|ai_nodes_remedy_minus_placebo|remedy-placebo').node, 'value'), '+0.698');   // council 30: leading zero
  assert.strictEqual(part(cellNode(root, '2|ai_per_endorsed|').node, 'value'), '60.8%');   // Part 6: a share, re-pinned on purpose
  assert.strictEqual(part(cellNode(root, '4|direct_over_null|primary').node, 'value'), '7.897');
  assert.strictEqual(part(cellNode(root, '9|canonical_ari|canonical_names').node, 'value'), '0.977');   // council 30: leading zero
  assert.strictEqual(part(cellNode(root, 'lead|own_map_mean_rank|primary').node, 'value'), '1.093');
  // R* is a recall ceiling, a share of steps
  assert.strictEqual(part(cellNode(root, '1|r_star|').node, 'value'), '54.8%');
});

test('issue3_per_map_count_point_and_interval_print_three_decimals', () => {
  const { root } = render(V2);
  // 2.2 issue 3: the JSON drops trailing zeros ("20.86", "24.07", "19"), so the page restores them: a per-map count
  // prints like its neighbours (29.488), point and both interval ends, never "19.0 [17.209, 20.86]"
  assert.strictEqual(part(cellNode(root, '8|ai_per_map|rung1').node, 'ci'), '[17.209, 20.860]');
  assert.strictEqual(part(cellNode(root, '8|ai_per_map|placebo').node, 'ci'), '[24.070, 28.953]');
  assert.strictEqual(part(cellNode(root, '8|ai_per_map|remedy').node, 'value'), '27.140');
  const odd = clone(V2);
  const c = rowOf(odd, 8).cells.find((x) => x.name === 'ai_per_map' && x.x === 'rung1');
  c.ci_hi = 21;
  assert.strictEqual(part(cellNode(render(odd).root, '8|ai_per_map|rung1').node, 'ci'), '[17.209, 21.000]');
  c.ci_lo = 17;
  c.ci_hi = 21.5;
  assert.strictEqual(part(cellNode(render(odd).root, '8|ai_per_map|rung1').node, 'ci'), '[17.000, 21.500]');
  // a count out of a universe prints whole, interval ends too; a mean keeps its three decimals
  assert.strictEqual(part(cellNode(root, '9|attested_absent_from_map|judge').node, 'ci'), '[250, 354]');
  assert.strictEqual(part(cellNode(root, '9|attested_absent_from_map|judge').node, 'value'), '304');
  assert.strictEqual(part(cellNode(root, 'lead|real_recall|primary').node, 'value'), '8.7%');
  assert.strictEqual(part(cellNode(root, '7|listening_added_attested_line|primary').node, 'ci'), '[11.3, 18.4] points');
});

test('issue3_formatting_only_the_document_and_its_numbers_are_untouched', () => {
  const before = JSON.stringify(V2);
  const { root } = render(V2);
  assert.strictEqual(JSON.stringify(V2), before, 'rendering never edits the profile');
  // the file keeps its digits; only the printing changes
  const c = rowOf(V2, 8).cells.find((x) => x.name === 'ai_per_map' && x.x === 'rung1');
  assert.strictEqual(c.value, 19);
  assert.strictEqual(c.ci_hi, 20.86);
  const k = rowOf(V2, 'ledger').cells.find((x) => x.name === 'attested_absent_from_map' && x.ci_lo !== undefined);
  assert.strictEqual(k.ci_lo, 250.304);
  assert.ok(textOf(root).length > 0);
});

test('issue3_one_band_prints_one_way_in_the_row_header_and_on_its_cell', () => {
  // Try it's slant row: the header band had no kind and printed "−.01–.003", the cell band "−.010–.003"; 2.6 part 5c:
  // both print the pull's percentage points, "−1.0–0.3 points"
  const doc = clone(V2);
  const row = rowOf(doc, 6);
  row.band = { lo: -0.01, hi: 0.003, over: row.band.over, narrow: row.band.narrow };
  row.cells.forEach((x) => { if (x.band) x.band = { lo: -0.01, hi: 0.003, over: x.band.over, narrow: x.band.narrow }; });
  const { root } = render(doc);
  const section = find(root, (n) => attr(n, 'data-row') === '6')[0].node;
  const bands = find(section, (n) => attr(n, 'data-part') === 'band').map((b) => textOf(b.node));
  assert.ok(bands.length >= 2, 'the row header band and at least one cell band');
  bands.forEach((t) => {
    assert.ok(t.includes('−1.0–0.3 points'), t);
    assert.ok(!/−\.01(?!\d)/.test(t), t);
  });
  // the members of a row band follow the same rule ("lexical only: 7.3%–20.0%", not ".073–.2")
  assert.ok(textOf(render(V2).root).includes('lexical only: 7.3%–20.0%'));
  // the lead's and row 7's band is listening_added at primary and lenient: points, in the header too
  const v2 = render(V2).root;
  for (const r of ['lead', '7']) {
    const sec = find(v2, (n) => attr(n, 'data-row') === r)[0].node;
    const head = find(sec, (n) => n.tagName === 'HEADER')[0].node;
    assert.ok(textOf(head).includes('band 22.5–24.1 points'), r + ': ' + textOf(head));
  }
});

test('V4_mde80_unsigned', () => {
  const { root } = render(V2);
  const m = cellNode(root, '8|mde80_leak_contrast|remedy-placebo').node;
  assert.strictEqual(part(m, 'value'), '7.4 points');
  const other = cellNode(root, '8|lived_recall_remedy_minus_placebo|remedy-placebo').node;
  assert.strictEqual(part(other, 'value'), '+1.2 points');
});

test('V7_formula_comes_from_strings_not_the_file', () => {
  const env = load({ files: ['core.js', 'profile.js'], strings: KIT });
  const root = env.window.document.createElement('div');
  env.SS.profile.render(root, V2);
  const lead = rowOf(V2, 'lead');
  for (const c of lead.cells.filter((x) => x.derived === true)) {
    const t = textOf(cellNode(root, 'lead|' + c.name + '|' + c.x).node);
    assert.ok(!t.includes(c.formula), 'raw file formula not printed for ' + c.name + ' ' + c.x);
    assert.ok(!/matched_endorsed_steps|boxes_drawn|equal_size_bound\b/.test(t) || c.name === 'equal_size_bound', t);
    const code = KIT['profile.formula.' + c.name + ':' + c.x] ? c.name + ':' + c.x : c.name;
    assert.ok(t.includes(KIT['profile.formula.' + code]), 'kit sentence for ' + code);
  }
  assert.ok(!textOf(root).includes('matched_endorsed_steps'));
  // no sentence for the name: the file's own formula is the fallback
  const odd = clone(V2);
  const y = rowOf(odd, 'lead').cells.find((c) => c.name === 'yield_per_box' && c.x === 'ai');
  y.name = 'brand_new_ratio';
  const t = textOf(cellNode(render(odd).root, 'lead|brand_new_ratio|ai').node);
  assert.ok(t.includes(y.formula));
});

test('V8_sizes_beside_the_lead_number_wherever_it_appears', () => {
  const { root } = render(V2);
  const row7 = byAttr(root, 'data-row', '7')[0].node;
  const cell = cellNode(root, '7|listening_added|primary');
  const group = cell.parents[cell.parents.length - 1];  // the cells wrapper of row 7
  const beside = find(row7, (n) => attr(n, 'data-group') === 'beside-sizes');
  assert.strictEqual(beside.length, 1);
  const t = textOf(beside[0].node);
  assert.ok(t.includes('29.488') && t.includes('15.465'), t);
  assert.ok(group, 'row 7 wrapper');
  // the lenient number (.225) is not +.241 and carries no sizes of its own
  assert.strictEqual(find(row7, (n) => attr(n, 'data-group') === 'beside-sizes').length, 1);
});

// audit fix C follow-up: the kit's formula sentences for the derived lines already end with
// "Arithmetic on reported numbers; not a test."; the app's own derived line is then not printed a second time.
test('V7_not_a_test_is_said_exactly_once_per_lead_line', () => {
  const count = (t) => (t.match(/with no statistical test/gi) || []).length;
  for (const [label, strings] of [['kit strings merged', KIT], ['no kit strings', undefined]]) {
    const env = load({ files: ['core.js', 'profile.js'], strings });
    const root = env.window.document.createElement('div');
    env.SS.profile.render(root, V2);
    const derived = rowOf(V2, 'lead').cells.filter((c) => c.derived === true);
    assert.strictEqual(derived.length, 4);
    for (const c of derived) {
      const t = textOf(cellNode(root, 'lead|' + c.name + '|' + c.x).node);
      assert.strictEqual(count(t), 1, label + ': ' + c.name + ' ' + c.x + ' says "not a test" once: ' + t);
    }
  }
  // with the kit's sentences, the two that end with the phrase drop the app's line; the two that do not keep it (the
  // equal-size sentences end with the box-for-box pointer since the 22 Oct wording pass, so both keep the app's line)
  const env = load({ files: ['core.js', 'profile.js'], strings: KIT });
  const root = env.window.document.createElement('div');
  env.SS.profile.render(root, V2);
  const has = (name, x) => byAttr(cellNode(root, 'lead|' + name + '|' + x).node, 'data-part', 'derived').length;
  assert.strictEqual(has('yield_per_box', 'ai'), 0);
  assert.strictEqual(has('yield_per_box', 'no_interview'), 0);
  assert.strictEqual(has('equal_size_bound', 'no_interview'), 1);
  assert.strictEqual(has('listening_edge_equal_size', 'no_interview'), 1);
});

// audit fix D: a cloud run's profile says, in its run footnote, which roles went to which provider under a declaration
// (run.remote_endpoints, PROFILE-SCHEMA 14.8: {role, provider, declaration_sha256}); role and provider names come from
// the kit's profile.role.* / profile.provider.* strings; no host is ever printed, even if a file carried one.
test('D_remote_endpoints_footnote_one_line_per_entry', () => {
  const doc = clone(V2);
  doc.run = Object.assign({}, doc.run, { remote_endpoints: [
    { role: 'judge', provider: 'openai_compat', declaration_sha256: 'a'.repeat(64), host: 'llm.example.invalid' },
    { role: 'recon', provider: 'anthropic', declaration_sha256: 'b'.repeat(64) },
    { role: 'judge2', provider: 'mistral', declaration_sha256: 'c'.repeat(64) }] });
  const env = load({ files: ['core.js', 'profile.js'], strings: KIT });
  const root = env.window.document.createElement('div');
  env.SS.profile.render(root, doc);
  const foot = byAttr(root, 'data-part', 'run-footnote');
  assert.strictEqual(foot.length, 1);
  const lines = byAttr(foot[0].node, 'data-remote-role').map((h) => h.node.textContent);
  assert.deepStrictEqual(lines, [
    'Judge: an OpenAI-compatible cloud endpoint, declaration on file',
    "Reconstructor (also draws the no-interview map): Anthropic's API, declaration on file",
    'Second judge family: mistral, declaration on file']);
  const all = textOf(root);
  assert.ok(!all.includes('example.invalid'), 'no host');
  assert.ok(!all.includes('a'.repeat(64)), 'no hash either');
  // no remote endpoint (every local run, the study file in its older form): no footnote at all
  for (const d of [V2, Object.assign(clone(V2), { run: Object.assign({}, V2.run, { remote_endpoints: [] }) }),
    Object.assign(clone(V2), { run: Object.assign({}, V2.run, { remote_endpoints: null }) })]) {
    const r = env.window.document.createElement('div');
    env.SS.profile.render(r, d);
    assert.strictEqual(byAttr(r, 'data-part', 'run-footnote').length, 0);
  }
});

// V3 switch: the run footnote of a file whose run block is in the public form (PROFILE-SCHEMA section 14): no run chain
// heads in the file, the code in run_chain_heads_source says why, and the sentence for that code comes from strings.
test('V3_run_footnote_says_chain_heads_are_available_from_the_authors', () => {
  assert.strictEqual(V3.run.run_chain_heads, null);
  const env = load({ files: ['core.js', 'profile.js'], strings: KIT });
  const root = env.window.document.createElement('div');
  env.SS.profile.render(root, V3);
  assert.deepStrictEqual(env.errors, []);
  const foot = byAttr(root, 'data-part', 'run-footnote');
  assert.strictEqual(foot.length, 1);
  const lines = byAttr(foot[0].node, 'data-key');
  assert.strictEqual(lines.length, 1);
  assert.strictEqual(attr(lines[0].node, 'data-key'), 'run.run_chain_heads_source');
  assert.strictEqual(textOf(foot[0].node), 'Run chain heads: available from the authors');
  assert.ok(!/undefined|\bnull\b|\[object Object\]/.test(textOf(foot[0].node)));
  // a lab export that withheld its heads says the same; one that holds heads, or never said why, prints no line
  const lab = clone(SAMPLE);
  lab.run.run_chain_heads = null;
  lab.run.run_chain_heads_source = 'available_from_authors';
  const r2 = env.window.document.createElement('div');
  env.SS.profile.render(r2, lab);
  assert.strictEqual(textOf(byAttr(r2, 'data-part', 'run-footnote')[0].node), 'Run chain heads: available from the authors');
  for (const mut of [(d) => { delete d.run.run_chain_heads_source; }, (d) => { d.run.run_chain_heads_source = 'free text, not a code'; },
    (d) => { d.run.run_chain_heads_source = 'unknown_code'; }, (d) => { d.run.run_chain_heads = { head: 'x' }; }]) {
    const d = clone(V3);
    mut(d);
    const r = env.window.document.createElement('div');
    env.SS.profile.render(r, d);
    assert.strictEqual(byAttr(r, 'data-part', 'run-footnote').length, 0, String(mut));
  }
  assert.deepStrictEqual(env.errors, []);
  // the short form is the app's; a code with no short form falls back to the kit's sentence (profile.reason.<code>)
  assert.ok(KIT['profile.reason.available_from_authors'] && KIT['profile.reason.not_recorded_public'], 'kit sentences exist');
  const env2 = load({ files: ['core.js', 'profile.js'], strings: KIT });
  const d4 = clone(V3);
  d4.run.run_chain_heads_source = 'cell_under_10';
  const r4 = env2.window.document.createElement('div');
  env2.SS.profile.render(r4, d4);
  assert.strictEqual(textOf(byAttr(r4, 'data-part', 'run-footnote')[0].node), 'Run chain heads: ' + KIT['profile.reason.cell_under_10']);
  assert.strictEqual(env2.SS.profile.runReason('not_recorded_public'), 'not recorded in public files');
  assert.strictEqual(env2.SS.profile.runReason('Not Recorded'), null);
});

test('V3_renders_the_public_file_with_every_cell_of_the_older_one', () => {
  const { root } = render(V3);
  const want = [];
  V2.rows.forEach((r) => r.cells.forEach((c) => want.push(cellKey(r.row, c))));
  assert.deepStrictEqual(ownCells(root).slice().sort(), want.slice().sort());
});

// 2.2 follow-up: the Study and Try-it panels name the remedy "rung 3", as the viewer's selector does.
test('R1 rung names: the profile labels carry the rung number, like the viewer', () => {
  assert.strictEqual(EN['profile.x.rung1'], "rung 1, the authors' original draft");   // Part 5a item 19 names
  assert.strictEqual(EN['profile.x.rung2'], "rung 2, the study's instructions");
  assert.strictEqual(EN['profile.x.remedy'], 'rung 3, remedy prompt');
  assert.strictEqual(EN['profile.x.placebo'], 'placebo prompt');
  assert.strictEqual(EN['profile.x.rung2_rung1'], 'rung 2 minus rung 1');
  assert.strictEqual(EN['profile.x.remedy_placebo'], 'rung 3 minus placebo');
  // no profile, strip or instruction text names the remedy without its rung number as a label or contrast
  Object.keys(EN).filter((k) => /^(profile|strip)\./.test(k)).forEach((k) => {
    assert.ok(!/(^|[^3] )remedy (prompt|minus)/.test(EN[k]) || /rung 3/.test(EN[k]), k + ': ' + EN[k]);
  });
  assert.ok(EN['instructions.rung3.null_note'].indexOf('rung 3 minus placebo') >= 0);
  assert.ok(EN['instructions.rung3.null_note'].indexOf('remedy minus placebo') < 0);
});
