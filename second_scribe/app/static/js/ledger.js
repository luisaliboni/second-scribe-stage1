/* SS.ledger: the rung selector and the absent-steps table (Task 25).
     renderSelector  a radio group for the prompt rung; choosing one sets SS.state.rung, and only the AI's map changes
     render          one real <table>: a row for each step the reference map marks missing, a column for the
                     no-interview map and one for each prompt rung (placebo directly before rung 3)
   "Ledger" is a code name. The visible title is "Wished-for steps under each set of instructions" (the profile has its own ledger row).
   Cell values (rows): kept | leaked | never_drawn | unattested | unknown | not_run for the AI's rungs,
   has | lacks | unknown for the no-interview map (code key `script`, never shown). `unknown` is a fate that cannot belong
   to a missing step, or a missing script_has; `not_run` is a rung the run did not do. Nothing is guessed or shown as 0.
   Clicking a cell emits "item:open" {item_id, map, rung, lane}; the viewer answers with SS.state.set.
   Data goes into the page through textContent and attributes only. Cell shapes come from views.css (ss-mk-*), layout
   from ledger.css. Classic script; attaches to window.SS (after core, encoding, state). */
(function () {
  'use strict';
  var root = typeof window !== 'undefined' ? window : globalThis;
  var SS = root.SS = root.SS || {};
  var hasOwn = Object.prototype.hasOwnProperty;

  var ORDER = ['rung1', 'rung2', 'placebo', 'rung3'];     // display order; placebo directly before rung 3
  var CELL_KEYS = ['script'].concat(ORDER);
  // The selector and the table's column heads name a rung the way the buttons and the caption do ("Rung 3") and say
  // what it is ("Rung 3: remedy prompt"), so one rung never has two names on a page (2.2 issue 2).
  var RUNG_LABEL_KEY = { rung1: 'ledger.rung.rung1', rung2: 'ledger.rung.rung2', placebo: 'ledger.rung.placebo', rung3: 'ledger.rung.rung3' };
  var AI_VALUE = { kept: 'kept', leaked: 'leaked', missed: 'never_drawn', unattested: 'unattested' };
  var SHAPE = {
    kept: 'ss-mk-dashed', leaked: 'ss-mk-leaked', never_drawn: 'ss-mk-never', unattested: 'ss-mk-never',
    unknown: 'ss-mk-unknown', has: 'ss-mk-script', lacks: 'ss-mk-none'
  };

  function isString(v) { return typeof v === 'string' && v !== ''; }
  function maps(view) { return view && Array.isArray(view.maps) ? view.maps : []; }

  function rungOrder(view) {
    var present = view && Array.isArray(view.rungs) ? view.rungs : [];
    return ORDER.filter(function (r) { return present.indexOf(r) >= 0; });
  }

  function mapIndex(view, state) {
    var list = maps(view);
    var pid = state && state.map;
    for (var i = 0; i < list.length; i++) if (list[i] && list[i].pid === pid) return i;
    return 0;
  }

  function scriptValue(item) {
    if (item.script_has === true) return 'has';
    if (item.script_has === false) return 'lacks';
    return 'unknown';
  }

  function aiValue(item, rung, ran) {
    if (ran.indexOf(rung) < 0) return 'not_run';
    var f = item.fate;
    if (f === null || typeof f !== 'object' || !hasOwn.call(f, rung) || f[rung] === null || f[rung] === undefined) return 'not_run';
    var fate = f[rung];
    return typeof fate === 'string' && hasOwn.call(AI_VALUE, fate) ? AI_VALUE[fate] : 'unknown';
  }

  // One row for each reference item with reference_status == "missing", in map order.
  function rows(view, mapIdx) {
    var list = maps(view);
    var map = list[mapIdx];
    var items = map && Array.isArray(map.items) ? map.items : [];
    var ran = view && Array.isArray(view.rungs) ? view.rungs : [];
    var out = [];
    items.forEach(function (item) {
      if (!item || item.reference_status !== 'missing') return;
      var cells = { script: scriptValue(item) };
      ORDER.forEach(function (r) { cells[r] = aiValue(item, r, ran); });
      out.push({ item_id: item.item_id, label: item.label === undefined || item.label === null ? '' : String(item.label), cells: cells });
    });
    return out;
  }

  function rungLabel(r) { return hasOwn.call(RUNG_LABEL_KEY, r) ? SS.t(RUNG_LABEL_KEY[r]) : String(r); }
  // item 42: the selector's cards, a short title and a plain description; a rung without its own pair keeps its name
  var CARD_RUNGS = { rung1: 1, rung2: 1, placebo: 1, rung3: 1 };
  function cardTitle(r) { return hasOwn.call(CARD_RUNGS, r) ? SS.t('ledger.card.' + r + '.title') : rungLabel(r); }
  function cardDesc(r) { return hasOwn.call(CARD_RUNGS, r) ? SS.t('ledger.card.' + r + '.desc') : ''; }

  function cellText(key, value) {
    if (key === 'script') return SS.t('ledger.script.' + value);
    if (value === 'unattested') return SS.t('lane.quote_not_found');
    return SS.t('ledger.cell.' + value);
  }

  // Everything the table shows, no DOM: {pid, modeB, title, note, rungs, rows: [{item_id, label, selected, cells: [...]}]}
  function model(view, state) {
    var st = state && typeof state === 'object' ? state : {};
    var idx = mapIndex(view, st);
    var map = maps(view)[idx];
    var modeB = !!view && view.reference_type === 'B';
    var order = rungOrder(view);
    var cols = ['script'].concat(order);
    var m = {
      pid: map && isString(map.pid) ? map.pid : null,
      modeB: modeB,
      title: SS.t(modeB ? 'ledger.title.mode_b' : 'ledger.title'),
      note: SS.t(modeB ? 'ledger.note.mode_b' : 'ledger.note'),
      rung: st.rung,
      rungs: order,
      columns: cols.map(function (k) {
        return { key: k, label: k === 'script' ? SS.t('lane.no_interview') : rungLabel(k), current: k !== 'script' && k === st.rung };
      }),
      rows: []
    };
    rows(view, idx).forEach(function (r) {
      m.rows.push({
        item_id: r.item_id,
        label: r.label,
        selected: isString(st.item) && st.item === r.item_id,
        cells: cols.map(function (k) {
          var v = r.cells[k];
          return { key: k, value: v, text: cellText(k, v), openable: v !== 'not_run' && !(k === 'script' && v === 'unknown') };
        })
      });
    });
    return m;
  }

  function el(tag, cls, text, parent) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null && text !== '') n.textContent = text;
    if (parent) parent.appendChild(n);
    return n;
  }

  // A panel is rebuilt on every state change, so keyboard focus is put back on the same control afterwards.
  function activeKey(rootEl, attrs) {
    var a = typeof document !== 'undefined' ? document.activeElement : null;
    if (!a || typeof a.getAttribute !== 'function' || typeof rootEl.contains !== 'function' || !rootEl.contains(a)) return null;
    var out = {};
    for (var i = 0; i < attrs.length; i++) {
      var v = a.getAttribute(attrs[i]);
      if (v === null) return null;
      out[attrs[i]] = v;
    }
    return out;
  }
  function restoreFocus(rootEl, tagName, want) {
    if (!want || typeof rootEl.querySelectorAll !== 'function') return;
    var nodes = rootEl.querySelectorAll(tagName);
    for (var i = 0; i < nodes.length; i++) {
      var ok = true;
      for (var k in want) if (nodes[i].getAttribute(k) !== want[k]) ok = false;
      if (ok) { if (typeof nodes[i].focus === 'function') nodes[i].focus(); return; }
    }
  }

  // SS.ledger.renderSelector(root, view, state): a fieldset with one radio per rung present, in display order.
  // Returns the checked radio (null when none is checked), so a caller can move focus to it (2.2 issue 1).
  var selectorSeq = 0;
  function renderSelector(rootEl, view, state) {
    var st = state && typeof state === 'object' ? state : {};
    var want = activeKey(rootEl, ['value']);
    rootEl.textContent = '';
    var set = el('fieldset', 'ss-rungsel', undefined, rootEl);
    el('legend', 'ss-rungsel-legend', SS.t('ledger.selector.legend'), set);
    el('p', 'ss-rungsel-note muted', SS.t(view && view.reference_type === 'C' ? 'ledger.selector.note.two' : 'ledger.selector.note'), set);
    var name = 'ss-rung-' + (++selectorSeq);
    var group = el('div', 'ss-rungsel-options', undefined, set);
    var checked = null;
    rungOrder(view).forEach(function (r) {
      var label = el('label', 'ss-rungsel-option' + (r === st.rung ? ' is-checked' : ''), undefined, group);
      var input = el('input', 'ss-rungsel-input', undefined, label);
      input.setAttribute('type', 'radio');
      input.setAttribute('name', name);
      input.setAttribute('value', r);
      input.checked = r === st.rung;
      if (input.checked) checked = input;
      input.addEventListener('change', function () { SS.state.set({ rung: r }); });
      // item 42: a card with a short title and a plain second line (the radio's name is both, read in order)
      var body = el('span', 'ss-rungsel-body', undefined, label);
      el('span', 'ss-rungsel-text', cardTitle(r), body);
      var desc = cardDesc(r);
      if (desc) el('span', 'ss-rungsel-desc', desc, body);
    });
    restoreFocus(rootEl, 'input', want);
    return checked;
  }

  // SS.ledger.render(root, view, state): the absent-steps table, or a plain sentence when no step is marked missing.
  function render(rootEl, view, state) {
    var st = state && typeof state === 'object' ? state : {};
    var m = model(view, st);
    var want = activeKey(rootEl, ['data-item-id', 'data-lane', 'data-rung']);
    rootEl.textContent = '';
    var wrap = el('div', 'ss-ledger', undefined, rootEl);
    if (!m.rows.length) {
      // Mode C has no reference map, so there is nothing to hold an absence: say that, keep the panel title.
      if (view && view.reference_type === 'C') {
        el('p', 'ss-ledger-title', m.title, wrap);
        el('p', 'ss-ledger-empty muted', SS.t('ledger.empty.mode_c'), wrap);
      } else {
        el('p', 'ss-ledger-empty muted', SS.t('ledger.empty'), wrap);
      }
      return m;
    }
    el('p', 'ss-ledger-note muted', m.note, wrap);
    var hint = el('p', 'ss-scroll-hint muted', SS.t('scroll.hint.columns'), wrap);
    hint.hidden = true;
    var frame = el('div', 'ss-ledger-frame', undefined, wrap);
    var scroll = el('div', 'ss-ledger-scroll', undefined, frame);
    if (SS.scrollCue) SS.scrollCue(scroll, frame, hint);
    scroll.setAttribute('role', 'region');
    scroll.setAttribute('aria-label', m.title);
    scroll.setAttribute('tabindex', '0');
    var table = el('table', 'ss-ledger-table', undefined, scroll);
    el('caption', 'ss-ledger-title', m.title, table);
    var head = el('tr', '', undefined, el('thead', '', undefined, table));
    var stepTh = el('th', 'ss-ledger-step', SS.t('ledger.col.step'), head);
    stepTh.setAttribute('scope', 'col');
    m.columns.forEach(function (c) {
      var th = el('th', 'ss-ledger-col' + (c.current ? ' is-current' : ''), c.label, head);
      th.setAttribute('scope', 'col');
      if (c.current) th.setAttribute('aria-current', 'true');
    });
    var body = el('tbody', '', undefined, table);
    m.rows.forEach(function (r) {
      var tr = el('tr', r.selected ? 'is-selected' : '', undefined, body);
      var rh = el('th', 'ss-ledger-rowhead', r.label, tr);
      rh.setAttribute('scope', 'row');
      if (r.selected) rh.setAttribute('aria-current', 'true');
      r.cells.forEach(function (c, i) {
        var col = m.columns[i];
        var td = el('td', 'ss-ledger-td' + (col.current ? ' is-current' : ''), undefined, tr);
        if (!c.openable) {
          el('span', 'ss-ledger-na muted', c.text, td);
          return;
        }
        var lane = c.key === 'script' ? 'script' : 'ai';
        var rung = c.key === 'script' ? st.rung : c.key;
        var shape = c.value === 'not_run' ? '' : (SHAPE[c.value] || 'ss-mk-unknown');
        var btn = el('button', 'ss-ledger-cell ' + shape, undefined, td);
        btn.setAttribute('type', 'button');
        btn.setAttribute('data-item-id', r.item_id);
        btn.setAttribute('data-lane', lane);
        btn.setAttribute('data-rung', c.key === 'script' ? 'script' : c.key);
        btn.setAttribute('aria-label', SS.t('ledger.cell.label', { step: r.label, column: col.label, value: c.text }));
        if (c.value === 'leaked') {
          var g = el('span', 'ss-mark-glyph', '▲', btn);
          g.setAttribute('aria-hidden', 'true');
        }
        el('span', 'ss-ledger-cell-text', c.text, btn);
        btn.addEventListener('click', function () {
          SS.bus.emit('item:open', { item_id: r.item_id, map: m.pid, rung: rung, lane: lane });
        });
      });
    });
    restoreFocus(rootEl, 'button', want);
    return m;
  }

  SS.ledger = { ORDER: ORDER, rungOrder: rungOrder, rows: rows, model: model, renderSelector: renderSelector, render: render };
})();
