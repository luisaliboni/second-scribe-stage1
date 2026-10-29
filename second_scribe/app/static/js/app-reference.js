/* The Reference screen (Task 37; spec 6.6, artboard AppReference): per transcript, choose A, B or C, each with its
   plain explanation; the bulk button (every confirmed map = A); the C note; the own-quote check that runs in every
   mode; and whether results are still closed because a B coding is open. Classic script; needs core.js, api.js
   and app-shell.js.

   Routes (Task 36): GET /api/references, POST /api/references {pid, type}, POST /api/references/bulk
   {confirmed_maps: "A"}. Nothing else: this screen never asks for a result, the no-interview map or an AI map,
   and it draws only the fields it names below (pid, type, map_confirmed, coding.n_marks, coding.locked,
   gate.waiting, gate.revealed).

   Part 5c (item 38): a choice or the bulk button redraws through SS.place.keep, so the page keeps its scroll
   position, the focus returns to the choice just made and the result shows beside it ("SYN-OF01 now uses
   reference A."). Item 37: a choice or button that cannot be used says why in one line, with a link to the Setup
   step that fixes it, counted from this screen's own reply (no other route is called): "A needs a confirmed map for
   this transcript. Maps confirmed: 0 of 12. Go to Check the maps". */
(function () {
  'use strict';
  var root = typeof window !== 'undefined' ? window : globalThis;
  var SS = root.SS = root.SS || {};
  var TYPES = ['A', 'B', 'C'];
  var mounted = null;       // the main element of the last render
  var MAPS_STEP = '#setup/maps';

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

  function rowsOf(data) {
    var list = data && Array.isArray(data.references) ? data.references : [];
    return list.filter(function (r) { return r && typeof r.pid === 'string'; }).map(function (r) {
      var c = r.coding && typeof r.coding === 'object' ? r.coding : {};
      return { pid: r.pid, type: TYPES.indexOf(r.type) >= 0 ? r.type : null, mapConfirmed: r.map_confirmed === true,
        nMarks: typeof c.n_marks === 'number' ? c.n_marks : 0, locked: c.locked === true };
    });
  }

  function gateOf(data) {
    var g = data && data.gate && typeof data.gate === 'object' ? data.gate : {};
    return { waiting: Array.isArray(g.waiting) ? g.waiting.filter(function (p) { return typeof p === 'string'; }) : [],
      revealed: g.revealed === true };
  }

  // One explainer card per reference type (static text only).
  function card(doc, t) {
    var c = el(doc, 'section', 'ss-ref-card');
    c.setAttribute('data-type', t);
    var head = el(doc, 'div', 'ss-ref-card-head');
    head.appendChild(el(doc, 'span', 'ss-ref-card-tag', SS.t('reference.' + t.toLowerCase() + '.tag')));
    head.appendChild(el(doc, 'h3', 'ss-ref-card-title', SS.t('reference.' + t.toLowerCase() + '.title')));
    c.appendChild(head);
    c.appendChild(el(doc, 'p', 'ss-ref-card-body', SS.t('reference.' + t.toLowerCase() + '.body')));
    var dl = el(doc, 'dl', 'ss-ref-card-list');
    dl.appendChild(el(doc, 'dt', '', SS.t('reference.can_label')));
    dl.appendChild(el(doc, 'dd', '', SS.t('reference.' + t.toLowerCase() + '.can')));
    dl.appendChild(el(doc, 'dt', '', SS.t('reference.cannot_label')));
    dl.appendChild(el(doc, 'dd', '', SS.t('reference.' + t.toLowerCase() + '.cannot')));
    c.appendChild(dl);
    c.appendChild(el(doc, 'p', 'ss-ref-card-needs', SS.t('reference.' + t.toLowerCase() + '.needs')));
    if (t === 'C') c.appendChild(el(doc, 'p', 'ss-ref-cnote', SS.t('reference.c_note')));
    return c;
  }

  var uid = 0;

  // The radio group of one transcript.
  // item 37: why a choice cannot be used, in one line with the step that fixes it (null when it can be used)
  function whyLine(doc, id, text, count, href, stepKey) {
    var p = el(doc, 'p', 'ss-ref-why');
    p.setAttribute('id', id);
    p.appendChild(el(doc, 'span', '', text + (count ? ' ' + count : '') + ' '));
    if (href) {
      var a = el(doc, 'a', 'ss-ref-fix', SS.t('reference.go_step', { step: SS.t(stepKey) }));
      a.setAttribute('href', href);
      p.appendChild(a);
    }
    return p;
  }

  function mapsCount(rows) {
    var done = rows.filter(function (x) { return x.mapConfirmed; }).length;
    return SS.t('reference.maps_count', { done: String(done), total: String(rows.length) });
  }

  function row(doc, r, gate, rows) {
    uid += 1;
    var fs = el(doc, 'fieldset', 'ss-ref-row');
    fs.setAttribute('data-pid', r.pid);
    if (r.type) fs.setAttribute('data-type', r.type);
    var legend = el(doc, 'legend', 'ss-ref-row-legend');
    legend.appendChild(el(doc, 'span', 'mono ss-ref-pid', r.pid));
    legend.appendChild(el(doc, 'span', 'ss-visually-hidden', ' ' + SS.t('reference.choice_label', { pid: r.pid })));
    fs.appendChild(legend);
    var opts = el(doc, 'div', 'ss-ref-options');
    var errorBox = el(doc, 'p', 'ss-setup-error');
    errorBox.setAttribute('role', 'alert');
    var name = 'ss-ref-' + uid;
    var bClosed = gate.revealed && !r.locked && r.type !== 'B';
    var whyA = name + '-why-a';
    var whyB = name + '-why-b';
    TYPES.forEach(function (t) {
      var id = name + '-' + t;
      var lab = el(doc, 'label', 'ss-ref-option');
      lab.setAttribute('for', id);
      lab.setAttribute('data-type', t);
      var input = el(doc, 'input');
      input.setAttribute('type', 'radio');
      input.setAttribute('name', name);
      input.setAttribute('id', id);
      input.setAttribute('value', t);
      input.checked = r.type === t;
      input.disabled = (t === 'A' && !r.mapConfirmed) || (t === 'B' && bClosed);
      if (input.checked) input.setAttribute('checked', 'checked');
      if (input.disabled) {
        lab.setAttribute('data-disabled', 'true');
        input.setAttribute('aria-describedby', t === 'A' ? whyA : whyB);
      }
      input.addEventListener('change', function () {
        if (!input.checked || r.type === t) return;
        choose(r.pid, t, errorBox);
      });
      lab.appendChild(input);
      lab.appendChild(el(doc, 'span', '', SS.t('reference.option.' + t.toLowerCase())));
      opts.appendChild(lab);
    });
    fs.appendChild(opts);

    var notes = el(doc, 'div', 'ss-ref-row-notes');
    if (!r.type) notes.appendChild(el(doc, 'p', 'ss-ref-row-note', SS.t('reference.not_chosen')));
    if (!r.mapConfirmed) {
      notes.appendChild(whyLine(doc, whyA, SS.t('reference.no_map'), mapsCount(rows), MAPS_STEP, 'setup.maps.title'));
    }
    if (bClosed) notes.appendChild(whyLine(doc, whyB, SS.t('reference.revealed_b'), '', '', ''));
    if (r.type === 'C') notes.appendChild(el(doc, 'p', 'ss-ref-row-note ss-ref-cnote', SS.t('reference.c_note')));
    if (r.type === 'B') {
      var line = el(doc, 'p', 'ss-ref-row-coding');
      line.setAttribute('data-locked', r.locked ? 'true' : 'false');
      line.appendChild(el(doc, 'span', 'ss-ref-coding-state',
        SS.tn(r.locked ? 'reference.coding_locked' : 'reference.coding_open', r.nMarks, { n: String(r.nMarks) })));
      var go = el(doc, 'a', 'ss-ref-go', SS.t(r.locked ? 'reference.read_coding' : 'reference.go_coding'));
      go.setAttribute('href', '#coding/' + encodeURIComponent(r.pid));
      line.appendChild(go);
      notes.appendChild(line);
    }
    fs.appendChild(notes);
    fs.appendChild(errorBox);
    return fs;
  }

  // The redraw keeps the place (item 38): the result, or the refusal, shows beside the choice just made.
  function choose(pid, type, errorBox) {
    errorBox.textContent = '';
    return SS.api.post('/api/references', { pid: pid, type: type }).then(function () {
      return SS.reference.refresh({ message: SS.t('reference.chosen', { pid: pid, type: type }) });
    }, function (err) {
      return SS.reference.refresh({ error: (err && err.message) || SS.t('api.failed') });
    });
  }

  function bulk(button, errorBox) {
    errorBox.textContent = '';
    button.disabled = true;
    return SS.api.post('/api/references/bulk', { confirmed_maps: 'A' }).then(function (doc) {
      var n = doc && Array.isArray(doc.set) ? doc.set.length : 0;
      return SS.reference.refresh({ message: n ? SS.t('reference.bulk_done', { n: String(n) }) : SS.t('reference.bulk_none') });
    }, function (err) {
      button.disabled = false;
      errorBox.textContent = (err && err.message) || SS.t('api.failed');
    });
  }

  SS.reference = {
    // Draws the screen for a GET /api/references reply (no fetch).
    draw: function draw(main, data) {
      var doc = docOf(main);
      clear(main);
      var page = el(doc, 'div', 'ss-ref');
      page.appendChild(el(doc, 'p', 'ss-kicker', SS.t('reference.kicker')));
      page.appendChild(el(doc, 'h1', 'ss-ref-title', SS.t('reference.heading')));
      page.appendChild(el(doc, 'p', 'ss-ref-intro', SS.t('reference.intro')));

      var cards = el(doc, 'div', 'ss-ref-cards');
      TYPES.forEach(function (t) { cards.appendChild(card(doc, t)); });
      page.appendChild(cards);

      var rows = rowsOf(data);
      var gate = gateOf(data);
      var list = el(doc, 'section', 'ss-ref-list');
      list.setAttribute('aria-labelledby', 'ss-ref-list-title');
      var head = el(doc, 'div', 'ss-ref-list-head');
      var h2 = el(doc, 'h2', 'ss-ref-list-title', SS.t('reference.list_title'));
      h2.setAttribute('id', 'ss-ref-list-title');
      head.appendChild(h2);
      var errorBox = el(doc, 'p', 'ss-setup-error');
      errorBox.setAttribute('role', 'alert');
      main.ssRefError = errorBox;
      var bulkWhy = null;
      if (rows.length) {
        var b = el(doc, 'button', 'primary ss-ref-bulk', SS.t('reference.bulk'));
        b.setAttribute('type', 'button');
        b.disabled = !rows.some(function (r) { return r.mapConfirmed && r.type !== 'A'; });
        b.addEventListener('click', function () { bulk(b, errorBox); });
        head.appendChild(b);
        if (b.disabled) {
          // item 37: the bulk button says why it cannot be used, and where to fix it
          var none = !rows.some(function (r) { return r.mapConfirmed; });
          bulkWhy = none
            ? whyLine(doc, 'ss-ref-bulk-why', SS.t('reference.bulk_why_none'), mapsCount(rows), MAPS_STEP, 'setup.maps.title')
            : whyLine(doc, 'ss-ref-bulk-why', SS.t('reference.bulk_why_done'), '', '', '');
          b.setAttribute('aria-describedby', 'ss-ref-bulk-why');
        }
      }
      list.appendChild(head);
      if (bulkWhy) list.appendChild(bulkWhy);
      list.appendChild(errorBox);
      if (!rows.length) {
        list.appendChild(el(doc, 'p', 'ss-shell-note', SS.t('reference.empty')));
        var back = el(doc, 'a', 'ss-setup-back', SS.t('reference.go_setup'));
        back.setAttribute('href', '#setup');
        list.appendChild(back);
      } else {
        var hasB = rows.some(function (r) { return r.type === 'B'; });
        if (gate.waiting.length) {
          list.appendChild(el(doc, 'p', 'ss-ref-gate', SS.t('reference.gate_waiting', { pids: gate.waiting.join(', ') })));
        } else if (hasB) {
          list.appendChild(el(doc, 'p', 'ss-ref-gate', SS.t('reference.gate_locked')));
        }
        var grid = el(doc, 'div', 'ss-ref-rows');
        rows.forEach(function (r) { grid.appendChild(row(doc, r, gate, rows)); });
        list.appendChild(grid);
      }
      page.appendChild(list);

      var every = el(doc, 'section', 'ss-ref-every');
      every.appendChild(el(doc, 'p', 'ss-kicker', SS.t('reference.every_mode_kicker')));
      every.appendChild(el(doc, 'h2', 'ss-ref-every-title', SS.t('reference.every_mode_title')));
      every.appendChild(el(doc, 'p', '', SS.t('reference.every_mode_body')));
      page.appendChild(every);
      page.appendChild(el(doc, 'p', 'ss-ref-pooling', SS.t('reference.pooling')));
      main.appendChild(page);
    },
    // The shell's render: fetch, then draw.
    // keep (from refresh): leave the page as it is until the new one is drawn.
    render: function render(main, sub, keep) {
      mounted = main;
      if (!keep) {
        SS.place.forget(main);
        clear(main);
        main.appendChild(el(docOf(main), 'p', 'ss-shell-note', SS.t('shell.loading')));
      }
      SS.place.watch(main);
      return SS.api.get('/api/references').then(function (data) {
        if (mounted !== main) return;
        SS.reference.draw(main, data);
      }, function (err) {
        if (mounted !== main) return;
        clear(main);
        main.appendChild(el(docOf(main), 'p', 'ss-shell-note', (err && err.message) || SS.t('api.failed')));
        if (err && (err.code === 'no_project' || err.code === 'sync_folder_unacknowledged')) {
          var back = el(docOf(main), 'a', 'ss-setup-back', SS.t('reference.go_setup'));
          back.setAttribute('href', '#setup');
          main.appendChild(back);
        }
      });
    },
    // Fetch again and redraw, keeping the place (item 38). opts.message / opts.error: the line beside the control.
    refresh: function refresh(opts) {
      if (!mounted) return Promise.resolve();
      var main = mounted;
      return SS.place.keep(main, function () { return SS.reference.render(main, '', true); }, {
        message: opts && opts.message, error: opts && opts.error,
        still: function () { return mounted === main; }
      });
    }
  };

  if (SS.shell && typeof SS.shell.register === 'function') {
    SS.shell.register('reference', { order: 20, titleKey: 'reference.title', render: SS.reference.render });
  }
})();
