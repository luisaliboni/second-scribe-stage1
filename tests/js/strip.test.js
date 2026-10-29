'use strict';
// Plan V11 (replaces T26): SS.strip.drill, the drill-down of profile row 5 (absences) and row 8 (instruction,
// descriptive) in three modes (dots, bars, table). It draws profile cells only and computes nothing; every
// number it prints is the number the renderer in profile.js prints for the same cell.
// Fixtures: tests/fixtures/profiles/profile-v2.json (reported) and profile-synth.json (synthetic).
const test = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const { load, ROOT } = require('./harness');

const FIX = path.join(ROOT, 'tests', 'fixtures', 'profiles');
const V2 = JSON.parse(fs.readFileSync(path.join(FIX, 'profile-v2.json'), 'utf8'));
const SYNTH = JSON.parse(fs.readFileSync(path.join(FIX, 'profile-synth.json'), 'utf8'));
const EN = JSON.parse(fs.readFileSync(path.join(ROOT, 'second_scribe', 'app', 'strings', 'en.json'), 'utf8'));
const clone = (o) => JSON.parse(JSON.stringify(o));
const MODES = ['dots', 'bars', 'table'];
const NOTE_B = "agreement between the AI and this researcher's reading; it does not measure distance from the participant, and it is not a member check";

function walk(node, fn) {
  fn(node);
  (node.children || []).forEach((c) => walk(c, fn));
}
function textOf(node) {
  const out = [];
  walk(node, (n) => { if (n.textContent) out.push(n.textContent); });
  return out.join(' ');
}
function attr(n, k) { return n.attributes ? n.attributes[k] : undefined; }
function find(root, pred) { const out = []; walk(root, (n) => { if (pred(n)) out.push(n); }); return out; }
function byAttr(root, k, v) { return find(root, (n) => attr(n, k) !== undefined && (v === undefined || attr(n, k) === v)); }
function part(node, name) {
  const hit = find(node, (n) => attr(n, 'data-part') === name);
  return hit.length ? textOf(hit[0]) : undefined;
}
function rowOf(doc, key) { return doc.rows.find((r) => r.row === key); }
function xOf(c) { return c.x === undefined || c.x === null ? '' : c.x; }
function keyOf(rowKey, c) { return rowKey + '|' + c.name + '|' + xOf(c); }

function env() {
  const e = load({ files: ['core.js', 'strip.js', 'profile.js'] });
  // capture listeners so a click can be simulated, and give nodes replaceChildren (the harness stub has neither)
  const make = e.window.document.createElement;
  e.window.document.createElement = (tag) => {
    const n = make(tag);
    n.replaceChildren = () => { n.children.length = 0; };
    n.addEventListener = (t, fn) => { n.listeners = n.listeners || {}; n.listeners[t] = fn; };
    return n;
  };
  return e;
}
function drill(e, rowKey, row, opts) {
  const box = e.window.document.createElement('div');
  e.SS.strip.drill(rowKey, box, row, opts);
  return box;
}
// line key -> { value, ci, level, n, p } as printed
function printed(box) {
  const out = new Map();
  byAttr(box, 'data-line').forEach((n) => {
    const k = attr(n, 'data-line');
    assert.ok(!out.has(k), 'line printed once: ' + k);
    out.set(k, { value: part(n, 'value'), ci: part(n, 'ci'), level: part(n, 'level'), n: part(n, 'n'), p: part(n, 'p') });
  });
  return out;
}
const CASES = [['v2', V2], ['synth', SYNTH]];

// ------------------------------------------------------------------------------------------- tests
test('row8_rung_order', () => {
  const e = env();
  for (const mode of MODES) {
    const box = drill(e, 8, rowOf(V2, 8), { mode });
    for (const name of ['ai_per_map', 'size_adjusted_recall']) {
      const xs = byAttr(box, 'data-line').map((n) => attr(n, 'data-line').split('|'))
        .filter((p) => p[1] === name).map((p) => p[2]);
      assert.deepStrictEqual(xs, ['rung1', 'rung2', 'placebo', 'remedy'], mode + ' ' + name);
    }
  }
  // a file that names the third rung "rung3" keeps the same slot: placebo directly before it
  const row = clone(rowOf(V2, 8));
  row.cells.forEach((c) => { if (c.x === 'remedy' && /^(ai_per_map|size_adjusted_recall)$/.test(c.name)) c.x = 'rung3'; });
  row.cells.reverse();  // file order must not matter for the rung order
  const xs = byAttr(drill(e, 8, row), 'data-line').map((n) => attr(n, 'data-line').split('|'))
    .filter((p) => p[1] === 'ai_per_map').map((p) => p[2]);
  assert.deepStrictEqual(xs, ['rung1', 'rung2', 'placebo', 'rung3']);
  // the model carries the same order
  const m = e.SS.strip.model(8, rowOf(V2, 8));
  assert.deepStrictEqual(JSON.parse(JSON.stringify(m.panels.map((p) => p.name))), ['ai_per_map', 'size_adjusted_recall', 'leak_rate']);
});

test('table_mode_same_numbers_as_dots', () => {
  const e = env();
  for (const [label, doc] of CASES) {
    for (const rowKey of [5, 8]) {
      const row = rowOf(doc, rowKey);
      const expected = new Map();
      row.cells.filter((c) => c.derived !== true).forEach((c) => expected.set(keyOf(rowKey, c), c));
      const per = {};
      for (const mode of MODES) {
        per[mode] = printed(drill(e, rowKey, row, { mode }));
        assert.deepStrictEqual([...per[mode].keys()].sort(), [...expected.keys()].sort(), label + ' ' + rowKey + ' ' + mode + ' covers every cell once');
      }
      for (const mode of ['bars', 'table']) {
        for (const [k, v] of per.dots) assert.deepStrictEqual(per[mode].get(k), v, label + ' ' + rowKey + ' ' + mode + ' ' + k);
      }
      // and the printed value is the number the profile renderer prints for that cell
      for (const [k, c] of expected) {
        if (c.state === 'not_run' || typeof c.value !== 'number') continue;
        assert.strictEqual(per.dots.get(k).value, e.SS.profile.fmt.value(c), k);
        if (typeof c.ci_lo === 'number') assert.strictEqual(per.dots.get(k).ci, e.SS.profile.fmt.ciOf(c), k);
      }
    }
  }
  assert.strictEqual(find(drill(e, 8, rowOf(V2, 8), { mode: 'table' }), (n) => n.tagName === 'TABLE').length >= 1, true);
});

test('hover_text_value_ci_n_unit_in_words', () => {
  const e = env();
  const box = drill(e, 8, rowOf(V2, 8), { mode: 'dots' });
  const line = byAttr(box, 'data-line', '8|size_adjusted_recall|rung1')[0];
  const mark = find(line, (n) => attr(n, 'data-part') === 'mark')[0];
  assert.ok(mark, 'a dot');
  assert.strictEqual(attr(mark, 'tabindex'), '0', 'focusable');
  const tip = textOf(find(line, (n) => attr(n, 'data-part') === 'tip')[0]);
  // 2.6 part 5c: a share prints as a percentage, as in the profile panels
  for (const s of ['60.9%', '[56.1%, 65.9%]', '95% interval', '809 endorsed steps']) assert.ok(tip.includes(s), s + ' in: ' + tip);
  assert.ok((attr(mark, 'aria-label') || '').includes('60.9%'), 'label for assistive tech');
});

test('contrasts_printed_as_given', () => {
  const e = env();
  for (const mode of MODES) {
    const box = drill(e, 8, rowOf(V2, 8), { mode });
    const c = byAttr(box, 'data-line', '8|size_adjusted_recall_2_minus_1|rung2-rung1')[0];
    assert.strictEqual(part(c, 'value'), '−6.9 points');
    assert.strictEqual(part(c, 'ci'), '[−12.7, −1.3] points');
    assert.ok(part(c, 'p').includes('p = .036') && part(c, 'p').includes('Holm-adjusted p = .072'), part(c, 'p'));
    const lc = byAttr(box, 'data-line', '8|leak_rate_remedy_minus_placebo|remedy-placebo')[0];
    assert.strictEqual(part(lc, 'value'), '−2.2 points');
    assert.ok(part(lc, 'level').includes('95%'));
    assert.ok(part(byAttr(box, 'data-line', '8|leak_rate_remedy_minus_placebo_90|remedy-placebo')[0], 'level').includes('90%'));
    // MDE80 prints as a magnitude (V4: no plus sign), in the same block as the contrasts
    const mde = byAttr(box, 'data-line', '8|mde80_leak_contrast|remedy-placebo')[0];
    assert.strictEqual(part(mde, 'value'), '7.4 points');
    assert.strictEqual(part(mde, 'ci'), undefined);
    assert.ok(textOf(mde).includes('MDE₈₀'));
    const block = byAttr(box, 'data-group', 'contrasts')[0];
    assert.ok(byAttr(block, 'data-line', '8|mde80_leak_contrast|remedy-placebo').length === 1);
    assert.ok(byAttr(block, 'data-line', '8|size_adjusted_recall_2_minus_1|rung2-rung1').length === 1);
    assert.ok(byAttr(block, 'data-line', '8|ai_per_map|rung1').length === 0, 'rung cells are not contrasts');
  }
  // the remedy-minus-placebo leak contrast lives only inside row 8
  assert.strictEqual(byAttr(drill(e, 5, rowOf(V2, 5)), 'data-line').filter((n) => /minus_placebo/.test(attr(n, 'data-line'))).length, 0);
});

test('no_verdict_wording', () => {
  const e = env();
  const bad = /rules? out|ruled out|verdict|works/i;
  const texts = [];
  for (const [, doc] of CASES) for (const rowKey of [5, 8]) for (const mode of MODES) for (const ref of ['A', 'B']) {
    texts.push(textOf(drill(e, rowKey, rowOf(doc, rowKey), { mode, referenceType: ref })));
  }
  texts.forEach((t) => assert.ok(!bad.test(t), 'rendered text: ' + (bad.exec(t) || [])[0]));
  const own = Object.entries(EN).filter(([k]) => k.startsWith('strip.'));
  assert.ok(own.length > 10, 'strip.* strings exist');
  own.forEach(([k, v]) => assert.ok(!bad.test(v), k + ': ' + v));
  // no headline: the drill carries no h1/h2/h3 and no element that states an outcome
  texts.forEach((t) => assert.ok(!/\b(significant|no effect|no difference|confirm|proves?)\b/i.test(t), t.slice(0, 80)));
  const box = drill(e, 8, rowOf(V2, 8));
  assert.strictEqual(find(box, (n) => /^H[123]$/.test(n.tagName)).length, 0);
  assert.strictEqual(e.errors.length, 0, 'every string key exists: ' + e.errors.join('; '));
});

test('not_run_and_missing_text', () => {
  const e = env();
  // the synthetic parity file has a not_run cell in row 5 (second judge family)
  for (const mode of MODES) {
    const box = drill(e, 5, rowOf(SYNTH, 5), { mode });
    const line = byAttr(box, 'data-line', '5|matcher_free_rate|judge2')[0];
    assert.ok(line, 'the not_run cell has a line');
    assert.strictEqual(part(line, 'value'), 'not run in this profile', mode);
    assert.strictEqual(find(line, (n) => attr(n, 'data-part') === 'mark').length, 0, 'no dot for a cell that did not run');
  }
  const row = clone(rowOf(V2, 8));
  const a = row.cells.find((c) => c.name === 'ai_per_map' && c.x === 'rung1');
  const b = row.cells.find((c) => c.name === 'ai_per_map' && c.x === 'rung2');
  const d = row.cells.find((c) => c.name === 'ai_per_map' && c.x === 'placebo');
  delete a.value; delete a.ci_lo; delete a.ci_hi;
  b.value = null;
  d.state = 'not_run'; d.needs = ['j2']; delete d.value;
  for (const mode of MODES) {
    const box = drill(e, 8, row, { mode });
    const la = byAttr(box, 'data-line', '8|ai_per_map|rung1')[0];
    const lb = byAttr(box, 'data-line', '8|ai_per_map|rung2')[0];
    const ld = byAttr(box, 'data-line', '8|ai_per_map|placebo')[0];
    assert.strictEqual(part(la, 'value'), 'not computed', mode);
    assert.strictEqual(part(lb, 'value'), 'not computed', mode);
    assert.ok(part(ld, 'value').startsWith('not run in this profile (needs: '), part(ld, 'value'));
    [la, lb, ld].forEach((l) => {
      assert.ok(!/(^|\s)0(\s|$)/.test(textOf(l)), 'never 0: ' + textOf(l));
      assert.strictEqual(find(l, (n) => attr(n, 'data-part') === 'mark').length, 0);
    });
  }
  // a row with no cells says so instead of printing nothing or a zero
  const empty = drill(e, 5, { row: 5, cells: [] });
  assert.ok(textOf(empty).includes('not computed'));
  assert.doesNotThrow(() => drill(e, 8, undefined));
});

test('mode_b_wording', () => {
  const e = env();
  for (const rowKey of [5, 8]) {
    const b = textOf(drill(e, rowKey, rowOf(V2, rowKey), { referenceType: 'B' }));
    assert.ok(b.toLowerCase().includes('researcher-marked absences'), rowKey + ': ' + b.slice(0, 120));
    assert.strictEqual(b.split(NOTE_B).length - 1, 1, 'the fixed note, once');
    assert.ok(!/\bleak/i.test(b), 'no "leak" in mode B');
    const a = textOf(drill(e, rowKey, rowOf(V2, rowKey), { referenceType: 'A' }));
    assert.ok(!a.includes(NOTE_B) && !/researcher/i.test(a), 'reference A carries neither');
    const quiet = textOf(drill(e, rowKey, rowOf(V2, rowKey), { referenceType: 'B', note: false }));
    assert.ok(!quiet.includes(NOTE_B), 'the viewer can print the note once per screen itself');
    assert.ok(quiet.toLowerCase().includes('researcher-marked absences'));
  }
  assert.strictEqual(EN['strip.mode_b_note'], NOTE_B);
  assert.strictEqual(e.errors.length, 0, e.errors.join('; '));
});

test('mode_buttons_switch_between_the_three_modes', () => {
  const e = env();
  const box = drill(e, 8, rowOf(V2, 8));
  const buttons = byAttr(box, 'data-mode').filter((n) => n.tagName === 'BUTTON');
  assert.deepStrictEqual(buttons.map((b) => attr(b, 'data-mode')), MODES);
  assert.deepStrictEqual(buttons.map((b) => attr(b, 'aria-pressed')), ['true', 'false', 'false']);
  const before = printed(box);
  buttons[2].listeners.click();
  assert.deepStrictEqual(byAttr(box, 'data-mode').filter((n) => n.tagName === 'BUTTON').map((b) => attr(b, 'aria-pressed')), ['false', 'false', 'true']);
  assert.ok(find(box, (n) => n.tagName === 'TABLE').length >= 1, 'table after the click');
  assert.deepStrictEqual([...printed(box).entries()], [...before.entries()], 'same numbers after the switch');
  assert.strictEqual(attr(byAttr(box, 'data-drill-mode')[0], 'data-drill-mode'), 'table');
  // unknown mode falls back to dots
  assert.strictEqual(attr(byAttr(drill(e, 8, rowOf(V2, 8), { mode: 'pie' }), 'data-drill-mode')[0], 'data-drill-mode'), 'dots');
});

test('drill_hook_renders_for_profile_v2_rows_5_and_8', () => {
  const e = env();
  const root = e.window.document.createElement('div');
  e.SS.profile.render(root, V2, { drill: e.SS.strip.drill });
  for (const k of ['5', '8']) {
    const d = byAttr(root, 'data-drill', k);
    assert.strictEqual(d.length, 1, 'one drill for row ' + k);
    assert.ok(byAttr(d[0], 'data-line').length > 5, 'lines drawn in row ' + k);
  }
  assert.strictEqual(byAttr(root, 'data-drill').length, 2, 'no other row gets a drill');
  // the profile's own cells are still printed exactly once (the drill uses data-line, not data-cell)
  const cells = byAttr(root, 'data-cell');
  const keys = cells.map((n) => attr(n, 'data-cell'));
  assert.strictEqual(new Set(keys).size, keys.length);
  // opts reach the hook
  const root2 = e.window.document.createElement('div');
  e.SS.profile.render(root2, V2, { drill: e.SS.strip.drill, referenceType: 'B', mode: 'table' });
  const d5 = byAttr(root2, 'data-drill', '5')[0];
  assert.ok(textOf(d5).toLowerCase().includes('researcher-marked absences'));
  assert.strictEqual(attr(byAttr(d5, 'data-drill-mode')[0], 'data-drill-mode'), 'table');
  // the parity file renders as well
  const root3 = e.window.document.createElement('div');
  assert.doesNotThrow(() => e.SS.profile.render(root3, SYNTH, { drill: e.SS.strip.drill }));
  assert.strictEqual(byAttr(root3, 'data-drill').length, 2);
  assert.strictEqual(e.errors.length, 0, e.errors.join('; '));
});

test('derived_lines_stay_beside_the_lead', () => {
  const e = env();
  const row = clone(rowOf(V2, 5));
  row.cells.push({ name: 'recovery_rate', x: 'derived_probe', unit: 'ideal', kind: 'rate', value: 0.5, derived: true, qualifiers: ['not_a_test'] });
  const keys = [...printed(drill(e, 5, row)).keys()];
  assert.ok(!keys.some((k) => k.includes('derived_probe')), 'a derived:true cell is not drawn here');
  assert.ok(keys.includes('5|recovery_rate|primary'));
});

test('draws_cells_only_never_view_numbers', () => {
  const e = env();
  const src = fs.readFileSync(path.join(ROOT, 'second_scribe', 'app', 'static', 'js', 'strip.js'), 'utf8');
  assert.ok(!/\bnumbers\b\s*[.\[]/.test(src.replace(/\/\*[\s\S]*?\*\//g, '').replace(/\/\/.*$/gm, '')), 'does not read view.numbers');
  // a value no cell carries never appears: shift one cell and watch the same number move
  const row = clone(rowOf(V2, 8));
  row.cells.find((c) => c.name === 'size_adjusted_recall' && c.x === 'rung1').value = 0.777;
  const box = drill(e, 8, row);
  assert.strictEqual(part(byAttr(box, 'data-line', '8|size_adjusted_recall|rung1')[0], 'value'), '77.7%');
});

// Writes static/dev/strip.html from the fixtures and en.json (dev only, never shipped):
//   SS_WRITE_STRIP_DEV_PAGE=1 node --test tests/js/strip.test.js
function devPage() {
  const js = (v, o) => 'window.' + v + ' = ' + JSON.stringify(o).replace(/<\//g, '<\\/').replace(/<!--/g, '\\u003c!--') + ';\n';
  const strings = {};
  for (const [k, v] of Object.entries(EN)) if (/^(profile|common|band|lane|strip)\./.test(k)) strings[k] = v;
  const slim = (d) => Object.assign({}, d, { rows: d.rows.filter((r) => r.row === 5 || r.row === 8) });
  return `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Strip dev page</title>
<link rel="stylesheet" href="../css/fonts.css">
<link rel="stylesheet" href="../css/tokens.css">
<link rel="stylesheet" href="../css/base.css">
<link rel="stylesheet" href="../css/profile.css">
<link rel="stylesheet" href="../css/strip.css">
<style>
  /* dev page layout only */
  main { max-width: 1180px; margin: 0 auto; padding: 24px 16px; }
  .dev-row { background: var(--panel); border: 1px solid var(--border); border-radius: 6px; padding: 16px 18px; margin: 0 0 20px; }
</style>
<script>
// Generated by tests/js/strip.test.js (SS_WRITE_STRIP_DEV_PAGE=1). Do not edit by hand.
${js('SS_STRINGS', strings)}${js('SS_PROFILE_V2', slim(V2))}${js('SS_PROFILE_SYNTH', slim(SYNTH))}</script>
<script>
  // #theme=dark|light  #mode=dots|bars|table  #ref=B  #doc=synth  #view=profile (drill-downs inside the profile panel)
  if (/(^|[#&])theme=dark($|&)/.test(location.hash)) document.documentElement.setAttribute("data-theme", "dark");
  else if (/(^|[#&])theme=light($|&)/.test(location.hash)) document.documentElement.setAttribute("data-theme", "light");
</script>
</head>
<body>
<main>
  <h1>Strip dev page</h1>
  <p class="muted">Developer page for the profile drill-downs of rows 5 and 8. Fixtures from tests/fixtures/profiles. Not shipped.</p>
  <div id="out"></div>
</main>
<script src="../js/core.js"></script>
<script src="../js/strip.js"></script>
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
      f.setAttribute("title", "strip at " + w[1] + "px");
      f.style.cssText = "border:0;display:block;height:100vh;width:" + w[1] + "px";
      document.body.appendChild(f);
      return;
    }
    var has = function (re) { return re.test(location.hash); };
    var doc = has(/(^|[#&])doc=synth($|&)/) ? window.SS_PROFILE_SYNTH : window.SS_PROFILE_V2;
    var m = /(?:^|[#&])mode=(dots|bars|table)/.exec(location.hash);
    var opts = { mode: m ? m[1] : "dots", referenceType: has(/(^|[#&])ref=B($|&)/) ? "B" : "A", drill: SS.strip.drill };
    var out = document.getElementById("out");
    if (has(/(^|[#&])view=profile($|&)/)) {
      SS.profile.render(out, doc, opts);
    } else {
      doc.rows.forEach(function (row) {
        var box = document.createElement("section");
        box.setAttribute("class", "dev-row");
        var h = document.createElement("h2");
        h.textContent = "Row " + row.row;
        box.appendChild(h);
        var c = document.createElement("div");
        box.appendChild(c);
        out.appendChild(box);
        SS.strip.drill(row.row, c, row, opts);
      });
    }
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
if (process.env.SS_WRITE_STRIP_DEV_PAGE === '1') {
  fs.writeFileSync(path.join(ROOT, 'second_scribe', 'app', 'static', 'dev', 'strip.html'), devPage());
}

// audit fix C, V1: a cell whose `derived` is a named formula (row 5 "unanchored") is not a "not a test" line
test('V1_drill_down_prints_no_not_a_test_line_on_named_derived_cells', () => {
  const e = env();
  for (const mode of MODES) {
    const t = textOf(drill(e, 5, rowOf(V2, 5), { mode }));
    assert.ok(!t.includes('with no statistical test'), mode);
  }
});
