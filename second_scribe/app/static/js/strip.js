/* SS.strip: the drill-downs of the profile panel (plan amendment V11, which replaces Task 26's numbers strip).
   Classic script; it attaches to window.SS and, when called, uses SS.core (SS.t) and SS.profile (profile.js:
   number printing, cell and setting names, units), which is loaded after it.

   SS.strip.drill(rowKey, container, row, opts)   plugs into the profile renderer's hook
       opts.drill(rowKey, container, row, opts)   (profile.js passes its own opts through).
     row 5 (absences): the row's cells as dots with their interval, one panel per cell name, one line per setting (x).
     row 8 (instruction, descriptive): one dot and range per rung for each rung cell in the file, in the order
       rung1, rung2, placebo, rung3 (a file may call the third rung "remedy"); the contrasts below, printed exactly
       as the file gives them, with p and Holm-adjusted p when present, and the MDE80 cell next to them.
     opts.mode            "dots" (default), "bars" or "table"; three buttons switch it. Every mode prints the same numbers.
     opts.referenceType   "B": titles read "researcher-marked absences" and the fixed note is shown.
     opts.note            false: do not print the fixed note (the viewer prints it once per screen).
   SS.strip.model(rowKey, row, opts) -> { panels, counts, contrasts }: the lines the three modes draw.

   Rules the tests pin (tests/js/strip.test.js):
   - Profile cells only, never view.numbers; nothing is computed. Every printed number is the one profile.js prints
     for the same cell. Positions on an axis are drawing, not results.
   - No verdict sentence, no headline, no claim wording. A cell that did not run reads "not run in this profile";
     a missing value reads "not computed", never 0, and gets no dot.
   Data reaches the DOM through textContent and attributes only. */
(function () {
  'use strict';
  var root = typeof window !== 'undefined' ? window : globalThis;
  var SS = root.SS = root.SS || {};
  var hasOwn = Object.prototype.hasOwnProperty;
  var MODES = ['dots', 'bars', 'table'];
  var RUNG_ORDER = ['rung1', 'rung2', 'placebo', 'rung3', 'remedy'];
  var UNSCALED = 99;

  function list(v) { return Array.isArray(v) ? v : []; }
  function isNum(v) { return typeof v === 'number' && isFinite(v); }
  function xOf(c) { return c.x === undefined || c.x === null ? '' : String(c.x); }
  function hasQ(c, q) { return list(c.qualifiers).indexOf(q) >= 0; }
  function kp(code) { return String(code).toLowerCase().replace(/[^a-z0-9_]/g, '_'); }
  function table() { var t = root.SS_STRINGS; return t !== null && typeof t === 'object' ? t : {}; }
  function has(key) { return hasOwn.call(table(), key) && typeof table()[key] === 'string'; }
  function P() { return SS.profile; }

  // ------------------------------------------------------------------------------------------- DOM helpers
  function el(tag, cls, text, parent) {
    var n = root.document.createElement(tag);
    if (cls) n.setAttribute('class', cls);
    if (text !== undefined && text !== null) n.textContent = String(text);
    if (parent) parent.appendChild(n);
    return n;
  }
  function part(tag, name, text, parent, cls) {
    var n = el(tag, cls || '', text, parent);
    n.setAttribute('data-part', name);
    return n;
  }
  function clear(node) {
    if (typeof node.replaceChildren === 'function') { node.replaceChildren(); return; }
    while (node.firstChild) node.removeChild(node.firstChild);
  }

  // ------------------------------------------------------------------------------------------- the model
  function referenceB(opts) { return !!opts && opts.referenceType === 'B'; }

  function cellTitle(row, name, opts) {
    var bk = 'strip.b.cell.' + kp(name);
    if (referenceB(opts) && has(bk)) return SS.t(bk);
    return P().words.cell(row.label_key, name);
  }

  function nText(c) {
    if (!hasOwn.call(c, 'n') || c.n === null) return undefined;
    if (c.n === '[SOURCE]') return SS.t('profile.source_placeholder');
    return SS.t(c.kind === 'count' ? 'profile.n.out_of' : 'profile.n.over',
      { n: P().fmt.num(c.n, 'count'), unit: c.unit ? P().words.unit(c.unit) : '' }).replace(/\s+$/, '');
  }

  function pText(c) {
    var f = P().fmt;
    var ps = [];
    if (isNum(c.p_lt)) ps.push(SS.t('profile.p_lt', { p: f.num(c.p_lt) }));
    else if (isNum(c.p)) ps.push(SS.t('profile.p', { p: f.num(c.p) }));
    if (isNum(c.p_holm_lt)) ps.push(SS.t('profile.p_holm_lt', { p: f.num(c.p_holm_lt) }));
    else if (isNum(c.p_holm)) ps.push(SS.t('profile.p_holm', { p: f.num(c.p_holm) }));
    return ps.length ? ps.join(' · ') : undefined;
  }

  function lineOf(rowKey, row, c, opts) {
    var f = P().fmt;
    var notRun = c.state === 'not_run';
    var hasCi = !notRun && (hasOwn.call(c, 'ci_lo') || hasOwn.call(c, 'ci_hi'));
    var value;
    if (notRun) {
      var needs = list(c.needs);
      value = needs.length ?
        SS.t('profile.not_run_needs', { needs: needs.map(function (n) {
          return P().words.has('profile.needs.' + kp(n)) ? SS.t('profile.needs.' + kp(n)) : n;
        }).join(', ') }) :
        SS.t('profile.not_run');
    } else {
      value = hasOwn.call(c, 'value') ? f.value(c) : f.num(undefined, c.kind);
    }
    var level = notRun ? undefined : f.level(c, hasCi);
    var boundWithCi = hasCi && c.kind === 'upper_bound' && isNum(c.ci_level);
    var x = xOf(c);
    var ln = {
      key: String(rowKey) + '|' + c.name + '|' + x,
      name: c.name,
      x: x,
      kind: c.kind,
      title: cellTitle(row, c.name, opts),
      xLabel: x ? P().words.x(x) : '',
      state: notRun ? 'not_run' : (c.state || ''),
      plot: !notRun && isNum(c.value),
      v: c.value,
      lo: isNum(c.ci_lo) && isNum(c.ci_hi) ? c.ci_lo : null,
      hi: isNum(c.ci_lo) && isNum(c.ci_hi) ? c.ci_hi : null,
      text: {
        value: value,
        ci: hasCi && !boundWithCi ? f.ciOf(c) : undefined,
        level: level,
        n: nText(c),
        p: pText(c)
      },
      derived: c.derived === true || hasQ(c, 'not_a_test'),
      partial: c.state === 'partial_not_run' || hasOwn.call(c, 'partial_not_run')
    };
    return ln;
  }

  function rank(l) { var i = RUNG_ORDER.indexOf(l.x); return i < 0 ? UNSCALED : i; }

  // Nice upper end for a count axis (1, 1.5, 2, 2.5, 3, 4, 5, 6, 8 x 10^k). Drawing only; never printed as a result.
  function niceMax(m) {
    if (!(m > 0)) return 1;
    var p = Math.pow(10, Math.floor(Math.log(m) / Math.LN10));
    var steps = [1, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10];
    for (var i = 0; i < steps.length; i++) if (steps[i] * p >= m - 1e-12) return steps[i] * p;
    return 10 * p;
  }

  function model(rowKey, row, opts) {
    row = row && typeof row === 'object' ? row : {};
    // Derived lines sit beside the lead only (profile.js), so the drill-down leaves them out too.
    var cells = list(row.cells).filter(function (c) {
      return c && typeof c === 'object' && c.derived !== true && !hasQ(c, 'not_a_test');
    });
    var lines = cells.map(function (c) { return lineOf(rowKey, row, c, opts); });
    var out = { rowKey: rowKey, panels: [], counts: [], contrasts: [] };
    var byName = {};
    lines.forEach(function (l) {
      if (l.kind === 'difference') { out.contrasts.push(l); return; }
      if (l.kind === 'count' && l.lo === null) { out.counts.push(l); return; }
      var p = byName[l.name];
      if (!p) {
        p = byName[l.name] = { name: l.name, title: l.title, lines: [], scale: { min: 0, max: 1 } };
        out.panels.push(p);
      }
      p.lines.push(l);
    });
    out.panels.forEach(function (p) {
      p.lines = p.lines.map(function (l, i) { return { l: l, i: i }; }).sort(function (a, b) {
        return (rank(a.l) - rank(b.l)) || (a.i - b.i);
      }).map(function (o) { return o.l; });
      var counted = p.lines.some(function (l) { return l.kind === 'count'; });
      if (counted) {
        var top = 0;
        p.lines.forEach(function (l) {
          if (l.plot) top = Math.max(top, l.hi === null ? l.v : Math.max(l.hi, l.v));
        });
        p.scale = { min: 0, max: niceMax(top), counted: true };
      }
    });
    return out;
  }

  // ------------------------------------------------------------------------------------------- printing a line
  function describe(l) {
    var t = l.text;
    var s = l.title + (l.xLabel ? ', ' + l.xLabel : '') + ': ' + t.value;
    if (t.ci) s += ' ' + t.ci;
    if (t.level) s += ' ' + t.level;
    if (t.n) s += ', ' + t.n;
    if (t.p) s += ', ' + t.p;
    return s;
  }

  function numbers(l, parent) {
    var t = l.text;
    var box = el('span', 'sd-nums', undefined, parent);
    part('span', 'value', t.value, box, 'sd-value');
    if (t.ci) part('span', 'ci', t.ci, box, 'sd-ci');
    if (t.level) part('span', 'level', t.level, box, 'sd-level');
    if (t.n) part('span', 'n', t.n, box, 'sd-n');
    if (t.p) part('span', 'p', t.p, box, 'sd-p');
    if (l.derived) part('span', 'derived', SS.t('profile.derived'), box, 'sd-note');
    if (l.partial) part('span', 'partial', SS.t('profile.partial'), box, 'sd-note');
    return box;
  }

  function pos(v, scale) {
    var span = scale.max - scale.min;
    var pct = span > 0 ? ((v - scale.min) / span) * 100 : 0;
    return Math.max(0, Math.min(100, pct));
  }
  function pct(n) { return (Math.round(n * 100) / 100) + '%'; }

  // One line of a dots or bars panel: setting name, the track with its mark, the printed numbers.
  function plotLine(l, scale, mode, parent) {
    var line = el('div', 'sd-line', undefined, parent);
    line.setAttribute('data-line', l.key);
    if (l.state) line.setAttribute('data-state', l.state);
    el('span', 'sd-lab', l.xLabel || l.title, line);
    var track = el('span', 'sd-track', undefined, line);
    if (l.plot) {
      var upper = l.kind === 'upper_bound';
      var p = pos(l.v, scale);
      if (l.lo !== null) {
        var r = el('span', 'sd-range', undefined, track);
        var a = pos(l.lo, scale);
        var b = pos(l.hi, scale);
        r.style.left = pct(a);
        r.style.width = pct(Math.max(0, b - a));
      }
      var shape = mode === 'bars' ? 'bar' : (upper ? 'triangle' : (l.x === 'placebo' ? 'hollow' : 'dot'));
      var cls = 'sd-mark sd-' + shape + (mode === 'bars' && upper ? ' sd-bound' : '') +
        (mode === 'bars' && l.x === 'placebo' ? ' sd-placebo' : '');
      var mark = part('span', 'mark', undefined, track, cls);
      mark.setAttribute('data-shape', shape);
      mark.setAttribute('tabindex', '0');
      mark.setAttribute('role', 'img');
      mark.setAttribute('aria-label', describe(l));
      if (mode === 'bars') { mark.style.width = pct(p); } else { mark.style.left = pct(p); }
      // The tip follows the mark in the DOM so CSS can show it on hover and on keyboard focus.
      var tip = part('span', 'tip', describe(l), track, 'sd-tip' + (p > 50 ? ' sd-tip-right' : ''));
      tip.setAttribute('aria-hidden', 'true');
    }
    numbers(l, line);
    return line;
  }

  function panel(p, mode, parent) {
    var box = el('div', 'sd-panel', undefined, parent);
    box.setAttribute('data-panel', p.name);
    el('h5', 'sd-panel-title', p.title, box);
    var axis = el('div', 'sd-axis', undefined, box);
    axis.setAttribute('aria-hidden', 'true');
    el('span', 'sd-lab', '', axis);
    var ticks = el('span', 'sd-track sd-ticks', undefined, axis);
    // Part 6: a panel of shares runs from 0% to 100% and says so; a panel of counts keeps its numbers
    var end = function (v) { return p.scale.counted ? P().fmt.num(v) : Math.round(v * 100) + '%'; };
    part('span', 'axis', end(p.scale.min), ticks, 'sd-tick sd-tick-lo');
    part('span', 'axis', end(p.scale.max), ticks, 'sd-tick sd-tick-hi');
    p.lines.forEach(function (l) { plotLine(l, p.scale, mode, box); });
    return box;
  }

  // ------------------------------------------------------------------------------------------- tables
  var COLS = [
    { name: 'value', key: 'strip.col.value' },
    { name: 'ci', key: 'strip.col.interval' },
    { name: 'n', key: 'strip.col.over' },
    { name: 'p', key: 'strip.col.p' }
  ];
  var COUNT_COLS = [COLS[0], COLS[2]];

  // Only the columns some line fills (the p column stays out of a table that has no p).
  function colsFor(lines) {
    return COLS.filter(function (c) {
      return c.name === 'value' || lines.some(function (l) { return !!l.text[c.name] || (c.name === 'ci' && !!l.text.level); });
    });
  }

  function headRow(parent, cols) {
    var tr = el('tr', '', undefined, parent);
    el('th', '', SS.t('strip.col.setting'), tr).setAttribute('scope', 'col');
    cols.forEach(function (c) { el('th', '', SS.t(c.key), tr).setAttribute('scope', 'col'); });
  }
  function cellTd(tr, col, text) {
    var td = el('td', 'sd-td-' + col.name, undefined, tr);
    td.setAttribute('data-label', SS.t(col.key));  // the narrow layout prints it before the value
    if (text !== undefined && text !== '') part('span', col.name, text, td);
    return td;
  }
  function tableLine(l, parent, withTitle, cols) {
    var tr = el('tr', 'sd-line', undefined, parent);
    tr.setAttribute('data-line', l.key);
    if (l.state) tr.setAttribute('data-state', l.state);
    var th = el('th', 'sd-lab', undefined, tr);
    th.setAttribute('scope', 'row');
    el('span', '', withTitle ? l.title : (l.xLabel || l.title), th);
    if (withTitle && l.xLabel) el('span', 'sd-x', l.xLabel, th);
    cols.forEach(function (col) {
      var td = cellTd(tr, col, l.text[col.name]);
      if (col.name === 'ci' && l.text.level) part('span', 'level', l.text.level, td, 'sd-level');
      if (col.name === 'value') {
        if (l.derived) part('span', 'derived', SS.t('profile.derived'), td, 'sd-note');
        if (l.partial) part('span', 'partial', SS.t('profile.partial'), td, 'sd-note');
      }
    });
    return tr;
  }

  function plainTable(lines, caption, cols, parent) {
    var t = el('table', 'sd-table', undefined, parent);
    el('caption', 'sd-caption sd-sr', caption, t);
    var head = el('thead', '', undefined, t);
    headRow(head, cols);
    var body = el('tbody', '', undefined, t);
    lines.forEach(function (l) { tableLine(l, body, true, cols); });
    return t;
  }

  function panelsTable(panels, parent) {
    var t = el('table', 'sd-table', undefined, parent);
    el('caption', 'sd-caption sd-sr', SS.t('strip.table.caption'), t);
    var all = [];
    panels.forEach(function (p) { all = all.concat(p.lines); });
    var cols = colsFor(all);
    var head = el('thead', '', undefined, t);
    headRow(head, cols);
    panels.forEach(function (p) {
      var body = el('tbody', '', undefined, t);
      body.setAttribute('data-panel', p.name);
      var tr = el('tr', 'sd-panel-row', undefined, body);
      var th = el('th', 'sd-panel-title', p.title, tr);
      th.setAttribute('scope', 'rowgroup');
      th.setAttribute('colspan', String(cols.length + 1));
      p.lines.forEach(function (l) { tableLine(l, body, false, cols); });
    });
    return t;
  }

  // ------------------------------------------------------------------------------------------- the drill-down
  function headingKey(rowKey, opts) {
    var k = 'strip.row' + rowKey + '.heading';
    if (referenceB(opts) && has(k + '_b')) return k + '_b';
    return has(k) ? k : 'strip.heading';
  }

  function fillBody(body, m, mode) {
    clear(body);
    var any = m.panels.length || m.counts.length || m.contrasts.length;
    if (!any) { el('p', 'sd-empty', SS.t('common.not_computed'), body); return; }
    if (m.panels.length) {
      if (mode === 'table') {
        panelsTable(m.panels, body);
      } else {
        m.panels.forEach(function (p) { panel(p, mode, body); });
        var lg = part('p', 'legend', SS.t(mode === 'bars' ? 'strip.legend.bars' : 'strip.legend.dots'), body, 'sd-legend');
        lg.setAttribute('data-mode-legend', mode);
      }
    }
    if (m.counts.length) {
      var cs = el('div', 'sd-group', undefined, body);
      cs.setAttribute('data-group', 'counts');
      el('h5', 'sd-sub', SS.t('strip.counts'), cs);
      plainTable(m.counts, SS.t('strip.counts'), COUNT_COLS, cs);
    }
    if (m.contrasts.length) {
      var ct = el('div', 'sd-group', undefined, body);
      ct.setAttribute('data-group', 'contrasts');
      el('h5', 'sd-sub', SS.t('strip.contrasts'), ct);
      plainTable(m.contrasts, SS.t('strip.contrasts'), colsFor(m.contrasts), ct);
    }
  }

  function drill(rowKey, container, row, opts) {
    opts = opts || {};
    var mode = MODES.indexOf(opts.mode) >= 0 ? opts.mode : 'dots';
    var m = model(rowKey, row, opts);
    clear(container);
    var wrap = el('div', 'sd-drill', undefined, container);
    wrap.setAttribute('data-drill-mode', mode);
    wrap.setAttribute('data-row', String(rowKey));
    el('h4', 'sd-heading', SS.t(headingKey(rowKey, opts)), wrap);
    if (referenceB(opts) && opts.note !== false) {
      el('p', 'sd-note-b', SS.t('strip.mode_b_note'), wrap).setAttribute('data-part', 'mode_b_note');
    }
    var bar = el('div', 'sd-modes', undefined, wrap);
    bar.setAttribute('role', 'group');
    bar.setAttribute('aria-label', SS.t('strip.modes.label'));
    var body = el('div', 'sd-body', undefined, wrap);
    var buttons = MODES.map(function (md) {
      var b = el('button', 'sd-mode', SS.t('strip.mode.' + md), bar);
      b.setAttribute('type', 'button');
      b.setAttribute('data-mode', md);
      b.setAttribute('aria-pressed', md === mode ? 'true' : 'false');
      b.addEventListener('click', function () {
        mode = md;
        wrap.setAttribute('data-drill-mode', mode);
        buttons.forEach(function (o) { o.setAttribute('aria-pressed', o.getAttribute('data-mode') === mode ? 'true' : 'false'); });
        fillBody(body, m, mode);
      });
      return b;
    });
    fillBody(body, m, mode);
    return container;
  }

  SS.strip = { drill: drill, model: model, MODES: MODES.slice() };
})();
