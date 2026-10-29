/* SS.lanes: the three readings of one map as three stacked lanes on shared phase columns.
     top     ("reference")         "The interviewer-drawn, participant-endorsed map"  (mode B: "This researcher's reading")
     middle  ("ai")                "The AI's map, from the transcript"
     bottom  (code name "script")  "The same model, without the interview"
   (council 30: the order of every legend; it was no-interview on top before)
   SS.lanes.model builds what is drawn (pure, tested without a DOM); SS.lanes.render puts it in the page.
   A map with no reference items (mode C, or AI maps only) has nothing to compare: it is "plain". Its lanes then draw the
   nodes of the no-interview map and of the AI's map as plain boxes by phase, in their own order, with no fate marks
   (the reference lane is left out). A lane whose map is missing (lanes.ai.<rung>.status, lanes.script.status) says so.
   On a compared map the AI's lane also draws the boxes only the AI drew (its nodes that match no step, `gutter`), on rows
   below its steps, the way the no-interview lane draws its own extra boxes (items 18 and 20: the lanes are the one view).
   Every box of a compared map says in one plain sentence what happened to it (SS.lanes.boxSentence, item 18): pointing at
   it, focusing it or tapping it shows the sentence in the status line above the lanes and lights its partner boxes in
   the other lanes; the sentence is also the box's accessible description (aria-describedby), so nothing is hover-only.
   Text from data goes in through textContent only; positions use element.style properties, never a style attribute.
   Classic script; attaches to window.SS (after core, layout, encoding, a11y). */
(function () {
  'use strict';
  var root = typeof window !== 'undefined' ? window : globalThis;
  var SS = root.SS = root.SS || {};

  // council 30 (seat 02 F6): endorsed, AI, no-interview, top to bottom, the order of every legend
  var LANES = [
    { key: 'reference', nameKey: 'lane.reference' },
    { key: 'ai', nameKey: 'lane.ai' },
    { key: 'script', nameKey: 'lane.no_interview' }
  ];
  var NAV_KEYS = { ArrowLeft: 1, ArrowRight: 1, ArrowUp: 1, ArrowDown: 1, Home: 1, End: 1 };
  var ROW_H = 84;            // one row of a lane's track (viewer.css --ss-track-h); boxes sit 10px below its top
  var ROW_TOP = 10;
  // The no-interview lane's extra rows (row 1, 2, ...) hold plain nodes (views.css .ss-plain-node, 44px, up to three
  // lines of label since item 14, at most 63px), so they stack at a pitch of 66px under row 0, whose steps are full
  // item boxes (62px).
  var EXTRA_ROW_H = 66;
  var noteSeq = 0;           // ids of the box descriptions (item 18), unique on the page

  // Top of a box on `row` of a track, px.
  function rowTop(row) { return row > 0 ? ROW_H + (row - 1) * EXTRA_ROW_H : ROW_TOP; }
  // Height of a track with `rows` rows, px.
  function trackHeight(rows) { return rows > 1 ? ROW_H + (rows - 1) * EXTRA_ROW_H : ROW_H; }

  function laneName(key, refType) {
    if (key === 'reference' && refType === 'B') return SS.t('lane.reference_b');
    var def = LANES.filter(function (l) { return l.key === key; })[0];
    return SS.t(def.nameKey);
  }

  function hasOwn(o, k) { return Object.prototype.hasOwnProperty.call(o, k); }
  function mapAt(view, mapIdx) {
    var maps = view && Array.isArray(view.maps) ? view.maps : [];
    return maps[mapIdx] || null;
  }

  // The status a lane's map carries when it is missing (the exporter writes {nodes: [], status: ...}), else null.
  function laneStatus(map, key, rung) {
    var lanes = map && map.lanes && typeof map.lanes === 'object' ? map.lanes : null;
    if (!lanes) return null;
    var entry = key === 'script' ? lanes.script : (lanes.ai && typeof lanes.ai === 'object' && hasOwn(lanes.ai, rung) ? lanes.ai[rung] : null);
    if (!entry || typeof entry !== 'object') return null;
    if (typeof entry.status !== 'string' || entry.status === '') return null;
    return Array.isArray(entry.nodes) && entry.nodes.length ? null : entry.status;
  }
  function anyStatus(map) {
    var lanes = map && map.lanes && typeof map.lanes === 'object' ? map.lanes : {};
    var ai = lanes.ai && typeof lanes.ai === 'object' ? lanes.ai : {};
    if (laneStatus(map, 'script', null)) return true;
    return Object.keys(ai).some(function (r) { return !!laneStatus(map, 'ai', r); });
  }
  function missingText(key) { return SS.t(key === 'script' ? 'lane.no_map_script' : 'lane.no_map_ai'); }

  // ------------------------------------------------------------------ item 18: one plain sentence per box
  function refWord(refType) { return SS.t(refType === 'B' ? 'box.ref_word.b' : 'box.ref_word'); }
  function stepLabel(item) { return item && item.label !== undefined && item.label !== null ? String(item.label) : ''; }
  function evidenceAt(item, rung) {
    var ev = item && item.evidence;
    if (!ev || typeof ev !== 'object' || !hasOwn(ev, rung)) return null;
    return ev[rung] && typeof ev[rung] === 'object' ? ev[rung] : null;
  }
  var KNOWN_FATES = { kept: 1, leaked: 1, recovered: 1, flipped: 1, missed: 1, unattested: 1 };

  // SS.lanes.boxSentence(lane, item, rung, refType, only) -> the sentence for one box, from the view data only.
  //   lane 'reference': the endorsed step and how the AI's map holds it at this rung (a wished-for step says so first);
  //   lane 'ai': the step the AI box matches, how it drew a step that never came, and whether its own quote backs it;
  //   lane 'script': the step the no-interview box matches. only = true: a box that matches no step (item is null).
  //   Nothing is computed: the sentence names the fate and the evidence the exporter recorded.
  function boxSentence(lane, item, rung, refType, only) {
    var ref = refWord(refType);
    if (only) return SS.t(lane === 'ai' ? 'box.ai.only' : 'box.script.only', { ref: ref });
    var fate = SS.encoding.fateAt(item, rung);
    var known = typeof fate === 'string' && hasOwn(KNOWN_FATES, fate);
    if (lane === 'script') return SS.t('box.script.match', { step: stepLabel(item), ref: ref });
    if (lane === 'reference') {
      if (item && item.reference_status === 'missing') {
        var how = known && fate !== 'recovered' && fate !== 'flipped' ? fate : 'unknown';
        return SS.t(refType === 'B' ? 'box.ref.wished.b' : 'box.ref.wished', { ai: SS.t('box.wished.' + how) });
      }
      if (fate === 'recovered') return SS.t('box.ref.has');
      if (fate === 'flipped') return SS.t('box.ref.has_as_never');
      if (fate === 'missed') return SS.t('box.ref.lacks');
      if (fate === 'unattested') return SS.t('box.ref.unattested');
      return SS.t('box.unknown');
    }
    if (fate === 'missed') return SS.t('box.ai.lacks');
    if (!known) return SS.t('box.unknown');
    var text = SS.t('box.ai.match', { step: stepLabel(item), ref: ref });
    if (fate === 'kept' || fate === 'flipped') text += SS.t('box.ai.how.never');
    if (fate === 'leaked') text += SS.t('box.ai.how.received');
    var ev = evidenceAt(item, rung);
    if (fate === 'unattested') text += SS.t('box.ai.backing.none');
    else if (ev && ev.speaker === 'participant') text += SS.t('box.ai.backing.participant');
    else if (ev && typeof ev.quote === 'string' && ev.quote !== '') text += SS.t('box.ai.backing.transcript');
    return text + '.';
  }

  // SS.lanes.sentenceFor(view, mapIdx, rung, lane, itemId): the sentence of the box a reader chose, for the evidence
  // drawer (null on a plain map or an unknown step). A no-interview lane that does not hold the step falls back to the
  // endorsed step's sentence.
  function sentenceFor(view, mapIdx, rung, lane, itemId) {
    var map = mapAt(view, mapIdx);
    if (!map || isPlain(view, mapIdx)) return null;
    var items = Array.isArray(map.items) ? map.items : [];
    var item = null;
    for (var i = 0; i < items.length; i++) if (items[i] && items[i].item_id === itemId) item = items[i];
    if (!item) return null;
    var refType = view && view.reference_type ? view.reference_type : null;
    if (lane === 'ai') return boxSentence('ai', item, rung, refType);
    if (lane === 'script' && item.script_has === true) return boxSentence('script', item, rung, refType);
    return boxSentence('reference', item, rung, refType);
  }

  // A box that is a real match in its lane (an AI box at a step the AI never drew, or one with no recorded fate, is a
  // placeholder: it is never lit as a partner).
  function realBox(t, rung) {
    if (t.lane !== 'ai') return true;
    var fate = SS.encoding.fateAt(t.item, rung);
    return typeof fate === 'string' && fate !== 'missed' && hasOwn(KNOWN_FATES, fate);
  }

  // The AI's nodes that match no step (gutter), placed like the no-interview lane's extra boxes: rows 1, 2, ... of their
  // phase column (no phase: the "No phase" column; a phase no column has: the first), filling the column's slots left
  // to right. `rows` is the most any rung needs, so the lane keeps one height at every rung.
  function aiExtras(map, layout) {
    var ai = map && map.lanes && map.lanes.ai && typeof map.lanes.ai === 'object' ? map.lanes.ai : {};
    var cols = layout.columns || [];
    function colOf(phase) {
      var none = !SS.layout.hasPhase(phase);
      for (var i = 0; i < cols.length; i++) if (none ? cols[i].noPhase === true : cols[i].phase === phase) return i;
      return 0;
    }
    var byRung = {};
    var rows = 1;
    Object.keys(ai).forEach(function (r) {
      var nodes = ai[r] && Array.isArray(ai[r].nodes) ? ai[r].nodes : [];
      var per = {};
      byRung[r] = [];
      nodes.forEach(function (n) {
        if (!n || n.gutter !== true) return;
        var col = colOf(n.phase);
        var c = cols[col];
        var slots = c ? Math.max(c.slots || 1, 1) : 1;
        var k = per[col] || 0;
        per[col] = k + 1;
        var row = 1 + Math.floor(k / slots);
        if (row + 1 > rows) rows = row + 1;
        byRung[r].push({
          lane: 'ai', node_id: n.node_id, node: n, x: (c ? c.x0 : 0) + SS.layout.COL_PAD + (k % slots) * SS.layout.SLOT_W,
          col: col, row: row, label: n.label === undefined || n.label === null ? '' : String(n.label),
          enc: { shape: 'normal', glyph: '' }, only: true
        });
      });
    });
    return { byRung: byRung, rows: rows };
  }

  // SS.lanes.isPlain(view, mapIdx): true when the map has no reference items but has a map to show (or a missing one to name).
  function isPlain(view, mapIdx) {
    var map = mapAt(view, mapIdx);
    if (!map || (Array.isArray(map.items) && map.items.length)) return false;
    var phases = view && Array.isArray(view.phases) ? view.phases : [];
    return SS.layout.placePlain(map, phases).has || anyStatus(map);
  }

  function rungName(r) {
    return /^(rung1|rung2|placebo|rung3)$/.test(String(r)) ? SS.t('views.rung.' + r) : String(r);
  }

  // SS.lanes.model(view, mapIdx, rung, opts?) -> {map, rung, refType, plain, layout, lanes: [lane]}
  //   lane = {key, name, targets: [...], plain: [...], missing, rung?}
  //   target = {lane, item_id, x, col, item, enc, label}, in x order (ties: array order).
  //   The no-interview lane is drawn from its own nodes (SS.layout.place(...).script): the first node on a step is a
  //   target at that step's x (with `text`, the node's own label, and `node_id`); every other node is a `plain` entry
  //   on a row below, in its phase column (`row`; the lane has layout.scriptRows rows). The other two lanes hold every item.
  //   A lane whose map is missing has no targets and `missing` says so.
  //   plain map (no items): `plain` holds the lane's nodes, {lane, node_id, x, col, node, enc, label}, in array order, and
  //   only the no-interview lane and the AI's lane exist (a lane with nothing to draw is left out). opts.allRungs gives
  //   the AI one lane for each rung of the view (the viewer uses it when it has no rung selector).
  function model(view, mapIdx, rung, opts) {
    var map = mapAt(view, mapIdx);
    var phases = view && Array.isArray(view.phases) ? view.phases : [];
    var refType = view && view.reference_type ? view.reference_type : null;
    var plain = isPlain(view, mapIdx);
    if (plain) return plainModel(view, map, phases, refType, rung, opts || {});
    var layout = SS.layout.place(map || { items: [] }, phases);
    var items = map && Array.isArray(map.items) ? map.items : [];
    var extras = aiExtras(map, layout);
    var lanes = LANES.map(function (def) {
      var targets = [];
      var status = def.key === 'reference' ? null : laneStatus(map, def.key, rung);
      var plainEntries = [];
      if (!status && def.key === 'script') {
        var byId = Object.create(null);
        items.forEach(function (item, idx) { if (item) byId[item.item_id] = { item: item, idx: idx }; });
        layout.script.forEach(function (e, idx) {
          var text = e.node && e.node.label !== undefined && e.node.label !== null ? String(e.node.label) : '';
          var enc = { shape: 'script', glyph: '' };
          if (e.item_id) {
            var hit = byId[e.item_id];
            targets.push({
              lane: 'script', item_id: e.item_id, node_id: e.node_id, x: e.x, col: e.col, item: hit.item, enc: enc,
              idx: hit.idx, text: text, label: SS.a11y.laneLabel('script', { label: text }, rung, refType)
            });
          } else {
            plainEntries.push({ lane: 'script', node_id: e.node_id, x: e.x, col: e.col, row: e.row, node: e.node, label: text, enc: enc, only: true });
          }
        });
        targets.sort(function (a, b) { return a.x !== b.x ? a.x - b.x : a.idx - b.idx; });
      } else if (!status) {
        items.forEach(function (item, idx) {
          var enc = SS.encoding.forItem(def.key, item, rung);
          var slot = layout.slots[item.item_id];
          if (!enc || !slot) return;
          targets.push({
            lane: def.key, item_id: item.item_id, x: slot.x, col: slot.col, item: item, enc: enc, idx: idx,
            label: SS.a11y.laneLabel(def.key, item, rung, refType)
          });
        });
        targets.sort(function (a, b) { return a.x !== b.x ? a.x - b.x : a.idx - b.idx; });
        if (def.key === 'ai' && hasOwn(extras.byRung, rung)) plainEntries = extras.byRung[rung];
      }
      return { key: def.key, name: laneName(def.key, refType), targets: targets, plain: plainEntries, missing: status ? missingText(def.key) : null };
    });
    return { map: map, rung: rung, refType: refType, plain: false, layout: layout, lanes: lanes, aiRows: extras.rows };
  }

  function plainModel(view, map, phases, refType, rung, opts) {
    var pl = SS.layout.placePlain(map, phases);
    pl.slots = Object.create(null);
    function entries(key, r) {
      var list = key === 'script' ? pl.lanes.script : (hasOwn(pl.lanes.ai, r) ? pl.lanes.ai[r] : []);
      return list.map(function (e) {
        var label = e.node && e.node.label !== undefined && e.node.label !== null ? String(e.node.label) : '';
        return { lane: key, node_id: e.node_id, x: e.x, col: e.col, node: e.node, label: label,
                 enc: { shape: key === 'script' ? 'script' : 'normal', glyph: '' } };
      });
    }
    function lane(key, r) {
      var status = laneStatus(map, key, r);
      var l = { key: key, name: laneName(key, refType), targets: [], plain: status ? [] : entries(key, r),
                missing: status ? missingText(key) : null };
      if (key === 'ai') l.rung = r;
      return l;
    }
    var out = [lane('script', null)];
    var rungs = opts.allRungs && view && Array.isArray(view.rungs) && view.rungs.length ? view.rungs : [rung];
    rungs.forEach(function (r) { out.push(lane('ai', r)); });
    out = out.filter(function (l) { return l.plain.length || l.missing; });
    return { map: map, rung: rung, refType: refType, plain: true, layout: pl, lanes: out };
  }

  // Tab order: lanes top to bottom, then x.
  function focusOrder(m) {
    var out = [];
    m.lanes.forEach(function (l) { l.targets.forEach(function (t) { out.push(t); }); });
    return out;
  }

  function laneIndex(m, key) {
    for (var i = 0; i < m.lanes.length; i++) if (m.lanes[i].key === key) return i;
    return -1;
  }

  // The target a key press moves to, or null (no such neighbour, or not a navigation key).
  //   Left/Right/Home/End along the lane; Up/Down to the neighbouring lane that has any item,
  //   onto the same item if that lane draws it, else the nearest by x (the earlier one on a tie).
  function nextFocus(m, cur, key) {
    var li = laneIndex(m, cur.lane);
    if (li < 0) return null;
    var targets = m.lanes[li].targets;
    var pos = -1;
    for (var i = 0; i < targets.length; i++) if (targets[i].item_id === cur.item_id) pos = i;
    if (pos < 0) return null;
    if (key === 'ArrowRight') return targets[pos + 1] || null;
    if (key === 'ArrowLeft') return pos > 0 ? targets[pos - 1] : null;
    if (key === 'Home') return targets[0];
    if (key === 'End') return targets[targets.length - 1];
    if (key !== 'ArrowUp' && key !== 'ArrowDown') return null;
    var step = key === 'ArrowDown' ? 1 : -1;
    for (var j = li + step; j >= 0 && j < m.lanes.length; j += step) {
      var cand = m.lanes[j].targets;
      if (!cand.length) continue;
      var best = null;
      for (var k = 0; k < cand.length; k++) {
        if (cand[k].item_id === cur.item_id) return cand[k];
        if (best === null || Math.abs(cand[k].x - cur.x) < Math.abs(best.x - cur.x)) best = cand[k];
      }
      return best;
    }
    return null;
  }

  function el(tag, cls, text, parent) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null && text !== '') n.textContent = text;
    if (parent) parent.appendChild(n);
    return n;
  }

  function buildItem(m, t, opts, pid, parent) {
    var btn = el('button', 'ss-item ss-lane-' + t.lane + ' ' + SS.encoding.shapeClass(t.enc.shape), undefined, parent);
    btn.setAttribute('type', 'button');
    btn.setAttribute('data-lane', t.lane);
    btn.setAttribute('data-item-id', t.item_id);
    btn.setAttribute('aria-label', t.label);
    var text = t.text !== undefined ? t.text : (t.item.label === undefined || t.item.label === null ? '' : String(t.item.label));
    btn.setAttribute('title', text);
    if (t.node_id !== undefined) btn.setAttribute('data-node-id', String(t.node_id));
    btn.setAttribute('tabindex', '-1');
    if (opts.item !== undefined && opts.item !== null && opts.item === t.item_id) {
      btn.className += ' is-selected';
      btn.setAttribute('aria-current', 'true');
    }
    btn.style.left = t.x + 'px';
    btn.style.width = SS.layout.ITEM_W + 'px';
    if (t.enc.glyph) {
      var g = el('span', 'ss-item-glyph ' + (t.enc.glyph === '▲' ? 'ss-glyph-up' : 'ss-glyph-down'), t.enc.glyph, btn);
      g.setAttribute('aria-hidden', 'true');
    }
    el('span', 'ss-item-label', text, btn);
    var tag = t.enc.tag || (t.enc.shape === 'unknown' ? SS.t('lane.item_not_computed') : '');
    if (tag) el('span', 'ss-item-tag', tag, btn);
    return btn;
  }

  // A plain node: a box with no button, no fate and nothing to open (there is no reference item behind it).
  // inList: the track is a list (a plain map); a no-interview node beside the steps of a compared map is plain text.
  // lift: the lane's row 0 holds no step on this map, so its plain rows move up one row (item 17: no empty band).
  // On a compared map (inList false) the box can take focus (tabindex -1, reached with the arrow keys or a tap), so its
  // sentence (item 18) shows for keyboard and touch too.
  function buildPlain(t, parent, rung, inList, lift) {
    var box = el('div', 'ss-item ss-plain-node ss-lane-' + t.lane + ' ' + SS.encoding.shapeClass(t.enc.shape), undefined, parent);
    if (inList !== false) box.setAttribute('role', 'listitem');
    else {
      box.setAttribute('role', 'group');
      box.setAttribute('aria-label', t.label);
      box.setAttribute('tabindex', '-1');
    }
    box.setAttribute('data-plain', 'true');
    box.setAttribute('data-lane', t.lane);
    box.setAttribute('data-node-id', String(t.node_id));
    if (t.lane === 'ai' && rung) box.setAttribute('data-rung', rung);
    box.setAttribute('title', t.label);
    box.style.left = t.x + 'px';
    if (t.row) box.style.top = (lift ? ROW_TOP + (t.row - 1) * EXTRA_ROW_H : rowTop(t.row)) + 'px';
    box.style.width = SS.layout.ITEM_W + 'px';
    el('span', 'ss-item-label', t.label, box);
    return box;
  }

  // How many readings are drawn: mode C has no reference map, so two (no-interview and the AI's); A and B draw three.
  function readingCount(view) { return view && view.reference_type === 'C' ? 2 : 3; }
  // SS.lanes.countT(key, view, vars?): the string for the count drawn; the two-reading wording lives under `<key>.two`.
  function countT(key, view, vars) { return SS.t(readingCount(view) === 2 ? key + '.two' : key, vars); }

  // SS.lanes.render(root, view, mapIdx, rung, opts?) -> {model, focus(lane, item_id)}
  //   opts.gloss === false leaves out the "map of the official pathway" gloss (it belongs once per page).
  //   opts.item marks the selected item. Enter / click on an item emits "item:open" {item_id, map, rung, lane}.
  //   opts.allRungs: for a plain map, one AI lane for each rung (see model).
  function render(rootEl, view, mapIdx, rung, opts) {
    opts = opts || {};
    var m = model(view, mapIdx, rung, { allRungs: opts.allRungs === true });
    var pid = m.map ? m.map.pid : null;
    rootEl.textContent = '';
    var wrap = el('div', 'ss-lanes', undefined, rootEl);
    wrap.setAttribute('role', 'group');
    wrap.setAttribute('aria-label', countT('lane.panel_label', view));

    var order = focusOrder(m);
    var drawable = order.length > 0 || m.plain || m.lanes.some(function (l) { return !!l.missing; });
    if (!m.map || !drawable) {
      el('p', 'ss-lanes-empty muted', SS.t('lane.empty'), wrap);
      return { model: m, focus: function () {} };
    }

    // item 18: the status line (what happened to the box under the pointer or in focus) and the hidden list of
    // sentences the boxes point to with aria-describedby. Only a compared map has statuses.
    var status = null;
    var notes = null;
    if (!m.plain) {
      status = el('p', 'ss-box-status', SS.t('box.hint'), wrap);
      status.setAttribute('aria-hidden', 'true');          // screen readers hear the same sentence as the box description
      status.setAttribute('data-part', 'box-status');
      notes = el('div', 'ss-box-notes', undefined, wrap);
      notes.hidden = true;
    }

    var hint = el('p', 'ss-scroll-hint muted', SS.t('scroll.hint.phases'), wrap);
    hint.hidden = true;
    var scroll = el('div', 'ss-lanes-scroll', undefined, wrap);
    if (SS.scrollCue) SS.scrollCue(scroll, wrap, hint);
    var inner = el('div', 'ss-lanes-inner', undefined, scroll);
    inner.style.width = m.layout.width + 'px';

    var head = el('div', 'ss-phases', undefined, inner);
    m.layout.columns.forEach(function (c) {
      var p = el('div', 'ss-phase', SS.layout.columnName(c), head);
      p.style.left = c.x0 + 'px';
      p.style.width = (c.x1 - c.x0) + 'px';
    });

    var buttons = {};
    var all = [];
    var seqs = {};                   // lane key -> [entry] in arrow-key order: the steps by x, then the extra boxes by row and x
    var entries = [];                // every box with a sentence: {el, t, lane, text}
    var lit = [];
    var chosen = null;               // the entry of the selected step: its sentence stays when the pointer leaves
    function setRoving(btn) {
      all.forEach(function (b) { b.setAttribute('tabindex', b === btn ? '0' : '-1'); });
    }
    function keyOf(lane, id) { return lane + '\u0000' + id; }

    function describe(node, text) {
      var id = 'ss-box-note-' + (++noteSeq);
      var n = el('span', '', text, notes);
      n.setAttribute('id', id);
      node.setAttribute('aria-describedby', id);
      node.setAttribute('data-status', text);
    }
    function partnersOf(entry) {
      if (entry.t.only || entry.t.item_id === undefined) return [];
      return entries.filter(function (e) {
        return e !== entry && e.lane !== entry.lane && !e.t.only && e.t.item_id === entry.t.item_id && realBox(e.t, rung);
      });
    }
    function unlight() {
      lit.forEach(function (n) { n.setAttribute('data-light', ''); });
      lit = [];
    }
    function light(entry) {
      unlight();
      entry.el.setAttribute('data-light', 'self');
      lit.push(entry.el);
      partnersOf(entry).forEach(function (p) { p.el.setAttribute('data-light', 'partner'); lit.push(p.el); });
      status.textContent = entry.text;
      status.setAttribute('data-on', 'true');
    }
    function rest() {
      if (chosen) { light(chosen); return; }
      unlight();
      status.textContent = SS.t('box.hint');
      status.setAttribute('data-on', 'false');
    }
    // Up/Down from an extra box: the neighbouring lane that has steps, onto the nearest step by x (the earlier on a tie).
    function nearestIn(from, key) {
      var li = laneIndex(m, from.lane);
      var step = key === 'ArrowDown' ? 1 : -1;
      for (var j = li + step; j >= 0 && j < m.lanes.length; j += step) {
        var cand = m.lanes[j].targets;
        if (!cand.length) continue;
        var best = null;
        cand.forEach(function (c) { if (best === null || Math.abs(c.x - from.x) < Math.abs(best.x - from.x)) best = c; });
        return best;
      }
      return null;
    }
    function onKey(entry) {
      return function (ev) {
        if (!ev || !Object.prototype.hasOwnProperty.call(NAV_KEYS, ev.key)) return;
        if (ev.preventDefault) ev.preventDefault();
        var seq = seqs[entry.lane] || [];
        var i = seq.indexOf(entry);
        var target = null;
        if (ev.key === 'ArrowRight') target = seq[i + 1] || null;
        else if (ev.key === 'ArrowLeft') target = i > 0 ? seq[i - 1] : null;
        else if (ev.key === 'Home') target = seq[0] || null;
        else if (ev.key === 'End') target = seq[seq.length - 1] || null;
        else {
          var nxt = entry.t.only ? nearestIn(entry.t, ev.key) : nextFocus(m, entry.t, ev.key);
          target = nxt ? byKey[keyOf(nxt.lane, nxt.item_id)] || null : null;
        }
        if (target) { setRoving(target.el); target.el.focus(); }
      };
    }
    var byKey = {};
    function watch(entry) {
      entries.push(entry);
      (seqs[entry.lane] = seqs[entry.lane] || []).push(entry);
      describe(entry.el, entry.text);
      entry.el.addEventListener('mouseenter', function () { light(entry); });
      entry.el.addEventListener('mouseleave', rest);
      entry.el.addEventListener('focus', function () { light(entry); });
      entry.el.addEventListener('blur', rest);
    }

    m.lanes.forEach(function (lane) {
      var sec = el('section', 'ss-lane ss-lane-row-' + lane.key, undefined, inner);
      sec.setAttribute('role', 'group');
      sec.setAttribute('data-lane-row', lane.key);
      var title = lane.name;
      if (lane.rung && opts.allRungs === true) {
        title = SS.t('lane.ai_at_rung', { lane: lane.name, rung: rungName(lane.rung) });
        sec.setAttribute('data-rung', lane.rung);
      }
      sec.setAttribute('aria-label', title);
      el('h3', 'ss-lane-title', title, sec);
      if (lane.key === 'script' && opts.gloss !== false) el('p', 'ss-lane-gloss', SS.t('lane.no_interview_gloss'), sec);
      if (lane.missing) {
        el('p', 'ss-lane-missing muted', lane.missing, sec);
        return;
      }
      var track = el('div', 'ss-track', undefined, sec);
      track.style.width = m.layout.width + 'px';
      // A no-interview lane with no node on any step of this map has an empty row 0: its plain rows move up, so the
      // lane is only as tall as its boxes (item 17).
      var lift = lane.key === 'script' && !m.plain && m.layout.scriptRows > 1 && lane.targets.length === 0;
      if (lane.key === 'script' && !m.plain && m.layout.scriptRows > 1) track.style.height = trackHeight(m.layout.scriptRows - (lift ? 1 : 0)) + 'px';
      if (lane.key === 'ai' && !m.plain && m.aiRows > 1) track.style.height = trackHeight(m.aiRows) + 'px';
      if (m.plain) track.setAttribute('role', 'list');
      var extra = lane.plain.map(function (t) { return { t: t, el: buildPlain(t, track, lane.rung || rung, m.plain, lift) }; });
      lane.targets.forEach(function (t) {
        var btn = buildItem(m, t, opts, pid, track);
        buttons[keyOf(t.lane, t.item_id)] = btn;
        all.push(btn);
        btn.addEventListener('focus', function () { setRoving(btn); });
        btn.addEventListener('click', function () {
          SS.bus.emit('item:open', { item_id: t.item_id, map: pid, rung: rung, lane: t.lane });
        });
        if (m.plain) return;
        var entry = { el: btn, t: t, lane: t.lane, text: boxSentence(t.lane, t.item, rung, m.refType) };
        byKey[keyOf(t.lane, t.item_id)] = entry;
        watch(entry);
        btn.addEventListener('keydown', onKey(entry));
      });
      if (m.plain) return;
      extra.sort(function (a, b) { return (a.t.row || 0) !== (b.t.row || 0) ? (a.t.row || 0) - (b.t.row || 0) : a.t.x - b.t.x; });
      extra.forEach(function (x) {
        var entry = { el: x.el, t: x.t, lane: x.t.lane, text: boxSentence(x.t.lane, null, rung, m.refType, true) };
        all.push(x.el);
        watch(entry);
        x.el.addEventListener('focus', function () { setRoving(x.el); });
        x.el.addEventListener('click', function () { if (typeof x.el.focus === 'function') x.el.focus(); });
        x.el.addEventListener('keydown', onKey(entry));
      });
    });

    // the selected step keeps its sentence and its lit partners (a tap on a touch screen selects the step)
    if (!m.plain && opts.item !== undefined && opts.item !== null) {
      chosen = byKey[keyOf(opts.itemLane || 'reference', opts.item)] || byKey[keyOf('reference', opts.item)] || null;
      if (chosen) light(chosen);
    }

    phaseRules(m.layout, inner);

    // One tab stop for the whole panel (roving tabindex), on the first item in tab order.
    if (!m.plain && all.length) all.forEach(function (b) { b.setAttribute('tabindex', '-1'); });
    if (order.length) {
      var firstTarget = order[0];
      setRoving(buttons[keyOf(firstTarget.lane, firstTarget.item_id)]);
    }

    return {
      model: m,
      focus: function (lane, itemId) {
        var b = buttons[keyOf(lane, itemId)];
        if (b) { setRoving(b); b.focus(); }
      }
    };
  }

  // SS.lanes.phaseRules(layout, inner): one thin dashed line at each phase boundary, the full height of the panel's
  // inner box (phase header, every lane, the overlay's gutter), so the phases read as separate areas (item 16). They
  // replace the per-track solid separators. Decorative: aria-hidden, no pointer events.
  function phaseRules(layout, inner) {
    (layout && layout.columns || []).forEach(function (c, i) {
      if (i === 0) return;
      var r = el('div', 'ss-phase-rule', undefined, inner);
      r.setAttribute('aria-hidden', 'true');
      r.style.left = c.x0 + 'px';
    });
  }

  SS.lanes = { LANES: LANES, phaseRules: phaseRules, boxSentence: boxSentence, sentenceFor: sentenceFor, aiExtras: aiExtras, ROW_H: ROW_H, ROW_TOP: ROW_TOP, EXTRA_ROW_H: EXTRA_ROW_H, rowTop: rowTop, trackHeight: trackHeight, model: model, isPlain: isPlain, readingCount: readingCount, countT: countT, focusOrder: focusOrder, nextFocus: nextFocus, render: render };
})();
