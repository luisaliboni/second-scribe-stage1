/* SS.state: the one place that says which view, map, rung and item are selected.
   The viewer reads it, every panel renders from it, and the URL hash is its spelling:
     #view=lanes|overlay|columns&map=<pid>&rung=<key>&item=<item_id>
   Switching the view changes only `view`, so the selected map, rung and item carry over (spec 6.3).
   Unknown values fall back to: lanes, the first map, rung2, no item.
   No DOM, no storage, no location: the viewer (Task 27) reads and writes location.hash with toHash / fromHash.
   Classic script; attaches to window.SS (after core.js). */
(function () {
  'use strict';
  var root = typeof window !== 'undefined' ? window : globalThis;
  var SS = root.SS = root.SS || {};
  var hasOwn = Object.prototype.hasOwnProperty;

  var VIEWS = ['lanes', 'overlay', 'columns'];
  var KEYS = ['view', 'map', 'rung', 'item'];
  var DEFAULT_RUNG = 'rung2';

  var bound = null;          // the view data the state is checked against (SS.state.use)
  var current = null;        // the current normalised state

  function isString(v) { return typeof v === 'string' && v !== ''; }

  function mapOf(pid) {
    var maps = bound && Array.isArray(bound.maps) ? bound.maps : [];
    for (var i = 0; i < maps.length; i++) if (maps[i] && maps[i].pid === pid) return maps[i];
    return null;
  }

  function hasItem(map, id) {
    var items = map && Array.isArray(map.items) ? map.items : [];
    for (var i = 0; i < items.length; i++) if (items[i] && items[i].item_id === id) return true;
    return false;
  }

  // Any object (a hash result, a patch merged over the current state) -> a state that is valid for the bound data.
  //   Without bound data only the view can be checked; map, rung and item are kept when they are non-empty strings.
  function normalize(raw) {
    var r = raw && typeof raw === 'object' ? raw : {};
    var s = { view: 'lanes', map: null, rung: DEFAULT_RUNG, item: null };
    if (isString(r.view) && VIEWS.indexOf(r.view) >= 0) s.view = r.view;
    if (!bound) {
      if (isString(r.map)) s.map = r.map;
      if (isString(r.rung)) s.rung = r.rung;
      if (isString(r.item)) s.item = r.item;
      return s;
    }
    var maps = Array.isArray(bound.maps) ? bound.maps : [];
    var map = isString(r.map) ? mapOf(r.map) : null;
    if (!map) map = maps[0] || null;
    s.map = map && isString(map.pid) ? map.pid : null;
    var rungs = Array.isArray(bound.rungs) ? bound.rungs : [];
    if (isString(r.rung) && rungs.indexOf(r.rung) >= 0) s.rung = r.rung;
    else if (rungs.indexOf(DEFAULT_RUNG) < 0 && rungs.length) s.rung = rungs[0];
    if (isString(r.item) && hasItem(map, r.item)) s.item = r.item;
    return s;
  }

  function copy(s) { return { view: s.view, map: s.map, rung: s.rung, item: s.item }; }

  function ensure() {
    if (!current) current = normalize({});
    return current;
  }

  function decode(text) {
    try { return decodeURIComponent(text); } catch (e) { return null; }
  }

  // '#view=lanes&map=A&rung=rung2&item=a1' -> state. A part that is malformed is ignored, so one bad
  // part does not spoil the others. Never throws.
  function fromHash(h) {
    var raw = Object.create(null);
    var text = typeof h === 'string' ? h.replace(/^#/, '') : '';
    text.split('&').forEach(function (part) {
      var i = part.indexOf('=');
      if (i <= 0) return;
      var k = decode(part.slice(0, i));
      var v = decode(part.slice(i + 1));
      if (k === null || v === null || KEYS.indexOf(k) < 0) return;
      raw[k] = v;
    });
    return normalize(raw);
  }

  // state -> '#view=...&map=...&rung=...&item=...'; empty parts (no map, no item) are left out.
  function toHash(s) {
    var n = s && typeof s === 'object' ? s : {};
    var parts = [];
    KEYS.forEach(function (k) {
      var v = n[k];
      if (isString(v)) parts.push(k + '=' + encodeURIComponent(v));
    });
    return '#' + parts.join('&');
  }

  function get() { return copy(ensure()); }

  // set(patch) -> the new state. Merges the patch over the current state, then checks the whole thing
  // like a hash (unknown values fall back, an item the map does not have is dropped), so changing the map
  // keeps view and rung and clears an item that belongs to the old map. Emits "state:change"
  // {state, prev, changed: [keys]} only when something actually changed.
  function set(patch) {
    var prev = copy(ensure());
    var merged = copy(prev);
    if (patch && typeof patch === 'object') {
      KEYS.forEach(function (k) { if (hasOwn.call(patch, k)) merged[k] = patch[k]; });
    }
    var next = normalize(merged);
    var changed = KEYS.filter(function (k) { return next[k] !== prev[k]; });
    current = next;
    if (changed.length) SS.bus.emit('state:change', { state: copy(next), prev: prev, changed: changed });
    return copy(next);
  }

  // use(viewData): check the state against these maps and rungs from now on. Re-checks the current state.
  function use(viewData) {
    bound = viewData && typeof viewData === 'object' ? viewData : null;
    var prev = current ? copy(current) : null;
    current = normalize(prev || {});
    if (prev) {
      var changed = KEYS.filter(function (k) { return current[k] !== prev[k]; });
      if (changed.length) SS.bus.emit('state:change', { state: copy(current), prev: prev, changed: changed });
    }
    return copy(current);
  }

  SS.state = { VIEWS: VIEWS, get: get, set: set, toHash: toHash, fromHash: fromHash, use: use };
})();
