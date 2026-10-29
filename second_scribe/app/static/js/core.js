/* SS core: strings, number formatting, safe lookup, event bus, view-data version gate.
   Classic script; attaches to window.SS. No DOM, no network, no storage. */
(function () {
  'use strict';
  var root = typeof window !== 'undefined' ? window : globalThis;
  var SS = root.SS = root.SS || {};
  var hasOwn = Object.prototype.hasOwnProperty;

  function logError(msg) {
    if (typeof console !== 'undefined' && console.error) console.error(msg);
  }

  function isObject(v) {
    return v !== null && typeof v === 'object';
  }

  // A value that was not recorded: absent, null, NaN, or a {status: ...} stand-in. Never shown as 0.
  function isMissing(v) {
    if (v === undefined || v === null) return true;
    if (typeof v === 'number' && v !== v) return true;
    return isObject(v) && !Array.isArray(v) && hasOwn.call(v, 'status');
  }

  function notComputed() {
    return SS.t('common.not_computed');
  }

  // SS.t(key, slots?): text from window.SS_STRINGS. Unknown key returns the key and logs an error.
  // With slots, {name} is replaced once (a slot value is never re-expanded); {{ and }} are literal
  // braces, as in Python's str.format_map, so en.json reads the same on both sides.
  SS.t = function t(key, slots) {
    var table = root.SS_STRINGS;
    if (!isObject(table) || typeof key !== 'string' || !hasOwn.call(table, key) || typeof table[key] !== 'string') {
      logError('SS.t: unknown string key: ' + String(key));
      return String(key);
    }
    var text = table[key];
    if (!isObject(slots)) return text;
    return text.replace(/\{\{|\}\}|\{([A-Za-z0-9_]+)\}/g, function (m, name) {
      if (m === '{{') return '{';
      if (m === '}}') return '}';
      if (hasOwn.call(slots, name) && slots[name] !== undefined && slots[name] !== null) return String(slots[name]);
      logError('SS.t: missing slot {' + name + '} for key ' + key);
      return m;
    });
  };

  // SS.tn(key, n, slots?): SS.t for a text with a count. When n is exactly 1 and the table has "<key>_one", that
  // singular text is used ("1 mark"); any other n, or a key with no singular, reads as SS.t(key). Slots as in SS.t.
  SS.tn = function tn(key, n, slots) {
    var table = root.SS_STRINGS;
    var one = typeof key === 'string' ? key + '_one' : key;
    if (Number(n) === 1 && isObject(table) && hasOwn.call(table, one) && typeof table[one] === 'string') return SS.t(one, slots);
    return SS.t(key, slots);
  };

  function oneDecimal(x) {
    return String(Math.round(x * 10) / 10);
  }

  SS.fmt = {
    // Any value for display. Missing is "not computed", never 0; a recorded 0 is "0".
    value: function value(v) {
      if (isMissing(v)) return notComputed();
      if (isObject(v)) return JSON.stringify(v);
      return String(v);
    },
    // "x of n"
    count: function count(x, n) {
      if (isMissing(x) || isMissing(n)) return notComputed();
      return SS.t('common.count', { x: x, n: n });
    },
    // ratio 0..1 -> "42%"
    pct: function pct(r) {
      if (isMissing(r) || typeof r !== 'number' || !isFinite(r)) return notComputed();
      return String(Math.round(Number((r * 100).toFixed(8)))) + '%';
    },
    // lo and hi are already in percentage points -> "a-b points" (one decimal at most)
    ci: function ci(lo, hi) {
      if (isMissing(lo) || isMissing(hi) || typeof lo !== 'number' || typeof hi !== 'number' ||
          !isFinite(lo) || !isFinite(hi)) return notComputed();
      return SS.t('common.ci_points', { lo: oneDecimal(lo), hi: oneDecimal(hi) });
    }
  };

  // SS.get(obj, "a.b.c"): own properties only, undefined for any missing step. Never throws, never 0.
  SS.get = function get(obj, path) {
    if (typeof path !== 'string' || path === '') return undefined;
    var steps = path.split('.');
    var cur = obj;
    for (var i = 0; i < steps.length; i++) {
      if (!isObject(cur) || !hasOwn.call(cur, steps[i])) return undefined;
      cur = cur[steps[i]];
    }
    return cur;
  };

  // SS.bus: on(name, fn) -> unsubscribe function; emit(name, payload). A throwing handler is
  // reported and does not stop the others.
  var handlers = {};
  SS.bus = {
    on: function on(name, fn) {
      if (typeof fn !== 'function') throw new TypeError('SS.bus.on needs a function');
      if (!hasOwn.call(handlers, name)) handlers[name] = [];
      handlers[name].push(fn);
      return function off() {
        var list = hasOwn.call(handlers, name) ? handlers[name] : [];
        var i = list.indexOf(fn);
        if (i >= 0) list.splice(i, 1);
      };
    },
    emit: function emit(name, payload) {
      if (!hasOwn.call(handlers, name)) return;
      var list = handlers[name].slice();
      for (var i = 0; i < list.length; i++) {
        try {
          list[i](payload);
        } catch (e) {
          logError('SS.bus handler for ' + name + ' failed: ' + (e && e.message ? e.message : String(e)));
        }
      }
    }
  };

  // SS.view.load(doc) -> doc. Major version 1 only ("view-data/1", or "view-data/1.<minor>").
  SS.view = {
    load: function load(doc) {
      var ok = isObject(doc) && typeof doc.schema === 'string' && /^view-data\/1(?:[.\-+].*)?$/.test(doc.schema);
      if (!ok) throw new Error('unsupported view data major version');
      return doc;
    }
  };
})();
