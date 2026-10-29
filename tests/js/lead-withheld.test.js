'use strict';
// Council 30 (MUST 4): on a small project the exporter withholds the lead cells (state not_run, reason_code
// cell_under_10, profile_floors._floor_cells). The plain lead says so in a sentence, never "did not compute", and the
// Technical details print the kit's sentence for the code (profile_strings.json, merged as strings.load_strings does),
// never the raw code.
const test = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const { load, ROOT } = require('./harness');

const EN = JSON.parse(fs.readFileSync(path.join(ROOT, 'second_scribe', 'app', 'strings', 'en.json'), 'utf8'));
const KIT = JSON.parse(fs.readFileSync(path.join(ROOT, 'second_scribe', 'profile_strings.json'), 'utf8'));
const nr = (name, x) => ({ name, x, value: null, state: 'not_run', reason_code: 'cell_under_10' });
const DOC = { schema: 'orgsci-profile/v2', state: 'reported', rows: [
  { row: 'lead', cells: [nr('ai_recall', 'primary'), nr('no_interview_recall', 'primary'), nr('listening_added', 'primary')] },
] };

function walk(n, fn) { fn(n); (n.children || []).forEach((c) => walk(c, fn)); }
function textOf(n) { const o = []; walk(n, (x) => { if (x.textContent) o.push(x.textContent); }); return o.join(' '); }

test('lead: withheld cells are said in a plain sentence', () => {
  const env = load({ files: ['core.js', 'profile.js'] });
  const said = JSON.parse(JSON.stringify(env.SS.profile.lead(DOC, {})));
  assert.strictEqual(said[0].id, 'added');
  assert.strictEqual(said[0].text, EN['results.lead.added.withheld']);
  assert.strictEqual(EN['results.lead.added.withheld'], 'The app withholds what listening added for this run, because one of ' +
    "the counts behind it describes fewer than 10 people. The maps below still show each person's three readings.");
  assert.ok(said[0].text.indexOf('did not compute') < 0);
  // a cell that was simply not run keeps the not-run sentence
  const bare = { rows: [{ row: 'lead', cells: [{ name: 'listening_added', x: 'primary', value: null, state: 'not_run' }] }] };
  assert.strictEqual(JSON.parse(JSON.stringify(env.SS.profile.lead(bare, {})))[0].text, EN['results.lead.added.not_run']);
});

test('details: the reason prints the kit sentence, never the raw code', () => {
  assert.strictEqual(typeof KIT['profile.reason.cell_under_10'], 'string');
  const env = load({ files: ['core.js', 'profile.js'], strings: KIT });
  const root = env.window.document.createElement('div');
  env.SS.profile.render(root, DOC, {});
  const t = textOf(root);
  assert.ok(t.indexOf(KIT['profile.reason.cell_under_10']) >= 0, t);
  assert.strictEqual(t.indexOf('cell_under_10'), -1, 'no raw code');
});

test('lead: "percentage points" at the first use, "points" after it (council 30)', () => {
  const env = load({ files: ['core.js', 'profile.js'] });
  const lead = [
    { name: 'ai_recall', x: 'primary', value: 0.394 }, { name: 'no_interview_recall', x: 'primary', value: 0.152 },
    { name: 'listening_added', x: 'primary', value: 0.241 },
    { name: 'rarefied_ai_minus_prior_only_recall', x: 'primary', value: 0.065, ci_lo: 0.043, ci_hi: 0.086 },
  ];
  const doc = { rows: [{ row: 'lead', cells: lead }, { row: 2, cells: [{ name: 'ai_per_endorsed', value: 0.608 }] }] };
  const said = JSON.parse(JSON.stringify(env.SS.profile.lead(doc, {})));
  assert.ok(said[0].text.indexOf('24.1 percentage points') >= 0, said[0].text);
  assert.ok(said[2].text.indexOf('recovers 6.5 points more') >= 0, said[2].text);
  // with the first sentence not run, the size sentence is the first use and says it in full
  const alone = JSON.parse(JSON.stringify(env.SS.profile.lead({ rows: [{ row: 'lead', cells: lead.slice(3) }, doc.rows[1]] }, {})));
  assert.ok(alone[2].text.indexOf('recovers 6.5 percentage points more') >= 0, alone[2].text);
});
