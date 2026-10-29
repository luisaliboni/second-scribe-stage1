/* SS.place: every app screen keeps the reader's place when it draws itself again (item 38; item 30's code, which
   lived in app-setup.js, moved here). Classic script; needs core.js. It registers no screen. The screens call it
   only from their handlers, never while the scripts load, so the order of app-*.js files does not matter.

   What "the place" is: the scroll position, the control used last, the folded parts (<details>) the reader had
   opened, and a result line beside the control.
   - SS.place.watch(main) records the control used last inside `main` (click, focus, change), before the action's
     own handler runs. SS.place.remember(main, node) does the same by hand; SS.place.forget(main) drops it (a fresh
     visit to a screen starts at the top).
   - SS.place.keep(main, redraw, opts) is for a redraw that waits for the server: the page keeps its height
     (min-height) and its old content until redraw() resolves, then the scroll position comes back, the focus
     returns to the same control (found again by what it is: tag, type, name, class, data attributes, label or text,
     and its place among controls like it) or, when that control is gone, to the nearest usable control in the same
     card (data-role, data-suffix, data-pid, data-kind, data-group, data-export), and opts.message appears as a status
     line right after that control's row (opts.error: the same place, as an alert).
   - SS.place.redraw(main, draw, opts) is the same for a redraw that happens at once (a screen that polls). It moves
     the focus only when the focus was inside `main` (or on nothing, right after an action), and scrolls only when
     the position moved, so a poll never pulls the reader back.
   Data reaches the page through textContent and attributes only. */
(function () {
  'use strict';
  var root = typeof window !== 'undefined' ? window : globalThis;
  var SS = root.SS = root.SS || {};
  var CONTROL = { BUTTON: 1, INPUT: 1, SELECT: 1, TEXTAREA: 1, SUMMARY: 1 };
  var AREA_ATTRS = ['data-role', 'data-suffix', 'data-pid', 'data-kind', 'data-group', 'data-export', 'data-quick'];
  var KEY_ATTRS = ['type', 'name', 'class', 'aria-label', 'data-role', 'data-suffix', 'data-pid', 'data-kind',
    'data-color', 'data-action', 'data-group', 'data-run', 'data-type', 'data-quick'];
  // Rows a result line goes after, never inside: a row of buttons, a check line, a group of options.
  var ROW = /(^|\s)(ss-models-actions|ss-setup-check|ss-setup-pick|ss-setup-remove-buttons|ss-ref-options|ss-ins-modes|ss-ins-actions|ss-coding-actions|ss-export-open|ss-res-tabs|ss-quick-actions|ss-quick-where)(\s|$)/;
  var RECENT_MS = 10000;     // redraw(): a control used this recently still counts when the focus fell to nothing

  function el(doc, tag, cls, text) {
    var n = doc.createElement(tag);
    if (cls) n.setAttribute('class', cls);
    if (text !== undefined) n.textContent = text;
    return n;
  }

  function docOf(node) {
    return (node && node.ownerDocument) || root.document;
  }

  function now() {
    return typeof Date.now === 'function' ? Date.now() : 0;
  }

  function kids(node) {
    var out = [];
    var c = node && node.children;
    for (var i = 0; c && i < c.length; i++) out.push(c[i]);
    return out;
  }

  function attr(node, k) {
    return node && typeof node.getAttribute === 'function' ? node.getAttribute(k) : null;
  }

  function parentOf(node) {
    return node ? node.parentNode || node.parent || null : null;
  }

  function isControl(node) {
    return !!node && (CONTROL[node.tagName] === 1 || (node.tagName === 'A' && attr(node, 'href') !== null));
  }

  function within(main, node) {
    for (var n = node; n; n = parentOf(n)) if (n === main) return true;
    return false;
  }

  // Every control under `node`, in page order.
  function controls(node, out) {
    out = out || [];
    kids(node).forEach(function (c) {
      if (isControl(c)) out.push(c);
      controls(c, out);
    });
    return out;
  }

  // What a control is, without the generated ids and numbers that change on each draw.
  function keyOf(node) {
    var parts = [node.tagName];
    KEY_ATTRS.forEach(function (k) {
      var v = attr(node, k);
      parts.push(v === null ? '' : String(v).replace(/[0-9]+/g, '#'));
    });
    var type = attr(node, 'type');
    if (type === 'radio' || type === 'checkbox') parts.push(attr(node, 'value') || '');
    if (node.tagName === 'BUTTON' || node.tagName === 'A' || node.tagName === 'SUMMARY') parts.push(node.textContent || '');
    return parts.join('|');
  }

  // The nearest card around `node` that names itself, as "attr=value".
  function areaOf(node, stop) {
    for (var n = parentOf(node); n && n !== stop; n = parentOf(n)) {
      for (var i = 0; i < AREA_ATTRS.length; i++) {
        var v = attr(n, AREA_ATTRS[i]);
        if (v !== null) return { node: n, key: AREA_ATTRS[i] + '=' + v };
      }
    }
    return null;
  }

  function areaNode(main, key) {
    var found = null;
    (function walk(n) {
      kids(n).forEach(function (c) {
        if (found) return;
        for (var i = 0; i < AREA_ATTRS.length; i++) {
          if (AREA_ATTRS[i] + '=' + attr(c, AREA_ATTRS[i]) === key) { found = c; return; }
        }
        walk(c);
      });
    })(main);
    return found;
  }

  // What kind of control it is, without its label or text: when the control itself is gone (a removed mark), the
  // focus goes to the one of the same kind now at its place (the next mark), or to the last one of that kind.
  function looseOf(node) {
    return [node.tagName, attr(node, 'type') || '', attr(node, 'class') || ''].join('|');
  }

  // Where a control is: its key, its place among controls with the same key, and its card with its place there.
  function anchorOf(main, node) {
    var key = keyOf(node);
    var all = controls(main);
    var same = all.filter(function (c) { return keyOf(c) === key; });
    var loose = looseOf(node);
    var kin = all.filter(function (c) { return looseOf(c) === loose; });
    var area = areaOf(node, main);
    var inArea = area ? controls(area.node) : [];
    return { key: key, nth: Math.max(0, same.indexOf(node)), area: area && area.key, at: Math.max(0, inArea.indexOf(node)),
      loose: loose, kin: Math.max(0, kin.indexOf(node)) };
  }

  // A control the person can reach: not disabled, not a hidden input, not inside a hidden or folded part of the page.
  function usableControl(c, main) {
    if (!c || c.disabled || attr(c, 'type') === 'hidden') return false;
    for (var n = c; n && n !== main; n = parentOf(n)) {
      if (n.hidden === true) return false;
      // inside a closed <details>, only its own summary can take the focus
      var up = parentOf(n);
      if (up && up.tagName === 'DETAILS' && n.tagName !== 'SUMMARY' && !isOpen(up)) return false;
    }
    return true;
  }

  // The control to focus after a redraw: the same one if it is there, else the nearest usable one in its card.
  function findAgain(main, anchor) {
    if (!anchor) return null;
    var same = controls(main).filter(function (c) { return keyOf(c) === anchor.key; });
    if (anchor.area) {
      var a = areaNode(main, anchor.area);
      var inArea = same.filter(function (c) { var r = areaOf(c, main); return r && r.key === anchor.area; });
      if (inArea.length) same = inArea;
      else if (a) {
        var list = controls(a);
        // the same place in the card when the card still has it (the next step of a form), else its first control
        for (var i = anchor.at < list.length ? anchor.at : 0; i < list.length; i++) if (usableControl(list[i], main)) return list[i];
        for (var j = list.length - 1; j >= 0; j--) if (usableControl(list[j], main)) return list[j];
        return null;
      }
    }
    var pick = same[Math.min(anchor.nth, same.length - 1)];
    if (!pick && anchor.loose) {
      var kin = controls(main).filter(function (c) { return looseOf(c) === anchor.loose && usableControl(c, main); });
      pick = kin[Math.min(anchor.kin, kin.length - 1)];
    }
    if (!pick) return null;
    if (usableControl(pick, main)) return pick;
    // the same control is there but cannot take the focus (disabled, or folded away): the next usable one after it
    var all = controls(main);
    for (var k = all.indexOf(pick) + 1; k < all.length; k++) if (usableControl(all[k], main)) return all[k];
    return pick;
  }

  // A result line goes after the control's row, not inside a row of buttons, a label or a group of options.
  function rowOf(node, main) {
    var n = node;
    var p = parentOf(n);
    while (p && p !== main && (p.tagName === 'LABEL' || ROW.test(attr(p, 'class') || ''))) {
      n = p;
      p = parentOf(p);
    }
    return n;
  }

  function insertAfter(node, extra) {
    var p = parentOf(node);
    if (!p) return false;
    var list = kids(p);
    var next = list[list.indexOf(node) + 1];
    if (next && typeof p.insertBefore === 'function') p.insertBefore(extra, next);
    else p.appendChild(extra);
    return true;
  }

  // Every <details> under `node`, in page order, as [node, key]; the key is its class and its summary's text.
  function detailsIn(node, out) {
    out = out || [];
    kids(node).forEach(function (c) {
      if (c.tagName === 'DETAILS') {
        var sum = kids(c).filter(function (k) { return k.tagName === 'SUMMARY'; })[0];
        out.push([c, (attr(c, 'class') || '') + '|' + (sum ? sum.textContent || '' : '')]);
      }
      detailsIn(c, out);
    });
    return out;
  }

  function isOpen(d) {
    return d.open === true || attr(d, 'open') !== null;
  }

  // The folded parts the reader had opened ("key#n"), so a redraw opens them again (it never closes one).
  function openParts(main) {
    var seen = {};
    var out = [];
    detailsIn(main).forEach(function (pair) {
      var n = seen[pair[1]] = (seen[pair[1]] || 0) + 1;
      if (isOpen(pair[0])) out.push(pair[1] + '#' + n);
    });
    return out;
  }

  function reopen(main, keys) {
    if (!keys || !keys.length) return;
    var seen = {};
    detailsIn(main).forEach(function (pair) {
      var n = seen[pair[1]] = (seen[pair[1]] || 0) + 1;
      if (keys.indexOf(pair[1] + '#' + n) >= 0 && !isOpen(pair[0])) {
        pair[0].open = true;
        if (typeof pair[0].setAttribute === 'function') pair[0].setAttribute('open', '');
      }
    });
  }

  function focusOn(target) {
    if (!target || typeof target.focus !== 'function') return;
    try { target.focus({ preventScroll: true }); } catch (e) { target.focus(); }
  }

  // The place to come back to: the scroll position and the control (focused now, else used last).
  // fresh: how recent a remembered control must be when the focus fell to nothing (keep: any age).
  function capture(main, fresh) {
    var doc = docOf(main);
    var active = doc ? doc.activeElement : undefined;
    var body = doc ? doc.body : undefined;
    var inside = !!active && active !== body && within(main, active);
    var anchor = null;
    if (inside && isControl(active)) anchor = anchorOf(main, active);
    else if (!active || active === body || inside) {
      var last = main.ssPlace;
      if (last && last.anchor && (typeof fresh !== 'number' || now() - last.at <= fresh)) anchor = last.anchor;
    }
    return { y: typeof root.scrollY === 'number' ? root.scrollY : null, anchor: anchor, open: openParts(main) };
  }

  // After the redraw: focus, scroll, the result line. Returns the control that took the focus (or null).
  function restore(main, place, opts) {
    opts = opts || {};
    reopen(main, place.open);
    var target = opts.target || findAgain(main, place.anchor);
    if (target) focusOn(target);
    var y = place.y;
    if (typeof y === 'number' && typeof root.scrollTo === 'function' && (opts.always || root.scrollY !== y)) root.scrollTo(0, y);
    if (main.style) main.style.minHeight = '';
    if (target) {
      main.ssPlace = { anchor: anchorOf(main, target), at: now() };
      if (opts.reveal !== false && typeof target.getBoundingClientRect === 'function' && typeof root.innerHeight === 'number') {
        var r = target.getBoundingClientRect();
        if ((r.top < 0 || r.bottom > root.innerHeight) && typeof target.scrollIntoView === 'function') {
          target.scrollIntoView({ block: 'center' });
        }
      }
    }
    var text = opts.error || opts.message;
    if (text) {
      var note = el(docOf(main), 'p', (opts.error ? 'ss-setup-error' : 'ss-setup-ok') + ' ss-setup-after ss-place-after', text);
      note.setAttribute('role', opts.error ? 'alert' : 'status');
      if (!target || !insertAfter(rowOf(target, main), note)) {
        var page = main.children && main.children.length ? main.children[0] : null;
        (page || main).appendChild(note);
      }
    }
    return target;
  }

  SS.place = {
    keyOf: keyOf,
    // Item 37: the one line that says why a control cannot be used, with a link to the step that fixes it when the
    // fix is elsewhere (href and linkText; omitted when the fix is right beside the control). The control should
    // name it in aria-describedby (id).
    why: function why(doc, id, text, href, linkText) {
      var p = el(doc, 'p', 'ss-setup-hint ss-why');
      if (id) p.setAttribute('id', id);
      p.appendChild(el(doc, 'span', '', text + (href ? ' ' : '')));
      if (href) {
        var a = el(doc, 'a', 'ss-why-fix', linkText);
        a.setAttribute('href', href);
        p.appendChild(a);
      }
      return p;
    },
    // Record the control used last in `main`, before the action's own handler runs.
    watch: function watch(main) {
      if (!main || main.ssPlaceWatched || typeof main.addEventListener !== 'function') return;
      main.ssPlaceWatched = true;
      var note = function (ev) {
        var t = ev && ev.target;
        while (t && t !== main && !isControl(t)) t = parentOf(t);
        if (t && t !== main) SS.place.remember(main, t);
      };
      main.addEventListener('click', note, true);
      main.addEventListener('focusin', note, true);
      main.addEventListener('change', note, true);
    },
    remember: function remember(main, node) {
      if (!main || !node) return;
      main.ssPlace = { anchor: anchorOf(main, node), at: now() };
    },
    forget: function forget(main) {
      if (main) main.ssPlace = null;
    },
    // A redraw that waits: redraw() may return a promise. opts.message / opts.error: the result line;
    // opts.target: the control to focus (or a function giving it after the redraw), else the one used last;
    // opts.still(): false when another screen took `main` meanwhile (then only the height is let go).
    keep: function keep(main, redraw, opts) {
      opts = opts || {};
      var place = capture(main);
      if (main.style && typeof main.offsetHeight === 'number' && main.offsetHeight > 0) {
        main.style.minHeight = main.offsetHeight + 'px';
      }
      var done = function () {
        if (typeof opts.still === 'function' && !opts.still()) {
          if (main.style) main.style.minHeight = '';
          return null;
        }
        var target = typeof opts.target === 'function' ? opts.target() : opts.target;
        return restore(main, place, { message: opts.message, error: opts.error, always: true, target: target || null });
      };
      var p;
      try { p = Promise.resolve(redraw()); } catch (e) { p = Promise.reject(e); }
      return p.then(done, function (err) {
        done();
        throw err;
      });
    },
    // A redraw that happens at once. Returns what draw() returned.
    redraw: function redraw(main, draw, opts) {
      opts = opts || {};
      var place = capture(main, RECENT_MS);
      var out = draw();
      restore(main, place, { message: opts.message, error: opts.error, reveal: opts.reveal === true });
      return out;
    }
  };
})();
