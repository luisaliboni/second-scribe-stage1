/* SS.profile: renders a profile file (schema orgsci-profile/v2, analysis/public/PROFILE-SCHEMA.md) as the results
   headline (plan amendment V3). It replaces the old numbers strip. Classic script; needs core.js only.

   SS.profile.render(root, doc, opts)
     rows in file order: lead, ledger, then rows 1 to 9; cells in array order.
     opts.gloss  (default true)  print the no-interview reading's gloss here; false when the page shows it already.
     opts.syntheticBand  (default true)  false: leave out the SYNTHETIC band of a synthetic file; the page prints it.
     opts.drill  function(rowKey, container, row, opts): fills the drill-down of row 5 (absences) and row 8 (instruction).
     opts.derived  (default true)  false: leave out the lead's derived "yield per box" lines (the app's Results and the
                   report, scout F9); every other cell is drawn.
     opts.compact  (default false)  the lead block only (the headline cell, the AI and no-interview cells, the sizes
                   beside it, the derived lines); everything else (the lead's floors and settings, the ledger, rows 1 to
                   9) goes into one closed <details> titled try.profile.full right after it. Same cells, each drawn
                   exactly once. Off, the output is what it was before the option existed (tests/js/profile-compact.test.js).
   SS.profile.lead(doc, opts) / SS.profile.renderLead(host, doc, opts): the plain sentences that open the app's
     Results and the report (scout F9), from the file's cells, as percentages and points.
   SS.profile.fmt.num(v, kind) / .ci(lo, hi, kind): number printing; shares and rates as percentages, differences
     between shares in percentage points (2.6, part 5c), every other kind as in RESULTS-PHASE1's profile table.

   Rules the tests pin (tests/js/profile.test.js):
   - The lead number has its sizes (cells tagged size_beside_lead) right beside it, always.
   - Derived lines (derived: true, not_a_test) sit only with the lead's sizes, never on their own, and every
     derived cell reads "arithmetic on reported numbers, not a test".
   - Nothing is computed here: no ratio, sum or difference between cells, no invented interval or bound.
   - not_run reads "not run in this profile (needs: ...)", "[SOURCE]" reads "not yet available", a missing
     value reads "not computed", never 0.
   Data reaches the DOM through textContent and attributes only. */
(function () {
  'use strict';
  var root = typeof window !== 'undefined' ? window : globalThis;
  var SS = root.SS = root.SS || {};
  var hasOwn = Object.prototype.hasOwnProperty;
  var PLACEHOLDER = '[SOURCE]';
  var MINUS = '−';
  var FLOORS = ['real_recall', 'shuffled_recall', 'wrong_transcript_difference', 'own_map_mean_rank',
    'rank_null_expectation'];
  var DRILL_ROWS = [5, 8];

  // String keys are lowercase [a-z0-9_] (tests/test_app_skeleton.py): codes such as "J2" or "rung2-rung1"
  // are looked up as "j2" and "rung2_rung1".
  function kp(code) { return String(code).toLowerCase().replace(/[^a-z0-9_]/g, '_'); }
  function has(key) {
    var table = root.SS_STRINGS;
    return table !== null && typeof table === 'object' && hasOwn.call(table, key) && typeof table[key] === 'string';
  }
  function isNum(v) { return typeof v === 'number' && isFinite(v); }
  function list(v) { return Array.isArray(v) ? v : []; }
  function hasQ(cell, q) { return list(cell.qualifiers).indexOf(q) >= 0; }
  function xOf(cell) { return cell.x === undefined || cell.x === null ? '' : String(cell.x); }

  // ------------------------------------------------------------------------------------------- numbers
  function group(digits) { return digits.replace(/\B(?=(\d{3})+(?!\d))/g, ','); }

  // 2.6 (part 5c): a reader sees percentages, in the style of SS.profile.lead. A share or a rate (display kinds
  // 'share' and 'rate') prints as a percentage with one decimal ("39.4%", interval "[36.1%, 42.7%]"); a difference
  // between two shares (display kind 'difference') prints in percentage points ("+24.1 points", interval
  // "[20.5, 27.7] points", band "6.8–7.3 points"). This is display scaling (x 100) of the cell's own value only.
  // fmtKind decides the display kind of a cell from its kind and name (PROFILE-SCHEMA sections 3, 11).
  function tenth(v) { return (Math.round(Math.abs(v) * 1000) / 10).toFixed(1); }
  function scaled(v, signed) {
    var s = tenth(v);
    if (s === '0.0') return s;   // never "−0.0" or "+0.0"
    if (v < 0) return MINUS + s;
    return signed ? '+' + s : s;
  }
  function isPct(kind) { return kind === 'share' || kind === 'rate'; }
  // Every other value keeps the digits the file carries: a leading zero below 1 for the PAD3 kinds ("0.614"; council 30, it was ".614";
  // a p-value keeps ".036"), U+2212 for negatives, a
  // plus on a positive difference between counts ("+14.023"), integer parts grouped ("1,268"). Missing -> "not
  // computed". V3: bounds, ratios, means, indexes and differences between counts print to at least three decimals,
  // as the article does: the file is JSON, so a trailing zero cannot survive parsing. Counts, ranks, p-values and
  // whole numbers are never padded.
  var PAD3 = ['count_difference', 'upper_bound', 'ratio', 'mean', 'index', 'per_map'];
  function num(v, kind, unsigned) {
    if (v === PLACEHOLDER) return SS.t('profile.source_placeholder');
    if (!isNum(v)) return SS.t('common.not_computed');
    if (isPct(kind)) return scaled(v, false) + '%';
    if (kind === 'difference') return SS.t('profile.points', { v: scaled(v, !unsigned) });
    var a = Math.abs(v);
    if (kind === 'count') { a = Math.round(a); if (a === 0) v = 0; }   // display only: a count is printed whole
    var s = String(a);
    if (/e/i.test(s)) s = a.toFixed(12).replace(/0+$/, '').replace(/\.$/, '');
    var dot = s.indexOf('.');
    var ip = dot < 0 ? s : s.slice(0, dot);
    var fp = dot < 0 ? '' : s.slice(dot + 1);
    // 'per_map' (a count averaged over maps, see fmtKind) is padded even when whole: "19.000", like its neighbours
    if ((fp || kind === 'per_map') && v !== 0 && PAD3.indexOf(kind) >= 0) while (fp.length < 3) fp += '0';
    // council 30 (seat 03): a ratio, mean, index, bound or count difference below 1 keeps its leading zero ("0.614",
    // "+0.698"), so no reader meets a bare ".614"; a p-value and a value of no kind keep the article's ".036"
    ip = (ip === '0' && fp && PAD3.indexOf(kind) < 0) ? '' : group(ip);
    s = ip + (fp ? '.' + fp : '');
    if (v < 0) return MINUS + s;
    if (v > 0 && kind === 'count_difference' && !unsigned) return '+' + s;
    return s;
  }
  function ci(lo, hi, kind) {
    if (lo === PLACEHOLDER || hi === PLACEHOLDER) return SS.t('profile.source_placeholder');
    if (!isNum(lo) || !isNum(hi)) return SS.t('common.not_computed');
    if (kind === 'difference') return SS.t('profile.ci_points', { lo: scaled(lo, false), hi: scaled(hi, false) });
    return '[' + num(lo, kind, true) + ', ' + num(hi, kind, true) + ']';
  }
  // A cell's printed value. V4: a smallest detectable contrast (mde80_*) is a magnitude, so no plus sign.
  // 2.2 (display only, never the data): a count prints as a whole number, interval ends included ("304 [250, 354]",
  // not "304 [250.304, 354.474]"); a per-map mean typed as a count (ai_per_map) prints like any other mean, three
  // decimals for the point, both interval ends and the band ("19.000 [17.209, 20.860]"), so one panel never mixes
  // precisions.
  // The JSON drops a trailing zero, so the padding is only a restoration of the digits the file was written with.
  // 2.6 (part 5c), the display kind of a cell, decided from the schema and the cell's name:
  // - share, rate: percentages;
  // - difference: percentage points, except a difference between counts per map ("ai_minus_no_interview_nodes_per_map",
  //   "ai_nodes_remedy_minus_placebo"), which keeps its digits (display kind 'count_difference');
  // - upper_bound: the bound of a share ("counterfactual_upper_bound", 0 of 90) is a percentage; the bound of a
  //   difference between shares ("largest_excess_ruled_out", the upper end of an interval) is in points;
  // - R* (kind ratio) is the highest recall the matcher calibration allows, a share of steps: a percentage;
  // - Part 6 (the author's Part 5c leftover): the AI's boxes per endorsed box and the no-interview boxes over the AI's
  //   boxes read as percentages too, like the rest of their panels;
  // - ratio, mean, index, rank, count and the per-map counts keep their digits.
  var SHARE_NAMED_RATIO = ['r_star', 'ai_per_endorsed', 'no_interview_over_ai_nodes'];
  function isCountDifference(name) { return /_per_map$/.test(name) || /^ai_(nodes|edges)_/.test(name); }
  function fmtKind(c) {
    var name = String(c.name);
    if (c.kind === 'count' && /_per_map$/.test(name)) return 'per_map';
    if (c.kind === 'ratio' && SHARE_NAMED_RATIO.indexOf(name) >= 0) return 'share';
    if (c.kind === 'upper_bound') return /excess|difference|minus/.test(name) ? 'difference' : 'share';
    if (c.kind === 'difference' && isCountDifference(name)) return 'count_difference';
    return c.kind;
  }
  // V4: a smallest detectable contrast (mde80_*) is a magnitude, and an upper bound is an end: no plus sign.
  function valueOf(c) {
    var unsigned = typeof c.name === 'string' && (c.name.indexOf('mde80') === 0 || c.kind === 'upper_bound');
    return num(c.value, fmtKind(c), unsigned);
  }
  function ciOf(c) {
    return ci(c.ci_lo, c.ci_hi, fmtKind(c));
  }
  // V2: the level of an upper bound that carries an interval. ci_level is the interval's two-sided level; the
  // upper end of a 90% interval is a one-sided 95% bound: (1 + level) / 2. Both are computed from the file.
  function pct(x) { return String(Number((x * 100).toFixed(1))); }
  function levelOf(c, hasCi) {
    if (!isNum(c.ci_level) || !(hasCi || c.kind === 'upper_bound')) return undefined;
    if (hasCi && c.kind === 'upper_bound') {
      return SS.t('profile.ci_level_bound_interval', {
        bound: pct((1 + c.ci_level) / 2), level: pct(c.ci_level), interval: ciOf(c) });
    }
    return SS.t(c.kind === 'upper_bound' ? 'profile.ci_level_bound' : 'profile.ci_level', { level: pct(c.ci_level) });
  }
  function range(lo, hi, kind) {
    if (kind === 'difference' && isNum(lo) && isNum(hi)) {
      return SS.t('common.ci_points', { lo: scaled(lo, false), hi: scaled(hi, false) });
    }
    return num(lo, kind, true) + '–' + num(hi, kind, true);
  }
  SS.profile = { fmt: { num: num, ci: ci, ciOf: ciOf, range: range, value: valueOf, level: levelOf, kind: fmtKind } };

  // ------------------------------------------------------------------------------------------- DOM helpers
  function el(tag, cls, text, parent) {
    var n = root.document.createElement(tag);
    if (cls) n.setAttribute('class', cls);
    if (text !== undefined && text !== null) n.textContent = String(text);
    if (parent) parent.appendChild(n);
    return n;
  }
  function clear(node) {
    if (typeof node.replaceChildren === 'function') { node.replaceChildren(); return; }
    while (node.firstChild) node.removeChild(node.firstChild);
  }
  // A coded string (§13): the sentence from the merged strings when there is one, else the code itself, in mono.
  // Formula codes are whole keys in the kit's strings file ("yield_per_box:ai"), so the whole code is tried first;
  // reason and missing codes are looked up by the part before any ':' ("leak_split_indeterminate:3").
  function coded(prefix, code, parent) {
    var c = String(code);
    var base = c.split(':')[0];
    if (prefix === 'profile.formula.' && c !== base && has(prefix + c)) {
      return el('span', 'pf-coded', SS.t(prefix + c), parent);
    }
    if (has(prefix + base)) return el('span', 'pf-coded', SS.t(prefix + base), parent);
    if (has(prefix + kp(base))) return el('span', 'pf-coded', SS.t(prefix + kp(base)), parent);
    return el('span', 'mono pf-code', c, parent);
  }
  function xLabel(x) { return has('profile.x.' + kp(x)) ? SS.t('profile.x.' + kp(x)) : x; }
  function unitWord(u) { return has('profile.unit.' + kp(u)) ? SS.t('profile.unit.' + kp(u)) : String(u); }
  function cellLabel(labelKey, name) {
    var k = labelKey + '.cell.' + kp(name);
    return has(k) ? SS.t(k) : String(name).replace(/_/g, ' ');
  }
  // The same phrases for the drill-downs (strip.js), so a cell is named the same way everywhere.
  SS.profile.words = { cell: cellLabel, x: xLabel, unit: unitWord, has: has };

  // ------------------------------------------------------------------------------------------- parts of a cell
  function restsOn(codes, parent) {
    codes = list(codes);
    if (!codes.length) return null;
    var d = el('details', 'pf-rests', undefined, parent);
    d.setAttribute('data-part', 'rests_on');
    el('summary', '', SS.t('profile.rests_on.summary', { codes: codes.join(', ') }), d);
    var ul = el('ul', '', undefined, d);
    codes.forEach(function (c) { el('li', '', has('profile.rests_on.' + kp(c)) ? SS.t('profile.rests_on.' + kp(c)) : c, ul); });
    return d;
  }

  function qualifierChips(codes, parent) {
    if (!codes.length) return null;
    var ul = el('ul', 'pf-quals', undefined, parent);
    ul.setAttribute('data-part', 'qualifiers');
    codes.forEach(function (q) {
      var li = el('li', 'pf-qual', undefined, ul);
      li.setAttribute('data-qualifier', q);
      coded('profile.qualifier.', q, li);
    });
    return ul;
  }

  function band(b, kind, parent) {
    if (!b || typeof b !== 'object') return null;
    var box = el('div', 'pf-band', undefined, parent);
    box.setAttribute('data-part', 'band');
    if (b.state === 'not_run') { el('span', '', SS.t('profile.band.not_run'), box); return box; }
    var line = [SS.t('profile.band', { range: range(b.lo, b.hi, kind) })];
    if (has('profile.band.over.' + kp(b.over))) line.push(SS.t('profile.band.over.' + kp(b.over)));
    if (b.narrow === true) line.push(SS.t('profile.band.narrow'));
    if (b.narrow === false) line.push(SS.t('profile.band.wide'));
    el('span', '', line.join(', '), box);
    var members = list(b.members);
    var subs = list(b.subbands);
    if (members.length || subs.length) {
      var d = el('details', 'pf-members', undefined, box);
      el('summary', '', SS.t('profile.band.members'), d);
      var ul = el('ul', '', undefined, d);
      members.forEach(function (m) {
        var v = (isNum(m.value_hi) && m.value_hi !== m.value) ? range(m.value, m.value_hi, kind) : num(m.value, kind, true);
        el('li', '', xLabel(m.x) + ': ' + v, ul);
      });
      subs.forEach(function (s) {
        var name = has('profile.band.sub.' + kp(s.name)) ? SS.t('profile.band.sub.' + kp(s.name)) : s.name;
        el('li', '', name + ': ' + range(s.lo, s.hi, kind), ul);
      });
    }
    return box;
  }

  function isBesideLead(cell) { return cell.derived === true || hasQ(cell, 'not_a_test'); }
  function isPooledOnly(cell) { return cell.note === 'pooled_only' || hasQ(cell, 'pooled_only'); }
  function isPartial(cell) { return cell.state === 'partial_not_run' || hasOwn.call(cell, 'partial_not_run'); }

  function zeroBound(row, cell) {
    var bounds = list(row.cells).filter(function (b) { return b && b.name === cell.name + '_upper_bound'; });
    var x = xOf(cell);
    var siblingsX = list(row.cells).filter(function (o) { return o && o.name === cell.name && o !== cell; })
      .map(xOf).filter(function (s) { return s; });
    var ends = function (b, s) { var bx = xOf(b); return s && bx.length > s.length && bx.slice(-(s.length + 1)) === '_' + s; };
    var mine = bounds.filter(function (b) { return ends(b, x); });
    if (!mine.length) {
      mine = bounds.filter(function (b) { return !siblingsX.some(function (s) { return ends(b, s); }); });
    }
    if (!mine.length) return SS.t('profile.zero.no_bound');
    return mine.map(function (b) {
      return SS.t('profile.zero.bound', { v: num(b.value, fmtKind(b), true), x: xLabel(xOf(b)), n: num(b.n, 'count') });
    }).join('; ');
  }

  // True when the string behind a formula code already ends with the derived line ("Arithmetic on reported numbers;
  // not a test."), compared without case or punctuation.
  function sentenceEndsNotATest(code) {
    var key = 'profile.formula.' + code;
    if (!has(key) && has('profile.formula.' + String(code).split(':')[0])) key = 'profile.formula.' + String(code).split(':')[0];
    if (!has(key)) return false;
    var norm = function (t) { return String(t).toLowerCase().replace(/[^a-z ]+/g, ' ').replace(/\s+/g, ' ').trim(); };
    var tail = norm(SS.t('profile.derived'));
    var sent = norm(SS.t(key));
    return sent.length >= tail.length && sent.slice(sent.length - tail.length) === tail;
  }
  function renderCell(rowKey, row, cell, parent, extraCls) {
    var c = cell || {};
    var key = String(rowKey) + '|' + c.name + '|' + xOf(c);
    var box = el('div', 'pf-cell' + (extraCls ? ' ' + extraCls : ''), undefined, parent);
    box.setAttribute('data-cell', key);
    if (typeof c.source === 'string') box.setAttribute('data-source', c.source);
    if (c.state) box.setAttribute('data-state', c.state);
    if (c.derived !== undefined) box.setAttribute('data-derived', isBesideLead(c) ? 'beside_lead' : 'named');

    var head = el('div', 'pf-label', undefined, box);
    el('span', 'pf-name', cellLabel(row.label_key, c.name), head);
    if (xOf(c)) el('span', 'pf-x', xLabel(xOf(c)), head);

    var nums = el('div', 'pf-nums', undefined, box);
    var notRun = c.state === 'not_run';
    var valueText;
    if (notRun) {
      var needs = list(c.needs);
      valueText = needs.length ?
        SS.t('profile.not_run_needs', { needs: needs.map(function (n) {
          return has('profile.needs.' + kp(n)) ? SS.t('profile.needs.' + kp(n)) : n;
        }).join(', ') }) :
        SS.t('profile.not_run');
    } else {
      valueText = hasOwn.call(c, 'value') ? valueOf(c) : SS.t('common.not_computed');
    }
    var value = el('span', 'pf-value', valueText, nums);
    value.setAttribute('data-part', 'value');

    var pooled = isPooledOnly(c);
    var hasCi = !notRun && !pooled && (hasOwn.call(c, 'ci_lo') || hasOwn.call(c, 'ci_hi'));
    var boundWithCi = hasCi && c.kind === 'upper_bound' && isNum(c.ci_level);
    if (hasCi && !boundWithCi) el('span', 'pf-ci', ciOf(c), nums).setAttribute('data-part', 'ci');
    var levelText = levelOf(c, hasCi);
    if (!notRun && levelText) el('span', 'pf-level', levelText, nums).setAttribute('data-part', 'ci_level');

    var meta = el('div', 'pf-meta', undefined, box);
    if (hasOwn.call(c, 'n') && c.n !== null) {
      var what = c.n === PLACEHOLDER ? SS.t('profile.source_placeholder') :
        SS.t(c.kind === 'count' ? 'profile.n.out_of' : 'profile.n.over',
          { n: num(c.n, 'count'), unit: c.unit ? unitWord(c.unit) : '' }).replace(/\s+$/, '');
      el('span', 'pf-n', what, meta).setAttribute('data-part', 'n');
    }
    var ps = [];
    if (isNum(c.p_lt)) ps.push(SS.t('profile.p_lt', { p: num(c.p_lt) }));
    else if (isNum(c.p)) ps.push(SS.t('profile.p', { p: num(c.p) }));
    if (isNum(c.p_holm_lt)) ps.push(SS.t('profile.p_holm_lt', { p: num(c.p_holm_lt) }));
    else if (isNum(c.p_holm)) ps.push(SS.t('profile.p_holm', { p: num(c.p_holm) }));
    if (ps.length) el('span', 'pf-p', ps.join(' · '), meta).setAttribute('data-part', 'p');

    var zero = !notRun && c.kind === 'count' && c.value === 0 && (hasQ(c, 'zero_count') || list(row.qualifiers).indexOf('zero_count') >= 0);
    if (zero) {
      box.setAttribute('data-zero', 'true');
      el('div', 'pf-bound', zeroBound(row, c), box).setAttribute('data-part', 'bound');
    }

    // V7: the sentence for a formula comes from the strings ("profile.formula.<name>:<x>", then "<name>"), never
    // from the file's free `formula`; that text is the fallback only when no string exists.
    var fcode = typeof c.formula_code === 'string' && c.formula_code ? c.formula_code : '';
    if (!fcode && typeof c.formula === 'string' && c.formula && c.name) {
      var withX = xOf(c) ? String(c.name) + ':' + xOf(c) : '';
      if (withX && has('profile.formula.' + withX)) fcode = withX;
      else if (has('profile.formula.' + String(c.name))) fcode = String(c.name);
    }
    // V1: "arithmetic on reported numbers, not a test" belongs to the lead's derived: true lines only (a cell whose
    // derived is a named formula shows its derived_arithmetic chip instead). The kit's sentence for a derived line
    // often ends with the same words; then the line is said once, there, and not printed again.
    if (isBesideLead(c) && !(fcode && sentenceEndsNotATest(fcode))) {
      el('div', 'pf-derived', SS.t('profile.derived'), box).setAttribute('data-part', 'derived');
    }
    if (fcode) {
      coded('profile.formula.', fcode, el('div', 'pf-formula', undefined, box));
    } else if (typeof c.formula === 'string' && c.formula) {
      el('div', 'pf-formula', SS.t('profile.formula', { formula: c.formula }), box);
    }
    // A note on a cell (kit exports): reason codes, several joined with ';' (profile_base.lab_string_problems).
    // "pooled_only" is not repeated here: it already reads as a qualifier phrase beside the value.
    if (typeof c.note === 'string' && c.note) {
      var noteCodes = c.note.split(';').filter(function (n) { return n && n !== 'pooled_only'; });
      if (noteCodes.length) {
        var nb = el('div', 'pf-reason pf-note', undefined, box);
        nb.setAttribute('data-part', 'note');
        noteCodes.forEach(function (n, i) {
          if (i) el('span', '', ' ', nb);
          coded('profile.reason.', n, nb);
        });
      }
    }
    if (notRun) {
      if (typeof c.reason_code === 'string' && c.reason_code) {
        coded('profile.reason.', c.reason_code, el('div', 'pf-reason', undefined, box));
      } else if (typeof c.reason === 'string' && c.reason) {
        // A free reason is a pack diagnostic (PROFILE-SCHEMA §13), not app copy: it can carry the kit's own
        // words (for example "script"), so it stays inspectable in the DOM and is not printed.
        box.setAttribute('data-reason', c.reason);
      }
    }
    if (isPartial(c)) {
      var pl = el('div', 'pf-partial', undefined, box);
      pl.setAttribute('data-part', 'partial');
      el('span', '', SS.t('profile.partial'), pl);
      var missing = list(c.missing);
      if (missing.length) {
        el('span', '', ': ', pl);
        missing.forEach(function (m, i) {
          if (i) el('span', '', ', ', pl);
          coded('profile.missing.', m, pl);
        });
      }
      if (c.state === 'partial_not_run' && typeof c.reason_code === 'string' && c.reason_code) {
        coded('profile.reason.', c.reason_code, el('div', 'pf-reason', undefined, box));
      }
    }

    if (c.band) band(c.band, fmtKind(c), box);

    var quals = list(c.qualifiers).filter(function (q) {
      if (q === 'size_beside_lead') return false;  // the group heading says it
      if (isBesideLead(c) && (q === 'not_a_test' || q === 'derived_arithmetic')) return false;  // derived line says it
      if (pooled && q === 'no_interval') return false;
      return true;
    });
    if (pooled && quals.indexOf('pooled_only') < 0) quals.push('pooled_only');
    qualifierChips(quals, box);
    restsOn(c.rests_on, box);
    return box;
  }

  // ------------------------------------------------------------------------------------------- rows
  // The number kind a row's own band prints with, so the same band reads the same in the header and on its cell
  // ("-.010-.003" both times, 2.2 issue 3): the kind of the cell that carries this very band; else, when the row holds
  // per-map counts, theirs; else the first cell whose kind is padded to three decimals; else none.
  // 2.6 (part 5c): a band with no carrier is often the range of one cell over its settings (the lead and row 7:
  // listening_added at primary and lenient). When one cell name holds both ends, the band prints in that cell's
  // kind (points there), checked before the per-map and padded rules.
  function rowBandKind(row) {
    var b = row.band;
    var cells = list(row.cells).filter(function (c) { return c && typeof c === 'object'; });
    var same = cells.filter(function (c) { return c.band && c.band.lo === b.lo && c.band.hi === b.hi; });
    if (same.length) return fmtKind(same[0]);
    var his = cells.filter(function (c) { return isNum(c.value) && c.value === b.hi; });
    var ends = cells.filter(function (c) {
      return isNum(c.value) && c.value === b.lo && his.some(function (h) { return h.name === c.name; });
    });
    if (ends.length) return fmtKind(ends[0]);
    var pm = cells.filter(function (c) { return fmtKind(c) === 'per_map'; });
    if (pm.length) return 'per_map';
    var padded = cells.filter(function (c) { return PAD3.indexOf(fmtKind(c)) >= 0 || isPct(fmtKind(c)) || fmtKind(c) === 'difference'; });
    return padded.length ? fmtKind(padded[0]) : null;
  }
  function rowHeader(row, section) {
    var h = el('header', 'pf-row-head', undefined, section);
    var tk = row.label_key + '.title';
    el('h3', 'pf-title', has(tk) ? SS.t(tk) : String(row.label_key), h);
    if (typeof row.population === 'string') {
      el('p', 'pf-pop', SS.t('profile.population', { label: row.population }), h);
    }
    qualifierChips(list(row.qualifiers), h);
    if (row.band) band(row.band, rowBandKind(row), h);
    restsOn(row.rests_on, h);
    return h;
  }

  function section(parent, row) {
    var s = el('section', 'pf-row pf-row-' + String(row.row), undefined, parent);
    s.setAttribute('data-row', String(row.row));
    rowHeader(row, s);
    return s;
  }

  function first(cells, name, x) {
    var hit = null;
    cells.forEach(function (c) { if (!hit && c.name === name && xOf(c) === x) hit = c; });
    if (!hit) cells.forEach(function (c) { if (!hit && c.name === name) hit = c; });
    return hit;
  }

  // more: where the lead's floors and settings go. Compact: a function that makes (once) a card inside the closed
  // <details>; otherwise the lead block itself, as always.
  function renderLead(parent, row, besides, opts, more) {
    var s = section(parent, row);
    var block = el('div', more ? 'pf-lead pf-lead-compact' : 'pf-lead', undefined, s);
    block.setAttribute('data-group', 'lead');
    var cells = list(row.cells).filter(function (c) { return c && !isBesideLead(c); });
    var used = [];
    var take = function (c) { if (c) used.push(c); return c; };

    var number = el('div', 'pf-lead-number', undefined, block);
    number.setAttribute('data-group', 'lead-number');
    el('p', 'pf-caption', SS.t('profile.lead.caption'), number);
    var main = take(first(cells, 'listening_added', 'primary')) || { name: 'listening_added', x: 'primary' };
    renderCell('lead', row, main, number, 'pf-main');
    var readings = el('div', 'pf-readings', undefined, number);
    var r1 = el('div', 'pf-reading pf-reading-ai', undefined, readings);
    el('p', 'pf-lane', SS.t('lane.ai'), r1);
    var ai = take(first(cells, 'ai_recall', 'primary'));
    if (ai) renderCell('lead', row, ai, r1);
    var r3 = el('div', 'pf-reading pf-reading-no-interview', undefined, readings);
    var lane = el('p', 'pf-lane', undefined, r3);
    el('span', '', SS.t('lane.no_interview'), lane);
    if (!opts || opts.gloss !== false) el('span', 'pf-gloss', SS.t('lane.no_interview_gloss'), lane);
    var ni = take(first(cells, 'no_interview_recall', 'primary'));
    if (ni) renderCell('lead', row, ni, r3);

    var sizes = el('div', 'pf-lead-sizes', undefined, block);
    sizes.setAttribute('data-group', 'lead-sizes');
    el('h4', '', SS.t('profile.lead.group.sizes'), sizes);
    var sizeCells = cells.filter(function (c) { return used.indexOf(c) < 0 && hasQ(c, 'size_beside_lead'); });
    var sizeList = el('div', 'pf-cells', undefined, sizes);
    sizeCells.forEach(function (c) { take(c); renderCell('lead', row, c, sizeList); });
    if (besides.length) {
      var der = el('div', 'pf-lead-derived', undefined, sizes);
      der.setAttribute('data-group', 'lead-derived');
      el('h4', '', SS.t('profile.lead.group.derived'), der);
      var derList = el('div', 'pf-cells', undefined, der);
      besides.forEach(function (b) { renderCell(b.rowKey, b.row, b.cell, derList); });
    }

    var floorCells = cells.filter(function (c) { return used.indexOf(c) < 0 && FLOORS.indexOf(c.name) >= 0; });
    var rest0 = cells.filter(function (c) { return used.indexOf(c) < 0 && FLOORS.indexOf(c.name) < 0; });
    var host = more && (floorCells.length || rest0.length) ? more() : block;
    if (floorCells.length) {
      var floors = el('div', 'pf-lead-floors', undefined, host);
      floors.setAttribute('data-group', 'lead-floors');
      el('h4', '', SS.t('profile.lead.group.floors'), floors);
      var fl = el('div', 'pf-cells', undefined, floors);
      floorCells.forEach(function (c) { take(c); renderCell('lead', row, c, fl); });
    }
    var rest = cells.filter(function (c) { return used.indexOf(c) < 0; });
    if (rest.length) {
      var other = el('div', 'pf-lead-settings', undefined, host);
      other.setAttribute('data-group', 'lead-settings');
      el('h4', '', SS.t('profile.lead.group.settings'), other);
      var ol = el('div', 'pf-cells', undefined, other);
      rest.forEach(function (c) { renderCell('lead', row, c, ol); });
    }
    return s;
  }

  // V8: the lead number never stands alone. Where it is repeated outside the lead block (row 7), the two map
  // sizes of the lead are printed right after it, from the file's own size cells.
  var SIZES_BESIDE = ['ai_nodes_per_map', 'no_interview_nodes_per_map'];
  function renderRow(parent, row, opts, sizeCells) {
    var s = section(parent, row);
    var wrap = el('div', 'pf-cells', undefined, s);
    list(row.cells).forEach(function (c) {
      if (!c || isBesideLead(c)) return;
      renderCell(row.row, row, c, wrap);
      if (c.name === 'listening_added' && xOf(c) === 'primary' && sizeCells.length) {
        var g = el('div', 'pf-beside-sizes', undefined, wrap);
        g.setAttribute('data-group', 'beside-sizes');
        el('h4', '', SS.t('profile.row.sizes_beside'), g);
        var gl = el('div', 'pf-cells', undefined, g);
        sizeCells.forEach(function (sc) { renderCell(row.row, sc.row, sc.cell, gl); });
      }
    });
    if (DRILL_ROWS.indexOf(row.row) >= 0 && opts && typeof opts.drill === 'function') {
      var d = el('div', 'pf-drill', undefined, s);
      d.setAttribute('data-drill', String(row.row));
      opts.drill(row.row, d, row, opts);
    }
    return s;
  }

  // audit fix D: the run footnote names each cloud role and its provider (run.remote_endpoints, PROFILE-SCHEMA 14.8),
  // from the kit's profile.role.* / profile.provider.* strings; never a host. A provider string is a sentence
  // ("An OpenAI-compatible cloud endpoint: ..."): the part before its colon names it, an article lower-cased.
  function roleName(code) {
    var k = 'profile.role.' + kp(code);
    return has(k) ? SS.t(k) : String(code);
  }
  function providerName(code) {
    var k = 'profile.provider.' + kp(code);
    if (!has(k)) return String(code);
    var short = SS.t(k).split(':')[0].trim().replace(/\.$/, '');
    return /^(A|An|The) /.test(short) ? short.charAt(0).toLowerCase() + short.slice(1) : short;
  }
  // PROFILE-SCHEMA section 14 (public form): a null field carries a code saying why. A table cell and a footnote line
  // want the short form, so the app's profile.run.reason.<code> ("available from the authors") is tried first, then the
  // kit's sentence profile.reason.<code>, which also covers a code the app has no short form for. null when neither
  // file knows the code, so no raw code and no "undefined" reaches the page.
  function runReason(code) {
    if (typeof code !== 'string' || !/^[a-z0-9_.-]{1,40}$/.test(code)) return null;
    var k = 'profile.run.reason.' + kp(code);
    if (has(k)) return SS.t(k);
    k = 'profile.reason.' + kp(code);
    return has(k) ? SS.t(k) : null;
  }
  SS.profile.runReason = runReason;
  // "Run chain heads: available from the authors" when the file holds no heads (study file, or a lab file that
  // withheld them) and names why in run_chain_heads_source.
  function chainHeadsLine(run) {
    if (!run || typeof run !== 'object' || run.run_chain_heads !== null) return null;
    var why = runReason(run.run_chain_heads_source);
    return why === null ? null : SS.t('profile.run.chain_heads', { reason: why });
  }
  function runFootnote(parent, run) {
    var eps = list(run && run.remote_endpoints).filter(function (e) { return e && typeof e === 'object'; });
    var heads = chainHeadsLine(run);
    if (!eps.length && heads === null) return null;
    var foot = el('footer', 'pf-run-foot', undefined, parent);
    foot.setAttribute('data-part', 'run-footnote');
    if (heads !== null) el('p', 'pf-run-heads', heads, foot).setAttribute('data-key', 'run.run_chain_heads_source');
    eps.forEach(function (e) {
      el('p', 'pf-run-remote', SS.t('profile.run.remote_endpoint', { role: roleName(e.role), provider: providerName(e.provider) }),
        foot).setAttribute('data-remote-role', String(e.role));
    });
    return foot;
  }

  // ------------------------------------------------------------------------------------------- the plain lead
  // SS.profile.lead(doc, opts) -> [{id, text, keys}]: three or four plain sentences built from the run's own profile
  // (scout F9, the author's items 9 to 11): what listening added, in points, with both percentages; the share of
  // endorsed steps left out; the size of the AI maps; the flattened ideals. Numbers are read from cells of the file
  // (never typed in, never computed here) and printed as percentages or points, never as ".547". A sentence whose
  // cells are not in the file, or were not run, says so in words. opts.judgeless (no judge from another family) and
  // opts.referenceType "C" (no reference map) give one sentence each instead. Mode B speaks of this researcher's map.
  function leadCell(doc, rowId, name, x) {
    var rows = list(doc && doc.rows);
    for (var i = 0; i < rows.length; i++) {
      if (!rows[i] || rows[i].row !== rowId) continue;
      var cells = list(rows[i].cells);
      for (var j = 0; j < cells.length; j++) {
        var c = cells[j];
        if (c && c.name === name && xOf(c) === (x || '') && isNum(c.value)) {
          return { path: 'rows.' + i + '.cells.' + j, value: c.value, ci_lo: c.ci_lo, ci_hi: c.ci_hi };
        }
      }
    }
    return null;
  }
  // true when one of the named cells is in the file, not run, because it describes fewer than 10 people
  function leadWithheld(doc, rowId, names, x) {
    return list(doc && doc.rows).some(function (r) {
      return r && r.row === rowId && list(r.cells).some(function (c) {
        return c && names.indexOf(c.name) >= 0 && xOf(c) === (x || '') && c.state === 'not_run' && c.reason_code === 'cell_under_10';
      });
    });
  }
  function pct1(v) { return (Math.round(v * 1000) / 10).toFixed(1) + '%'; }
  function pct0(v) { return String(Math.round(v * 100)) + '%'; }
  function pts1(v) { return (Math.round(Math.abs(v) * 1000) / 10).toFixed(1); }
  function count(v) { return String(Math.round(v)); }
  // The string for `key` in mode B when the strings have one ("<key>.b"), else the plain one.
  function leadKey(key, modeB) { return modeB && has(key + '.b') ? key + '.b' : key; }

  SS.profile.lead = function lead(doc, opts) {
    opts = opts || {};
    var modeB = opts.referenceType === 'B';
    if (opts.referenceType === 'C') return [{ id: 'none', keys: [], text: SS.t('results.lead.no_reference') }];
    if (opts.judgeless === true) return [{ id: 'none', keys: [], text: SS.t('results.lead.no_judge') }];
    var out = [];
    var ai = leadCell(doc, 'lead', 'ai_recall', 'primary');
    var ni = leadCell(doc, 'lead', 'no_interview_recall', 'primary');
    var added = leadCell(doc, 'lead', 'listening_added', 'primary');
    if (ai && ni && added) {
      out.push({ id: 'added', keys: [added.path, ai.path, ni.path], text: SS.t(leadKey(added.value < 0 ? 'results.lead.added_less' : 'results.lead.added', modeB),
        { added: pts1(added.value), ai: pct1(ai.value), ni: pct1(ni.value) }) });
    } else {
      // council 30 (MUST 4): withheld by the fewer-than-10 rule is said as such, never as "did not compute"
      out.push({ id: 'added', keys: [], text: SS.t(leadWithheld(doc, 'lead', ['ai_recall', 'no_interview_recall', 'listening_added'], 'primary')
        ? 'results.lead.added.withheld' : 'results.lead.added.not_run') });
    }
    var omitted = leadCell(doc, 1, 'omitted', null);
    out.push(omitted ? { id: 'omitted', keys: [omitted.path], text: SS.t(leadKey('results.lead.omitted', modeB), { omitted: pct1(omitted.value) }) }
      : { id: 'omitted', keys: [], text: SS.t(leadKey('results.lead.omitted.not_run', modeB)) });
    var size = leadCell(doc, 2, 'ai_per_endorsed', null);
    if (size) {
      var text = SS.t(leadKey('results.lead.size', modeB), { size: pct0(size.value) });
      var keys = [size.path];
      var boxed = leadCell(doc, 'lead', 'rarefied_ai_minus_prior_only_recall', 'primary');
      if (boxed && isNum(boxed.ci_lo) && isNum(boxed.ci_hi) && boxed.value >= 0) {
        // "percentage points" at the first use on the page, "points" after it (council 30)
        text += ' ' + SS.t(out[0] && out[0].keys.length ? 'results.lead.size_cut' : 'results.lead.size_cut.first', { boxed: pts1(boxed.value), lo: (boxed.ci_lo < 0 ? MINUS : '') + pts1(boxed.ci_lo), hi: (boxed.ci_hi < 0 ? MINUS : '') + pts1(boxed.ci_hi) });
        keys.push(boxed.path);
      }
      out.push({ id: 'size', keys: keys, text: text });
    } else {
      out.push({ id: 'size', keys: [], text: SS.t(leadKey('results.lead.size.not_run', modeB)) });
    }
    var ideals = leadCell(doc, 5, 'n_ideals', 'primary');
    var flat = leadCell(doc, 5, 'flattened', 'primary');
    var against = leadCell(doc, 5, 'counterfactual', 'primary');
    if (ideals && ideals.value === 0) {
      out.push({ id: 'flattened', keys: [ideals.path], text: SS.t(leadKey('results.lead.flattened.no_ideals', modeB)) });
    } else if (ideals && flat) {
      // Council 30 (MUST 2): "flattened" and "counterfactual" are disjoint counts (metrics.leak_split). A wish drawn as
      // received although the AI's own quote says it never came (the danger case) is reported whenever it is above 0,
      // and "none as received" is said only when neither count is.
      var one = Math.round(ideals.value) === 1 ? '_one' : '';
      var nFlat = Math.round(flat.value);
      var nAgainst = against ? Math.round(against.value) : 0;
      var k = [ideals.path, flat.path];
      if (against) k.push(against.path);
      var t;
      if (nFlat + nAgainst === 0) {
        t = SS.t(leadKey('results.lead.flattened.none' + one, modeB), { ideals: count(ideals.value) });
      } else if (nAgainst === 0) {
        t = SS.t(leadKey('results.lead.flattened' + one, modeB), { ideals: count(ideals.value), n: count(nFlat) });
        if (against) t += ' ' + SS.t('results.lead.flattened.quote_none');
      } else {
        t = SS.t(leadKey('results.lead.received' + one, modeB), { ideals: count(ideals.value), n: count(nFlat + nAgainst) });
        t += ' ' + (nFlat === 0 ? SS.t('results.lead.flattened.quote_all')
          : SS.t('results.lead.flattened.quote_some', { n: count(nAgainst) }) + ' ' +
            (nFlat === 1 ? SS.t('results.lead.flattened.rest_one') : SS.t('results.lead.flattened.rest', { n: count(nFlat) })));
      }
      out.push({ id: 'flattened', keys: k, text: t });
    } else {
      out.push({ id: 'flattened', keys: [], text: SS.t(leadKey('results.lead.flattened.not_run', modeB)) });
    }
    return out;
  };

  // Draws SS.profile.lead's sentences into `host` as paragraphs (data-lead, data-keys); returns the list.
  SS.profile.renderLead = function renderPlainLead(host, doc, opts) {
    clear(host);
    var said = SS.profile.lead(doc, opts);
    said.forEach(function (f) {
      var p = el('p', 'pf-plain', f.text, host);
      p.setAttribute('data-lead', f.id);
      if (f.keys.length) p.setAttribute('data-keys', f.keys.join(' '));
    });
    return said;
  };

  SS.profile.render = function render(rootNode, doc, opts) {
    opts = opts || {};
    clear(rootNode);
    var rows = list(doc && doc.rows);
    if (doc && doc.state === 'synthetic') {
      // opts.syntheticBand === false: the page (the viewer) prints the SYNTHETIC band itself, once, at its head.
      if (opts.syntheticBand !== false) el('div', 'band synthetic pf-band-top', SS.t('band.synthetic'), rootNode).setAttribute('data-band', 'synthetic');
    } else if (doc && doc.state === 'pending') {
      el('div', 'band pending pf-band-top', SS.t('profile.state.pending'), rootNode).setAttribute('data-band', 'pending');
    }
    // Derived lines sit only beside the lead and its sizes, wherever the file put them.
    // opts.derived === false (the app's Results and the report, scout F9): the "yield per box" lines, arithmetic not
    // used in the study's paper, are left out; the file keeps them.
    var besides = [];
    rows.forEach(function (r) {
      list(r.cells).forEach(function (c) { if (c && isBesideLead(c) && opts.derived !== false) besides.push({ rowKey: r.row, row: r, cell: c }); });
    });
    var wrap = el('div', 'pf-profile', undefined, rootNode);
    wrap.setAttribute('data-schema', String(doc && doc.schema));
    // Compact: the rest goes into one closed <details>, built detached and attached after the lead if it holds anything.
    var full = null;
    var fullBody = null;
    var leadMore = null;
    if (opts.compact === true) {
      full = el('details', 'pf-full');
      el('summary', 'pf-full-summary', SS.t('try.profile.full'), full);
      fullBody = el('div', 'pf-full-body', undefined, full);
      leadMore = function () { return el('section', 'pf-row pf-lead-more', undefined, fullBody); };
    }
    var rowsHost = fullBody || wrap;
    var sizeCells = [];
    rows.forEach(function (r) {
      if (r.row !== 'lead') return;
      SIZES_BESIDE.forEach(function (nm) {
        var hit = list(r.cells).filter(function (c) { return c && c.name === nm && hasQ(c, 'size_beside_lead'); })[0];
        if (hit) sizeCells.push({ row: r, cell: hit });
      });
    });
    var leadDone = false;
    rows.forEach(function (r) {
      if (r.row === 'lead') { renderLead(wrap, r, besides, opts, leadMore); leadDone = true; }
      else renderRow(rowsHost, r, opts, sizeCells);
    });
    if (!leadDone && besides.length) {
      renderLead(wrap, { row: 'lead', label_key: 'profile.lead', cells: [], qualifiers: [], rests_on: [] }, besides, opts, leadMore);
    }
    if (full && fullBody.children.length) wrap.appendChild(full);
    runFootnote(wrap, doc && doc.run);
    return rootNode;
  };
})();
