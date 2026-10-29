'use strict';
// Task 25: the rung selector and the absent-steps table ("ledger" in code only; the visible title is
// "Wished-for steps under each set of instructions"). The fixture is SYNTHETIC (tests/fixtures/views/mini.view.json).
// The stub DOM has no real events, so the tests wrap createElement to record listeners and call them directly.
const test = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const { load, ROOT } = require('./harness');

const VIEW = JSON.parse(fs.readFileSync(path.join(ROOT, 'tests', 'fixtures', 'views', 'mini.view.json'), 'utf8'));
const EN = JSON.parse(fs.readFileSync(path.join(ROOT, 'second_scribe', 'app', 'strings', 'en.json'), 'utf8'));
const clone = (o) => JSON.parse(JSON.stringify(o));
// Arrays and objects built inside the vm context belong to another realm: compare after a JSON round-trip.
const eq = (a, b, msg) => assert.deepStrictEqual(clone(a), b, msg);

function boot(view) {
  const env = load();
  const doc = env.window.document;
  const make = doc.createElement;
  doc.createElement = (tag) => {
    const n = make(tag);
    n.listeners = {};
    n.addEventListener = (type, fn) => { (n.listeners[type] = n.listeners[type] || []).push(fn); };
    // a real DOM empties the node when textContent is set to ''; the stub does not
    let text = n.textContent;
    Object.defineProperty(n, 'textContent', {
      get: () => text,
      set: (v) => { text = v; if (v === '') n.children.length = 0; },
    });
    return n;
  };
  env.view = clone(view || VIEW);
  env.SS.state.use(clone(env.view));
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
const tag = (root, t) => find(root, (n) => n.tagName === t.toUpperCase());
function allText(root) {
  const out = [];
  walk(root, (n) => { if (n.textContent) out.push(n.textContent); });
  return out.join('\n');
}
function fire(node, type, ev) { (node.listeners[type] || []).forEach((f) => f(ev || {})); }
function mount(env, state, fn) {
  const root = env.window.document.createElement('div');
  env.SS.ledger[fn || 'render'](root, clone(env.view), state);
  return root;
}
const stateOf = (env, patch) => Object.assign(env.SS.state.get(), patch || {});

// ---------------------------------------------------------------- rungOrder

test('placebo_next_to_rung3', () => {
  const { SS } = boot();
  eq(SS.ledger.rungOrder({ rungs: ['rung1', 'rung2', 'rung3', 'placebo'] }), ['rung1', 'rung2', 'placebo', 'rung3']);
  eq(SS.ledger.rungOrder({ rungs: ['placebo', 'rung3', 'rung2', 'rung1'] }), ['rung1', 'rung2', 'placebo', 'rung3']);
  eq(SS.ledger.rungOrder(VIEW), ['rung1', 'rung2', 'placebo', 'rung3']);
  // filtered to the rungs present; placebo stays directly before rung 3 when both are there
  eq(SS.ledger.rungOrder({ rungs: ['rung1', 'rung3'] }), ['rung1', 'rung3']);
  eq(SS.ledger.rungOrder({ rungs: ['rung2', 'placebo'] }), ['rung2', 'placebo']);
  eq(SS.ledger.rungOrder({}), []);
  eq(SS.ledger.rungOrder(null), []);
});

// ---------------------------------------------------------------- rows

test('rows_only_missing_items', () => {
  const { SS } = boot();
  eq(SS.ledger.rows(VIEW, 0).map((r) => r.item_id), ['a1', 'a4', 'a5', 'a7']);
  eq(SS.ledger.rows(VIEW, 1).map((r) => r.item_id), ['b3']);
  const r = SS.ledger.rows(VIEW, 0)[0];
  assert.strictEqual(r.label, 'Pre-application meeting');
  eq(Object.keys(r.cells), ['script', 'rung1', 'rung2', 'placebo', 'rung3']);
  eq(SS.ledger.rows(VIEW, 7), []);
  eq(SS.ledger.rows({}, 0), []);
});

test('cell_values_from_fate', () => {
  const { SS } = boot();
  const row = (rows, id) => clone(rows.find((r) => r.item_id === id).cells);
  const a1 = row(SS.ledger.rows(VIEW, 0), 'a1');
  assert.strictEqual(a1.rung1, 'kept');
  assert.strictEqual(a1.rung2, 'leaked');
  assert.strictEqual(a1.placebo, 'leaked');
  assert.strictEqual(a1.rung3, 'kept');
  // missed -> never_drawn; unattested stays its own value (it reads "Not attested by its own quote")
  const v = clone(VIEW);
  v.maps[0].items[0].fate = { rung1: 'missed', rung2: 'unattested', placebo: 'kept', rung3: 'flipped' };
  const c = row(SS.ledger.rows(v, 0), 'a1');
  assert.strictEqual(c.rung1, 'never_drawn');
  assert.strictEqual(c.rung2, 'unattested');
  assert.strictEqual(c.placebo, 'kept');
  // a fate that cannot belong to a missing item is not guessed
  assert.strictEqual(c.rung3, 'unknown');
});

test('absent_rung_is_not_run', () => {
  const { SS } = boot();
  // MINI-B has no rung3 fate at all
  const b3 = SS.ledger.rows(VIEW, 1)[0].cells;
  assert.strictEqual(b3.rung3, 'not_run');
  assert.strictEqual(b3.rung1, 'kept');
  // a view that did not run rung 3 and placebo: every such cell is not_run, never a guess
  const v = clone(VIEW);
  v.rungs = ['rung1', 'rung2'];
  const rows = SS.ledger.rows(v, 0);
  rows.forEach((r) => {
    assert.strictEqual(r.cells.rung3, 'not_run', r.item_id);
    assert.strictEqual(r.cells.placebo, 'not_run', r.item_id);
  });
  // an item with no fate object at all: not_run in every rung
  const w = clone(VIEW);
  delete w.maps[0].items[0].fate;
  eq(Object.values(SS.ledger.rows(w, 0)[0].cells).slice(1), ['not_run', 'not_run', 'not_run', 'not_run']);
});

test('script_null_is_unknown', () => {
  const { SS } = boot();
  const by = {};
  SS.ledger.rows(VIEW, 0).forEach((r) => { by[r.item_id] = r.cells.script; });
  assert.strictEqual(by.a1, 'lacks');   // script_has false
  assert.strictEqual(by.a4, 'has');     // script_has true
  assert.strictEqual(by.a7, 'unknown'); // script_has null
  const v = clone(VIEW);
  delete v.maps[0].items[0].script_has;
  assert.strictEqual(SS.ledger.rows(v, 0)[0].cells.script, 'unknown');
});

// ---------------------------------------------------------------- selector

test('selector_radio_group_in_order_and_sets_state', () => {
  const env = boot();
  const { SS } = env;
  const root = mount(env, stateOf(env, { rung: 'rung2' }), 'renderSelector');
  const radios = find(root, (n) => n.tagName === 'INPUT' && n.attributes.type === 'radio');
  eq(radios.map((r) => r.attributes.value), ['rung1', 'rung2', 'placebo', 'rung3']);
  assert.strictEqual(new Set(radios.map((r) => r.attributes.name)).size, 1, 'one radio group');
  eq(radios.map((r) => r.checked === true || r.attributes.checked === 'checked'), [false, true, false, false]);
  assert.ok(tag(root, 'fieldset').length === 1 && tag(root, 'legend').length === 1, 'fieldset with a legend');
  // 2.2 issue 2: the selector names a rung the way the buttons and the caption do, and says what it is. Item 42
  // (Part 5c, re-pinned on purpose): each choice is a card, a short title with the buttons' name and a second line
  // that is the plain name of the table and the caption without its "(rung N)"
  const cards = find(root, (n) => /(^| )ss-rungsel-option( |$)/.test(n.className || ''));
  eq(cards.map((c) => c.tagName), ['LABEL', 'LABEL', 'LABEL', 'LABEL']);
  ['rung1', 'rung2', 'placebo', 'rung3'].forEach((r, i) => {
    const t = allText(cards[i]).toLowerCase();
    assert.ok(t.indexOf(EN['views.rung.' + r].toLowerCase()) >= 0, r + ' carries the buttons\' name');
    assert.ok(t.indexOf(EN['ledger.card.' + r + '.title'].toLowerCase()) >= 0, r + ' title');
    const desc = EN['ledger.card.' + r + '.desc'];
    assert.ok(t.indexOf(desc.toLowerCase()) >= 0 && EN['ledger.rung.' + r].toLowerCase().indexOf(desc.toLowerCase()) >= 0, r + ' desc');
  });
  // choosing a rung sets the shared state, and only the rung
  const seen = [];
  SS.bus.on('state:change', (p) => seen.push(p));
  radios[3].checked = true;
  fire(radios[3], 'change', { target: radios[3] });
  assert.strictEqual(SS.state.get().rung, 'rung3');
  eq(seen.map((p) => p.changed), [['rung']]);
});

test('selector_only_rungs_present', () => {
  const env = boot();
  env.view.rungs = ['rung1', 'rung3'];
  env.SS.state.use(clone(env.view));
  const root = mount(env, stateOf(env), 'renderSelector');
  const radios = find(root, (n) => n.tagName === 'INPUT' && n.attributes.type === 'radio');
  eq(radios.map((r) => r.attributes.value), ['rung1', 'rung3']);
});

test('selector_says_only_the_ai_map_changes', () => {
  const env = boot();
  const text = allText(mount(env, stateOf(env), 'renderSelector'));
  assert.ok(text.indexOf(EN['ledger.selector.note']) >= 0);
  assert.ok(EN['ledger.selector.note'].length > 10);
});

// ---------------------------------------------------------------- table

test('table_is_a_real_table_with_scopes', () => {
  const env = boot();
  const root = mount(env, stateOf(env, { rung: 'rung2' }));
  assert.strictEqual(tag(root, 'table').length, 1);
  assert.strictEqual(tag(root, 'caption').length, 1);
  const ths = tag(root, 'th');
  const cols = ths.filter((t) => t.attributes.scope === 'col');
  const rowsH = ths.filter((t) => t.attributes.scope === 'row');
  // step + no-interview + 4 rungs
  assert.strictEqual(cols.length, 6);
  assert.strictEqual(rowsH.length, 4, 'one row header per missing step of MINI-A');
  eq(rowsH.map((t) => t.textContent), ['Pre-application meeting', 'Fire safety check', 'Neighbour consultation', 'Street parking approval']);
  assert.strictEqual(tag(root, 'tbody').length, 1);
});

test('table_title_and_headers_wording', () => {
  const env = boot();
  const root = mount(env, stateOf(env));
  const cap = tag(root, 'caption')[0].textContent;
  assert.strictEqual(cap, 'Wished-for steps under each set of instructions');  // council 30
  assert.ok(!/ledger/i.test(allText(root)), 'the word ledger is the profile row, not this table');
  const heads = tag(root, 'th').filter((t) => t.attributes.scope === 'col').map((t) => t.textContent);
  assert.ok(heads.indexOf('The same model, without the interview') >= 0);
  // rung headers reuse the selector's rung labels (2.2 issue 2), in display order, placebo before rung 3
  const rungHeads = heads.slice(2);
  eq(rungHeads, [EN['ledger.rung.rung1'], EN['ledger.rung.rung2'], EN['ledger.rung.placebo'], EN['ledger.rung.rung3']]);
  // the visible wording never says "script"
  assert.ok(!/\bscript\b/i.test(allText(root)));
});

test('cells_read_as_words_not_codes', () => {
  const env = boot();
  const text = allText(mount(env, stateOf(env)));
  [EN['ledger.script.has'], EN['ledger.script.lacks'], EN['ledger.script.unknown'],
    EN['ledger.cell.kept'], EN['ledger.cell.leaked'], EN['ledger.cell.never_drawn']].forEach((s) => {
    assert.ok(s && text.indexOf(s) >= 0, s);
  });
  ['kept', 'never_drawn', 'not_run'].forEach((code) => {
    assert.ok(text.indexOf(code) < 0, 'code ' + code + ' must not be shown');
  });
  // MINI-A has a missing-rung-free fate set, MINI-B shows "not run" for rung 3
  const envB = boot();
  const textB = allText(mount(envB, stateOf(envB, { map: 'MINI-B' })));
  assert.ok(textB.indexOf(EN['ledger.cell.not_run']) >= 0);
});

test('unattested_reads_not_attested_by_its_own_quote', () => {
  const env = boot();
  env.view.maps[0].items[0].fate.rung2 = 'unattested';
  const text = allText(mount(env, stateOf(env)));
  assert.ok(text.indexOf('Not attested by its own quote') >= 0);
  assert.ok(!/no support in the transcript/i.test(text));
});

test('shape_and_word_never_colour_alone', () => {
  const env = boot();
  const root = mount(env, stateOf(env));
  const leaked = find(root, (n) => /\bss-mk-leaked\b/.test(n.className || ''));
  assert.ok(leaked.length >= 1);
  leaked.forEach((n) => {
    assert.ok(allText(n).indexOf('▲') >= 0, 'triangle glyph on a leaked cell');
    assert.ok(allText(n).indexOf(EN['ledger.cell.leaked']) >= 0, 'and its words');
  });
});

test('click_cell_emits_item_open', () => {
  const env = boot();
  const { SS } = env;
  const root = mount(env, stateOf(env, { rung: 'rung2' }));
  const events = [];
  SS.bus.on('item:open', (p) => events.push(clone(p)));
  const cells = find(root, (n) => n.tagName === 'BUTTON' && n.attributes['data-item-id'] === 'a1');
  // no-interview cell + 4 rungs, all with something to open
  assert.strictEqual(cells.length, 5);
  const r3 = cells.find((b) => b.attributes['data-rung'] === 'rung3');
  fire(r3, 'click', {});
  eq(events, [{ item_id: 'a1', map: 'MINI-A', rung: 'rung3', lane: 'ai' }]);
  const sc = cells.find((b) => b.attributes['data-lane'] === 'script');
  fire(sc, 'click', {});
  assert.strictEqual(events[1].lane, 'script');
  assert.strictEqual(events[1].item_id, 'a1');
  // every cell button names the step, the column and the value
  cells.forEach((b) => assert.ok(b.attributes['aria-label'].indexOf('Pre-application meeting') === 0, b.attributes['aria-label']));
});

test('not_run_and_unknown_cells_have_nothing_to_open', () => {
  const env = boot();
  const root = mount(env, stateOf(env, { map: 'MINI-B' }));
  const b = find(root, (n) => n.tagName === 'BUTTON' && n.attributes['data-item-id'] === 'b3');
  assert.ok(!b.some((x) => x.attributes['data-rung'] === 'rung3'), 'rung 3 was not run');
  const rootA = mount(env, stateOf(env, { map: 'MINI-A' }));
  const a7 = find(rootA, (n) => n.tagName === 'BUTTON' && n.attributes['data-item-id'] === 'a7');
  assert.ok(!a7.some((x) => x.attributes['data-lane'] === 'script'), 'script_has is null: nothing to open');
});

test('selected_rung_and_item_are_marked', () => {
  const env = boot();
  const root = mount(env, stateOf(env, { rung: 'rung2', item: 'a4' }));
  const cur = find(root, (n) => n.attributes['aria-current'] === 'true');
  assert.ok(cur.length >= 1);
  assert.ok(cur.some((n) => n.tagName === 'TH' && n.attributes.scope === 'col' && n.textContent === EN['ledger.rung.rung2']));
  assert.ok(cur.some((n) => n.tagName === 'TH' && n.attributes.scope === 'row' && n.textContent === 'Fire safety check'));
});

test('mode_b_title_uses_researcher_wording', () => {
  const env = boot();
  env.view.reference_type = 'B';
  const root = mount(env, stateOf(env));
  const cap = tag(root, 'caption')[0].textContent;
  assert.strictEqual(cap, EN['ledger.title.mode_b']);
  assert.ok(/researcher-marked absences/i.test(cap));
  assert.notStrictEqual(cap, EN['ledger.title']);
});

test('empty_map_says_so', () => {
  const env = boot();
  const v = clone(VIEW);
  v.maps[0].items.forEach((i) => { i.reference_status = 'happened'; });
  env.view = v;
  const root = mount(env, stateOf(env));
  assert.strictEqual(tag(root, 'table').length, 0);
  assert.ok(allText(root).indexOf(EN['ledger.empty']) >= 0);
});

test('mode_c_says_there_is_no_reference_for_absences', () => {
  // R10: with no reference there is no map to hold absences; the line must not claim a reference has none.
  const env = boot();
  const v = clone(VIEW);
  v.reference_type = 'C';
  v.maps[0].items.forEach((i) => { i.reference_status = 'happened'; });
  env.view = v;
  const root = mount(env, stateOf(env));
  assert.strictEqual(tag(root, 'table').length, 0);
  const text = allText(root);
  assert.ok(text.indexOf(EN['ledger.empty.mode_c']) >= 0, 'mode C gets its own line');
  assert.strictEqual(EN['ledger.empty.mode_c'], 'No reference for absences with this reference type.');  // council 30
  assert.strictEqual(text.indexOf(EN['ledger.empty']), -1, 'mode C never says a reference has no absences');
  assert.ok(text.indexOf(EN['ledger.title']) >= 0, 'the panel title stays');
});

test('modes_a_and_b_keep_the_no_missing_step_line', () => {
  ['A', 'B'].forEach((t) => {
    const env = boot();
    const v = clone(VIEW);
    v.reference_type = t;
    v.maps[0].items.forEach((i) => { i.reference_status = 'happened'; });
    env.view = v;
    const text = allText(mount(env, stateOf(env)));
    assert.ok(text.indexOf(EN['ledger.empty']) >= 0, t);
    assert.strictEqual(text.indexOf(EN['ledger.empty.mode_c']), -1, t);
  });
});

test('render_replaces_previous_content_and_logs_no_missing_strings', () => {
  const env = boot();
  const root = env.window.document.createElement('div');
  env.SS.ledger.render(root, clone(env.view), stateOf(env));
  env.SS.ledger.render(root, clone(env.view), stateOf(env, { rung: 'rung3' }));
  assert.strictEqual(tag(root, 'table').length, 1, 'rendering twice leaves one table');
  assert.deepStrictEqual(env.errors, [], 'every key used exists in en.json');
});

test('bad_input_never_throws', () => {
  const env = boot();
  const root = env.window.document.createElement('div');
  assert.doesNotThrow(() => env.SS.ledger.render(root, null, null));
  assert.doesNotThrow(() => env.SS.ledger.render(root, {}, {}));
  assert.doesNotThrow(() => env.SS.ledger.renderSelector(root, null, null));
});

test('issue2_one_rung_one_name_selector_button_and_caption_agree', () => {
  // "Rung 3" on the Show-the-steps button; item 19 (23 Oct): the selector, the table and the stamp give the plain name,
  // which still carries the short one ("A remedy with two prompting strategies of Zhou et al. 2023 (rung 3)")
  ['rung1', 'rung2', 'placebo', 'rung3'].forEach((r) => {
    const short = EN['views.rung.' + r];
    const long = EN['ledger.rung.' + r];
    assert.ok(long.toLowerCase().includes(short.toLowerCase()), `${long} carries the name the buttons use (${short})`);
  });
  assert.strictEqual(EN['ledger.rung.rung1'], "The authors' original draft (rung 1)");
  assert.strictEqual(EN['ledger.rung.rung2'], "The study's instructions (rung 2)");
  assert.strictEqual(EN['ledger.rung.placebo'], 'A placebo matched to the remedy in length and format');
  assert.strictEqual(EN['ledger.rung.rung3'], 'A remedy with two prompting strategies of Zhou et al. 2023 (rung 3)');
  assert.strictEqual(EN['ledger.selector.legend'], 'Instructions given to the AI');
  const env = boot();
  const root = mount(env, stateOf(env));
  const labels = tag(root, 'span').filter((n) => /(^| )ss-rungsel-text( |$)/.test(n.attributes.class || '')).map((n) => n.textContent);
  assert.ok(labels.length === 0 || labels.indexOf('remedy prompt') < 0, 'no bare "remedy prompt" label is left');
});
