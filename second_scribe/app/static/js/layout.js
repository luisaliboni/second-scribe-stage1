/* SS.layout: phase columns and one x per item, shared by every lane and every rung.
   Pure arithmetic on view-data/1 maps; no DOM. Classic script; attaches to window.SS (after core.js). */
(function () {
  'use strict';
  var root = typeof window !== 'undefined' ? window : globalThis;
  var SS = root.SS = root.SS || {};

  var ITEM_W = 150;                    // width of an item box, px
  var SLOT_W = 162;                    // one slot: the box plus a gap
  var COL_PAD = 10;                    // padding inside a phase column, each side

  function isInt(n) {
    return typeof n === 'number' && isFinite(n) && Math.floor(n) === n && n >= 0;
  }

  function hasPhase(p) { return typeof p === 'string' && p !== ''; }
  function nodesOf(entry) { return entry && Array.isArray(entry.nodes) ? entry.nodes : []; }
  function aiLanes(map) {
    var ai = map && map.lanes && map.lanes.ai && typeof map.lanes.ai === 'object' ? map.lanes.ai : {};
    return ai;
  }

  // The name a column is headed with: its phase, or "No phase" for the trailing column of steps without one.
  function columnName(c) {
    if (!c) return '';
    if (c.noPhase) return SS.t('lane.no_phase');
    return c.phase === undefined || c.phase === null ? '' : String(c.phase);
  }

  // SS.layout.place(map, phases) -> {columns: [{phase, x0, x1, slots, noPhase?}], slots: {item_id: {x, col, slot}}, width,
  //                                   script: [{node_id, node, x, col, slot, row, item_id?}], scriptRows}
  //   columns follow the Setup order of `phases`; a phase named only by an item is appended after them.
  //   An item (or an AI node that matched none) with no phase is not dropped: a last column, {phase: null, noPhase: true},
  //   holds it, headed "No phase" (columnName); that column exists only when something needs it.
  //   Inside a phase an item sits at its x_slot (the rank the exporter gave it); a clash moves the later item
  //   (array order) to the next free slot. The result depends on the map only, never on a lane or a rung,
  //   so an item has the same x everywhere.
  //   `script` places each node of the no-interview map (lanes.script.nodes, array order): the first node that names an
  //   item (item_id) sits at that item's x, row 0, and keeps item_id; every other node (a second node on the same item,
  //   a gutter node, a node with neither field) goes to the rows below (row 1, 2, ...) of its column, filling the
  //   column's slots left to right: the item's column for a node that names one, else its phase's column (no phase: the
  //   "No phase" column; a phase no column has: the first). So nothing sits over a step it does not match, and the
  //   columns keep the width of their items. `scriptRows` is how many rows the no-interview lane needs (at least 1).
  function place(map, phases) {
    var items = map && Array.isArray(map.items) ? map.items : [];
    var order = Array.isArray(phases) ? phases.slice() : [];
    var known = Object.create(null);
    order.forEach(function (p) { known[p] = true; });
    var noPhase = false;
    items.forEach(function (it) {
      if (!it) return;
      if (hasPhase(it.phase)) {
        if (!known[it.phase]) { known[it.phase] = true; order.push(it.phase); }
      } else {
        noPhase = true;
      }
    });
    var ai = aiLanes(map);
    Object.keys(ai).forEach(function (rung) {
      nodesOf(ai[rung]).forEach(function (n) { if (n && n.gutter === true && !hasPhase(n.phase)) noPhase = true; });
    });
    var itemIds = Object.create(null);
    items.forEach(function (it) { if (it && typeof it.item_id === 'string') itemIds[it.item_id] = it; });
    var scriptNodes = nodesOf(map && map.lanes && typeof map.lanes === 'object' ? map.lanes.script : null);
    var onItem = Object.create(null);                 // item_id -> node_id of the first node that sits on it
    var scriptKind = scriptNodes.map(function (n) {
      if (!n) return null;
      var id = typeof n.item_id === 'string' && itemIds[n.item_id] ? n.item_id : null;
      if (id !== null && !onItem[id]) { onItem[id] = true; return { on: id }; }
      var phase = id !== null ? itemIds[id].phase : n.phase;
      if (!hasPhase(phase)) noPhase = true;
      return { extra: true, phase: phase };
    });
    var keys = order.map(function (phase) { return { phase: phase, noPhase: false }; });
    if (noPhase) keys.push({ phase: null, noPhase: true });
    function colOf(phase) {
      for (var i = 0; i < keys.length; i++) if (hasPhase(phase) ? keys[i].phase === phase : keys[i].noPhase) return i;
      return 0;
    }
    var extras = keys.map(function () { return []; });
    scriptKind.forEach(function (k, i) { if (k && k.extra) extras[colOf(k.phase)].push(i); });
    var scriptAt = [];
    var scriptRows = 1;

    var columns = [];
    var slots = Object.create(null);
    var x0 = 0;
    keys.forEach(function (key, col) {
      var mine = [];
      items.forEach(function (it, idx) {
        if (it && (key.noPhase ? !hasPhase(it.phase) : it.phase === key.phase)) mine.push({ it: it, idx: idx });
      });
      mine.sort(function (a, b) {
        var sa = isInt(a.it.x_slot) ? a.it.x_slot : Infinity;
        var sb = isInt(b.it.x_slot) ? b.it.x_slot : Infinity;
        return sa !== sb ? (sa < sb ? -1 : 1) : a.idx - b.idx;
      });
      var used = Object.create(null);
      var count = 0;
      mine.forEach(function (m) {
        var s = isInt(m.it.x_slot) ? m.it.x_slot : 0;
        while (used[s]) s++;
        used[s] = true;
        if (s + 1 > count) count = s + 1;
        slots[m.it.item_id] = { x: x0 + COL_PAD + s * SLOT_W, col: col, slot: s };
      });
      var n = Math.max(count, 1);
      extras[col].forEach(function (i, k) {
        var slot = k % n;
        var row = 1 + Math.floor(k / n);
        if (row + 1 > scriptRows) scriptRows = row + 1;
        scriptAt[i] = { x: x0 + COL_PAD + slot * SLOT_W, col: col, slot: slot, row: row };
      });
      var x1 = x0 + 2 * COL_PAD + n * SLOT_W;
      var c = { phase: key.phase, x0: x0, x1: x1, slots: n };
      if (key.noPhase) c.noPhase = true;
      columns.push(c);
      x0 = x1;
    });
    var script = [];
    scriptNodes.forEach(function (n, i) {
      var k = scriptKind[i];
      if (!k) return;
      if (k.on) {
        var sl = slots[k.on];
        script.push({ node_id: n.node_id, node: n, x: sl.x, col: sl.col, slot: sl.slot, row: 0, item_id: k.on });
      } else {
        var at = scriptAt[i];
        script.push({ node_id: n.node_id, node: n, x: at.x, col: at.col, slot: at.slot, row: at.row });
      }
    });
    return { columns: columns, slots: slots, width: x0, script: script, scriptRows: scriptRows };
  }

  // SS.layout.placePlain(map, phases) -> {columns, width, has, lanes: {script: [...], reference: [...], ai: {rung: [...]}}}
  //   For a map with no reference items (mode C, AI maps only): the nodes of each lane, placed by phase in their own
  //   order, one slot each; entry = {node_id, node, phase, x, col, slot}. A node's phase that Setup does not list is
  //   appended as a column, and a node with no phase goes to the trailing no-phase column. A column is as wide as the
  //   most any lane or rung puts in that phase, so the columns never change with the rung. `has` says whether any lane
  //   has a node. Depends on the map only.
  function placePlain(map, phases) {
    var lanes = map && map.lanes && typeof map.lanes === 'object' ? map.lanes : {};
    var ai = aiLanes(map);
    var lists = [];                                     // [{set, key, rung, nodes}]
    lists.push({ key: 'script', rung: null, nodes: nodesOf(lanes.script) });
    lists.push({ key: 'reference', rung: null, nodes: nodesOf(lanes.reference) });
    Object.keys(ai).forEach(function (rung) { lists.push({ key: 'ai', rung: rung, nodes: nodesOf(ai[rung]) }); });

    var order = Array.isArray(phases) ? phases.slice() : [];
    var known = Object.create(null);
    order.forEach(function (p) { known[p] = true; });
    var noPhase = false;
    var has = false;
    lists.forEach(function (l) {
      l.nodes.forEach(function (n) {
        if (!n) return;
        has = true;
        if (hasPhase(n.phase)) { if (!known[n.phase]) { known[n.phase] = true; order.push(n.phase); } }
        else noPhase = true;
      });
    });
    var keys = order.map(function (phase) { return { phase: phase, noPhase: false }; });
    if (noPhase) keys.push({ phase: null, noPhase: true });
    var colOf = function (n) {
      for (var i = 0; i < keys.length; i++) {
        if (keys[i].noPhase ? !hasPhase(n.phase) : n.phase === keys[i].phase) return i;
      }
      return 0;
    };

    var most = keys.map(function () { return 1; });
    var placed = lists.map(function (l) {
      var per = keys.map(function () { return 0; });
      var out = [];
      l.nodes.forEach(function (n) {
        if (!n) return;
        var col = colOf(n);
        var slot = per[col]++;
        if (per[col] > most[col]) most[col] = per[col];
        out.push({ node_id: n.node_id, node: n, phase: hasPhase(n.phase) ? n.phase : null, col: col, slot: slot });
      });
      return out;
    });
    var columns = [];
    var x0 = 0;
    keys.forEach(function (key, col) {
      var x1 = x0 + 2 * COL_PAD + most[col] * SLOT_W;
      var c = { phase: key.phase, x0: x0, x1: x1, slots: most[col] };
      if (key.noPhase) c.noPhase = true;
      columns.push(c);
      x0 = x1;
    });
    var out = { columns: columns, width: x0, has: has, lanes: { script: [], reference: [], ai: {} } };
    lists.forEach(function (l, i) {
      var entries = placed[i].map(function (e) {
        e.x = columns[e.col].x0 + COL_PAD + e.slot * SLOT_W;
        return e;
      });
      if (l.key === 'ai') out.lanes.ai[l.rung] = entries;
      else out.lanes[l.key] = entries;
    });
    return out;
  }

  SS.layout = {
    ITEM_W: ITEM_W, SLOT_W: SLOT_W, COL_PAD: COL_PAD,
    place: place, placePlain: placePlain, columnName: columnName, hasPhase: hasPhase
  };
})();
