/* Setup step "maps" (Task 33; spec 6.5 step 5, section 7; Review Focus 2; plan V12): each imported map drawn back
   the way the kit read it, in the reference lane's encoding (a solid box happened; a dashed box marked "Missing" is
   missing; shape and text carry the meaning, the red only repeats it), on the study's phases, with the counts
   "12 boxes, 3 marked missing, 14 arrows". A map that reads nothing missing is flagged; for Visio the step offers
   red set by a master shape or style. The user can move a box to another phase, then confirms the map
   (POST /api/maps/confirm, logged). Nothing runs until every paired map is confirmed.
   Classic script; queues itself for SS.setup. */
(function () {
  'use strict';
  var root = typeof window !== 'undefined' ? window : globalThis;
  var SS = root.SS = root.SS || {};
  var uid = 0;

  function el(doc, tag, cls, text) {
    var n = doc.createElement(tag);
    if (cls) n.setAttribute('class', cls);
    if (text !== undefined) n.textContent = text;
    return n;
  }

  function clear(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
  }

  function err(e) {
    return (e && e.message) || SS.t('api.failed');
  }

  // Columns: the study's phases in order, then one for boxes whose phase is not a study phase (if any).
  // Boxes in a column keep the map's left-to-right order (x, then file order).
  function columns(prev) {
    var phases = Array.isArray(prev.phases) ? prev.phases.slice() : [];
    var cols = phases.map(function (p) { return { phase: p, title: p, nodes: [], headings: [] }; });
    var other = { phase: null, title: SS.t('setup.maps.other_phase'), nodes: [], headings: [] };
    // A box the importer flagged as a possible phase heading is listed apart from the steps; it is still drawn.
    (prev.nodes || []).forEach(function (n, i) {
      var c = phases.indexOf(n.phase);
      var col = c >= 0 ? cols[c] : other;
      (n.phase_heading ? col.headings : col.nodes).push({ node: n, i: i });
    });
    if (other.nodes.length || other.headings.length) cols.push(other);
    function byX(a, b) {
      var ax = typeof a.node.x === 'number' ? a.node.x : Infinity;
      var bx = typeof b.node.x === 'number' ? b.node.x : Infinity;
      return ax !== bx ? ax - bx : a.i - b.i;
    }
    cols.forEach(function (c) {
      c.nodes.sort(byX);
      c.nodes = c.nodes.map(function (x) { return x.node; });
      c.headings.sort(byX);
      c.headings = c.headings.map(function (x) { return x.node; });
    });
    return cols;
  }

  // council 30 (seat 02 F7): "phase from its place on the map" is said once, above the drawing (positionNote); a box
  // says where its phase came from only when that is somewhere else (set by you, or named in the map file while the
  // note is shown)
  function positionNote(prev) {
    return (prev.nodes || []).some(function (n) { return n.phase_from === 'position'; });
  }

  function box(doc, n, noted) {
    var missing = n.status === 'missing';
    var b = el(doc, 'li', 'ss-setup-mapbox ' + (missing ? 'is-missing' : 'is-happened'));
    b.setAttribute('data-node-id', String(n.id));
    b.setAttribute('data-status', missing ? 'missing' : 'happened');
    b.appendChild(el(doc, 'span', 'ss-setup-mapbox-text', n.text || SS.t('setup.maps.no_text')));
    if (missing) b.appendChild(el(doc, 'span', 'ss-setup-mapbox-tag', SS.t('setup.maps.missing_tag')));
    if (n.phase_heading) {
      b.setAttribute('data-phase-heading', 'true');
      b.appendChild(el(doc, 'span', 'ss-setup-mapbox-tag ss-setup-mapbox-heading', SS.t('setup.maps.heading_tag')));
    }
    if (n.phase_from === 'you') b.appendChild(el(doc, 'span', 'ss-setup-mapbox-from', SS.t('setup.maps.phase_you')));
    else if (n.phase_from === 'map' && noted) {
      b.appendChild(el(doc, 'span', 'ss-setup-mapbox-from', SS.t('setup.maps.phase_map')));
    }
    return b;
  }

  function drawing(doc, prev) {
    var noted = positionNote(prev);
    var outer = el(doc, 'div', 'ss-setup-mapdraw-wrap');
    if (noted) outer.appendChild(el(doc, 'p', 'ss-setup-muted ss-setup-mapdraw-note', SS.t('setup.maps.phase_position_note')));
    var wrap = el(doc, 'div', 'ss-setup-mapdraw');
    wrap.setAttribute('role', 'group');
    wrap.setAttribute('aria-label', SS.t('setup.maps.drawing_label', { pid: prev.pid }));
    outer.appendChild(wrap);
    columns(prev).forEach(function (c) {
      var col = el(doc, 'div', 'ss-setup-mapcol' + (c.phase === null ? ' is-other' : ''));
      col.appendChild(el(doc, 'h4', 'ss-setup-mapcol-title', c.title));
      var list = el(doc, 'ul', 'ss-setup-mapcol-list');
      c.nodes.forEach(function (n) { list.appendChild(box(doc, n, noted)); });
      if (!c.nodes.length && !c.headings.length) {
        list.appendChild(el(doc, 'li', 'ss-setup-muted', SS.t('setup.maps.empty_phase')));
      }
      col.appendChild(list);
      if (c.headings.length) {
        col.appendChild(el(doc, 'p', 'ss-setup-muted ss-setup-mapcol-caption', SS.t('setup.maps.headings_caption')));
        var heads = el(doc, 'ul', 'ss-setup-mapcol-headings');
        c.headings.forEach(function (n) { heads.appendChild(box(doc, n, noted)); });
        col.appendChild(heads);
      }
      wrap.appendChild(col);
    });
    return outer;
  }

  function arrows(doc, prev) {
    var byId = {};
    (prev.nodes || []).forEach(function (n) { byId[n.id] = n.text || String(n.id); });
    var d = el(doc, 'details', 'ss-setup-details');
    d.appendChild(el(doc, 'summary', '', SS.t('setup.maps.arrows_summary', { n: String((prev.edges || []).length) })));
    var list = el(doc, 'ul', 'ss-setup-arrows');
    (prev.edges || []).forEach(function (e) {
      var missing = e.status === 'missing';
      var li = el(doc, 'li', missing ? 'is-missing' : 'is-happened');
      li.appendChild(el(doc, 'span', '', (byId[e.source] || e.source) + ' → ' + (byId[e.target] || e.target)));
      if (e.label) li.appendChild(el(doc, 'span', 'ss-setup-muted', ' (' + e.label + ')'));
      if (missing) li.appendChild(el(doc, 'span', 'ss-setup-mapbox-tag', SS.t('setup.maps.missing_tag')));
      list.appendChild(li);
    });
    d.appendChild(list);
    return d;
  }

  function phaseEditor(doc, prev) {
    var d = el(doc, 'details', 'ss-setup-details');
    d.appendChild(el(doc, 'summary', '', SS.t('setup.maps.phases_summary')));
    var selects = [];
    var t = el(doc, 'table', 'ss-setup-table');
    var tbody = el(doc, 'tbody');
    (prev.nodes || []).forEach(function (n) {
      uid += 1;
      var tr = el(doc, 'tr');
      var th = el(doc, 'th', '', n.text || String(n.id));
      th.setAttribute('scope', 'row');
      var id = 'ss-setup-maps-phase-' + uid;
      var lab = el(doc, 'label', 'ss-visually-hidden', SS.t('setup.maps.phase_of', { box: n.text || String(n.id) }));
      lab.setAttribute('for', id);
      var sel = el(doc, 'select', 'ss-setup-select');
      sel.setAttribute('id', id);
      (prev.phases || []).forEach(function (p) {
        var o = el(doc, 'option', '', p);
        o.setAttribute('value', p);
        if (p === n.phase) o.setAttribute('selected', 'selected');
        sel.appendChild(o);
      });
      if ((prev.phases || []).indexOf(n.phase) < 0) {
        var none = el(doc, 'option', '', SS.t('setup.maps.pick_phase'));
        none.setAttribute('value', '');
        none.setAttribute('selected', 'selected');
        sel.appendChild(none);
      }
      sel.value = (prev.phases || []).indexOf(n.phase) >= 0 ? n.phase : '';
      var td = el(doc, 'td');
      td.appendChild(lab);
      td.appendChild(sel);
      tr.appendChild(th);
      tr.appendChild(td);
      tbody.appendChild(tr);
      selects.push({ node: n, sel: sel });
    });
    t.appendChild(tbody);
    d.appendChild(t);
    var button = el(doc, 'button', '', SS.t('setup.maps.save_phases'));
    button.setAttribute('type', 'button');
    var errorBox = el(doc, 'p', 'ss-setup-error');
    errorBox.setAttribute('role', 'alert');
    button.addEventListener('click', function () {
      var changes = {};
      selects.forEach(function (s) {
        if (s.sel.value && s.sel.value !== s.node.phase) changes[s.node.id] = s.sel.value;
      });
      if (!Object.keys(changes).length) return;
      button.disabled = true;
      errorBox.textContent = '';
      SS.api.post('/api/maps/phases', { pid: prev.pid, node_phase: changes }).then(function () {
        return SS.setup.refresh();
      }, function (e) {
        button.disabled = false;
        errorBox.textContent = err(e);
      });
    });
    d.appendChild(button);
    d.appendChild(errorBox);
    return d;
  }

  function card(doc, prev) {
    var c = el(doc, 'section', 'ss-setup-card');
    c.setAttribute('data-pid', prev.pid);
    var h = el(doc, 'h3', 'ss-setup-subtitle');
    h.appendChild(el(doc, 'span', 'mono', prev.pid));
    h.appendChild(el(doc, 'span', 'ss-setup-muted', ' · ' + prev.map));
    c.appendChild(h);
    var counts = el(doc, 'p', 'ss-setup-lead', prev.counts_text);
    counts.setAttribute('data-counts', 'true');
    c.appendChild(counts);
    if (prev.counts && prev.counts.unresolved) {
      c.appendChild(el(doc, 'p', 'ss-setup-hint', SS.t('setup.maps.unresolved', { n: String(prev.counts.unresolved) })));
    }
    if (prev.phase_headings) {
      c.appendChild(el(doc, 'p', 'ss-setup-hint', SS.t('setup.maps.headings_note')));
    }
    if (prev.changed) {
      c.appendChild(el(doc, 'p', 'ss-setup-warning ss-setup-warning-inline', SS.t('setup.maps.changed')));
    }
    (prev.warnings || []).forEach(function (w) {
      var p = el(doc, 'div', 'ss-setup-warning ss-setup-warning-inline');
      if (w.code === 'zero_missing') p.setAttribute('role', 'alert');
      p.setAttribute('data-code', w.code);
      p.appendChild(el(doc, 'p', '', w.message));
      if (w.code === 'zero_missing' && prev.offer_inherit) {
        p.appendChild(el(doc, 'p', '', SS.t('setup.maps.inherit_offer')));
        var b = el(doc, 'button', '', SS.t('setup.maps.inherit_button'));
        b.setAttribute('type', 'button');
        b.setAttribute('data-action', 'inherit');
        var eb = el(doc, 'p', 'ss-setup-error');
        eb.setAttribute('role', 'alert');
        b.addEventListener('click', function () {
          b.disabled = true;
          var rule = prev.rule || {};
          SS.api.post('/api/missing-rule', { suffix: '.vsdx', kind: 'red_line_or_text', colors: rule.colors,
            inherit: true }).then(function () { return SS.setup.refresh(); }, function (e) {
            b.disabled = false;
            eb.textContent = err(e);
          });
        });
        p.appendChild(b);
        p.appendChild(eb);
      }
      c.appendChild(p);
    });
    c.appendChild(drawing(doc, prev));
    c.appendChild(arrows(doc, prev));
    if ((prev.phases || []).length) c.appendChild(phaseEditor(doc, prev));
    if (prev.confirmed) {
      var ok = el(doc, 'p', 'ss-setup-ok', SS.t('setup.maps.confirmed'));
      ok.setAttribute('role', 'status');
      c.appendChild(ok);
    } else {
      var button = el(doc, 'button', 'primary', SS.t('setup.maps.confirm'));
      button.setAttribute('type', 'button');
      button.setAttribute('data-action', 'confirm');
      var errorBox = el(doc, 'p', 'ss-setup-error');
      errorBox.setAttribute('role', 'alert');
      button.addEventListener('click', function () {
        button.disabled = true;
        errorBox.textContent = '';
        SS.api.post('/api/maps/confirm', { pid: prev.pid, sha256: prev.sha256 }).then(function () {
          return SS.setup.refresh();
        }, function (e) {
          button.disabled = false;
          errorBox.textContent = err(e);
        });
      });
      c.appendChild(button);
      c.appendChild(errorBox);
    }
    return c;
  }

  function draw(container, table, previews) {
    var doc = container.ownerDocument || root.document;
    clear(container);
    container.appendChild(el(doc, 'p', 'ss-setup-intro', SS.t('setup.maps.intro')));
    if (!table.confirmed) container.appendChild(el(doc, 'p', 'ss-setup-note', SS.t('setup.maps.needs_pairing')));
    if (!previews.length) {
      container.appendChild(el(doc, 'p', 'ss-setup-note', SS.t('setup.maps.empty')));
      return;
    }
    var done = previews.filter(function (p) { return p.preview && p.preview.confirmed; }).length;
    var lead = el(doc, 'p', 'ss-setup-lead', SS.t('setup.maps.progress', { done: String(done),
      total: String(previews.length) }));
    lead.setAttribute('role', 'status');
    container.appendChild(lead);
    previews.forEach(function (p) {
      if (p.error) {
        var bad = el(doc, 'section', 'ss-setup-card');
        bad.appendChild(el(doc, 'h3', 'ss-setup-subtitle mono', p.pid));
        bad.appendChild(el(doc, 'p', 'ss-setup-error', p.error));
        container.appendChild(bad);
        return;
      }
      container.appendChild(card(doc, p.preview));
    });
  }

  function render(container) {
    var doc = container.ownerDocument || root.document;
    clear(container);
    container.appendChild(el(doc, 'p', 'ss-shell-note', SS.t('shell.loading')));
    return SS.api.get('/api/pairing').then(function (table) {
      var rows = ((table && table.rows) || []).filter(function (r) { return !!r.map && !!r.transcript; });
      return Promise.all(rows.map(function (r) {
        return SS.api.get('/api/maps/preview', { pid: r.pid }).then(function (prev) {
          return { pid: r.pid, preview: prev };
        }, function (e) {
          return { pid: r.pid, error: err(e) };
        });
      })).then(function (previews) { draw(container, table || {}, previews); });
    }, function (e) {
      clear(container);
      container.appendChild(el(doc, 'p', 'ss-setup-error', err(e)));
    });
  }

  SS.setupMaps = { columns: columns, draw: draw };
  (SS.setupQueue = SS.setupQueue || []).push(['maps', {
    order: 45, titleKey: 'setup.maps.title', needsProject: true, render: render
  }]);
})();
