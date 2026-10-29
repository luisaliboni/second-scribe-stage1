/* Try it page (Task 28): mounts the one viewer on the shipped synthetic data. Loaded last, after the viewer.
     window.SS_DATA.tryit           view-data/1 of the recorded demo run (or the labelled test pattern), data/tryit.js
     window.SS_DATA.tryit_profile   the synthetic orgsci-profile/v2, data/tryit-profile.js
   Item 11 (23 Oct): the page opens with the finding in words (#tryit-finding), filled here from the profile's lead
   row: the AI map's and the no-interview map's recall of endorsed steps (cells ai_recall and no_interview_recall,
   x "primary"), as percentages. Without both cells the sentence stays hidden. The maps come next; the whole profile
   goes into #tryit-profile, inside the page's closed "Technical details" (every cell stays in the DOM).
   The viewer draws the SYNTHETIC band and the test-pattern label itself (viewer.js, bands). The oracle state and the
   list of logged runs are written into the page by build_site. Without the data file the page says so in words.
   Classic script; attaches SS.tryit to window.SS. */
(function () {
  'use strict';
  var root = typeof window !== 'undefined' ? window : globalThis;
  var SS = root.SS = root.SS || {};

  // The lead row's value for one cell name at x "primary", or null.
  function leadValue(profile, name) {
    var rows = profile && Array.isArray(profile.rows) ? profile.rows : [];
    for (var i = 0; i < rows.length; i++) {
      if (!rows[i] || rows[i].row !== 'lead') continue;
      var cells = rows[i] && Array.isArray(rows[i].cells) ? rows[i].cells : [];
      for (var j = 0; j < cells.length; j++) {
        var c = cells[j];
        if (c && c.name === name && c.x === 'primary' && typeof c.value === 'number' && isFinite(c.value)) return c.value;
      }
    }
    return null;
  }
  function percent(v) { return (v * 100).toFixed(1) + '%'; }

  // The finding sentence, or null when the profile lacks either number.
  function finding(profile) {
    var ai = leadValue(profile, 'ai_recall');
    var none = leadValue(profile, 'no_interview_recall');
    if (ai === null || none === null) return null;
    return SS.t('try.finding', { ai: percent(ai), none: percent(none) });
  }

  function start() {
    var doc = root.document;
    var host = doc && doc.getElementById ? doc.getElementById('tryit-viewer') : null;
    if (!host) return null;
    var data = root.SS_DATA || {};
    var view = data.tryit;
    if (!view || typeof view !== 'object') {
      var p = doc.createElement('p');
      p.className = 'site-note';
      p.textContent = SS.t('try.no_data');
      host.appendChild(p);
      return null;
    }
    var opts = {};
    // council 30 (seat 02 F3): the run block sits in "Technical details", first in its body
    var tech = doc.querySelector ? doc.querySelector('.try-details-body') : null;
    if (tech) {
      var runHost = doc.createElement('div');
      runHost.className = 'try-run';
      runHost.setAttribute('data-part', 'run-block');
      tech.insertBefore(runHost, tech.firstChild);
      opts.runHost = runHost;
    }
    if (data.tryit_profile && typeof data.tryit_profile === 'object') {
      opts.profile = data.tryit_profile;
      var profileHost = doc.getElementById('tryit-profile');
      if (profileHost) opts.profileHost = profileHost;      // the whole profile, inside "Technical details"
      else opts.compactProfile = true;                      // a page without that slot keeps the lead in view
      var said = finding(data.tryit_profile);
      var lead = doc.getElementById('tryit-finding');
      if (said && lead) {
        lead.textContent = said;
        lead.hidden = false;
        if (lead.removeAttribute) lead.removeAttribute('hidden');
      }
    }
    return SS.viewer.mount(host, view, opts);
  }

  SS.tryit = { start: start, finding: finding };
  if (root.document) {
    if (root.document.readyState === 'loading' && root.document.addEventListener) {
      root.document.addEventListener('DOMContentLoaded', start);
    } else {
      start();
    }
  }
})();
