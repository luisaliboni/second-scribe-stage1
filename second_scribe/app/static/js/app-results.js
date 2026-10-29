/* The app's Results screen (Task 40; spec 6.3 "Results = Try it with the own-data band", 6.7 gate; plan V2, V13, V14).
   Its purpose is to see how well the AI reads these interviews. Part 5a (scout F9, F10; the author's items 9 to 11):
   the runs list puts the run just made first and says, per row, its date, the instructions used, the people and the
   models. Item 40 (the author, after a real run): an opened run starts with the maps (the "point at a box" line, the
   lanes with the instructions selector, the drawer), then the plain sentences from its own profile (SS.profile.lead,
   placed by the viewer's opts.beforeLedger), then the absent-steps table, then one closed "Technical details" with the
   whole profile (SS.viewer.mount with
   opts.profileHost; the "yield per box" lines left out) and the run id. One tab per reference group: reference types
   are never put together. Saving the Profile for comparison is Export's (Task 42),
   named here only after the results.
   Blindness (Task 36): on load this screen asks only for GET /api/results/gate and GET /api/results/runs, which hold
   nothing AI-derived. The profile and the view (GET /api/results/profile, GET /api/view, both behind the gate; the
   first answer logs the project's reveal) are asked for only when the user presses "Show the results of this run".
   A refused profile says it was not made and gives each reason as a plain sentence (the exporter's own words are in the terminal log); the lanes are still drawn.
   The hash is #results/<run_id>, followed by the viewer's own state (&view=...&map=...). Classic script; needs core.js,
   api.js, app-shell.js and the viewer scripts (viewer.js and what it needs). */
(function () {
  'use strict';
  var root = typeof window !== 'undefined' ? window : globalThis;
  var SS = root.SS = root.SS || {};
  var ROUTES = { gate: '/api/results/gate', runs: '/api/results/runs', profile: '/api/results/profile', view: '/api/view' };
  var GROUPS = ['A', 'B', 'C'];
  var RUN_ID = /^[ABC]-[0-9a-f]{8}$/;
  var screen = null;        // {main, gate, runs, group, opened: {run_id, ...}, mounted}

  function el(doc, tag, cls, text) {
    var n = doc.createElement(tag);
    if (cls) n.setAttribute('class', cls);
    if (text !== undefined && text !== null) n.textContent = text;
    return n;
  }
  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }
  function docOf(node) { return node.ownerDocument || root.document; }
  function isObj(v) { return v !== null && typeof v === 'object' && !Array.isArray(v); }
  function msg(err) { return (err && err.message) || SS.t('api.failed'); }

  function gateOf(doc) {
    var g = isObj(doc) ? doc : {};
    return { open: g.open === true, revealed: g.revealed === true,
      waiting: Array.isArray(g.waiting) ? g.waiting.filter(function (p) { return typeof p === 'string'; }) : [] };
  }
  function runsOf(doc) {
    var list = isObj(doc) && Array.isArray(doc.runs) ? doc.runs : [];
    return list.filter(function (r) { return isObj(r) && typeof r.run_id === 'string' && RUN_ID.test(r.run_id) && GROUPS.indexOf(r.group) >= 0; })
      .map(function (r) {
        var models = isObj(r.models) ? r.models : {};
        return { run_id: r.run_id, group: r.group, ready: r.ready === true, state: typeof r.state === 'string' ? r.state : '',
          started: typeof r.started === 'string' ? r.started : '',
          rungs: Array.isArray(r.rungs) ? r.rungs.filter(function (x) { return typeof x === 'string'; }) : [],
          rung1_own: r.rung1_own === true,
          n_people: typeof r.n_people === 'number' && isFinite(r.n_people) ? r.n_people : null,
          models: { recon: typeof models.recon === 'string' ? models.recon : '', judge: typeof models.judge === 'string' ? models.judge : '' } };
      })
      // the run just made first (scout F10): newest start time first; a run with no start time last, by id
      .sort(function (a, b) {
        if (a.started !== b.started) return a.started < b.started ? 1 : -1;
        return a.run_id < b.run_id ? -1 : 1;
      });
  }

  // ---- a run in words (scout F10): date, instructions used (item 19's names), people and models
  var MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  function two(n) { return n < 10 ? '0' + n : String(n); }
  // "23 Oct 2026, 15:27" in this computer's time zone, or null for a missing or unreadable time
  function when(iso) {
    if (!iso) return null;
    var d = new Date(iso);
    if (isNaN(d.getTime())) return null;
    return d.getDate() + ' ' + MONTHS[d.getMonth()] + ' ' + d.getFullYear() + ', ' + two(d.getHours()) + ':' + two(d.getMinutes());
  }
  var RUNG_ORDER = ['rung1', 'rung2', 'rung3', 'placebo'];   // the study's order (appendix Step 8)
  function instructionsText(r) {
    return RUNG_ORDER.filter(function (x) { return r.rungs.indexOf(x) >= 0; }).map(function (x) {
      return x === 'rung1' && r.rung1_own ? SS.t('results.run.instructions_own') : SS.t('ledger.rung.' + x);
    }).join('; ');
  }
  function facts(r) {
    var out = [when(r.started) || SS.t('results.run.started_unknown')];
    var ins = instructionsText(r);
    if (ins) out.push(ins);
    if (r.n_people !== null) out.push(r.n_people === 1 ? SS.t('results.run.people_one') : SS.t('results.run.people', { n: String(r.n_people) }));
    if (r.models.recon) {
      out.push(r.models.judge ? SS.t('results.run.models', { recon: r.models.recon, judge: r.models.judge })
        : SS.t('results.run.models_no_judge', { recon: r.models.recon }));
    }
    return out;
  }

  // ---- the hash: #results/<run_id>&<viewer state>
  function subRun(sub) {
    var id = typeof sub === 'string' ? sub.split('&')[0] : '';
    return RUN_ID.test(id) ? id : '';
  }
  function prefixHash(runId) {
    var h = root.location && typeof root.location.hash === 'string' ? root.location.hash.replace(/^#/, '') : '';
    if (SS.shell && SS.shell.parseHash(h).screen === 'results') return;
    var want = '#results/' + runId + (h ? '&' + h : '');
    try {
      if (root.history && typeof root.history.replaceState === 'function') root.history.replaceState(null, '', want);
      else if (root.location) root.location.hash = want;
    } catch (e) { /* a sandboxed frame may refuse; the screen still works */ }
  }

  function unmount() {
    if (!screen || !screen.mounted) return;
    var m = screen.mounted;
    screen.mounted = null;
    if (m.viewer && typeof m.viewer.unmount === 'function') m.viewer.unmount();
    if (typeof m.offState === 'function') m.offState();
    if (m.onLeave && typeof root.removeEventListener === 'function') root.removeEventListener('hashchange', m.onLeave);
  }

  // ---- pieces
  function gateBox(doc, gate) {
    var box = el(doc, 'div', 'ss-res-gate');
    box.setAttribute('data-part', 'gate');
    box.setAttribute('data-open', gate.open ? 'true' : 'false');
    if (!gate.open) {
      box.appendChild(el(doc, 'p', 'ss-res-gate-text', SS.t('results.gate.closed', { pids: gate.waiting.join(', ') })));
      // Part 6: the banner says to lock each coding on the Coding screen, so the link goes there
      var a = el(doc, 'a', 'ss-setup-back', SS.t('results.go_coding'));
      a.setAttribute('href', '#coding');
      box.appendChild(a);
    } else if (gate.revealed) {
      box.appendChild(el(doc, 'p', 'ss-res-gate-text', SS.t('results.gate.revealed')));
    } else {
      box.appendChild(el(doc, 'p', 'ss-res-gate-text', SS.t('results.gate.before_open')));
    }
    return box;
  }

  function tabs(doc, groups) {
    var bar = el(doc, 'div', 'ss-res-tabs');
    bar.setAttribute('role', 'tablist');
    bar.setAttribute('aria-label', SS.t('results.tabs.label'));
    groups.forEach(function (g) {
      var b = el(doc, 'button', 'ss-res-tab', SS.t('results.tab.' + g.toLowerCase()));
      b.setAttribute('type', 'button');
      b.setAttribute('role', 'tab');
      b.setAttribute('data-group', g);
      b.setAttribute('aria-selected', g === screen.group ? 'true' : 'false');
      b.addEventListener('click', function () {
        if (screen.group === g) return;
        unmount();
        screen.group = g;
        screen.opened = null;
        draw();
      });
      bar.appendChild(b);
    });
    return bar;
  }

  function runRow(doc, r, gate, newest) {
    var row = el(doc, 'div', 'ss-res-run');
    row.setAttribute('data-run', r.run_id);
    var about = el(doc, 'div', 'ss-res-run-about');
    var line = el(doc, 'p', 'ss-res-run-facts');
    line.setAttribute('data-part', 'facts');
    line.setAttribute('data-gloss-skip', '');   // a line of facts, not prose: no meaning markers
    if (newest) {
      var tag = el(doc, 'span', 'ss-res-run-newest', SS.t('results.run.newest'));
      line.appendChild(tag);
      line.appendChild(doc.createTextNode(' '));
    }
    line.appendChild(doc.createTextNode(facts(r).join(' · ')));
    about.appendChild(line);
    about.appendChild(el(doc, 'span', 'mono ss-res-run-id', SS.t('results.run.label', { run_id: r.run_id })));
    row.appendChild(about);
    if (!r.ready) {
      // A run that stopped will never finish: say so, instead of telling the user to wait for it.
      row.appendChild(el(doc, 'span', 'ss-res-run-note', SS.t(r.state === 'stopped' ? 'results.run.stopped' : 'results.run.not_ready')));
      return row;
    }
    var b = el(doc, 'button', 'primary ss-res-show', SS.t('results.run.show'));
    b.setAttribute('type', 'button');
    b.setAttribute('data-run', r.run_id);
    b.disabled = !gate.open || (screen.opened && screen.opened.run_id === r.run_id && screen.opened.loading === true);
    b.addEventListener('click', function () { open(r.run_id); });
    row.appendChild(b);
    if (!gate.open) {
      // item 37: a closed button says why, with the screen that fixes it
      var why = el(doc, 'p', 'ss-res-why');
      why.setAttribute('id', 'ss-res-why-' + r.run_id);
      why.appendChild(el(doc, 'span', '', SS.t('results.run.closed_why') + ' '));
      var go = el(doc, 'a', 'ss-res-fix', SS.t('results.go_coding'));
      go.setAttribute('href', '#coding');
      why.appendChild(go);
      b.setAttribute('aria-describedby', 'ss-res-why-' + r.run_id);
      row.appendChild(why);
    }
    return row;
  }

  // `reasons` are plain sentences made by the server (results.refusal_messages); the exporter's own words are in the
  // terminal log only.
  function refusedBox(doc, reasons) {
    var box = el(doc, 'div', 'ss-res-refused');
    box.setAttribute('data-part', 'refused');
    box.setAttribute('role', 'note');
    box.appendChild(el(doc, 'p', 'ss-res-refused-title', SS.t('results.refused.intro')));
    var ul = el(doc, 'ul', 'ss-res-reasons');
    reasons.forEach(function (r) { ul.appendChild(el(doc, 'li', '', String(r))); });
    box.appendChild(ul);
    box.appendChild(el(doc, 'p', '', SS.t('results.refused.after')));
    return box;
  }

  // Mounts an opened run into `host` (scout F9, the author's items 9 to 11): the heading and the run in words, the plain
  // lead built from the run's own profile (SS.profile.lead), the three-lanes viewer, then one closed "Technical
  // details" holding the whole profile (every panel, interval and ledger, without the "yield per box" lines) and the
  // run id. A refused profile keeps its plain reasons at the top; the lanes still come.
  function mountRun(doc, host, o) {
    var head = el(doc, 'div', 'ss-res-run-head');
    head.appendChild(el(doc, 'h2', 'ss-res-heading', SS.t(o.refused ? 'results.heading.refused' : 'results.heading')));
    var known = screen.runs.filter(function (r) { return r.run_id === o.run_id; })[0];
    if (known) {
      var f = el(doc, 'p', 'ss-res-run-facts', facts(known).join(' · '));
      f.setAttribute('data-gloss-skip', '');
      f.setAttribute('data-part', 'facts');
      head.appendChild(f);
    }
    host.appendChild(head);
    if (o.refused) host.appendChild(refusedBox(doc, o.refused));
    var judgeless = !!(SS.viewer && typeof SS.viewer.noJudge === 'function' && SS.viewer.noJudge(o.view));
    var refType = o.view && typeof o.view.reference_type === 'string' ? o.view.reference_type : '';
    // item 40: the maps come first (the "point at a box" line, then the lanes with the instructions selector and the
    // drawer), then the plain sentences, then the absent-steps table, then the closed Technical details
    var lead = null;
    if (o.profile || judgeless || refType === 'C') {
      lead = el(doc, 'div', 'ss-res-lead');
      lead.setAttribute('data-part', 'lead');
      SS.profile.renderLead(lead, o.profile || {}, { judgeless: judgeless, referenceType: refType });
    }
    host.appendChild(el(doc, 'p', 'ss-res-next', SS.t('results.lead.next')));
    var vh = el(doc, 'div', 'ss-res-viewer');
    host.appendChild(vh);
    var details = el(doc, 'details', 'ss-res-details');
    details.setAttribute('data-part', 'technical');
    details.appendChild(el(doc, 'summary', 'ss-res-details-summary', SS.t('results.details.summary')));
    var dBody = el(doc, 'div', 'ss-res-details-body');
    // council 30 (seat 02 F3): the run block (run id, code version, check of the whole log) moves here from the evidence
    // panel. It names the run from the view; the run-id line is printed only when the view names no run or another one.
    var vrun = isObj(o.view) && isObj(o.view.run) ? o.view.run.run_id : null;
    if (vrun !== o.run_id) dBody.appendChild(el(doc, 'p', 'mono ss-res-run-id', SS.t('results.run.label', { run_id: o.run_id })));
    var runHost = el(doc, 'div', 'ss-res-run-block');
    runHost.setAttribute('data-part', 'run-block');
    dBody.appendChild(runHost);
    var profHost = null;
    if (o.profile) {
      dBody.appendChild(el(doc, 'p', 'ss-res-details-intro', SS.t('results.details.intro')));
      profHost = el(doc, 'div', 'ss-res-profile');
      dBody.appendChild(profHost);
    }
    details.appendChild(dBody);
    host.appendChild(details);
    var m = { viewer: null, offState: null, onLeave: null };
    // Registered before the viewer's own hashchange listener, so leaving the screen removes the viewer's first.
    if (typeof root.addEventListener === 'function') {
      m.onLeave = function () {
        var h = root.location ? root.location.hash : '';
        if (!SS.shell || SS.shell.parseHash(h).screen !== 'results') unmount();
      };
      root.addEventListener('hashchange', m.onLeave);
    }
    screen.mounted = m;
    var vopts = o.profile ? { profile: o.profile, profileHost: profHost, derived: false } : {};
    vopts.runHost = runHost;
    if (lead) vopts.beforeLedger = lead;
    m.viewer = SS.viewer.mount(vh, o.view, vopts);
    prefixHash(o.run_id);
    if (SS.bus && typeof SS.bus.on === 'function') {
      m.offState = SS.bus.on('state:change', function () { prefixHash(o.run_id); });
    }
    var more = el(doc, 'p', 'ss-res-secondary');
    more.setAttribute('data-part', 'secondary');
    more.appendChild(el(doc, 'span', '', SS.t(o.refused ? 'results.secondary.refused' : 'results.secondary') + ' '));
    var a = el(doc, 'a', 'ss-res-export', SS.t('results.secondary_link'));
    a.setAttribute('href', '#export');
    more.appendChild(a);
    host.appendChild(more);
  }

  // Part 5c (item 38): a redraw (a tab, the Show button) keeps the scroll position and the focus (SS.place).
  function draw() {
    if (!screen) return;
    SS.place.redraw(screen.main, drawNow);
  }

  function drawNow() {
    var main = screen.main;
    var doc = docOf(main);
    unmount();
    clear(main);
    var page = el(doc, 'div', 'ss-res');
    page.appendChild(el(doc, 'h1', 'ss-res-title', SS.t('results.title')));
    page.appendChild(el(doc, 'p', 'ss-res-purpose', SS.t('results.purpose')));
    var gate = screen.gate;
    page.appendChild(gateBox(doc, gate));
    var runs = screen.runs;
    var groups = GROUPS.filter(function (g) { return runs.some(function (r) { return r.group === g; }); });
    if (!groups.length) {
      page.appendChild(el(doc, 'p', 'ss-shell-note', SS.t('results.runs.none')));
      var a = el(doc, 'a', 'ss-setup-back', SS.t('results.runs.go_instructions'));
      a.setAttribute('href', '#instructions');
      page.appendChild(a);
      main.appendChild(page);
      return;
    }
    if (groups.indexOf(screen.group) < 0) screen.group = groups[0];
    page.appendChild(tabs(doc, groups));
    var panel = el(doc, 'div', 'ss-res-panel');
    panel.setAttribute('role', 'tabpanel');
    panel.setAttribute('data-group', screen.group);
    panel.appendChild(el(doc, 'p', 'ss-res-note', SS.t('results.group_note')));
    runs.filter(function (r) { return r.group === screen.group; }).forEach(function (r, i) {
      panel.appendChild(runRow(doc, r, gate, i === 0 && runs.filter(function (x) { return x.group === screen.group; }).length > 1));
    });
    var o = screen.opened;
    if (o && o.error) {
      var e = el(doc, 'p', 'ss-setup-error', o.error);
      e.setAttribute('role', 'alert');
      panel.appendChild(e);
    } else if (o && o.loading) {
      var l = el(doc, 'p', 'ss-shell-note', SS.t('results.run.loading'));
      l.setAttribute('role', 'status');
      panel.appendChild(l);
    }
    page.appendChild(panel);
    var host = el(doc, 'section', 'ss-res-body');
    host.setAttribute('data-part', 'results');
    page.appendChild(host);
    main.appendChild(page);
    if (o && o.view) mountRun(doc, host, o);
  }

  // The explicit action: the profile (lead first), then the view. Each answer comes from a gated route.
  function open(runId) {
    var s = screen;
    if (!s || !RUN_ID.test(runId)) return Promise.resolve();
    unmount();
    s.opened = { run_id: runId, loading: true };
    draw();
    var got = { run_id: runId };
    return SS.api.get(ROUTES.profile, { run: runId }).then(function (p) {
      if (isObj(p) && isObj(p.profile)) got.profile = p.profile;
      if (isObj(p) && (Array.isArray(p.refusals) || Array.isArray(p.refused))) {
        var msgs = Array.isArray(p.refusals)
          ? p.refusals.filter(isObj).map(function (x) { return x.message; }).filter(function (x) { return typeof x === 'string' && x !== ''; })
          : [];
        // an answer with no plain sentences never falls back to the exporter's words
        got.refused = msgs.length ? msgs : [SS.t('results.refused.reason.other')];
      }
      return SS.api.get(ROUTES.view, { run: runId });
    }).then(function (v) {
      if (screen !== s) return;
      if (!isObj(v) || !isObj(v.view)) throw new Error(SS.t('results.error.view_missing'));
      got.view = v.view;
      s.opened = got;
      s.gate.revealed = true;
      draw();
    }).catch(function (err) {
      if (screen !== s) return;
      s.opened = { run_id: runId, error: msg(err) };
      if (err && err.code === 'coding_unlocked') s.gate.open = false;
      draw();
    });
  }

  function render(main, sub) {
    unmount();
    screen = { main: main, gate: gateOf(null), runs: [], group: null, opened: null, mounted: null };
    var s = screen;
    var want = subRun(sub);
    var doc = docOf(main);
    SS.place.forget(main);
    SS.place.watch(main);
    clear(main);
    main.appendChild(el(doc, 'p', 'ss-shell-note', SS.t('shell.loading')));
    return Promise.all([SS.api.get(ROUTES.gate), SS.api.get(ROUTES.runs)]).then(function (got) {
      if (screen !== s) return;
      s.gate = gateOf(got[0]);
      s.runs = runsOf(got[1]);
      var hit = s.runs.filter(function (r) { return r.run_id === want; })[0];
      s.group = hit ? hit.group : (s.runs.length ? s.runs[0].group : null);   // else the newest run's group
      draw();                           // never opens a run by itself: the hash only picks the tab
    }, function (err) {
      if (screen !== s) return;
      clear(main);
      main.appendChild(el(doc, 'p', 'ss-shell-note', msg(err)));
      if (err && (err.code === 'no_project' || err.code === 'sync_folder_unacknowledged')) {
        var back = el(doc, 'a', 'ss-setup-back', SS.t('reference.go_setup'));
        back.setAttribute('href', '#setup');
        main.appendChild(back);
      }
    });
  }

  SS.results = { ROUTES: ROUTES, render: render, open: open, unmount: unmount };

  if (SS.shell && typeof SS.shell.register === 'function') {
    SS.shell.register('results', { order: 50, titleKey: 'results.title', render: render });
  }
})();
