'use strict';
// Task 30 (plan V4): the study page (SS.study, static/js/site-study.js) and the Install page (SS.install,
// static/js/site-install.js).
// Fixture: tests/fixtures/profiles/profile-v2.json, analysis/public/profile-v2.json (e5ef8840...) with its reading-3
// cells at temperature 0 (council 23 fix 4; sha256 in tests/test_profile_view.py). Panels 0 to 11 (the analysis session's list, numbered from
// 0): panel 0 the lead, panel 1 the ledger, panels 2 to 10 rows 1 to 9, panel 11 the run block (chain heads and the
// declaration-log head; never per-record hashes). Ids are written "panel-<n>": a capital P with digits reads as a
// participant id to the kit's leak scan.
const test = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const { load, ROOT, JS_DIR } = require('./harness');

const FIX = path.join(ROOT, 'tests', 'fixtures', 'profiles');
const V3 = JSON.parse(fs.readFileSync(path.join(FIX, 'profile-v3.json'), 'utf8'));
const V2 = JSON.parse(fs.readFileSync(path.join(FIX, 'profile-v2.json'), 'utf8'));
const SYNTH = JSON.parse(fs.readFileSync(path.join(FIX, 'profile-synth.json'), 'utf8'));
const EN = JSON.parse(fs.readFileSync(path.join(ROOT, 'second_scribe', 'app', 'strings', 'en.json'), 'utf8'));
const PROMPT = fs.readFileSync(path.join(ROOT, 'prompts', 'prior_only.p0.v2.txt'), 'utf8').replace(/\n+$/, '');
const clone = (o) => JSON.parse(JSON.stringify(o));
const HEX64 = /\b[0-9a-f]{64}\b/g;
const HEX40 = /\b[0-9a-f]{40}\b/g;
const IDS = Array.from({ length: 12 }, (_, i) => 'panel-' + i);

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
function count(hay, needle) { return hay.split(needle).length - 1; }

function env() {
  const e = load({ files: ['core.js', 'strip.js', 'profile.js'] });
  for (const f of ['site-study.js', 'site-install.js']) {
    vm.runInContext(fs.readFileSync(path.join(JS_DIR, f), 'utf8'), e.window, { filename: f });
  }
  return e;
}
function renderStudy(doc) {
  const e = env();
  const root = e.window.document.createElement('div');
  e.SS.study.render(root, doc);
  return { root, SS: e.SS, errors: e.errors, window: e.window };
}

// ------------------------------------------------------------------------------------------- states
test('state_of_each_file', () => {
  const { SS } = env();
  assert.strictEqual(SS.study.state(V3), 'reported');
  assert.strictEqual(SS.study.state({ state: 'not_yet_run' }), 'not_yet_run');
  assert.strictEqual(SS.study.state(SYNTH), 'synthetic');
  assert.strictEqual(SS.study.state(Object.assign(clone(V3), { state: 'pending' })), 'pending');
  assert.strictEqual(SS.study.state(Object.assign(clone(V3), { schema: 'orgsci-profile/v3' })), 'unreadable');
  assert.strictEqual(SS.study.state(Object.assign(clone(V3), { state: 'interpreted' })), 'unreadable');
  assert.strictEqual(SS.study.state(null), 'unreadable');
  assert.strictEqual(SS.study.state(undefined), 'not_yet_run', 'no data file at all reads as the stub');
});

test('states_render_distinct', () => {
  const docs = {
    reported: V3,
    not_yet_run: { state: 'not_yet_run' },
    synthetic: SYNTH,
    pending: Object.assign(clone(V3), { state: 'pending' }),
    unreadable: { schema: 'orgsci-profile/v9', state: 'reported', rows: [] },
  };
  const titles = new Set();
  const texts = new Set();
  for (const [s, doc] of Object.entries(docs)) {
    const { root, errors } = renderStudy(doc);
    assert.deepStrictEqual(errors, [], s + ': no unknown string keys');
    assert.strictEqual(attr(root, 'data-state'), s);
    const status = byAttr(root, 'data-part', 'status');
    if (s === 'reported') {
      assert.strictEqual(status.length, 0);
      assert.ok(byAttr(root, 'data-cell').length > 100);
      continue;
    }
    assert.strictEqual(status.length, 1, s + ' has one status block');
    const t = textOf(status[0].node);
    titles.add(EN['study.state.' + s + '.title']);
    texts.add(t);
    assert.ok(t.includes(EN['study.state.' + s + '.title']), s + ' title');
    assert.ok(t.includes(EN['study.state.' + s + '.body']), s + ' body');
    assert.strictEqual(byAttr(root, 'data-cell').length, 0, s + ' shows no profile cell');
    assert.strictEqual(byAttr(root, 'data-study-panel').length, 0, s + ' shows no panel');
  }
  assert.strictEqual(titles.size, 4, 'four distinct non-reported titles');
  assert.strictEqual(texts.size, 4, 'four distinct non-reported screens');
});

test('synthetic_or_pending_file_shows_none_of_its_numbers', () => {
  for (const doc of [SYNTH, Object.assign(clone(V3), { state: 'pending' })]) {
    const { root } = renderStudy(doc);
    const t = textOf(root);
    assert.ok(!/\d/.test(t), 'no digit on a refused screen: ' + t.match(/.{0,30}\d.{0,30}/));
    assert.strictEqual(find(root, (n) => /synthetic/.test(attr(n, 'class') || '') || attr(n, 'data-band') !== undefined).length, 0);
    assert.ok(!t.includes(EN['band.synthetic']));
  }
});

// ------------------------------------------------------------------------------------------- panels
test('panels_fixed_order_with_nulls', () => {
  const { SS } = env();
  const ps = clone(SS.study.panels(V3));
  assert.deepStrictEqual(ps.map((p) => p.id), IDS);
  assert.deepStrictEqual(ps.map((p) => p.index), IDS.map((_, i) => i));
  assert.deepStrictEqual(ps.map((p) => p.row), ['lead', 'ledger', 1, 2, 3, 4, 5, 6, 7, 8, 9, 'run']);
  assert.ok(ps.every((p) => p.present === true));
  assert.deepStrictEqual(ps.map((p) => p.title_key), V3.rows.map((r) => r.label_key + '.title').concat(['study.run.title']));

  // a row that is not in the file keeps its place and reads "not computed"
  const doc = clone(V3);
  doc.rows = doc.rows.filter((r) => r.row !== 3);
  delete doc.run;
  const ps2 = clone(SS.study.panels(doc));
  assert.deepStrictEqual(ps2.map((p) => p.id), ps.map((p) => p.id));
  assert.strictEqual(ps2[4].present, false);
  assert.strictEqual(ps2[11].present, false);
  const { root } = renderStudy(doc);
  const secs = byAttr(root, 'data-study-panel').map((h) => attr(h.node, 'id'));
  assert.deepStrictEqual(secs, IDS, 'sections in panel order');
  const p4 = byAttr(root, 'data-study-panel', '4')[0].node;
  assert.ok(textOf(p4).includes(EN['profile.row3.support.title']));
  assert.ok(textOf(p4).includes(EN['common.not_computed']));
  const p11 = byAttr(root, 'data-study-panel', '11')[0].node;
  assert.ok(textOf(p11).includes(EN['common.not_computed']));
});

test('rendered_panels_in_order_with_anchors_and_nav', () => {
  const { root } = renderStudy(V3);
  const secs = byAttr(root, 'data-study-panel');
  assert.deepStrictEqual(secs.map((h) => attr(h.node, 'data-study-panel')), IDS.map((_, i) => String(i)));
  secs.forEach((h, i) => assert.strictEqual(attr(h.node, 'id'), IDS[i]));
  // panels 0 to 10 are the profile rows in file order
  assert.deepStrictEqual(secs.slice(0, 11).map((h) => attr(h.node, 'data-row')), V3.rows.map((r) => String(r.row)));
  const links = find(root, (n) => n.tagName === 'A' && /^#panel-\d+$/.test(attr(n, 'href') || ''));
  assert.deepStrictEqual(links.map((l) => attr(l.node, 'href')), secs.map((h) => '#' + attr(h.node, 'id')));
});

test('renders_every_profile_cell_once', () => {
  const { root } = renderStudy(V3);
  for (const r of V3.rows) {
    for (const c of r.cells) {
      const key = r.row + '|' + c.name + '|' + (c.x === undefined || c.x === null ? '' : c.x);
      const hits = byAttr(root, 'data-cell', key).filter((h) => !h.parents.some((p) => attr(p, 'data-drill') !== undefined));
      // council 30: the four derived yield lines are left out on purpose (see technical_details_closed_and_hold_every_panel)
      assert.strictEqual(hits.length, c.derived === true ? 0 : 1, key);
    }
  }
});

test('drill_downs_for_rows_5_and_8', () => {
  const { root } = renderStudy(V3);
  const drills = byAttr(root, 'data-drill').map((h) => attr(h.node, 'data-drill'));
  assert.deepStrictEqual(drills, ['5', '8']);
});

// ------------------------------------------------------------------------------------------- numbers
function numericLeaves(o, out) {
  if (typeof o === 'number') out.push(o);
  else if (o && typeof o === 'object') Object.values(o).forEach((v) => numericLeaves(v, out));
  return out;
}
function stringLeaves(o, out) {
  if (typeof o === 'string') out.push(o);
  else if (o && typeof o === 'object') Object.values(o).forEach((v) => stringLeaves(v, out));
  return out;
}
const NUM_TOKEN = /[−+]?(?:\d[\d,]*(?:\.\d+)?|\.\d+)/g;

test('each_number_has_key_path', () => {
  const { root, SS } = renderStudy(V3);
  // 1) every element the page itself fills from the file names its key path, and prints that value
  const keyed = byAttr(root, 'data-key');
  assert.ok(keyed.length >= 40, 'the run block and the page head name their key paths');
  for (const { node } of keyed) {
    const kpth = attr(node, 'data-key');
    const v = SS.get(V3, kpth);
    assert.notStrictEqual(v, undefined, kpth + ' resolves in the file');
    const t = textOf(node);
    if (typeof v === 'number') assert.strictEqual(t, SS.profile.fmt.num(v), kpth);
    else if (typeof v === 'string' && /^[0-9a-f]{40,64}$/.test(v)) assert.strictEqual(t, v, kpth);
    else if (v === '[SOURCE]') assert.strictEqual(t, EN['study.run.source_placeholder'], kpth);
  }
  // 2) every number on the page is a number of the file (or a digit of a string in it), never typed in
  const allowed = new Set();
  numericLeaves(V3, []).forEach((v) => {
    [SS.profile.fmt.num(v), SS.profile.fmt.num(v, 'difference'), SS.profile.fmt.num(v, 'count'), SS.profile.fmt.num(v, 'per_map'), String(v),
      String(Math.round(v * 100)), v % 1 === 0 ? v.toFixed(1) : String(v)].forEach((s) => s.replace(/^[−+]/, '') && allowed.add(s.replace(/^[−+]/, '')));
    // the findings print a share of the file as a percentage with one decimal (.394 -> 39.4) (items 8 and 9, 23 Oct)
    if (Math.abs(v) <= 1) allowed.add((Math.round(Math.abs(v) * 1000) / 10).toFixed(1));
  });
  stringLeaves(V3, []).forEach((s) => (s.match(NUM_TOKEN) || []).forEach((t) => allowed.add(t.replace(/^[−+]/, ''))));
  const own = Object.keys(EN).filter((k) => /^(study|profile|strip|lane|common)\./.test(k));
  own.forEach((k) => (EN[k].match(NUM_TOKEN) || []).forEach((t) => allowed.add(t.replace(/^[−+]/, ''))));
  const stray = [];
  walk(root, (n) => {
    if (!n.textContent || (n.children && n.children.length)) return;
    const t = n.textContent.replace(HEX64, '').replace(HEX40, '');
    (t.match(NUM_TOKEN) || []).forEach((tok) => {
      const bare = tok.replace(/^[−+]/, '').replace(/,$/, '');
      if (bare && !allowed.has(bare)) stray.push(tok + ' in "' + n.textContent.slice(0, 60) + '"');
    });
  });
  assert.deepStrictEqual(stray, []);
});

// ------------------------------------------------------------------------------------------- panel 11, the run block
const COMMON_RUN_KEYS = [
  'run.gate_chain.head', 'run.gate_chain.entries', 'run.gate_chain.verified', 'run.gate_chain.verify_date',
  'run.declaration_log.head', 'run.declaration_log.records', 'run.declaration_log.last_seq', 'run.declaration_log.verified',
  'run.prompt_set.q_phase_sha256', 'run.prompt_set.j_m_phase_sha256',
  'run.reading3_prompt.path', 'run.reading3_prompt.sha256',
  'run.models.0.family', 'run.models.0.model_id', 'run.models.2.model_id', 'run.models.3.model_id',
  'run.settings.matcher_primary.lexical_threshold', 'run.settings.matcher_lenient.judge_label',
  'run.settings.reconstructor_temperature', 'run.settings.reading3_temperature', 'run.settings.reading3_draws',
  'run.settings.bootstrap_resamples', 'run.settings.interval_method', 'run.settings.gate_k_people',
  'run.verify.gate_chain', 'run.verify.j', 'run.verify.m',
];
const BAD_WORDS = /undefined|\bnull\b|\[object Object\]|NaN|\[SOURCE\]/;

function panel11(doc) {
  const r = renderStudy(doc);
  r.p11 = byAttr(r.root, 'data-study-panel', '11')[0].node;
  r.t = textOf(r.p11);
  r.at = (k) => textOf(byAttr(r.p11, 'data-key', k)[0].node);
  r.keys = byAttr(r.p11, 'data-key').map((h) => attr(h.node, 'data-key'));
  return r;
}

// the study file as it ships (PROFILE-SCHEMA section 14, public form)
test('run_block_public_form_lists_its_fields_by_key', () => {
  const r = panel11(V3);
  assert.deepStrictEqual(r.errors, []);
  for (const k of COMMON_RUN_KEYS.concat(['run.run_chain_heads_source', 'run.kit.release_commit',
    'run.kit.run_commits.0', 'run.kit.run_commits.1', 'run.kit.run_commits.2'])) assert.ok(r.keys.includes(k), k);
  assert.ok(r.t.includes(EN['study.run.limit']), 'the stated limit is on screen');
  assert.ok(r.t.includes(V3.run.gate_chain.head));
  assert.ok(r.t.includes(V3.run.declaration_log.head));
  // the older form's keys are not drawn as rows of "not computed"
  for (const k of ['run.kit.release_branch', 'run.reading3_prompt.working_tree_sha256', 'run.run_chain_heads.q']) {
    assert.ok(!r.keys.includes(k), k + ' is not part of the public form');
  }
  assert.ok(!BAD_WORDS.test(r.t), 'no undefined / null / [object Object] / [SOURCE]: ' + r.t.match(BAD_WORDS));
});

test('run_block_public_form_chain_heads_available_from_the_authors', () => {
  assert.strictEqual(V3.run.run_chain_heads, null);
  assert.strictEqual(V3.run.run_chain_heads_source, 'available_from_authors');
  const r = panel11(V3);
  const row = byAttr(r.p11, 'data-key', 'run.run_chain_heads_source')[0].parents.slice(-1)[0];
  assert.strictEqual(textOf(row), EN['study.run.run_chain_heads'] + ' ' + EN['profile.run.reason.available_from_authors']);
  assert.strictEqual(textOf(row), 'Run chain heads available from the authors');
  assert.strictEqual(r.at('run.run_chain_heads_source'), 'available from the authors');
  // a code with no sentence in either strings file never prints raw
  const doc = clone(V3);
  doc.run.run_chain_heads_source = 'some_new_code';
  const r2 = panel11(doc);
  assert.strictEqual(r2.at('run.run_chain_heads_source'), EN['common.not_computed']);
  assert.deepStrictEqual(r2.errors, []);
  // free text is not a code: never printed
  doc.run.run_chain_heads_source = 'Heads are held by the authors, ask them';
  assert.strictEqual(panel11(doc).at('run.run_chain_heads_source'), EN['common.not_computed']);
  // no source at all
  delete doc.run.run_chain_heads_source;
  assert.strictEqual(panel11(doc).at('run.run_chain_heads_source'), EN['common.not_computed']);
});

test('run_block_public_form_null_model_fields_read_not_recorded', () => {
  const nulls = [];
  V3.run.models.forEach((m, i) => ['family', 'model_id', 'weights', 'quantisation', 'thinking'].forEach((f) => {
    if (m[f] === null) nulls.push([i, f, m.source]);
  }));
  assert.deepStrictEqual(nulls.map((n) => n[0] + '.' + n[1]), ['0.quantisation', '1.thinking', '3.model_id']);
  const r = panel11(V3);
  for (const [i, f, src] of nulls) {
    assert.strictEqual(src, 'not_recorded_public');
    assert.strictEqual(r.at('run.models.' + i + '.' + f), 'not recorded in public files', i + '.' + f);
  }
  // a model whose source is a note (not a code) and whose field is null reads "not computed"
  const doc = clone(V3);
  doc.run.models[0].source = 'a free text note about where this came from';
  assert.strictEqual(panel11(doc).at('run.models.0.quantisation'), EN['common.not_computed']);
  // a recorded value is printed as it is
  assert.strictEqual(r.at('run.models.2.model_id'), V3.run.models[2].model_id);
  assert.strictEqual(r.at('run.models.1.quantisation'), 'AWQ');
});

test('run_block_public_form_commits_list_and_release', () => {
  const r = panel11(V3);
  assert.strictEqual(r.at('run.kit.run_commits.0'), V3.run.kit.run_commits[0]);
  assert.strictEqual(r.at('run.kit.run_commits.2'), V3.run.kit.run_commits[2]);
  const labels = byAttr(r.p11, 'data-key', 'run.kit.run_commits.1')[0].parents.slice(-1)[0].children[0];
  assert.strictEqual(labels.textContent, EN['study.run.kit.run_commits.j']);
  assert.strictEqual(V3.run.kit.release_commit, null);
  assert.strictEqual(r.at('run.kit.release_commit'), EN['study.run.kit.release_commit_public']);
  // a list that is not three long gets numbered labels, none is dropped
  const doc = clone(V3);
  doc.run.kit.run_commits = doc.run.kit.run_commits.concat(['c'.repeat(40)]);
  const r2 = panel11(doc);
  assert.strictEqual(r2.keys.filter((k) => /^run\.kit\.run_commits\./.test(k)).length, 4);
  const lab = byAttr(r2.p11, 'data-key', 'run.kit.run_commits.3')[0].parents.slice(-1)[0].children[0];
  assert.strictEqual(lab.textContent, 'Run commit 4');
  // an empty list is a row-less group, not a crash
  doc.run.kit.run_commits = [];
  assert.doesNotThrow(() => panel11(doc));
});

test('run_block_public_form_every_hash_on_the_page_is_listed', () => {
  const { root } = renderStudy(V3);
  const t = textOf(root);
  const ok = new Set([V3.run.gate_chain.head, V3.run.declaration_log.head, V3.run.prompt_set.q_phase_sha256,
    V3.run.prompt_set.j_m_phase_sha256, V3.run.reading3_prompt.sha256]);
  (t.match(HEX64) || []).forEach((h) => assert.ok(ok.has(h), 'unexpected hash ' + h));
  // the quoted no-interview prompt is the file's own text (it contains the word "null"): checked without it
  const noPrompt = t.split(EN['study.reading3.prompt'].replace(/\{\{/g, '{').replace(/\}\}/g, '}')).join(' ');
  assert.ok(noPrompt.length < t.length, 'the quoted prompt was found and left out');
  assert.ok(!BAD_WORDS.test(noPrompt), 'whole page: ' + noPrompt.match(BAD_WORDS));
});

test('run_block_missing_pieces_never_crash_and_never_print_undefined', () => {
  for (const mut of [
    (d) => { delete d.run.run_chain_heads; delete d.run.run_chain_heads_source; },
    (d) => { d.run.run_chain_heads = 7; },
    (d) => { d.run.run_chain_heads = []; },
    (d) => { delete d.run.kit; },
    (d) => { d.run.kit = null; },
    (d) => { d.run.kit.run_commits = null; },
    (d) => { d.run.kit.run_commits = 'x'; },
    (d) => { d.run.models = null; },
    (d) => { d.run.models = [null, 3, {}]; },
    (d) => { d.run.models[0].source = null; },
  ]) {
    const doc = clone(V3);
    mut(doc);
    const r = panel11(doc);
    assert.deepStrictEqual(r.errors, []);
    assert.ok(!BAD_WORDS.test(r.t), r.t.match(BAD_WORDS) + ' after ' + mut);
  }
});

// the older per-phase form still draws (the fixture is kept for this)
test('run_block_older_per_phase_form_still_draws', () => {
  const r = panel11(V2);
  assert.deepStrictEqual(r.errors, []);
  for (const k of COMMON_RUN_KEYS.concat(['run.run_chain_heads.q', 'run.run_chain_heads.j', 'run.run_chain_heads.m',
    'run.kit.release_commit', 'run.kit.release_branch', 'run.kit.run_commits.q', 'run.kit.run_commits.j',
    'run.kit.run_commits.m', 'run.reading3_prompt.working_tree_sha256'])) assert.ok(r.keys.includes(k), k);
  assert.ok(r.t.includes(V2.run.gate_chain.head));
  assert.strictEqual(r.at('run.kit.release_commit'), V2.run.kit.release_commit);
  assert.strictEqual(r.at('run.kit.run_commits.q'), V2.run.kit.run_commits.q);
  // q, j, m heads, the second judge's served id, one quantisation and one thinking flag are "[SOURCE]" in the file
  assert.strictEqual(count(r.t, EN['study.run.source_placeholder']), 6, '"[SOURCE]" reads "available from the authors"');
  for (const k of ['run.run_chain_heads.q', 'run.run_chain_heads.j', 'run.run_chain_heads.m', 'run.models.3.model_id']) {
    assert.strictEqual(r.at(k), EN['study.run.source_placeholder'], k);
  }
  assert.ok(!r.t.includes('[SOURCE]'));
});

test('run_block_never_prints_source_notes_or_unlisted_keys', () => {
  const doc = clone(V3);
  const planted = 'ab'.repeat(32);
  doc.run.records = [{ record_sha256: planted }];
  doc.run.gate_chain.entry_hashes = [planted];
  const { root } = renderStudy(doc);
  const t = textOf(root);
  assert.ok(!t.includes(planted), 'a per-record hash in the file is never printed');
  for (const s of stringLeaves(V3.run, [])) {
    if (s.length > 60 && !/^[0-9a-f]+$/.test(s)) assert.ok(!t.includes(s), 'source note printed: ' + s.slice(0, 40));
  }
  // every 64-hex on the page is a head, a prompt-set hash or the reading-3 prompt hash
  const heads = new Set([V3.run.gate_chain.head, V3.run.declaration_log.head, V3.run.prompt_set.q_phase_sha256,
    V3.run.prompt_set.j_m_phase_sha256, V3.run.reading3_prompt.sha256, V3.run.reading3_prompt.working_tree_sha256]);
  (t.match(HEX64) || []).forEach((h) => assert.ok(heads.has(h), 'unexpected hash ' + h));
});

test('run_block_null_reads_not_computed', () => {
  const doc = clone(V3);
  doc.run.gate_chain.head = null;
  delete doc.run.verify;
  const { root } = renderStudy(doc);
  const head = byAttr(root, 'data-key', 'run.gate_chain.head');
  assert.strictEqual(head.length, 1);
  assert.strictEqual(textOf(head[0].node), EN['common.not_computed']);
  const vj = byAttr(root, 'data-key', 'run.verify.j');
  assert.strictEqual(textOf(vj[0].node), EN['common.not_computed']);
});

// ------------------------------------------------------------------------------------------- wording
test('reading3_prompt_quoted_once_from_the_as_run_file', () => {
  const { root } = renderStudy(V3);
  const t = textOf(root);
  assert.strictEqual(count(t, PROMPT), 1, 'the as-run prompt appears once');
  assert.ok(V3.run.reading3_prompt.sha256.startsWith('9b48f863'));
  const q = byAttr(root, 'data-part', 'reading3_prompt');
  assert.strictEqual(q.length, 1);
  assert.ok(textOf(q[0].node).includes(V3.run.reading3_prompt.sha256));
});

test('gloss_once_per_page', () => {
  for (const doc of [V3, { state: 'not_yet_run' }]) {
    const { root } = renderStudy(doc);
    const t = textOf(root);
    assert.ok(t.includes(EN['lane.no_interview']));
    assert.strictEqual(count(t, EN['lane.no_interview_gloss']), 1);
  }
});

test('three_readings_named_and_reference_is_one_reading', () => {
  const { root } = renderStudy(V3);
  const t = textOf(root);
  for (const k of ['lane.reference', 'lane.ai', 'lane.no_interview']) assert.ok(t.includes(EN[k]), k);
  // the author's item 8 (23 Oct): the reference, source and pooling notes are gone from the page and the strings
  for (const k of ['study.reference_note', 'study.source_note', 'study.pooling.side_by_side']) assert.strictEqual(k in EN, false, k);
  const own = Object.keys(EN).filter((k) => k.startsWith('study.') || k.startsWith('install.'));
  assert.ok(own.length > 20);
  for (const k of own) {
    assert.ok(!/\b(error|errors|erred|mistake|mistakes|missed|wrong|fail|failed|correct|fix|first|script|certificate)\b/i.test(EN[k]),
      k + ': ' + EN[k]);
  }
});

test('study_page_shows_nothing_synthetic', () => {
  const { root } = renderStudy(V3);
  assert.strictEqual(find(root, (n) => /synthetic|test-pattern/.test(attr(n, 'class') || '')).length, 0);
  assert.strictEqual(byAttr(root, 'data-band').length, 0);
  const t = textOf(root);
  assert.ok(!t.includes(EN['band.synthetic']));
  // the source note (the one place that named synthetic evidence) is gone (item 8): the word never appears
  assert.ok(!/synthetic/i.test(t));
});

// ------------------------------------------------------------------------------------------- items 8 and 9 (23 Oct)
const REAL = JSON.parse(fs.readFileSync(path.join(ROOT, 'analysis', 'public', 'profile.json'), 'utf8'));

test('open_page_is_four_findings_and_what_to_do', () => {
  const { root, SS, errors } = renderStudy(REAL);
  const f = byAttr(root, 'data-finding');
  assert.deepStrictEqual(f.map((h) => attr(h.node, 'data-finding')), ['added', 'omitted', 'size', 'flattened']);
  const txt = f.map((h) => textOf(h.node));
  // the plan's numbers, as the paper reports them, read from the file
  assert.ok(txt[0].includes('added 24 percentage points') && txt[0].includes('39.4%') && txt[0].includes('15.2%'), txt[0]);
  // council 30 (MUST 3): a descriptive contrast, with the proposal's own reason why it does not isolate the effect
  assert.ok(txt[0].includes('so this contrast does not isolate the effect of hearing the interview') &&
    !/drew on what people said|beyond the official/.test(txt[0]), txt[0]);
  assert.ok(txt[1].includes('53.9%'), txt[1]);
  assert.ok(txt[2].includes('about 61% as many boxes') && txt[2].includes('6.5 points more') &&
    txt[2].includes('4.3 to 8.6'), txt[2]);
  assert.ok(txt[3].startsWith('In 4 cases') && txt[3].includes('In no case'), txt[3]);
  // each finding names the cells it reads, and each cell is in the file
  for (const h of f) {
    const keys = attr(h.node, 'data-keys').split(' ');
    assert.ok(keys.length >= 1);
    for (const k of keys) assert.strictEqual(typeof SS.get(REAL, k + '.value'), 'number', k);
  }
  assert.strictEqual(SS.get(REAL, 'rows.0.cells.2.value'), 0.241);
  assert.strictEqual(SS.get(REAL, 'rows.0.cells.26.value'), 0.065);
  assert.strictEqual(SS.get(REAL, 'rows.6.cells.3.value'), 4);
  assert.strictEqual(SS.get(REAL, 'rows.6.cells.4.value'), 0);
  // proportions as percentages: no bare ".394" style number in the open page
  const open = byAttr(root, 'data-part', 'findings').concat(byAttr(root, 'data-part', 'todo')).map((h) => textOf(h.node)).join(' ');
  assert.ok(!/(^|[^\d])\.\d/.test(open), 'no leading-dot share in the findings');
  assert.ok(textOf(byAttr(root, 'data-part', 'todo')[0].node).includes(EN['study.todo.body']));
  assert.deepStrictEqual(errors, []);
});

test('technical_details_closed_and_hold_every_panel', () => {
  const { root } = renderStudy(REAL);
  const det = byAttr(root, 'data-part', 'technical');
  assert.strictEqual(det.length, 1);
  const d = det[0].node;
  assert.strictEqual(d.tagName, 'DETAILS');
  assert.strictEqual(attr(d, 'open'), undefined, 'closed by default');
  assert.strictEqual(textOf(d.children[0]), EN['study.details.summary']);
  // all twelve panels, the prompt and the page meta sit inside it; every cell of the file is still drawn once
  const inside = (h) => h.parents.indexOf(d) >= 0;
  const secs = byAttr(root, 'data-study-panel');
  assert.strictEqual(secs.length, 12);
  assert.ok(secs.every(inside), 'every panel inside Technical details');
  assert.ok(byAttr(root, 'data-part', 'reading3_prompt').every(inside));
  // council 30 (01 NOTE, council 23 D4): the four derived "yield per box" lines (derived: true, arithmetic the paper does
  // not use) are the one exception, on purpose; they are counted here so the exception cannot grow
  let derived = 0;
  for (const r of REAL.rows) {
    for (const c of r.cells) {
      const key = r.row + '|' + c.name + '|' + (c.x === undefined || c.x === null ? '' : c.x);
      const hits = byAttr(root, 'data-cell', key).filter((h) => !h.parents.some((p) => attr(p, 'data-drill') !== undefined));
      if (c.derived === true) { derived += 1; assert.strictEqual(hits.length, 0, key + ' (derived) is left out'); continue; }
      assert.strictEqual(hits.length, 1, key);
      assert.ok(inside(hits[0]), key + ' inside Technical details');
    }
  }
  assert.strictEqual(derived, 4);
  assert.ok(!/10\.4 points|yield per box/i.test(textOf(root)), 'no equal-size yield line on the page');
  assert.ok(textOf(root).includes('+6.5 points'), 'the box-for-box contrast stays');
  // the findings come before Technical details, and nothing numeric from the panels is open
  const order = [];
  walk(root, (n) => { const p = attr(n, 'data-part'); if (p === 'findings' || p === 'todo' || p === 'technical') order.push(p); });
  assert.deepStrictEqual(order, ['findings', 'todo', 'technical']);
});

test('new_study_sentences_follow_the_register', () => {
  const keys = ['study.finding.added', 'study.finding.omitted', 'study.finding.size', 'study.finding.flattened',
    'study.finding.flattened.none', 'study.finding.flattened.some', 'study.todo.body', 'study.details.note'];
  for (const k of keys) {
    const v = EN[k];
    assert.strictEqual(typeof v, 'string', k);
    assert.ok(!/\u2014|rather than|official script|\b(first|replicable|validated|ground truth)\b/i.test(v), k);
    for (const sentence of v.split(/(?<=\.)\s+/)) assert.ok(sentence.split(/\s+/).length >= 8, k + ': short sentence "' + sentence + '"');
  }
});

test('reads_the_data_from_ss_data_study_only', () => {
  const e = env();
  e.window.SS_DATA = { study: { state: 'not_yet_run' } };
  assert.strictEqual(e.SS.study.data(), e.window.SS_DATA.study);
  delete e.window.SS_DATA;
  e.window.study = V3;   // not the contract's global: ignored
  assert.strictEqual(e.SS.study.data(), undefined);
});

// ------------------------------------------------------------------------------------------- Install
test('install_tabs_and_commands_exact', () => {
  const { SS, window, errors } = env();
  const m = clone(SS.install.model());
  assert.deepStrictEqual(m.map((t) => t.id), ['macos', 'windows', 'linux']);
  const cmds = (t) => [].concat(...t.steps.map((s) => s.commands || [])).map((k) => EN[k]);
  for (const t of m) {
    const c = cmds(t);
    assert.ok(c.includes(t.id === 'windows' ? 'py -m second_scribe.app' : 'python3 -m second_scribe.app'), t.id);
    assert.ok(!c.includes(t.id === 'windows' ? 'python3 -m second_scribe.app' : 'py -m second_scribe.app'), t.id);
    assert.ok(c.some((x) => x.includes('OLLAMA_NO_CLOUD')), t.id + ' sets OLLAMA_NO_CLOUD');
    assert.ok(c.some((x) => /gemma/.test(x)) && c.some((x) => /qwen/.test(x)), t.id + ' a Gemma and a Qwen');
    const prose = t.steps.map((s) => [s.body_key, ...(s.more_keys || []), ...(s.after_keys || [])].map((k) => EN[k]).join(' ')).join(' ');
    assert.ok(/3\.10/.test(EN[t.steps[0].title_key] + prose), t.id + ' Python 3.10');
    assert.ok(/LM Studio/.test(prose), t.id + ' LM Studio');
    assert.ok(/examples and carry no endorsement/.test(prose), t.id + ' examples, with no endorsement');
    // item 21: the start step says where to run the command, shows the cd line before it, and names the message
    const start = t.steps.find((s) => s.id === 'start');
    assert.deepStrictEqual(start.commands.map((k) => EN[k].split(' ')[0]), ['cd', t.id === 'windows' ? 'py' : 'python3'], t.id);
    assert.ok(EN[start.body_key].includes('README.md'), t.id);
    assert.ok(start.after_keys.map((k) => EN[k]).join(' ').includes('No module named second_scribe'), t.id);
  }
  const root = window.document.createElement('div');
  SS.install.render(root, { tab: 'windows' });
  assert.deepStrictEqual(errors, []);
  const tabs = find(root, (n) => attr(n, 'role') === 'tab');
  assert.strictEqual(tabs.length, 3);
  assert.strictEqual(find(root, (n) => attr(n, 'role') === 'tablist').length, 1);
  assert.deepStrictEqual(tabs.map((h) => attr(h.node, 'aria-selected')), ['false', 'true', 'false']);
  assert.deepStrictEqual(tabs.map((h) => attr(h.node, 'tabindex')), ['-1', '0', '-1']);
  const panels = find(root, (n) => attr(n, 'role') === 'tabpanel');
  assert.strictEqual(panels.length, 3);
  panels.forEach((p, i) => {
    assert.strictEqual(attr(tabs[i].node, 'aria-controls'), attr(p.node, 'id'));
    assert.strictEqual(attr(p.node, 'aria-labelledby'), attr(tabs[i].node, 'id'));
    assert.strictEqual(p.node.hidden, i !== 1);
  });
  // every command is in a code element with its own copy button
  const codes = find(root, (n) => n.tagName === 'CODE').map((h) => h.node.textContent);
  assert.ok(codes.includes('py -m second_scribe.app'));
  assert.ok(codes.includes('python3 -m second_scribe.app'));
  const copies = find(root, (n) => attr(n, 'data-copy') !== undefined);
  assert.strictEqual(copies.length, codes.length);
  // links to the two documents
  const hrefs = find(root, (n) => n.tagName === 'A').map((h) => attr(h.node, 'href'));
  assert.ok(hrefs.includes('../INSTALL.md') && hrefs.includes('../DATAFLOW.md'));
  assert.ok(!hrefs.some((h) => /^https?:/.test(h || '')), 'no outside link');
});

test('install_tab_keyboard', () => {
  const { SS } = env();
  const k = SS.install.nextIndex;
  assert.strictEqual(k(0, 'ArrowRight', 3), 1);
  assert.strictEqual(k(2, 'ArrowRight', 3), 0);
  assert.strictEqual(k(0, 'ArrowLeft', 3), 2);
  assert.strictEqual(k(1, 'Home', 3), 0);
  assert.strictEqual(k(1, 'End', 3), 2);
  assert.strictEqual(k(1, 'a', 3), -1);
});

test('install_default_tab_from_platform', () => {
  const { SS } = env();
  assert.strictEqual(SS.install.guessTab('Win32'), 'windows');
  assert.strictEqual(SS.install.guessTab('MacIntel'), 'macos');
  assert.strictEqual(SS.install.guessTab('Linux x86_64'), 'linux');
  assert.strictEqual(SS.install.guessTab(''), 'macos');
  assert.strictEqual(SS.install.guessTab(undefined), 'macos');
});
