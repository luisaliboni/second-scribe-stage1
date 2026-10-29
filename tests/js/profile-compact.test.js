'use strict';
// SS.profile.render(root, doc, {compact: true}): the Try it page opens with the lead block only; the rest of the
// profile (the lead's floors and settings, the ledger, rows 1 to 9) sits in one closed <details>.
// Without the option the output must stay as it was: pinned by a digest of the whole rendered tree, taken on the
// code before the option existed (T30's study page renders with no option).
const test = require('node:test');
const assert = require('node:assert');
const crypto = require('crypto');
const fs = require('fs');
const path = require('path');
const { load, ROOT } = require('./harness');

const FIX = path.join(ROOT, 'tests', 'fixtures', 'profiles');
const FILES = ['profile-v2', 'profile-synth', 'e2-m1-sample-demo'];
const DOCS = {};
FILES.forEach((f) => { DOCS[f] = JSON.parse(fs.readFileSync(path.join(FIX, f + '.json'), 'utf8')); });
const EN = JSON.parse(fs.readFileSync(path.join(ROOT, 'second_scribe', 'app', 'strings', 'en.json'), 'utf8'));
const clone = (o) => JSON.parse(JSON.stringify(o));

// sha256 of JSON.stringify(ser(root)) after SS.profile.render(root, doc) with no options, taken on the code before the
// compact option existed (da9eac4). They pin every tag, attribute and text of the plain render.
// Re-pinned after audit fix C (V1 derived line, V2 bound text, V3 digits, V4 unsigned MDE, V7 formula, V8 row-7 sizes).
// Submission 2.0 re-pins explanatory copy; rendering logic and numeric fixtures are unchanged.
// Submission 2.2 (issue 3) re-pins number printing only: counts whole, per-map counts and row bands at three decimals.
// Submission 2.2 follow-up re-pins two labels only: "rung 3, remedy prompt" and "rung 3 minus placebo" (checked: the
// old digests are reproduced with the old two strings, and the trees differ in those texts alone).
// Submission 2.6 (council 23 fix 4) re-pins profile-v2 only: its reading-3 cells now hold the temperature-0 values of
// the v4 profile (checked: the old digest is reproduced with the old fixture; the two trees differ only in the numbers and
// formulas of the replaced cells and in the data-source attribute of those cells).
// re-pinned 22 Oct: reading-3 T 0 wording (WORDING-PLACES). All three digests: strings only (checked: with the
// pre-wording en.json and profile_strings.json swapped in, the previous digests are reproduced exactly).
// re-pinned 23 Oct: profile.rests_on.p wording (docs re-record, F4). All three digests: strings only (checked: with the
// previous en.json swapped in, the previous digests are reproduced exactly).
// re-pinned 23 Oct (Part 5a): rung names "rung 1, the authors' original draft" / "rung 2, the study's instructions"
// (item 19). All three digests: strings only (checked: with the pre-Part-5a en.json passed as strings, the previous
// digests are reproduced exactly).
// re-pinned 23 Oct (Part 5c): shares and rates print as percentages, differences between shares in percentage points
// (profile.js fmtKind). All three digests: number texts only (checked: rendering each fixture with the pre-Part-5c
// profile.js and with this one gives trees with the same tags, attributes and children, differing only in text
// nodes: 264, 197 and 235 of them, every one a share, rate, bound or difference reprinted as a percentage or points).
// The digests include the strings profile.points and profile.ci_points (app-work/PART5C-PROFILE-STRINGS.json).
// re-pinned 23 Oct (Part 6, the author's Part 5c leftover): the AI's boxes per endorsed box (ai_per_endorsed) and the
// no-interview boxes over the AI's boxes (no_interview_over_ai_nodes) print as percentages. All three digests: those
// value texts only (checked: rendering each fixture with the pre-Part-6 profile.js and with this one gives trees with
// the same tags, attributes and children, differing in 3, 1 and 3 text nodes, every one of those two cells).
// re-pinned 23 Oct (Part 7, council 30): the "X, not Y" rewrites, "endorsed steps" for the recall denominator, "lexical
// only, N0% overlap", and the leading zero on three-decimal values below 1 (ratio, mean, index, per-map and count
// differences). All three digests: text nodes only (checked: rendering each fixture with the before-part7 profile.js and
// en.json reproduces the previous digests exactly; against this one the trees have the same tags, attributes and
// children, differing in 95, 89 and 92 text nodes, every one of those strings or numbers).
const SNAPSHOT = {
  'profile-v2': 'c1ba71c0301ec2e0047355305ffc4c043e13536701c49b1bef1ccb899f769b3a',
  'profile-synth': '882207bf9f55e6e8c10a51d4b5d2e8567593a1e131a219b4b0d6ee954d8ec329',
  'e2-m1-sample-demo': '85dd807912464a9727a0cbf6bf52ac99aa243e0d3681fe140baa09bca2439c05',
};

function ser(n) {
  return { t: n.tagName, a: n.attributes || {}, x: n.textContent || '', c: (n.children || []).map(ser) };
}
function digest(root) { return crypto.createHash('sha256').update(JSON.stringify(ser(root))).digest('hex'); }
function walk(node, fn, parents) {
  parents = parents || [];
  fn(node, parents);
  (node.children || []).forEach((c) => walk(c, fn, parents.concat([node])));
}
function attr(n, k) { return n.attributes ? n.attributes[k] : undefined; }
function find(root, pred) { const out = []; walk(root, (n, p) => { if (pred(n)) out.push({ node: n, parents: p }); }); return out; }
function textOf(node) { const out = []; walk(node, (n) => { if (n.textContent) out.push(n.textContent); }); return out.join(' '); }

function render(doc, opts) {
  const env = load({ files: ['core.js', 'profile.js'] });
  const root = env.window.document.createElement('div');
  env.SS.profile.render(root, doc, opts);
  return { root, env };
}
// the data-cell keys under a node, with their multiplicity
function cellKeys(node) {
  const keys = [];
  walk(node, (n) => { if (attr(n, 'data-cell') !== undefined) keys.push(attr(n, 'data-cell')); });
  return keys;
}
function fullDetails(root) { return find(root, (n) => n.tagName === 'DETAILS' && /(^| )pf-full( |$)/.test(attr(n, 'class') || '')); }

// ------------------------------------------------------------------------------------------- unchanged without it
for (const f of FILES) {
  test('compact_off_output_is_unchanged: ' + f, () => {
    const plain = render(clone(DOCS[f]));
    assert.deepStrictEqual(plain.env.errors, []);
    assert.strictEqual(digest(plain.root), SNAPSHOT[f]);
    assert.strictEqual(digest(render(clone(DOCS[f]), {}).root), SNAPSHOT[f]);
    assert.strictEqual(digest(render(clone(DOCS[f]), { compact: false }).root), SNAPSHOT[f]);
    assert.strictEqual(fullDetails(plain.root).length, 0);
  });
}

// ------------------------------------------------------------------------------------------- compact
for (const f of FILES) {
  test('compact_shows_the_lead_only_and_the_rest_in_a_closed_details: ' + f, () => {
    const doc = clone(DOCS[f]);
    const { root, env } = render(doc, { compact: true });
    assert.deepStrictEqual(env.errors, []);

    const hits = fullDetails(root);
    assert.strictEqual(hits.length, 1, 'one details for the rest of the profile');
    const det = hits[0].node;
    assert.strictEqual(attr(det, 'open'), undefined, 'closed');
    const summary = det.children.filter((c) => c.tagName === 'SUMMARY');
    assert.strictEqual(summary.length, 1);
    assert.strictEqual(det.children[0], summary[0], 'the summary comes first');
    assert.strictEqual(summary[0].textContent, EN['try.profile.full']);
    assert.strictEqual(EN['try.profile.full'], 'Show the full profile');

    // outside the details: the lead section, its number (headline, AI, no interview) and its sizes. Nothing else.
    const outside = [];
    walk(root, (n, parents) => {
      if (attr(n, 'data-cell') !== undefined && parents.indexOf(det) < 0) outside.push(n);
    });
    const outKeys = outside.map((n) => attr(n, 'data-cell'));
    assert.ok(outKeys.length > 0);
    for (const k of ['lead|listening_added|primary', 'lead|ai_recall|primary', 'lead|no_interview_recall|primary']) {
      assert.strictEqual(outKeys.filter((x) => x === k).length, 1, k + ' is shown, once, outside the details');
    }
    const groupsOutside = find(root, (n) => attr(n, 'data-group') !== undefined && find(det, (m) => m === n).length === 0)
      .map((h) => attr(h.node, 'data-group')).sort();
    for (const good of ['lead', 'lead-number', 'lead-sizes', 'lead-derived']) {
      assert.ok(groupsOutside.indexOf(good) >= 0, good + ' is shown outside the details');
    }
    assert.deepStrictEqual(groupsOutside, ['lead', 'lead-derived', 'lead-number', 'lead-sizes']);
    for (const bad of ['lead-floors', 'lead-settings']) assert.strictEqual(groupsOutside.indexOf(bad), -1, bad);
    const rowsOutside = find(root, (n) => attr(n, 'data-row') !== undefined && find(det, (m) => m === n).length === 0)
      .map((h) => attr(h.node, 'data-row'));
    assert.deepStrictEqual(rowsOutside, ['lead']);
    // every size cell of the file (size_beside_lead) and every derived line is outside, none inside
    const lead = doc.rows.find((r) => r.row === 'lead');
    const sizeKeys = lead.cells.filter((c) => (c.qualifiers || []).indexOf('size_beside_lead') >= 0 && c.derived === undefined)
      .map((c) => 'lead|' + c.name + '|' + (c.x == null ? '' : c.x));
    assert.ok(sizeKeys.length > 0);
    sizeKeys.forEach((k) => assert.strictEqual(outKeys.filter((x) => x === k).length, 1, k));

    // inside: the rest, each cell once; outside + inside = every cell of the plain render, each once
    const inKeys = cellKeys(det);
    assert.ok(inKeys.length > 0);
    assert.strictEqual(inKeys.some((k) => outKeys.indexOf(k) >= 0 && k.indexOf('lead|') !== 0), false);
    assert.ok(inKeys.some((k) => k.indexOf('ledger|') === 0), 'the ledger is inside');
    assert.ok(inKeys.some((k) => k.indexOf('1|') === 0), 'row 1 is inside');
    assert.ok(inKeys.some((k) => k.indexOf('lead|real_recall|') === 0), 'the floors are inside');
    const plainKeys = cellKeys(render(clone(DOCS[f])).root);
    assert.deepStrictEqual(outKeys.concat(inKeys).sort(), plainKeys.slice().sort());
    assert.strictEqual(new Set(outKeys.concat(inKeys)).size, plainKeys.length, 'no cell drawn twice');

    // the details sits right after the lead, inside the profile
    const prof = find(root, (n) => attr(n, 'class') === 'pf-profile')[0].node;
    assert.strictEqual(attr(prof.children[0], 'data-row'), 'lead');
    assert.strictEqual(prof.children[1], det);
    assert.strictEqual(prof.children.length, 2);
  });
}

test('compact_keeps_the_drill_downs_inside_the_details', () => {
  const calls = [];
  const { root, env } = render(clone(DOCS['profile-synth']), {
    compact: true,
    drill: (rowKey, container) => { calls.push(rowKey); container.textContent = 'drill ' + rowKey; },
  });
  assert.deepStrictEqual(env.errors, []);
  assert.deepStrictEqual(calls.slice().sort(), [5, 8]);
  const det = fullDetails(root)[0].node;
  const drills = find(root, (n) => attr(n, 'data-drill') !== undefined);
  assert.strictEqual(drills.length, 2);
  drills.forEach((d) => assert.ok(d.parents.indexOf(det) >= 0, 'drill ' + attr(d.node, 'data-drill') + ' is inside'));
});

test('compact_leaves_the_band_and_the_other_options_alone', () => {
  const synth = clone(DOCS['profile-synth']);
  const a = render(synth, { compact: true });
  assert.strictEqual(find(a.root, (n) => attr(n, 'data-band') === 'synthetic').length, 1);
  const b = render(synth, { compact: true, syntheticBand: false, gloss: false });
  assert.strictEqual(find(b.root, (n) => attr(n, 'data-band') === 'synthetic').length, 0);
  assert.ok(!textOf(b.root).includes('a map of the official pathway'), 'gloss: false still holds');
});

test('compact_on_a_profile_with_no_rows_draws_no_details', () => {
  const doc = clone(DOCS['profile-synth']);
  doc.rows = [];
  const { root, env } = render(doc, { compact: true });
  assert.deepStrictEqual(env.errors, []);
  assert.strictEqual(fullDetails(root).length, 0);
});
