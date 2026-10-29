/* SS.viewer.mount(root, doc, opts): the one viewer for Try it, the app's Results and the report (Task 27).
     doc            a view-data/1 file (SS.view.load refuses any other major version, with a plain message)
     opts.profile   an orgsci-profile/v2 file: when given, SS.profile.render is the results headline, above the lanes,
                    and its absence and instruction rows drill down through SS.strip.drill (plan V2, V3)
     opts.compactProfile  true: the profile shows its lead block only, the rest in a closed "Show the full profile"
                    (SS.profile.render's compact option; the app's Results page does not set it)
     opts.profileHost  an element outside the viewer: the profile section is drawn there instead of above the lanes
                    (item 11: Try it puts the whole profile in its closed "Technical details"); the lanes then print
                    the no-interview gloss themselves
     opts.derived   false: the profile leaves out the lead's "yield per box" lines (the app's Results and the report)
     opts.beforeLedger  an element placed between the drawer and the absent-steps table (item 40: the app's Results
                    and the report put their plain sentences there, after the maps)
   Sections, in order (each has a data-section attribute):
     bands     SYNTHETIC band and test-pattern label (source "synthetic"), or "Your data · reference: ..." (source "own");
               in reference type B also the fixed note, once per screen
     profile   the profile panel (only with opts.profile)
     views     map picker and the stamp (item 42: one row), the three lanes, then the selector of the instructions given to the AI (item 19:
               placed after the maps). Item 20: the lanes are the one view; there is no view switch, and a hash that
               names another view still draws the lanes.
     drawer    the evidence drawer for the chosen step, and the steps behind a number
     ledger    the absent-steps table
   State (SS.state) is the one source for view, map, rung and item; location.hash is its spelling and is kept in sync.
   The viewer answers "item:open" with SS.state.set({item}) (and the map, and the rung when the lane is the AI's) and
   "number:open" {row, rung} by listing the steps behind that number in the drawer.
   No judge (one model family: run.models.judge absent, or states.needs_judge): every comparison, no-interview map,
   absence and ladder panel reads "Needs a judge from another model family" instead of "not computed" (spec 2.2).
   The AI's own maps need no judge, so under that sentence the views section still draws them: the no-interview map and
   the AI's map at every rung as plain boxes (SS.views.render with allRungs), with a map picker and no rung selector.
   Data goes into the page through textContent and attributes only. mount() returns {unmount()}: it removes the
   viewer's listeners and leaves the page as it is. Classic script; attaches to window.SS (loaded last). */
(function () {
  'use strict';
  var root = typeof window !== 'undefined' ? window : globalThis;
  var SS = root.SS = root.SS || {};
  var hasOwn = Object.prototype.hasOwnProperty;
  var STATE_KEYS = ['view', 'map', 'rung', 'item'];
  var DRILL_ROWS = [5, 8];

  function isString(v) { return typeof v === 'string' && v !== ''; }

  function el(tag, cls, text, parent) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null && text !== '') n.textContent = text;
    if (parent) parent.appendChild(n);
    return n;
  }
  function section(parent, name) {
    var s = el('section', 'ss-viewer-section ss-section-' + name, undefined, parent);
    s.setAttribute('data-section', name);
    return s;
  }
  function hasString(key) {
    var t = root.SS_STRINGS;
    return t !== null && typeof t === 'object' && hasOwn.call(t, key) && typeof t[key] === 'string';
  }

  function noJudge(view) {
    var judge = SS.get(view, 'run.models.judge');
    if (judge === undefined || judge === null || judge === '') return true;
    return SS.get(view, 'states.needs_judge') === true;
  }

  // The part of location.hash the viewer does not own (a dev page's theme=dark, say) is kept.
  function foreignHashParts() {
    var h = typeof location !== 'undefined' && typeof location.hash === 'string' ? location.hash.replace(/^#/, '') : '';
    return h.split('&').filter(function (part) {
      var i = part.indexOf('=');
      return i > 0 && STATE_KEYS.indexOf(part.slice(0, i)) < 0;
    });
  }
  function writeHash(state) {
    var h = SS.state.toHash(state);
    var extra = foreignHashParts();
    if (extra.length) h += (h === '#' ? '' : '&') + extra.join('&');
    try {
      if (typeof history !== 'undefined' && history && typeof history.replaceState === 'function') history.replaceState(null, '', h);
      else location.hash = h;
    } catch (e) {
      try { location.hash = h; } catch (e2) { /* a sandboxed frame may refuse; the page still works */ }
    }
  }

  // ------------------------------------------------------------------------------------------- bands
  function referenceWord(view) {
    var t = view.reference_type;
    return SS.t(t === 'A' ? 'band.reference.participant' : (t === 'B' ? 'band.reference.researcher' : 'band.reference.none'));
  }

  function drawBands(host, view, profile) {
    host.textContent = '';
    function band(kind, cls, text) {
      var b = el('div', 'ss-band ss-band-' + cls, text, host);
      b.setAttribute('data-band', kind);
      return b;
    }
    // The SYNTHETIC band goes first, once; a synthetic profile is told not to print its own (syntheticBand: false).
    if (view.source === 'synthetic' || (!!profile && profile.state === 'synthetic')) band('synthetic', 'synthetic', SS.t('band.synthetic'));
    if (view.test_pattern === true) band('test_pattern', 'test', SS.t('band.test_pattern'));
    if (view.source === 'own') band('own', 'own', SS.t('band.own', { reference: referenceWord(view) }));
    if (view.reference_type === 'B') {
      var note = el('p', 'ss-modeb-note', SS.t('strip.mode_b_note'), host);
      note.setAttribute('data-part', 'mode_b_note');
    }
  }

  // ------------------------------------------------------------------------------------------- panels
  function needsJudge(host, titleKey, cls) {
    host.textContent = '';
    var box = el('div', 'ss-needs-judge ' + (cls || ''), undefined, host);
    box.setAttribute('data-needs-judge', 'true');
    el('h3', 'ss-needs-judge-title', SS.t(titleKey), box);
    el('p', 'ss-needs-judge-text', SS.t('common.needs_judge'), box);
    return box;
  }

  function mount(rootEl, doc, opts) {
    opts = opts && typeof opts === 'object' ? opts : {};
    rootEl.textContent = '';
    var view;
    try {
      view = SS.view.load(doc);
    } catch (e) {
      var msg = el('div', 'ss-viewer ss-viewer-unsupported', undefined, rootEl);
      el('p', 'ss-viewer-message', SS.t('viewer.unsupported'), msg);
      return { unmount: function () {} };
    }

    var modeB = view.reference_type === 'B';
    var judgeless = noJudge(view);
    var profile = opts.profile && typeof opts.profile === 'object' ? opts.profile : null;
    var behind = null;               // {row}: the drawer also lists the steps behind a number
    var pendingFocus = null;         // keep keyboard focus on the same lane item across a re-render
    var views = null;                // {focus} of the last SS.views.render
    var offs = [];
    var onHash = null;

    SS.state.use(view);
    var fromHash = SS.state.fromHash(typeof location !== 'undefined' ? location.hash : '');
    fromHash.view = 'lanes';                                  // item 20: one view
    SS.state.set(fromHash);
    var itemLane = null;             // the lane the selected step was chosen in (item 18: its sentence leads the drawer)

    var wrap = el('div', 'ss-viewer', undefined, rootEl);
    var bandsHost = section(wrap, 'bands');
    drawBands(bandsHost, view, profile);

    // ---- profile (the results headline)
    var profileApart = !!(profile && opts.profileHost && typeof opts.profileHost.appendChild === 'function');
    if (profile) {
      if (profileApart) opts.profileHost.textContent = '';
      var profHost = section(profileApart ? opts.profileHost : wrap, 'profile');
      SS.profile.render(profHost, profile, {
        gloss: !profileApart,
        syntheticBand: false,
        compact: opts.compactProfile === true,           // Try it: the lead first, the rest on request
        derived: opts.derived,                           // false: no "yield per box" lines (app Results, report)
        mode: opts.mode,
        referenceType: view.reference_type,
        note: false,                                   // the viewer prints the fixed note once, in the bands
        drill: function (rowKey, container, row, o) { drill(rowKey, container, row, o); }
      });
    }

    // ---- views
    var viewsHost = section(wrap, 'views');
    var viewsBody = null;
    var pickerHost = null;
    var selectorHost = null;
    var panelHost = null;
    var stampHost = null;
    var selectorInput = null;        // the checked rung radio of the last draw: where "Show the steps at ..." lands
    var plainBody = false;           // judgeless: the AI's maps are still drawn, plain
    if (judgeless) {
      needsJudge(viewsHost, SS.lanes.readingCount(view) === 2 ? 'viewer.panel.views.two' : 'viewer.panel.views', 'ss-needs-judge-views');
      if (Array.isArray(view.maps) && view.maps.some(function (m, i) { return SS.lanes.isPlain(view, i); })) {
        var plainControls = el('div', 'ss-viewer-controls', undefined, viewsHost);
        if (view.maps.length > 1) pickerHost = el('div', 'ss-viewer-picker', undefined, plainControls);
        panelHost = el('div', 'ss-viewer-panel', undefined, viewsHost);
        plainBody = true;
      }
    } else {
      el('h2', 'ss-viewer-title', SS.lanes.countT('viewer.panel.views', view), viewsHost);
      var controls = el('div', 'ss-viewer-controls', undefined, viewsHost);
      if (Array.isArray(view.maps) && view.maps.length > 1) pickerHost = el('div', 'ss-viewer-picker', undefined, controls);
      // item 42: the caption naming the map and the instructions sits in the row above the lanes, at its right end
      stampHost = el('p', 'ss-viewer-stamp mono muted', undefined, controls);
      panelHost = el('div', 'ss-viewer-panel', undefined, viewsHost);
      selectorHost = el('div', 'ss-viewer-selector', undefined, viewsHost);
      viewsBody = true;
    }

    // ---- drawer and ledger
    var drawerHost = section(wrap, 'drawer');
    if (opts.beforeLedger && typeof opts.beforeLedger === 'object' && opts.beforeLedger.nodeType === 1) {
      wrap.appendChild(opts.beforeLedger);
    }
    var ledgerHost = section(wrap, 'ledger');

    // the map picker is built once; a state change only moves aria-pressed
    var pickerButtons = [];
    function buildControls(s) {
      if (!viewsBody && !plainBody) return;
      if (pickerHost) {
        var group = el('div', 'ss-viewer-picker-group', undefined, pickerHost);
        group.setAttribute('role', 'group');
        group.setAttribute('aria-label', SS.t('viewer.map_picker'));
        view.maps.forEach(function (m) {
          var b = el('button', 'ss-viewer-pick mono', m.pid, group);
          b.setAttribute('type', 'button');
          b.setAttribute('data-map', m.pid);
          b.addEventListener('click', function () { SS.state.set({ map: m.pid }); });
          pickerButtons.push(b);
        });
      }
    }
    function pressControls(s) {
      if (!viewsBody && !plainBody) return;
      pickerButtons.forEach(function (b) { b.setAttribute('aria-pressed', b.getAttribute('data-map') === s.map ? 'true' : 'false'); });
    }

    function drawViews(s) {
      if (plainBody) {
        // only a map with nothing to compare is drawn here; a comparison would need the judge
        if (SS.lanes.isPlain(view, SS.views.mapIndex(view, s))) views = SS.views.render(panelHost, view, s, { gloss: !profile || profileApart, allRungs: true });
        else { panelHost.textContent = ''; views = null; }
        return;
      }
      if (!viewsBody) return;
      views = SS.views.render(panelHost, view, lanesOnly(s), { gloss: !profile || profileApart, itemLane: itemLane });
      stampHost.textContent = SS.views.stamp(s, view);
      selectorInput = SS.ledger.renderSelector(selectorHost, view, s);
    }
    function lanesOnly(s) { return { view: 'lanes', map: s.map, rung: s.rung, item: s.item }; }
    // opts.runHost (council 30, seat 02 F3): the run block (run id, code version, check of the whole log) goes there,
    // in the page's Technical details, once; the evidence panel then opens on its hint alone
    var runHost = opts.runHost && typeof opts.runHost.appendChild === 'function' ? opts.runHost : null;
    if (runHost) { runHost.textContent = ''; SS.drawer.renderRun(runHost, SS.drawer.runInfo(view), {}); }
    function drawDrawer(s) {
      if (judgeless) {
        needsJudge(drawerHost, modeB ? 'drawer.title.mode_b' : 'drawer.title', 'ss-needs-judge-drawer');
        if (!runHost) SS.drawer.renderRun(drawerHost, SS.drawer.runInfo(view), {});   // the run and its log check need no judge
        return;
      }
      var said = s.item ? SS.lanes.sentenceFor(view, SS.views.mapIndex(view, s), s.rung, itemLane, s.item) : null;
      SS.drawer.render(drawerHost, view, s, { behind: behind ? { rung: s.rung } : null, status: said, run: runHost ? false : undefined });
    }
    function drawLedger(s) {
      if (judgeless) { needsJudge(ledgerHost, modeB ? 'ledger.title.mode_b' : 'ledger.title', 'ss-needs-judge-ledger'); return; }
      SS.ledger.render(ledgerHost, view, s);
    }

    // ---- the profile's drill-downs: SS.strip.drill plus "steps behind these numbers"
    function drill(rowKey, container, row, o) {
      if (judgeless) {
        container.textContent = '';
        var hk = 'strip.row' + rowKey + '.heading' + (modeB ? '_b' : '');
        if (!hasString(hk)) hk = 'strip.row' + rowKey + '.heading';
        if (!hasString(hk)) hk = 'strip.heading';
        needsJudge(container, hk, 'ss-needs-judge-drill');
        return;
      }
      SS.strip.drill(rowKey, container, row, o);
      if (DRILL_ROWS.indexOf(rowKey) < 0) return;
      var bar = el('div', 'ss-behind', undefined, container);
      bar.setAttribute('role', 'group');
      bar.setAttribute('aria-label', SS.t('drawer.behind.label'));
      el('span', 'ss-behind-label', SS.t('drawer.behind.label'), bar);
      SS.ledger.rungOrder(view).forEach(function (r) {
        var b = el('button', 'ss-behind-btn', SS.t('drawer.behind.show', { rung: SS.t('views.rung.' + r) }), bar);
        b.setAttribute('type', 'button');
        b.setAttribute('data-behind', r);
        b.addEventListener('click', function () { SS.bus.emit('number:open', { row: rowKey, rung: r }); });
      });
    }

    // ---- first draw
    var s0 = SS.state.get();
    buildControls(s0);
    pressControls(s0);
    drawViews(s0);
    drawDrawer(s0);
    drawLedger(s0);
    writeHash(s0);

    // ---- listeners
    offs.push(SS.bus.on('state:change', function (p) {
      var s = p && p.state ? p.state : SS.state.get();
      pressControls(s);
      drawViews(s);
      drawDrawer(s);
      drawLedger(s);
      writeHash(s);
      if (pendingFocus && views && typeof views.focus === 'function') views.focus(pendingFocus.lane, pendingFocus.item_id);
      pendingFocus = null;
    }));

    offs.push(SS.bus.on('item:open', function (p) {
      if (!p || !isString(p.item_id)) return;
      // A lane button that had focus gets it back on the rebuilt item (a ledger cell restores its own focus).
      var a = typeof document !== 'undefined' ? document.activeElement : null;
      if (a && typeof a.getAttribute === 'function' && a.getAttribute('data-lane') !== null && a.getAttribute('data-rung') === null) {
        pendingFocus = { lane: a.getAttribute('data-lane'), item_id: p.item_id };
      }
      itemLane = isString(p.lane) ? p.lane : null;
      var patch = { item: p.item_id };
      if (isString(p.map)) patch.map = p.map;
      if (p.lane === 'ai' && isString(p.rung)) patch.rung = p.rung;
      SS.state.set(patch);
      pendingFocus = null;
    }));

    // 2.2 issue 1: "Show the steps at Rung N" changes the rung selector of the views panel, which can sit thousands of
    // pixels away from the button. Bring the panel into sight and put keyboard focus on the checked rung radio, so
    // the change is seen and announced ("Rung 3: remedy prompt, checked"). Smooth scrolling only when motion is allowed.
    function motionReduced() {
      try {
        var de = root.document && root.document.documentElement;
        if (de && typeof de.getAttribute === 'function' && de.getAttribute('data-motion') === 'reduce') return true;
        return !!(root.matchMedia && root.matchMedia('(prefers-reduced-motion: reduce)').matches);
      } catch (e) { return false; }
    }
    function revealRungSelector() {
      if (!viewsBody) return;
      if (selectorInput && typeof selectorInput.focus === 'function') {
        try { selectorInput.focus({ preventScroll: true }); } catch (e) { selectorInput.focus(); }
      }
      if (viewsHost && typeof viewsHost.scrollIntoView === 'function') {
        try { viewsHost.scrollIntoView({ block: 'start', behavior: motionReduced() ? 'auto' : 'smooth' }); }
        catch (e) { viewsHost.scrollIntoView(); }
      }
    }

    offs.push(SS.bus.on('number:open', function (p) {
      if (!p || !isString(p.rung) || SS.ledger.rungOrder(view).indexOf(p.rung) < 0) return;
      behind = { row: p.row };
      var before = SS.state.get().rung;
      SS.state.set({ rung: p.rung });
      if (before === p.rung) drawDrawer(SS.state.get());   // no state change, so the drawer is redrawn here
      revealRungSelector();
    }));

    offs.push(SS.bus.on('number:close', function () {
      behind = null;
      drawDrawer(SS.state.get());
    }));

    if (typeof root.addEventListener === 'function') {
      onHash = function () { SS.state.set(SS.state.fromHash(location.hash)); };
      root.addEventListener('hashchange', onHash);
    }

    return {
      unmount: function () {
        offs.forEach(function (off) { off(); });
        offs = [];
        if (onHash && typeof root.removeEventListener === 'function') root.removeEventListener('hashchange', onHash);
        onHash = null;
      }
    };
  }

  SS.viewer = { mount: mount, noJudge: noJudge };
})();
