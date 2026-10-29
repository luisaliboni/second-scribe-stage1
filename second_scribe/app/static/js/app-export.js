/* The Export screen (Task 42; spec 6.9, 9; plan V6, V13, V14; artboard AppExport). Classic script; needs core.js,
   api.js and app-shell.js.

   Order (V13, DECISIONS L35): the report is the main action and comes at the top. The "Profile for comparison" (V14:
   never "Profile for pooling"), the methods paragraph and the run record follow it. The profile is saved only after
   the researcher has seen every line of the file, with the refusals ("group below k: not exportable", the publish
   gate's codes) and the differencing warning.

   Routes (Task 42, routes/export.py): GET /api/export/runs (run ids and groups, the gate; no result), and, behind the
   results gate (refused while a mode-B coding is open): GET /api/export/list?run_id=, POST /api/export/report
   {run_id}, POST /api/export/profile/preview {run_id, k}, POST /api/export/profile/save {run_id, k, sha256},
   POST /api/export/methods {run_id}, POST /api/export/methods/save {run_id}, POST /api/export/certificate/save
   {run_id}, POST /api/export/open {path, reveal} (the "Open" and "Show in folder" buttons beside each saved file, scout
   F11). Data goes into the page through textContent and attributes only. The hash #export/<run_id> picks a run.
   Part 5c: picking another run redraws in place and keeps the place (item 38, SS.place); the closed screen and the
   runs that cannot be picked say why, with a link to the screen that fixes it (item 37). */
(function () {
  'use strict';
  var root = typeof window !== 'undefined' ? window : globalThis;
  var SS = root.SS = root.SS || {};
  var SECTIONS = ['report', 'profile', 'methods', 'record'];
  var mounted = null;
  var current = null;          // {runs, gate, k_min, run_id}

  function el(doc, tag, cls, text) {
    var n = doc.createElement(tag);
    if (cls) n.setAttribute('class', cls);
    if (text !== undefined && text !== null) n.textContent = text;
    return n;
  }
  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }
  function docOf(node) { return node.ownerDocument || root.document; }
  function message(err) { return (err && err.message) || SS.t('api.failed'); }

  function button(doc, cls, key, onClick) {
    var b = el(doc, 'button', cls, SS.t(key));
    b.setAttribute('type', 'button');
    b.addEventListener('click', function () { onClick(b); });
    return b;
  }

  function errorBox(doc) {
    var p = el(doc, 'p', 'ss-setup-error');
    p.setAttribute('role', 'alert');
    return p;
  }

  function finishedRuns(data) {
    var runs = data && Array.isArray(data.runs) ? data.runs : [];
    return runs.filter(function (r) { return r && typeof r.run_id === 'string'; });
  }

  // The run to show: the one named in the hash when it is finished, else the last finished one.
  function pickRun(runs, wanted) {
    var done = runs.filter(function (r) { return r.finished === true; });
    for (var i = 0; i < done.length; i++) if (done[i].run_id === wanted) return done[i];
    return done.length ? done[done.length - 1] : null;
  }

  function referenceText(group) {
    var g = typeof group === 'string' && /^[ABC]$/.test(group) ? group.toLowerCase() : 'a';
    return SS.t('export.reference.' + g);
  }

  function card(doc, key, cls) {
    var s = el(doc, 'section', 'ss-export-card ' + (cls || ''));
    s.setAttribute('data-export', key);
    var h = el(doc, 'h2', 'ss-export-card-title', SS.t('export.' + key + '.title'));
    h.setAttribute('id', 'ss-export-' + key + '-title');
    s.setAttribute('aria-labelledby', 'ss-export-' + key + '-title');
    s.appendChild(h);
    s.appendChild(el(doc, 'p', 'ss-export-card-body', SS.t('export.' + key + '.body')));
    return s;
  }

  // "Open" and "Show in folder" beside a saved file (scout F11): POST /api/export/open {path, reveal}; the app hands
  // the file to the system's opener. Shown only when GET /api/export/runs said the computer has one (can_open).
  var OPENABLE = /\.(html|json|csv|txt|md)$/i;
  function openButtons(doc, path) {
    if (!current || current.canOpen !== true || typeof path !== 'string' || !path) return null;
    var wrap = el(doc, 'span', 'ss-export-open');
    var err = el(doc, 'span', 'ss-export-open-error');
    err.setAttribute('role', 'alert');
    function act(cls, key, reveal) {
      var b = button(doc, 'ss-export-open-btn ' + cls, key, function (btn) {
        btn.disabled = true;
        err.textContent = '';
        return SS.api.post('/api/export/open', { path: path, reveal: reveal }).then(function () {
          btn.disabled = false;
        }, function (e) {
          btn.disabled = false;
          err.textContent = message(e);
        });
      });
      b.setAttribute('data-path', path);
      wrap.appendChild(b);
    }
    if (OPENABLE.test(path)) act('ss-export-open-file', 'export.open', false);
    act('ss-export-open-folder', 'export.show', true);
    wrap.appendChild(err);
    return wrap;
  }

  function savedLine(doc, parent, path) {
    var p = el(doc, 'p', 'ss-export-saved');
    p.setAttribute('role', 'status');
    p.appendChild(el(doc, 'span', '', SS.t('export.saved_to') + ' '));
    p.appendChild(el(doc, 'code', 'ss-export-path', path));
    parent.appendChild(p);
    var o = openButtons(doc, path);
    if (o) parent.appendChild(o);
    return p;
  }

  // ---------------------------------------------------------------------------------------------- report
  function reportCard(doc, run) {
    var c = card(doc, 'report', 'ss-export-card-main');
    var out = el(doc, 'div', 'ss-export-result');
    var err = errorBox(doc);
    var b = button(doc, 'primary ss-export-report', 'export.report.make', function (btn) {
      btn.disabled = true;
      err.textContent = '';
      clear(out);
      out.appendChild(el(doc, 'p', 'ss-shell-note', SS.t('export.working')));
      return SS.api.post('/api/export/report', { run_id: run.run_id }).then(function (r) {
        btn.disabled = false;
        clear(out);
        var p = el(doc, 'p', 'ss-export-made', SS.t('export.report.made'));
        p.setAttribute('role', 'status');
        out.appendChild(p);
        out.appendChild(el(doc, 'code', 'ss-export-path ss-export-file', r && r.file ? r.file : ''));
        var o = openButtons(doc, r && r.path ? r.path : '');
        if (o) out.appendChild(o);
        return SS.exportScreen.refreshList();
      }, function (e) {
        btn.disabled = false;
        clear(out);
        err.textContent = message(e);
      });
    });
    c.appendChild(b);
    c.appendChild(out);
    c.appendChild(err);
    return c;
  }

  // ---------------------------------------------------------------------------------------------- profile
  function lineList(doc, cls, items) {
    var ul = el(doc, 'ul', cls);
    items.forEach(function (it) { ul.appendChild(el(doc, 'li', '', it && it.message ? it.message : String(it))); });
    return ul;
  }

  // The three plain checks above the raw lines (scout F12), from the preview's `checks` counts: only group numbers
  // leave; the smallest group against k; how many numbers are empty because they were not run here.
  function drawChecks(doc, host, pv) {
    var ch = pv && pv.checks && typeof pv.checks === 'object' ? pv.checks : null;
    if (!ch) return null;
    var box = el(doc, 'div', 'ss-export-checks');
    box.setAttribute('data-part', 'checks');
    box.appendChild(el(doc, 'p', 'ss-export-checks-title', SS.t('export.profile.checks.title')));
    var ul = el(doc, 'ul', 'ss-export-check-list');
    function item(id, status, text) {
      var li = el(doc, 'li', 'ss-export-check');
      li.setAttribute('data-check', id);
      li.setAttribute('data-status', status);
      li.appendChild(el(doc, 'span', 'ss-export-check-status', SS.t('export.profile.check.' + status)));
      li.appendChild(el(doc, 'span', 'ss-export-check-text', text));
      ul.appendChild(li);
    }
    item('groups', 'ok', SS.t('export.profile.check.groups'));
    var n = ch.smallest_group;
    if (typeof n === 'number') {
      var below = n < Number(pv.k);
      item('smallest', below ? 'fail' : 'ok', SS.t(below ? 'export.profile.check.smallest_below' : 'export.profile.check.smallest',
        { n: String(n), k: String(pv.k) }));
    }
    var empty = typeof ch.not_run === 'number' ? ch.not_run : 0;
    item('not_run', empty ? 'note' : 'ok', empty
      ? SS.t('export.profile.check.not_run', { n: String(empty), total: String(ch.cells) })
      : SS.t('export.profile.check.not_run_none'));
    box.appendChild(ul);
    host.appendChild(box);
    return box;
  }

  function drawPreview(doc, host, pv, run, k, err) {
    clear(host);
    if (pv.n_people !== null && pv.n_people !== undefined) {
      host.appendChild(el(doc, 'p', 'ss-export-people', SS.t('export.profile.people',
        { n: String(pv.n_people), k: String(pv.k) })));
    }
    drawChecks(doc, host, pv);
    var refusals = Array.isArray(pv.refusals) ? pv.refusals : [];
    var warnings = Array.isArray(pv.warnings) ? pv.warnings : [];
    if (refusals.length) {
      var r = el(doc, 'div', 'ss-export-refusals');
      r.setAttribute('role', 'alert');
      r.appendChild(el(doc, 'p', 'ss-export-refusals-title', SS.t('export.profile.not_exportable')));
      r.appendChild(lineList(doc, 'ss-export-refusal-list', refusals));
      host.appendChild(r);
    }
    if (warnings.length) {
      var w = el(doc, 'div', 'ss-setup-warning ss-export-warnings');
      w.appendChild(el(doc, 'p', 'ss-export-warnings-title', SS.t('export.profile.warnings')));
      w.appendChild(lineList(doc, 'ss-export-warning-list', warnings));
      host.appendChild(w);
    }
    if (Array.isArray(pv.declaration) && pv.declaration.length) {
      host.appendChild(lineList(doc, 'ss-export-declaration', pv.declaration));
    }
    var lines = Array.isArray(pv.preview) ? pv.preview : [];
    if (lines.length) {
      // Closed (scout F12): the three checks above say what the lines hold; the raw file is one click away.
      var det = el(doc, 'details', 'ss-export-lines');
      det.appendChild(el(doc, 'summary', '', SS.t('export.profile.lines', { n: String(lines.length) })));
      if (pv.state === 'pending' || pv.state === 'synthetic') {
        det.appendChild(el(doc, 'p', 'ss-export-state', SS.t('export.profile.state.' + pv.state)));
      }
      det.appendChild(el(doc, 'pre', 'ss-export-pre mono', lines.join('\n')));
      host.appendChild(det);
    }
    if (pv.exportable === true && typeof pv.sha256 === 'string') {
      var save = button(doc, 'ss-export-profile-save', 'export.profile.save', function (btn) {
        btn.disabled = true;
        err.textContent = '';
        return SS.api.post('/api/export/profile/save', { run_id: run.run_id, k: k, sha256: pv.sha256 }).then(function (s) {
          savedLine(doc, host, s && s.path ? s.path : '');
          return SS.exportScreen.refreshList();
        }, function (e) {
          btn.disabled = false;
          err.textContent = message(e);
        });
      });
      host.appendChild(save);
    }
  }

  function profileCard(doc, run, kMin) {
    var c = card(doc, 'profile');
    var field = el(doc, 'div', 'ss-setup-field ss-export-k');
    var id = 'ss-export-k';
    var label = el(doc, 'label', 'ss-setup-label', SS.t('export.profile.k_label'));
    label.setAttribute('for', id);
    var input = el(doc, 'input', 'ss-setup-input ss-export-k-input');
    input.setAttribute('id', id);
    input.setAttribute('type', 'number');
    input.setAttribute('min', String(kMin));
    input.setAttribute('step', '1');
    input.setAttribute('inputmode', 'numeric');
    input.value = String(kMin);
    input.setAttribute('value', String(kMin));
    field.appendChild(label);
    field.appendChild(input);
    field.appendChild(el(doc, 'p', 'ss-setup-hint', SS.t('export.profile.k_hint')));
    c.appendChild(field);
    var host = el(doc, 'div', 'ss-export-result');
    var err = errorBox(doc);
    c.appendChild(button(doc, 'ss-export-profile-preview', 'export.profile.preview', function (btn) {
      var k = parseInt(String(input.value), 10);
      btn.disabled = true;
      err.textContent = '';
      clear(host);
      host.appendChild(el(doc, 'p', 'ss-shell-note', SS.t('export.working')));
      return SS.api.post('/api/export/profile/preview', { run_id: run.run_id, k: isFinite(k) ? k : null }).then(function (pv) {
        btn.disabled = false;
        drawPreview(doc, host, pv || {}, run, k, err);
      }, function (e) {
        btn.disabled = false;
        clear(host);
        err.textContent = message(e);
      });
    }));
    c.appendChild(host);
    c.appendChild(err);
    return c;
  }

  // ---------------------------------------------------------------------------------------------- methods, record
  function methodsCard(doc, run) {
    var c = card(doc, 'methods');
    var host = el(doc, 'div', 'ss-export-result');
    var err = errorBox(doc);
    var actions = el(doc, 'div', 'ss-export-actions');
    actions.appendChild(button(doc, 'ss-export-methods-show', 'export.methods.show', function (btn) {
      btn.disabled = true;
      err.textContent = '';
      return SS.api.post('/api/export/methods', { run_id: run.run_id }).then(function (r) {
        btn.disabled = false;
        clear(host);
        host.appendChild(el(doc, 'pre', 'ss-export-methods-text', r && typeof r.text === 'string' ? r.text : ''));
      }, function (e) { btn.disabled = false; err.textContent = message(e); });
    }));
    actions.appendChild(button(doc, 'ss-export-methods-save', 'export.methods.save', function (btn) {
      btn.disabled = true;
      err.textContent = '';
      return SS.api.post('/api/export/methods/save', { run_id: run.run_id }).then(function (r) {
        btn.disabled = false;
        clear(host);
        host.appendChild(el(doc, 'pre', 'ss-export-methods-text', r && typeof r.text === 'string' ? r.text : ''));
        savedLine(doc, host, r && r.path ? r.path : '');
        return SS.exportScreen.refreshList();
      }, function (e) { btn.disabled = false; err.textContent = message(e); });
    }));
    c.appendChild(actions);
    c.appendChild(host);
    c.appendChild(err);
    return c;
  }

  function recordCard(doc, run) {
    var c = card(doc, 'record');
    var host = el(doc, 'div', 'ss-export-result');
    var err = errorBox(doc);
    c.appendChild(button(doc, 'ss-export-record-save', 'export.record.save', function (btn) {
      btn.disabled = true;
      err.textContent = '';
      return SS.api.post('/api/export/certificate/save', { run_id: run.run_id }).then(function (r) {
        btn.disabled = false;
        clear(host);
        savedLine(doc, host, r && r.path ? r.path : '');
        return SS.exportScreen.refreshList();
      }, function (e) { btn.disabled = false; err.textContent = message(e); });
    }));
    c.appendChild(host);
    c.appendChild(err);
    return c;
  }

  // ---------------------------------------------------------------------------------------------- saved list
  function drawList(doc, host, data) {
    clear(host);
    host.appendChild(el(doc, 'h2', 'ss-export-list-title', SS.t('export.list.title')));
    var outs = data && Array.isArray(data.outputs) ? data.outputs.filter(function (o) { return o && o.saved; }) : [];
    if (!outs.length) {
      host.appendChild(el(doc, 'p', 'ss-shell-note', SS.t('export.list.empty')));
      return;
    }
    var ul = el(doc, 'ul', 'ss-export-list-rows');
    outs.forEach(function (o) {
      var li = el(doc, 'li', 'ss-export-list-row');
      li.setAttribute('data-kind', String(o.kind));
      li.appendChild(el(doc, 'span', 'ss-export-list-kind', SS.t('export.kind.' + String(o.kind))));
      li.appendChild(el(doc, 'code', 'ss-export-path', String(o.path || '')));
      var b = openButtons(doc, typeof o.path === 'string' ? o.path : '');
      if (b) li.appendChild(b);
      ul.appendChild(li);
    });
    host.appendChild(ul);
  }

  // ---------------------------------------------------------------------------------------------- screen
  SS.exportScreen = {
    SECTIONS: SECTIONS,
    drawChecks: drawChecks,
    openButtons: openButtons,
    pickRun: pickRun,
    // Draws the screen for a GET /api/export/runs reply (no fetch); `wanted` is the run id from the hash.
    draw: function draw(main, data, wanted) {
      var doc = docOf(main);
      clear(main);
      var page = el(doc, 'div', 'ss-export');
      page.appendChild(el(doc, 'h1', 'ss-export-title', SS.t('export.heading')));
      page.appendChild(el(doc, 'p', 'ss-export-intro', SS.t('export.intro')));
      main.appendChild(page);
      var gate = data && data.gate && typeof data.gate === 'object' ? data.gate : { open: true };
      var runs = finishedRuns(data);
      current = { runs: runs, run: null, listHost: null, canOpen: !!(data && data.can_open === true) };
      if (gate.open === false) {
        var closed = el(doc, 'p', 'ss-export-closed');
        closed.appendChild(el(doc, 'span', '', SS.t('export.closed', { n: String(gate.waiting || 0) }) + ' '));
        var fix = el(doc, 'a', 'ss-export-fix', SS.t('export.go_coding'));        // item 37: where to fix it
        fix.setAttribute('href', '#coding');
        closed.appendChild(fix);
        page.appendChild(closed);
        return null;
      }
      var run = pickRun(runs, wanted);
      if (!run) {
        page.appendChild(el(doc, 'p', 'ss-shell-note', SS.t('export.no_runs')));
        return null;
      }
      current.run = run;
      var head = el(doc, 'div', 'ss-export-run');
      if (runs.length > 1) {
        var lab = el(doc, 'label', 'ss-setup-label', SS.t('export.run_picker'));
        lab.setAttribute('for', 'ss-export-run');
        var sel = el(doc, 'select', 'ss-setup-input ss-export-run-select');
        sel.setAttribute('id', 'ss-export-run');
        runs.forEach(function (r) {
          var o = el(doc, 'option', '', SS.t(r.finished ? 'export.run_option' : 'export.run_unfinished_option',
            { run_id: r.run_id, group: String(r.group || '') }));
          o.setAttribute('value', r.run_id);
          if (!r.finished) o.setAttribute('disabled', 'disabled');
          if (r.run_id === run.run_id) o.setAttribute('selected', 'selected');
          sel.appendChild(o);
        });
        // item 38: another run redraws in place (the scroll and the focus on the picker stay); the hash follows
        // without a jump through the shell
        sel.addEventListener('change', function () {
          var want = sel.value;
          var hash = '#export/' + encodeURIComponent(want);
          try {
            if (root.history && typeof root.history.replaceState === 'function') root.history.replaceState(null, '', hash);
            else if (root.location) { root.location.hash = hash; return; }
          } catch (e) { if (root.location) { root.location.hash = hash; return; } }
          SS.place.keep(main, function () { return SS.exportScreen.render(main, encodeURIComponent(want), true); },
            { still: function () { return mounted === main; } });
        });
        head.appendChild(lab);
        head.appendChild(sel);
        if (runs.some(function (r) { return !r.finished; })) {
          // item 37: the runs that cannot be picked say why, with the screen that fixes it
          var why = el(doc, 'p', 'ss-setup-hint ss-export-why');
          why.setAttribute('id', 'ss-export-run-why');
          why.appendChild(el(doc, 'span', '', SS.t('export.unfinished_why') + ' '));
          var go = el(doc, 'a', 'ss-export-fix', SS.t('export.go_run'));
          go.setAttribute('href', '#instructions');
          why.appendChild(go);
          sel.setAttribute('aria-describedby', 'ss-export-run-why');
          head.appendChild(why);
        }
      } else {
        var line = el(doc, 'p', 'ss-export-run-line');
        line.appendChild(el(doc, 'span', 'ss-setup-key', SS.t('export.run_picker')));
        line.appendChild(el(doc, 'code', 'ss-export-run-id', run.run_id));
        head.appendChild(line);
      }
      var ref = el(doc, 'p', 'ss-export-run-line');
      ref.appendChild(el(doc, 'span', 'ss-setup-key', SS.t('export.reference_label')));
      ref.appendChild(el(doc, 'span', 'ss-export-reference', referenceText(run.group)));
      head.appendChild(ref);
      page.appendChild(head);

      page.appendChild(reportCard(doc, run));
      var rest = el(doc, 'div', 'ss-export-rest');
      rest.appendChild(profileCard(doc, run, typeof data.k_min === 'number' ? data.k_min : 10));
      rest.appendChild(methodsCard(doc, run));
      rest.appendChild(recordCard(doc, run));
      page.appendChild(rest);
      var list = el(doc, 'section', 'ss-export-list');
      page.appendChild(list);
      current.listHost = list;
      return run;
    },
    refreshList: function refreshList() {
      if (!current || !current.run || !current.listHost) return Promise.resolve();
      var host = current.listHost;
      return SS.api.get('/api/export/list', { run_id: current.run.run_id }).then(function (data) {
        drawList(docOf(host), host, data);
      }, function () { /* the list is a convenience; the actions report their own errors */ });
    },
    // keep (the run picker): leave the page as it is until the new one is drawn.
    render: function render(main, sub, keep) {
      mounted = main;
      if (!keep) {
        SS.place.forget(main);
        clear(main);
        main.appendChild(el(docOf(main), 'p', 'ss-shell-note', SS.t('shell.loading')));
      }
      SS.place.watch(main);
      var wanted = typeof sub === 'string' && sub ? decodeURIComponent(sub) : '';
      return SS.api.get('/api/export/runs').then(function (data) {
        if (mounted !== main) return null;
        var run = SS.exportScreen.draw(main, data || {}, wanted);
        return run ? SS.exportScreen.refreshList() : null;
      }, function (err) {
        if (mounted !== main) return;
        clear(main);
        var doc = docOf(main);
        main.appendChild(el(doc, 'p', 'ss-shell-note', message(err)));
        // 2.2 issue 4: the same way back as Reference, Coding, Compare instructions and Results
        if (err && (err.code === 'no_project' || err.code === 'sync_folder_unacknowledged')) {
          var back = el(doc, 'a', 'ss-setup-back', SS.t('reference.go_setup'));
          back.setAttribute('href', '#setup');
          main.appendChild(back);
        }
      });
    }
  };

  if (SS.shell && typeof SS.shell.register === 'function') {
    SS.shell.register('export', { order: 60, titleKey: 'export.title', render: SS.exportScreen.render });
  }
})();
