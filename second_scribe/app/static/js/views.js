/* SS.views: the three ways to read one map, all from the same data, the same shapes and the same positions.
     lanes    three stacked lanes on shared phase columns (SS.lanes, Task 23)
     overlay  view B: the reference map, with the AI's mark on each box, and an "Only the AI drew" gutter
     columns  view C: the no-interview map | the AI's map | the reference map, side by side on shared rows
   An item sits at one place per view: the same x in overlay and lanes (SS.layout), the same row in every column of
   `columns`, at every rung. Only the AI's mark (overlay) or the AI column (columns) changes with the rung.
   Since item 20 (23 Oct) the viewer draws the lanes only and shows no view switch; the overlay and columns renderers
   below stay in the kit, unused by any page, because their tests pin the shared layout they rely on.
   SS.views.stamp(state) names the map and the instructions shown, in plain words. Data goes into the page through textContent and attributes only;
   positions use element.style properties, never a style attribute. Models are pure (no DOM) and tested without one.
   Classic script; attaches to window.SS (after core, layout, encoding, a11y, lanes). */
(function () {
  'use strict';
  var root = typeof window !== 'undefined' ? window : globalThis;
  var SS = root.SS = root.SS || {};
  var hasOwn = Object.prototype.hasOwnProperty;

  var VIEWS = ['lanes', 'overlay', 'columns'];
  var KNOWN_RUNGS = { rung1: 1, rung2: 1, placebo: 1, rung3: 1 };
  var PHASE_H = 34;          // a phase heading row in `columns`, px
  var ROW_H = 64;            // one item row in `columns`, px (compact, item 12: a box of at most 60px and a gap)
  var GUTTER_ROW_H = 56;     // one row of the "Only the AI drew" gutter, px
  var NODE_ROW_H = 56;       // one row of a no-interview node in `columns` (a 44px plain node and a gap), px
  var NAV_KEYS = { ArrowLeft: 1, ArrowRight: 1, ArrowUp: 1, ArrowDown: 1, Home: 1, End: 1 };
  var MARK_BY_FATE = { kept: 'as_missing', flipped: 'as_missing', leaked: 'as_happened', recovered: 'as_happened', missed: 'never' };

  function isString(v) { return typeof v === 'string' && v !== ''; }
  function viewKey(v) { return VIEWS.indexOf(v) >= 0 ? v : 'lanes'; }
  function rungKey(r) { return isString(r) ? r : 'rung2'; }

  // ------------------------------------------------------------------ names and stamp

  // The lanes and columns views draw one lane for each reading, so their names follow the count (view optional: three).
  function viewName(v, view) {
    var k = viewKey(v);
    return SS.t('views.name.' + k + (k !== 'overlay' && SS.lanes.readingCount(view) === 2 ? '.two' : ''));
  }
  function rungName(r) {
    var k = rungKey(r);
    return hasOwn.call(KNOWN_RUNGS, k) ? SS.t('views.rung.' + k) : k;
  }

  // The instructions' plain name, as the rung selector shows it ("The study's instructions (rung 2)").
  function rungLongName(r) {
    var k = rungKey(r);
    return hasOwn.call(KNOWN_RUNGS, k) ? SS.t('ledger.rung.' + k) : k;
  }
  // "Map SYN-OW03 · The study's instructions (rung 2)" (item 20: plain, and no view name since there is one view).
  // Names the map and the instructions, not the clicked item. `view` is kept in the signature for callers.
  function stamp(state, view) {
    var s = state && typeof state === 'object' ? state : {};
    if (isString(s.map)) return SS.t('views.stamp', { rung: rungLongName(s.rung), map: s.map });
    return SS.t('views.stamp_no_map', { rung: rungLongName(s.rung) });
  }

  function mapIndex(view, state) {
    var maps = view && Array.isArray(view.maps) ? view.maps : [];
    var pid = state && state.map;
    for (var i = 0; i < maps.length; i++) if (maps[i] && maps[i].pid === pid) return i;
    return 0;
  }

  // ------------------------------------------------------------------ models (pure)

  // The AI's mark on one item at one rung: its shape (same table as the lanes) and a short sentence.
  function aiMark(item, rung) {
    var enc = SS.encoding.forItem('ai', item, rung);
    var fate = SS.encoding.fateAt(item, rung);
    var text;
    if (fate === 'unattested') text = SS.t('lane.quote_not_found');
    else if (typeof fate === 'string' && hasOwn.call(MARK_BY_FATE, fate)) text = SS.t('views.mark.' + MARK_BY_FATE[fate]);
    else text = SS.t('views.mark.unknown');
    return { enc: enc, text: text };
  }

  // The column a phase belongs to: its own; a node with no phase goes to the no-phase column; an unknown phase to the first.
  function columnIndex(layout, phase) {
    var none = !SS.layout.hasPhase(phase);
    for (var i = 0; i < layout.columns.length; i++) {
      if (none ? layout.columns[i].noPhase === true : layout.columns[i].phase === phase) return i;
    }
    return 0;
  }

  // AI nodes at a rung that matched no item (gutter: true), each in its own phase column
  // (an unknown phase goes in the first, no phase in the "No phase" column), stacked in rows in array order.
  function gutterPlace(map, rung, layout) {
    var ai = map && map.lanes && map.lanes.ai && typeof map.lanes.ai === 'object' ? map.lanes.ai : null;
    var entry = ai && hasOwn.call(ai, rung) ? ai[rung] : null;
    var nodes = entry && Array.isArray(entry.nodes) ? entry.nodes : [];
    var perCol = {};
    var out = [];
    nodes.forEach(function (n) {
      if (!n || n.gutter !== true) return;
      var col = layout.columns.length ? columnIndex(layout, n.phase) : 0;
      var row = perCol[col] || 0;
      perCol[col] = row + 1;
      var c = layout.columns[col];
      out.push({
        node_id: n.node_id, label: n.label === undefined || n.label === null ? '' : String(n.label),
        phase: c ? c.phase : n.phase, col: col, row: row,
        x: (c ? c.x0 : 0) + SS.layout.COL_PAD
      });
    });
    return out;
  }

  // Rows the gutter needs: the most any one phase holds at any rung of the map, so its size never changes with the rung.
  function gutterRows(map, layout) {
    var ai = map && map.lanes && map.lanes.ai && typeof map.lanes.ai === 'object' ? map.lanes.ai : {};
    var rows = 1;
    Object.keys(ai).forEach(function (rung) {
      gutterPlace(map, rung, layout).forEach(function (n) { if (n.row + 1 > rows) rows = n.row + 1; });
    });
    return rows;
  }

  // SS.views.overlayModel(view, mapIdx, rung) -> {kind, map, rung, refType, layout,
  //   lanes: [{key: 'reference', name, targets: [{lane, item_id, x, col, item, enc, ai: {enc, text}, label, idx}]}],
  //   aiMissing: text when the AI's map at this rung is missing, else null,
  //   gutter: {name, note, rows, nodes: [{node_id, label, phase, col, row, x}]}}
  //   Boxes keep the reference shape and the lanes' x. `lanes` has the same shape as SS.lanes.model so the
  //   same focus helpers work.
  function overlayModel(view, mapIdx, rung) {
    var base = SS.lanes.model(view, mapIdx, rung);
    var ref = base.lanes.filter(function (l) { return l.key === 'reference'; })[0];
    var targets = ref.targets.map(function (t) {
      return {
        lane: 'reference', item_id: t.item_id, x: t.x, col: t.col, item: t.item, enc: t.enc, idx: t.idx,
        ai: aiMark(t.item, rung), label: SS.a11y.itemLabel(t.item, rung, base.refType)
      };
    });
    var aiLane = base.lanes.filter(function (l) { return l.key === 'ai'; })[0];
    return {
      kind: 'overlay', map: base.map, rung: rung, refType: base.refType, layout: base.layout,
      aiMissing: aiLane && aiLane.missing ? aiLane.missing : null,
      lanes: [{ key: 'reference', name: ref.name, targets: targets }],
      gutter: {
        name: SS.t('views.gutter.title'), note: SS.t('views.gutter.note'),
        rows: gutterRows(base.map, base.layout), nodes: gutterPlace(base.map, rung, base.layout)
      }
    };
  }

  // SS.views.columnsModel(view, mapIdx, rung) -> {kind, map, rung, refType, layout, rows, height,
  //   lanes: [{key, name, targets: [{... as SS.lanes.model, row, y}], plain: [{... as SS.lanes.model, row, y}]}]}
  //   Rows are the map's items in phase then x_slot order (SS.layout), one per item, shared by the three columns,
  //   under a heading row for each phase that has an item. After a phase's items come the rows of the no-interview
  //   nodes that sit on no step of their own (the lanes' `plain` entries of that column, in their order), drawn in the
  //   no-interview column only. The result depends on the map only, never on the rung.
  function columnsModel(view, mapIdx, rung) {
    var base = SS.lanes.model(view, mapIdx, rung);
    var ref = base.lanes.filter(function (l) { return l.key === 'reference'; })[0];
    var script = base.lanes.filter(function (l) { return l.key === 'script'; })[0];
    var extra = script ? script.plain : [];
    var rows = [];
    var ys = {};
    var rowOf = {};
    var extraAt = {};
    var y = 0;
    var n = 0;
    base.layout.columns.forEach(function (c, col) {
      var mine = ref.targets.filter(function (t) { return t.col === col; });
      var more = extra.filter(function (e) { return e.col === col; }).sort(function (a, b) { return (a.row || 0) !== (b.row || 0) ? (a.row || 0) - (b.row || 0) : a.x - b.x; });
      if (!mine.length && !more.length) return;
      rows.push({ type: 'phase', phase: SS.layout.columnName(c), y: y });
      y += PHASE_H;
      mine.forEach(function (t) {
        rows.push({ type: 'item', item_id: t.item_id, row: n, y: y });
        ys[t.item_id] = y;
        rowOf[t.item_id] = n;
        y += ROW_H;
        n++;
      });
      more.forEach(function (e) {
        rows.push({ type: 'node', node_id: e.node_id, row: n, y: y });
        extraAt[e.node_id] = { row: n, y: y };
        y += NODE_ROW_H;
        n++;
      });
    });
    var lanes = base.lanes.map(function (l) {
      return {
        key: l.key, name: l.name, missing: l.missing,
        targets: l.targets.map(function (t) {
          var c = {
            lane: t.lane, item_id: t.item_id, x: t.x, col: t.col, item: t.item, enc: t.enc, idx: t.idx,
            label: t.label, row: rowOf[t.item_id], y: ys[t.item_id]
          };
          if (t.text !== undefined) c.text = t.text;
          if (t.node_id !== undefined) c.node_id = t.node_id;
          return c;
        }).sort(function (a, b) { return a.row - b.row; }),
        // the AI's own extra boxes (SS.lanes.model draws them in the lanes since item 20) belong to the overlay's gutter here
        plain: (l.key === 'script' ? l.plain || [] : []).map(function (e) {
          var at = extraAt[e.node_id] || { row: n, y: y };
          return { lane: e.lane, node_id: e.node_id, node: e.node, label: e.label, enc: e.enc, col: e.col, row: at.row, y: at.y };
        })
      };
    });
    return {
      kind: 'columns', map: base.map, rung: rung, refType: base.refType, layout: base.layout,
      rows: rows, height: y + 8, lanes: lanes
    };
  }

  // The target a key press moves to, or null.
  //   lanes / overlay: the Task 23 rule (SS.lanes.nextFocus).
  //   columns: Up / Down / Home / End along a column; Left / Right to the neighbouring column that has any item,
  //   onto the same item if that column draws it, else the nearest row (the earlier one on a tie).
  function nextFocus(m, cur, key) {
    if (!m || m.kind !== 'columns') return SS.lanes.nextFocus(m, cur, key);
    var li = -1;
    for (var a = 0; a < m.lanes.length; a++) if (m.lanes[a].key === cur.lane) li = a;
    if (li < 0) return null;
    var targets = m.lanes[li].targets;
    var pos = -1;
    for (var i = 0; i < targets.length; i++) if (targets[i].item_id === cur.item_id) pos = i;
    if (pos < 0) return null;
    if (key === 'ArrowDown') return targets[pos + 1] || null;
    if (key === 'ArrowUp') return pos > 0 ? targets[pos - 1] : null;
    if (key === 'Home') return targets[0];
    if (key === 'End') return targets[targets.length - 1];
    if (key !== 'ArrowLeft' && key !== 'ArrowRight') return null;
    var step = key === 'ArrowRight' ? 1 : -1;
    for (var j = li + step; j >= 0 && j < m.lanes.length; j += step) {
      var cand = m.lanes[j].targets;
      if (!cand.length) continue;
      var best = null;
      for (var k = 0; k < cand.length; k++) {
        if (cand[k].item_id === cur.item_id) return cand[k];
        if (best === null || Math.abs(cand[k].row - cur.row) < Math.abs(best.row - cur.row)) best = cand[k];
      }
      return best;
    }
    return null;
  }

  function focusOrder(m) {
    var out = [];
    m.lanes.forEach(function (l) { l.targets.forEach(function (t) { out.push(t); }); });
    return out;
  }

  // ------------------------------------------------------------------ DOM

  function el(tag, cls, text, parent) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null && text !== '') node.textContent = text;
    if (parent) parent.appendChild(node);
    return node;
  }

  function labelOf(item) {
    return item && item.label !== undefined && item.label !== null ? String(item.label) : '';
  }
  // The text a box shows: a no-interview node shows its own label (t.text), any other box its item's.
  function textOf(t) { return t.text !== undefined ? t.text : labelOf(t.item); }

  // One item as a button. The shape classes are the lanes' (viewer.css); the caller positions it.
  function itemButton(t, ctx, parent) {
    var btn = el('button', 'ss-item ss-lane-' + t.lane + ' ' + SS.encoding.shapeClass(t.enc.shape), undefined, parent);
    btn.setAttribute('type', 'button');
    btn.setAttribute('data-lane', t.lane);
    btn.setAttribute('data-item-id', t.item_id);
    btn.setAttribute('aria-label', t.label);
    btn.setAttribute('title', textOf(t));
    if (t.node_id !== undefined) btn.setAttribute('data-node-id', String(t.node_id));
    btn.setAttribute('tabindex', '-1');
    if (ctx.item !== null && ctx.item === t.item_id) {
      btn.className += ' is-selected';
      btn.setAttribute('aria-current', 'true');
    }
    return btn;
  }

  function glyphSpan(btn, glyph) {
    var g = el('span', 'ss-item-glyph ' + (glyph === '▲' ? 'ss-glyph-up' : 'ss-glyph-down'), glyph, btn);
    g.setAttribute('aria-hidden', 'true');
  }

  // Roving tabindex, arrow-key movement and click for a set of item buttons. One tab stop for the whole panel.
  function makeNav(m, ctx) {
    var buttons = {};
    var all = [];
    function keyOf(lane, id) { return lane + '\u0000' + id; }
    function setRoving(btn) { all.forEach(function (b) { b.setAttribute('tabindex', b === btn ? '0' : '-1'); }); }
    return {
      add: function (btn, t) {
        buttons[keyOf(t.lane, t.item_id)] = btn;
        all.push(btn);
        btn.addEventListener('focus', function () { setRoving(btn); });
        btn.addEventListener('click', function () {
          SS.bus.emit('item:open', { item_id: t.item_id, map: ctx.pid, rung: ctx.rung, lane: t.lane });
        });
        btn.addEventListener('keydown', function (ev) {
          if (!ev || !hasOwn.call(NAV_KEYS, ev.key)) return;
          if (ev.preventDefault) ev.preventDefault();
          var nxt = nextFocus(m, t, ev.key);
          var target = nxt ? buttons[keyOf(nxt.lane, nxt.item_id)] : null;
          if (target) { setRoving(target); target.focus(); }
        });
      },
      finish: function () {
        var order = focusOrder(m);
        if (order.length) setRoving(buttons[keyOf(order[0].lane, order[0].item_id)]);
        return function focus(lane, itemId) {
          var b = buttons[keyOf(lane, itemId)];
          if (b) { setRoving(b); b.focus(); }
        };
      }
    };
  }


  function emptyPanel(rootEl, cls, m) {
    rootEl.textContent = '';
    var wrap = el('div', cls, undefined, rootEl);
    wrap.setAttribute('role', 'group');
    el('p', 'ss-lanes-empty muted', SS.t('lane.empty'), wrap);
    return { model: m, focus: function () {} };
  }

  // View B. The reference map in its own shape, the AI's mark under each box, and the gutter.
  function renderOverlay(rootEl, view, mapIdx, rung, ctx) {
    var m = overlayModel(view, mapIdx, rung);
    var targets = m.lanes[0].targets;
    if (!m.map || !targets.length) return emptyPanel(rootEl, 'ss-lanes ss-ov', m);
    rootEl.textContent = '';
    var wrap = el('div', 'ss-lanes ss-ov', undefined, rootEl);
    wrap.setAttribute('role', 'group');
    wrap.setAttribute('aria-label', SS.t('views.overlay.panel_label'));
    var hint = el('p', 'ss-scroll-hint muted', SS.t('scroll.hint.phases'), wrap);
    hint.hidden = true;
    var scroll = el('div', 'ss-lanes-scroll', undefined, wrap);
    if (SS.scrollCue) SS.scrollCue(scroll, wrap, hint);
    var inner = el('div', 'ss-lanes-inner ss-ov-inner', undefined, scroll);
    inner.style.width = m.layout.width + 'px';

    var head = el('div', 'ss-phases', undefined, inner);
    m.layout.columns.forEach(function (c) {
      var p = el('div', 'ss-phase', SS.layout.columnName(c), head);
      p.style.left = c.x0 + 'px';
      p.style.width = (c.x1 - c.x0) + 'px';
    });

    var nav = makeNav(m, ctx);
    var sec = el('section', 'ss-lane ss-lane-row-reference', undefined, inner);
    sec.setAttribute('role', 'group');
    sec.setAttribute('aria-label', m.lanes[0].name);
    el('h3', 'ss-lane-title', m.lanes[0].name, sec);
    el('p', 'ss-lane-gloss', SS.t('views.overlay.marks_note', { rung: rungName(rung) }), sec);
    if (m.aiMissing) el('p', 'ss-lane-missing muted', m.aiMissing, sec);
    var track = el('div', 'ss-track ss-ov-track', undefined, sec);
    track.style.width = m.layout.width + 'px';
    targets.forEach(function (t) {
      var btn = itemButton(t, ctx, track);
      btn.style.left = t.x + 'px';
      btn.style.width = SS.layout.ITEM_W + 'px';
      el('span', 'ss-item-label', labelOf(t.item), btn);
      var mark = el('span', 'ss-mark ss-mk-' + t.ai.enc.shape, undefined, btn);
      if (t.ai.enc.glyph) {
        var g = el('span', 'ss-mark-glyph', t.ai.enc.glyph, mark);
        g.setAttribute('aria-hidden', 'true');
      }
      el('span', 'ss-mark-text', t.ai.text, mark);
      nav.add(btn, t);
    });

    // The gutter: what only the AI drew at this rung. Plain text (it has no reference item to open).
    var g = m.gutter;
    var gsec = el('section', 'ss-lane ss-gutter', undefined, inner);
    gsec.setAttribute('role', 'group');
    gsec.setAttribute('aria-label', g.name);
    el('h3', 'ss-lane-title', g.name, gsec);
    el('p', 'ss-lane-gloss', g.note, gsec);
    var gtrack = el('div', 'ss-track ss-gutter-track', undefined, gsec);
    gtrack.setAttribute('role', 'list');
    gtrack.style.width = m.layout.width + 'px';
    gtrack.style.height = (g.rows * GUTTER_ROW_H + 16) + 'px';

    if (!g.nodes.length) {
      el('p', 'ss-gutter-empty muted', SS.t('views.gutter.empty'), gtrack);
    }
    g.nodes.forEach(function (n) {
      var box = el('div', 'ss-item ss-lane-ai ss-shape-normal ss-gutter-node', undefined, gtrack);
      box.setAttribute('role', 'listitem');
      box.style.left = n.x + 'px';
      box.style.top = (8 + n.row * GUTTER_ROW_H) + 'px';
      box.style.width = SS.layout.ITEM_W + 'px';
      el('span', 'ss-item-label', n.label, box);
    });
    SS.lanes.phaseRules(m.layout, inner);

    return { model: m, focus: nav.finish() };
  }

  // View C. Three columns on shared rows.
  function renderColumns(rootEl, view, mapIdx, rung, ctx, opts) {
    var m = columnsModel(view, mapIdx, rung);
    if (!m.map || !(focusOrder(m).length || m.lanes.some(function (l) { return !!l.missing; }))) return emptyPanel(rootEl, 'ss-lanes ss-cols', m);
    rootEl.textContent = '';
    var wrap = el('div', 'ss-lanes ss-cols', undefined, rootEl);
    wrap.setAttribute('role', 'group');
    wrap.setAttribute('aria-label', SS.lanes.countT('views.columns.panel_label', view));
    var hint = el('p', 'ss-scroll-hint muted', SS.t('scroll.hint.phases'), wrap);
    hint.hidden = true;
    var scroll = el('div', 'ss-lanes-scroll', undefined, wrap);
    if (SS.scrollCue) SS.scrollCue(scroll, wrap, hint);
    var inner = el('div', 'ss-cols-inner', undefined, scroll);

    // Headings first (grid row 1), so a name that wraps never shifts the rows below it.
    m.lanes.forEach(function (lane) {
      var head = el('div', 'ss-colv-head', undefined, inner);
      el('h3', 'ss-colv-title', lane.name, head);
      if (lane.key === 'script' && opts.gloss !== false) el('p', 'ss-colv-gloss', SS.t('lane.no_interview_gloss'), head);
    });

    var nav = makeNav(m, ctx);
    m.lanes.forEach(function (lane) {
      var col = el('section', 'ss-colv ss-colv-' + lane.key, undefined, inner);
      col.setAttribute('role', 'group');
      col.setAttribute('aria-label', lane.name);
      var track = el('div', 'ss-colv-track', undefined, col);
      track.style.height = m.height + 'px';
      if (lane.missing) el('p', 'ss-lane-missing muted', lane.missing, track);
      m.rows.forEach(function (r) {
        if (r.type !== 'phase') return;
        var p = el('div', 'ss-colv-phase', r.phase, track);
        p.style.top = r.y + 'px';
      });
      (lane.plain || []).forEach(function (e) {
        var box = el('div', 'ss-item ss-plain-node ss-lane-' + e.lane + ' ' + SS.encoding.shapeClass(e.enc.shape), undefined, track);
        box.setAttribute('data-plain', 'true');
        box.setAttribute('data-lane', e.lane);
        box.setAttribute('data-node-id', String(e.node_id));
        box.setAttribute('title', e.label);
        box.style.top = e.y + 'px';
        el('span', 'ss-item-label', e.label, box);
      });
      lane.targets.forEach(function (t) {
        var btn = itemButton(t, ctx, track);
        btn.style.top = t.y + 'px';
        if (t.enc.glyph) glyphSpan(btn, t.enc.glyph);
        el('span', 'ss-item-label', textOf(t), btn);
        var tag = t.enc.tag || (t.enc.shape === 'unknown' ? SS.t('lane.item_not_computed') : '');
        if (tag) el('span', 'ss-item-tag', tag, btn);
        nav.add(btn, t);
      });
    });
    return { model: m, focus: nav.finish() };
  }

  // SS.views.render(root, view, state, opts?) -> {model, focus(lane, item_id)}
  //   state = {view, map, rung, item}. Unknown view -> lanes; unknown map -> the first map.
  //   opts.gloss === false leaves out "a map of the official pathway (prompt quoted)" (it belongs once per page).
  //   opts.allRungs: a plain map (no reference items) shows the AI's map at every rung, one lane each (see SS.lanes.model).
  //   opts.itemLane: the lane the selected step was chosen in, so the lanes keep that box's sentence lit (item 18).
  //   A click or Enter on a box emits "item:open" {item_id, map, rung, lane}; the viewer answers by setting state.item.
  function render(rootEl, view, state, opts) {
    opts = opts || {};
    var s = state && typeof state === 'object' ? state : {};
    var v = viewKey(s.view);
    var mapIdx = mapIndex(view, s);
    var rung = rungKey(s.rung);
    var item = isString(s.item) ? s.item : null;
    var map = view && Array.isArray(view.maps) ? view.maps[mapIdx] : null;
    var ctx = { pid: map ? map.pid : null, rung: rung, item: item };
    // With no reference items there is nothing to compare: the overlay and the columns would be empty, so the plain lanes show the maps.
    if (v !== 'lanes' && SS.lanes.isPlain(view, mapIdx)) v = 'lanes';
    if (v === 'overlay') return renderOverlay(rootEl, view, mapIdx, rung, ctx);
    if (v === 'columns') return renderColumns(rootEl, view, mapIdx, rung, ctx, opts);
    return SS.lanes.render(rootEl, view, mapIdx, rung, { gloss: opts.gloss, item: item, itemLane: opts.itemLane, allRungs: opts.allRungs });
  }

  // SS.views.renderSwitch(root, state): three toggle buttons in a fixed order. A click changes only the view
  // (SS.state.set({view})); map, rung and item stay. The viewer re-renders on "state:change".
  function renderSwitch(rootEl, state, view) {
    var s = state && typeof state === 'object' ? state : {};
    var cur = viewKey(s.view);
    rootEl.textContent = '';
    var group = el('div', 'ss-viewswitch', undefined, rootEl);
    group.setAttribute('role', 'group');
    group.setAttribute('aria-label', SS.t('views.switch_label'));
    VIEWS.forEach(function (key) {
      var b = el('button', 'ss-viewswitch-btn', SS.t('views.switch.' + key + (key !== 'overlay' && SS.lanes.readingCount(view) === 2 ? '.two' : '')), group);
      b.setAttribute('type', 'button');
      b.setAttribute('data-view', key);
      b.setAttribute('aria-pressed', key === cur ? 'true' : 'false');
      b.addEventListener('click', function () {
        if (SS.state && typeof SS.state.set === 'function') SS.state.set({ view: key });
      });
    });
    return group;
  }

  SS.views = {
    VIEWS: VIEWS, PHASE_H: PHASE_H, ROW_H: ROW_H, GUTTER_ROW_H: GUTTER_ROW_H, NODE_ROW_H: NODE_ROW_H,
    name: viewName, stamp: stamp, mapIndex: mapIndex,
    overlayModel: overlayModel, columnsModel: columnsModel, nextFocus: nextFocus,
    render: render, renderSwitch: renderSwitch
  };
})();
