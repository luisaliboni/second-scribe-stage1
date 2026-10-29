/* The report page (Task 42): exports/report-<run_id>/index.html, opened from the folder, offline. Loaded last.
     window.SS_DATA.report_view     view-data/1 of the run (data/view.js)
     window.SS_DATA.report_profile  its orgsci-profile/v2 profile (data/profile.js; absent when the exporter refused)
   It writes the plain sentences from the profile into #report-lead (scout F9), mounts the one viewer (the three
   readings of one map, the drawer and the absent-steps table; the profile panels go into #report-profile, inside the
   closed "Technical details", which opens before printing), then draws the lanes of every map at one rung under it, so a printed copy holds every map, not only the one
   picked on screen. The print button calls window.print() (Save as PDF is the browser's). When it is done it sets
   data-report="ready" on <html>, and data-report-styled="yes" when the stylesheets are applied (the page's own check,
   read by the tests and the screenshot harness). Data goes into the page through textContent only.
   Classic script; attaches SS.report to window.SS. */
(function () {
  'use strict';
  var root = typeof window !== 'undefined' ? window : globalThis;
  var SS = root.SS = root.SS || {};
  var PRINT_RUNG_ORDER = ['rung2', 'rung1', 'rung3', 'placebo'];
  var PRINT_WIDTH_PX = 660;   // the printed line on A4 or Letter with print.css margins, less the lane frame
  var PRINT_ZOOM_MAX = 0.62;

  function el(doc, tag, cls, text, parent) {
    var n = doc.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null && text !== '') n.textContent = text;
    if (parent) parent.appendChild(n);
    return n;
  }

  // The rung every map is drawn at for print: rung 2 when the run has it (the viewer's own default), else the first.
  function printRung(view) {
    var rungs = Array.isArray(view.rungs) ? view.rungs : [];
    for (var i = 0; i < PRINT_RUNG_ORDER.length; i++) {
      if (rungs.indexOf(PRINT_RUNG_ORDER[i]) >= 0) return PRINT_RUNG_ORDER[i];
    }
    return rungs.length ? rungs[0] : 'rung2';
  }

  // On paper a map's lanes shrink until the widest fits the printed line (print.css reads --report-print-zoom); the
  // lanes keep their natural width on screen, where they scroll.
  function printZoom(body) {
    var inner = body.querySelector ? body.querySelector('.ss-lanes-inner') : null;
    var natural = inner ? inner.scrollWidth : 0;
    if (!natural || !body.style || !body.style.setProperty) return null;
    var z = Math.max(0.3, Math.min(PRINT_ZOOM_MAX, PRINT_WIDTH_PX / natural));
    z = Math.floor(z * 1000) / 1000;
    body.style.setProperty('--report-print-zoom', String(z));
    return z;
  }

  function drawMaps(doc, host, view) {
    if (!host || !SS.lanes || !Array.isArray(view.maps) || !view.maps.length) return 0;
    var rung = printRung(view);
    var rungName = SS.t('views.rung.' + rung);
    el(doc, 'h3', 'report-maps-title', SS.t('report.maps.title', { rung: rungName }), host);
    var n = 0;
    view.maps.forEach(function (m, i) {
      var fig = el(doc, 'section', 'report-map', undefined, host);
      fig.setAttribute('data-map', String(m && m.pid ? m.pid : i));
      el(doc, 'h4', 'report-map-title mono', SS.t('report.maps.map', { map: m && m.pid ? m.pid : String(i + 1), rung: rungName }), fig);
      var body = el(doc, 'div', 'report-map-body', undefined, fig);
      SS.lanes.render(body, view, i, rung, { gloss: false, allRungs: SS.lanes.isPlain(view, i) });
      printZoom(body);
      n += 1;
    });
    return n;
  }

  // The plain sentences that open the report (scout F9), from the run's own profile: SS.profile.renderLead, as the
  // app's Results screen writes them. A run with no judge or no reference gets its one plain sentence instead.
  function lead(doc, view, profile) {
    var host = doc.getElementById('report-lead');
    if (!host || !SS.profile || typeof SS.profile.renderLead !== 'function') return null;
    var judgeless = !!(SS.viewer && typeof SS.viewer.noJudge === 'function' && SS.viewer.noJudge(view));
    var refType = typeof view.reference_type === 'string' ? view.reference_type : '';
    if (!profile && !judgeless && refType !== 'C') return null;
    return SS.profile.renderLead(host, profile || {}, { judgeless: judgeless, referenceType: refType });
  }

  function styled(doc) {
    if (!root.getComputedStyle || !doc.body) return false;
    var cs = root.getComputedStyle(doc.body);
    var bg = cs.backgroundColor;
    return /Schibsted Grotesk/.test(cs.fontFamily || '') && !!bg && bg !== 'rgba(0, 0, 0, 0)' && bg !== 'transparent';
  }

  function start() {
    var doc = root.document;
    if (!doc) return null;
    var html = doc.documentElement;
    // #theme=dark or #theme=light forces a theme (screenshots); otherwise the system's choice holds
    var theme = /(?:^|[#&])theme=(dark|light)(?:&|$)/.exec(root.location ? String(root.location.hash) : '');
    if (theme) html.setAttribute('data-theme', theme[1]);
    var button = doc.getElementById('report-print');
    if (button) button.addEventListener('click', function () { root.print(); });
    var data = root.SS_DATA || {};
    var view = data.report_view;
    var host = doc.getElementById('report-viewer');
    var mounted = null;
    var profile = data.report_profile && typeof data.report_profile === 'object' ? data.report_profile : null;
    if (view && typeof view === 'object') lead(doc, view, profile);
    if (host && view && typeof view === 'object' && SS.viewer) {
      var opts = { derived: false };
      // item 40: the maps first, then the plain sentences (the whole lead section moves after the drawer), then the
      // absent-steps table
      var leadSection = doc.querySelector ? doc.querySelector('.report-lead-section') : null;
      if (leadSection) opts.beforeLedger = leadSection;
      if (profile) {
        opts.profile = profile;
        opts.profileHost = doc.getElementById('report-profile');
      }
      mounted = SS.viewer.mount(host, view, opts);
      drawMaps(doc, doc.getElementById('report-maps'), view);
    }
    // The technical details stay closed on screen; a printed copy holds them.
    var details = doc.getElementById('report-technical');
    if (details && typeof root.addEventListener === 'function') {
      root.addEventListener('beforeprint', function () { details.open = true; });
    }
    html.setAttribute('data-report-styled', styled(doc) ? 'yes' : 'no');
    html.setAttribute('data-report', 'ready');
    return mounted;
  }

  SS.report = { start: start, printRung: printRung, printZoom: printZoom, lead: lead };
  if (root.document) {
    if (root.document.readyState === 'loading' && root.document.addEventListener) {
      root.document.addEventListener('DOMContentLoaded', start);
    } else {
      start();
    }
  }
})();
