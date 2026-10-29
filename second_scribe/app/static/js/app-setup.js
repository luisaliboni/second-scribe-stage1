/* SS.setup: the Setup screen (Tasks 32-35; spec 6.5) and its step registry. Classic script; needs core.js,
   api.js and app-shell.js.

   Each Setup step lives in its own file, static/js/app-setup-<step>.js, and registers itself. The server loads
   app-*.js files sorted by name, so every app-setup-<step>.js runs BEFORE this file; a step therefore queues its
   definition instead of calling SS.setup directly:

     (function () {
       var root = typeof window !== 'undefined' ? window : globalThis;
       var SS = root.SS = root.SS || {};
       (SS.setupQueue = SS.setupQueue || []).push(['models', {
         order: 60,                         // position on the page (project 10, context 20, then the later steps)
         titleKey: 'setup.models.title',    // the step's heading
         needsProject: true,                // default true: shown only once a usable project is open
         render: function (container, project) { ... }
       }]);
     })();

   This file drains the queue when it loads and then replaces SS.setupQueue with an object whose push registers
   at once, so the order of loading never matters. SS.setup.step(id, def) registers directly (tests use it).
   render(container, project) gets an empty <div> and the GET /api/project summary (null when no project is open).
   A step that changed the project calls SS.setup.refresh(), which fetches the summary again and redraws. The hash
   is #setup/<step id>; without one the page opens on the project step.

   Item 30 (23 Oct): a refresh keeps the place: the page keeps its height while it redraws (no "Loading" in
   between), the scroll position comes back, focus returns to the control used last, and SS.setup.refresh({ message })
   shows the result as a status line next to that control. Part 5c (item 38): that code is now the shared helper
   SS.place (app-place.js), used by every app screen.

   Part 5c (scout F15): the step list marks each step "Done", "Needs you", "Optional" or "Running", with counts where
   they help ("Done · 12 of 12"), in words beside the step's name (never colour only). The marks come from
   GET /api/runs/ready, the readiness the Run screen shows, and are read again after every action the server accepts
   (the "api:posted" event of SS.api). */
(function () {
  'use strict';
  var root = typeof window !== 'undefined' ? window : globalThis;
  var SS = root.SS = root.SS || {};
  var hasOwn = Object.prototype.hasOwnProperty;
  var ID = /^[a-z][a-z0-9-]*$/;
  var steps = {};
  var mounted = null;       // {main, sub} of the last render, for refresh()
  var ready = null;         // the last GET /api/runs/ready reply, for the step marks (F15)
  var marksTimer = null;
  var offPosted = null;
  // A step -> what marks it: the readiness item of GET /api/runs/ready (Part 5b) that the step fixes.
  var MARK_ITEM = { context: 'context', import: 'pairing', labels: 'labels', maps: 'maps', models: 'models',
    check: 'model_check' };

  function el(doc, tag, cls, text) {
    var n = doc.createElement(tag);
    if (cls) n.setAttribute('class', cls);
    if (text !== undefined) n.textContent = text;
    return n;
  }

  function clear(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
  }

  function docOf(node) {
    return node.ownerDocument || root.document;
  }

  // A project the steps may use: open, and not held back by the sync-folder warning.
  function usable(project) {
    return !!project && !project.blocked;
  }

  function isObj(v) {
    return v !== null && typeof v === 'object' && !Array.isArray(v);
  }

  function itemOf(r, key) {
    var items = isObj(r) && Array.isArray(r.items) ? r.items : [];
    for (var i = 0; i < items.length; i++) if (isObj(items[i]) && items[i].key === key) return items[i];
    return null;
  }

  // F15: a step's mark. {state: 'done' | 'needs' | 'running' | 'optional', count: '12 of 12' or ''}, or null when
  // nothing is known yet. The project step is done once a usable project is open; the missing rule always has a
  // rule in force (the default until one is saved), so it is optional; Check the maps is optional while no map is
  // paired (references B and C need no map).
  function stepState(id, project, r) {
    if (id === 'project') return { state: usable(project) ? 'done' : 'needs', count: '' };
    if (!usable(project) || !isObj(r)) return null;
    if (id === 'rule') return { state: 'optional', count: '' };
    var key = MARK_ITEM[id];
    if (!key) return null;
    var item = itemOf(r, key);
    if (!item) return id === 'maps' ? { state: 'optional', count: '' } : null;
    var count = typeof item.done === 'number' && typeof item.total === 'number'
      ? SS.t('setup.mark.count', { done: String(item.done), total: String(item.total) }) : '';
    if (key === 'model_check' && item.state === 'running') return { state: 'running', count: '' };
    return { state: item.ok ? 'done' : 'needs', count: count };
  }

  // Writes the marks into a drawn step list (no fetch).
  function paintMarks(nav, project, r) {
    kids(nav).forEach(function (li) {
      var id = li.getAttribute('data-step');
      var a = kids(li)[0];
      if (!a) return;
      var old = kids(a).filter(function (c) { return /ss-setup-step-(mark|sep)/.test(c.getAttribute('class') || ''); });
      old.forEach(function (c) { a.removeChild(c); });
      var st = stepState(id, project, r);
      li.setAttribute('data-state', st ? st.state : 'unknown');
      if (!st) return;
      var text = SS.t('setup.mark.' + st.state) + (st.count ? ' · ' + st.count : '');
      var mark = el(docOf(nav), 'span', 'ss-setup-step-mark', text);
      mark.setAttribute('data-state', st.state);
      a.appendChild(el(docOf(nav), 'span', 'ss-visually-hidden ss-setup-step-sep', ', '));
      a.appendChild(mark);
    });
  }

  function kids(node) {
    var out = [];
    var c = node && node.children;
    for (var i = 0; c && i < c.length; i++) out.push(c[i]);
    return out;
  }

  // Reads GET /api/runs/ready again and repaints the marks of the step list on show. Calls close together become one.
  function updateMarks() {
    if (!mounted) return Promise.resolve();
    var m = mounted;
    if (marksTimer !== null && typeof root.clearTimeout === 'function') root.clearTimeout(marksTimer);
    return new Promise(function (resolve) {
      var go = function () {
        marksTimer = null;
        if (mounted !== m || !usable(m.project)) { resolve(); return; }
        SS.api.get('/api/runs/ready').then(function (doc) {
          if (mounted !== m) return;
          ready = isObj(doc) ? doc : null;
          if (m.nav) paintMarks(m.nav, m.project, ready);
        }, function () { /* the marks are a convenience; the steps say what they need */ }).then(resolve, resolve);
      };
      if (typeof root.setTimeout === 'function') marksTimer = root.setTimeout(go, 60);
      else go();
    });
  }

  SS.setup = {
    step: function step(id, def) {
      if (typeof id !== 'string' || !ID.test(id)) throw new Error('SS.setup.step: bad step id ' + String(id));
      if (hasOwn.call(steps, id)) throw new Error('SS.setup.step: step ' + id + ' is already registered');
      if (!def || typeof def.render !== 'function' || typeof def.titleKey !== 'string') {
        throw new Error('SS.setup.step: step ' + id + ' needs titleKey and render');
      }
      steps[id] = { id: id, order: typeof def.order === 'number' ? def.order : 100, titleKey: def.titleKey,
        needsProject: def.needsProject !== false, render: def.render };
    },
    // Step ids by order, then id.
    list: function list() {
      return Object.keys(steps).sort(function (a, b) {
        return (steps[a].order - steps[b].order) || (a < b ? -1 : a > b ? 1 : 0);
      });
    },
    // The step for the hash part after '#setup/': that step when registered, else the lowest-ordered one.
    pick: function pick(sub) {
      var want = typeof sub === 'string' ? sub.split('/')[0] : '';
      if (want && hasOwn.call(steps, want)) return want;
      var ids = SS.setup.list();
      return ids.length ? ids[0] : '';
    },
    usable: usable,
    // Draws the Setup page into `main` for the given project summary (no fetch; render() fetches).
    draw: function draw(main, sub, project) {
      var doc = docOf(main);
      clear(main);
      var page = el(doc, 'div', 'ss-setup');
      page.appendChild(el(doc, 'h1', 'ss-setup-title', SS.t('setup.title')));
      var ids = SS.setup.list();
      if (!ids.length) {
        page.appendChild(el(doc, 'p', 'ss-shell-note', SS.t('shell.empty')));
        main.appendChild(page);
        return;
      }
      var current = SS.setup.pick(sub);
      var nav = el(doc, 'ol', 'ss-setup-steps');
      nav.setAttribute('aria-label', SS.t('setup.steps_label'));
      ids.forEach(function (id) {
        var li = el(doc, 'li', 'ss-setup-step');
        li.setAttribute('data-step', id);
        var a = el(doc, 'a', 'ss-setup-step-link');
        a.appendChild(el(doc, 'span', 'ss-setup-step-name', SS.t(steps[id].titleKey)));
        a.setAttribute('href', '#setup/' + id);
        if (id === current) a.setAttribute('aria-current', 'step');
        if (steps[id].needsProject && !usable(project)) li.setAttribute('data-locked', 'true');
        li.appendChild(a);
        nav.appendChild(li);
      });
      page.appendChild(nav);
      paintMarks(nav, project, ready);
      if (mounted && mounted.main === main) {
        mounted.nav = nav;
        mounted.project = project;
      }

      var section = el(doc, 'section', 'ss-setup-body');
      section.setAttribute('aria-labelledby', 'ss-setup-step-title');
      var h2 = el(doc, 'h2', 'ss-setup-step-title', SS.t(steps[current].titleKey));
      h2.setAttribute('id', 'ss-setup-step-title');
      section.appendChild(h2);
      var container = el(doc, 'div', 'ss-setup-step-body');
      section.appendChild(container);
      page.appendChild(section);
      main.appendChild(page);

      if (steps[current].needsProject && !usable(project)) {
        var note = el(doc, 'p', 'ss-shell-note', SS.t(project ? 'setup.needs_ack' : 'setup.needs_project'));
        container.appendChild(note);
        var back = el(doc, 'a', 'ss-setup-back', SS.t('setup.go_project'));
        back.setAttribute('href', '#setup/' + ids[0]);
        container.appendChild(back);
        return;
      }
      SS.place.watch(main);
      try {
        return steps[current].render(container, project);
      } catch (e) {
        if (typeof console !== 'undefined' && console.error) console.error(e);
        clear(container);
        container.appendChild(el(doc, 'p', 'ss-shell-note', SS.t('shell.screen_failed')));
      }
    },
    // The screen's render (SS.shell): fetch the project summary, then draw.
    // keep (from refresh): leave the page as it is until the new one is drawn.
    render: function render(main, sub, keep) {
      if (!keep) SS.place.forget(main);
      mounted = { main: main, sub: sub, nav: null, project: null };
      if (!offPosted && SS.bus && typeof SS.bus.on === 'function') {
        // every action the server accepted may change a mark (importing and the missing rule update in place)
        offPosted = SS.bus.on('api:posted', function () { if (mounted) updateMarks(); });
      }
      if (!keep) {
        clear(main);
        main.appendChild(el(docOf(main), 'p', 'ss-shell-note', SS.t('shell.loading')));
      }
      return SS.api.get('/api/project').then(function (doc) {
        if (!mounted || mounted.main !== main || mounted.sub !== sub) return;   // a newer render took over
        var drawn = SS.setup.draw(main, sub, doc.project || null);
        updateMarks();
        return drawn;
      }, function (err) {
        if (!mounted || mounted.main !== main) return;
        clear(main);
        main.appendChild(el(docOf(main), 'p', 'ss-shell-note', (err && err.message) || SS.t('api.failed')));
      });
    },
    // Fetch the summary again and redraw the step on show (after a step changed the project).
    // opts.message: a result line shown next to the control used. Keeps the scroll position and the focus.
    refresh: function refresh(opts) {
      if (!mounted) return Promise.resolve();
      var main = mounted.main;
      var sub = mounted.sub;
      return SS.place.keep(main, function () { return SS.setup.render(main, sub, true); }, {
        message: opts && typeof opts.message === 'string' ? opts.message : '',
        still: function () { return !!mounted && mounted.main === main && mounted.sub === sub; }
      });
    },
    // The control used last (set by the step's click, focus and change events; tests call it directly).
    remember: function remember(node) {
      if (!mounted || !node) return;
      SS.place.remember(mounted.main, node);
    },
    keyOf: function keyOf(node) { return SS.place.keyOf(node); },
    // F15: a step's mark from a GET /api/runs/ready reply ({state, count} or null), and a fresh read of the marks.
    stepState: stepState,
    updateMarks: updateMarks
  };

  // Steps that loaded before this file queued themselves; register them, then let later pushes register at once.
  var queued = Array.isArray(SS.setupQueue) ? SS.setupQueue : [];
  SS.setupQueue = {
    push: function push(entry) {
      if (!entry || typeof entry[0] !== 'string') throw new Error('SS.setupQueue.push: expects [id, def]');
      SS.setup.step(entry[0], entry[1]);
      return 0;
    }
  };
  queued.forEach(function (entry) {
    try {
      SS.setupQueue.push(entry);
    } catch (e) {
      if (typeof console !== 'undefined' && console.error) console.error(e);
    }
  });

  if (SS.shell && typeof SS.shell.register === 'function') {
    SS.shell.register('setup', { order: 10, titleKey: 'setup.title', render: SS.setup.render });
  }
})();
