/* SS.study: the site's "The study" page (plan Task 30 under amendment V4). Classic script; needs core.js, profile.js
   and (for the row 5 and row 8 drill-downs) strip.js. Data comes only from data/study.js:
   window.SS_DATA["study"] (site contract 4, as T28 builds it), which holds the authors' profile file
   (schema orgsci-profile/v2) or the build's stub {"state": "not_yet_run"}.

   SS.study.state(doc)   -> "reported" | "not_yet_run" | "synthetic" | "pending" | "unreadable"
   SS.study.panels(doc)  -> [{id, index, row, label_key, title_key, present}], always all twelve, in order (the
                            panel list sent to the analysis session, numbered from 0): panel 0 the lead, panel 1 the
                            ledger, panels 2 to 10 rows 1 to 9, panel 11 the run block. id is "panel-<index>".
                            (Panel names are written as "panel-<n>" in code: a capital P with digits reads as a
                            participant id to the kit's leak scan.)
   SS.study.runFields(doc) -> [{group, key, label_key, value, model, reason, n}] the run block's fields, a fixed list of
                            key paths. The public form (PROFILE-SCHEMA section 4 and 14) is read as the study file
                            ships it: run_chain_heads null with run_chain_heads_source, kit.run_commits a list,
                            kit.release_commit null, a model field null with that model's source
                            "not_recorded_public". The older per-phase form ({q, j, m} heads and commits,
                            kit.release_branch, reading3_prompt.working_tree_sha256) still draws when a file has it.
                            `reason` is the code that explains a null value (its sentence: SS.profile.runReason).
   SS.study.render(root, doc)  draws the page for that state.

   Rules (tests/js/study.test.js):
   - Only a "reported" file is drawn. Any other file state gets its own screen and none of its numbers: this page
     never shows a test corpus's profile, a lab's pending profile or anything it cannot read.
   - Panels 0 to 10 are SS.profile.render's rows (nothing computed here); a row missing from the file keeps its place and
     reads "not computed".
   - Panel 11 prints a fixed list of run keys: the chain heads and the declaration-log head, the commits, prompt hashes,
     models and declared settings. Never per-record hashes, never the file's free-text source notes. "[SOURCE]"
     reads "available from the authors"; a missing value reads "not computed", never 0.
   - Every value the page fills from the file carries data-key with its key path in the file.
   - The interviewer-drawn maps are one reading, shown as totals over all maps only.
   - The open page (the author's items 8 and 9, 23 Oct) is the three readings, four findings in plain sentences and one
     paragraph on what a team does with them. Each finding's numbers are read from cells of the file (never typed in)
     and printed as percentages or points; the paragraph names its cells' key paths in data-keys. Everything else (the
     prompt, the page meta, the panel list, the twelve panels) sits in one closed <details> "Technical details", still
     in the page, so every cell stays in the DOM.
   Data reaches the DOM through textContent and attributes only. */
(function () {
  'use strict';
  var root = typeof window !== 'undefined' ? window : globalThis;
  var SS = root.SS = root.SS || {};
  var hasOwn = Object.prototype.hasOwnProperty;
  var PLACEHOLDER = '[SOURCE]';
  var SCHEMA_RE = /^orgsci-profile\/v2(?:\.\d+)*$/;
  var FILE_STATES = ['reported', 'synthetic', 'pending'];
  var ROWS = [
    { row: 'lead', key: 'profile.lead' },
    { row: 'ledger', key: 'profile.ledger' },
    { row: 1, key: 'profile.row1.coverage' },
    { row: 2, key: 'profile.row2.size' },
    { row: 3, key: 'profile.row3.support' },
    { row: 4, key: 'profile.row4.links' },
    { row: 5, key: 'profile.row5.absences' },
    { row: 6, key: 'profile.row6.slant' },
    { row: 7, key: 'profile.row7.floors' },
    { row: 8, key: 'profile.row8.instruction' },
    { row: 9, key: 'profile.row9.instrument_error' }
  ];
  // The run block, as key paths under run. Anything else in run (a future per-record list, source notes) is not shown.
  var RUN_GROUPS = [
    ['chains', ['gate_chain.head', 'gate_chain.entries', 'gate_chain.verified', 'gate_chain.verify_date',
      'declaration_log.head', 'declaration_log.records', 'declaration_log.last_seq', 'declaration_log.verified',
      '@run_chain_heads']],
    ['kit', ['kit.release_commit', 'kit.release_branch', '@kit.run_commits']],
    ['prompts', ['prompt_set.q_phase_sha256', 'prompt_set.j_m_phase_sha256', 'reading3_prompt.path',
      'reading3_prompt.sha256', 'reading3_prompt.working_tree_sha256']],
    ['models', null],
    ['settings', ['settings.matcher_primary.lexical_threshold', 'settings.matcher_primary.judge_label',
      'settings.matcher_lenient.lexical_threshold', 'settings.matcher_lenient.judge_label',
      'settings.reconstructor_temperature', 'settings.reading3_temperature', 'settings.reading3_draws',
      'settings.bootstrap_resamples', 'settings.interval_method', 'settings.gate_k_people']],
    ['verify', ['verify.gate_chain', 'verify.j', 'verify.m']]
  ];
  // Keys the public form does not write at all: drawn only when the file has them (no row of "not computed").
  var OLD_FORM_ONLY = ['kit.release_branch', 'reading3_prompt.working_tree_sha256'];
  var PHASES = ['q', 'j', 'm'];
  var RELEASE_PUBLIC = { release_public: true };
  var MODEL_FIELDS = ['family', 'model_id', 'weights', 'quantisation', 'thinking'];
  var MONO_KEYS = /(head|commit|commits\.[a-z0-9]+|sha256|path|release_branch|model_id|weights|run_chain_heads\.[a-z]+)$/;

  function isObj(v) { return v !== null && typeof v === 'object' && !Array.isArray(v); }
  function list(v) { return Array.isArray(v) ? v : []; }
  function kp(code) { return String(code).toLowerCase().replace(/[^a-z0-9_]/g, '_'); }
  function has(key) {
    var t = root.SS_STRINGS;
    return t !== null && typeof t === 'object' && hasOwn.call(t, key) && typeof t[key] === 'string';
  }

  // ------------------------------------------------------------------------------------------- DOM helpers
  function el(tag, cls, text, parent) {
    var n = root.document.createElement(tag);
    if (cls) n.setAttribute('class', cls);
    if (text !== undefined && text !== null) n.textContent = String(text);
    if (parent) parent.appendChild(n);
    return n;
  }
  function clear(node) {
    if (typeof node.replaceChildren === 'function') { node.replaceChildren(); return; }
    while (node.firstChild) node.removeChild(node.firstChild);
  }
  function kids(node) { return node && node.children ? Array.prototype.slice.call(node.children) : []; }

  // ------------------------------------------------------------------------------------------- data and state
  function data() {
    var d = root.SS_DATA;
    return isObj(d) && hasOwn.call(d, 'study') ? d.study : undefined;
  }

  function state(doc) {
    if (doc === undefined) return 'not_yet_run';
    if (!isObj(doc)) return 'unreadable';
    if (doc.state === 'not_yet_run' && !hasOwn.call(doc, 'rows')) return 'not_yet_run';
    if (typeof doc.schema !== 'string' || !SCHEMA_RE.test(doc.schema)) return 'unreadable';
    if (FILE_STATES.indexOf(doc.state) < 0) return 'unreadable';
    if (!Array.isArray(doc.rows)) return 'unreadable';
    return doc.state;
  }

  function panels(doc) {
    var rows = list(doc && doc.rows);
    var out = ROWS.map(function (p, i) {
      var r = null;
      rows.forEach(function (x) { if (!r && x && x.row === p.row) r = x; });
      var labelKey = r && typeof r.label_key === 'string' ? r.label_key : p.key;
      return { id: 'panel-' + i, index: i, row: p.row, label_key: labelKey, title_key: labelKey + '.title', present: !!r };
    });
    out.push({ id: 'panel-' + ROWS.length, index: ROWS.length, row: 'run', label_key: 'study.run', title_key: 'study.run.title',
      present: isObj(doc && doc.run) });
    return out;
  }

  function reasonCode(v) { return typeof v === 'string' && /^[a-z0-9_.-]{1,40}$/.test(v) ? v : undefined; }

  // run_chain_heads: null (public form: the code in run_chain_heads_source says why), {q, j, m} (older form), or
  // anything else, which reads "not computed" rather than drawing an object.
  function chainHeadFields(run, group) {
    var h = isObj(run) ? run.run_chain_heads : undefined;
    if (isObj(h) && PHASES.some(function (k) { return hasOwn.call(h, k); })) {
      return PHASES.map(function (k) {
        return { group: group, key: 'run.run_chain_heads.' + k, label_key: 'study.run.run_chain_heads.' + k, value: h[k] };
      });
    }
    var src = isObj(run) ? run.run_chain_heads_source : undefined;
    return [{ group: group, key: 'run.run_chain_heads_source', label_key: 'study.run.run_chain_heads',
      value: h === null || h === undefined ? reasonCode(src) : undefined,
      reason: h === null || h === undefined ? reasonCode(src) : undefined }];
  }

  // kit.run_commits: a list (Q, J, M in that order, public form) or {q, j, m} (older form).
  function commitFields(run, group) {
    var rc = isObj(run) && isObj(run.kit) ? run.kit.run_commits : undefined;
    if (Array.isArray(rc)) {
      return rc.map(function (c, i) {
        var named = rc.length === PHASES.length;
        var f = { group: group, key: 'run.kit.run_commits.' + i, value: c };
        if (named) f.label_key = 'study.run.kit.run_commits.' + PHASES[i];
        else { f.label_key = 'study.run.kit.run_commits.n'; f.n = i + 1; }
        return f;
      });
    }
    return PHASES.map(function (k) {
      return { group: group, key: 'run.kit.run_commits.' + k, label_key: 'study.run.kit.run_commits.' + k,
        value: isObj(rc) ? rc[k] : undefined };
    });
  }

  function runFields(doc) {
    var run = doc && doc.run;
    var out = [];
    RUN_GROUPS.forEach(function (g) {
      var group = g[0];
      if (group === 'models') {
        list(isObj(run) ? run.models : null).forEach(function (m, i) {
          if (!isObj(m)) return;
          MODEL_FIELDS.forEach(function (f) {
            if (f !== 'model_id' && f !== 'family' && !hasOwn.call(m, f)) return;
            var v = hasOwn.call(m, f) ? m[f] : undefined;
            out.push({ group: group, key: 'run.models.' + i + '.' + f, label_key: 'study.run.model.' + f,
              value: v, model: i, role: m.role,
              reason: v === null || v === undefined ? reasonCode(m.source) : undefined });
          });
        });
        return;
      }
      g[1].forEach(function (k) {
        if (k === '@run_chain_heads') { chainHeadFields(run, group).forEach(function (f) { out.push(f); }); return; }
        if (k === '@kit.run_commits') { commitFields(run, group).forEach(function (f) { out.push(f); }); return; }
        if (OLD_FORM_ONLY.indexOf(k) >= 0 && SS.get(run, k) === undefined) return;
        var f = { group: group, key: 'run.' + k, label_key: 'study.run.' + k, value: SS.get(run, k) };
        // the public file has no kit release commit of its own: the kit is the public repository's release
        if (k === 'kit.release_commit' && f.value === null) f.value = RELEASE_PUBLIC;
        out.push(f);
      });
    });
    return out;
  }

  // `reason` (a code from the file) explains a null; its sentence is SS.profile.runReason's, else "not computed".
  function fmtValue(v, reason) {
    if (v === RELEASE_PUBLIC) return SS.t('study.run.kit.release_commit_public');
    if (reason !== undefined) {
      var why = SS.profile.runReason(reason);
      if (why !== null) return why;
    }
    if (typeof v === 'string' && v === reason) return SS.t('common.not_computed');   // a code with no sentence
    if (v === undefined || v === null) return SS.t('common.not_computed');
    if (v === PLACEHOLDER) return SS.t('study.run.source_placeholder');
    if (v === true) return SS.t('study.run.yes');
    if (v === false) return SS.t('study.run.no');
    if (typeof v === 'number') return SS.profile.fmt.num(v);
    if (typeof v === 'string') return has('study.run.value.' + kp(v)) ? SS.t('study.run.value.' + kp(v)) : v;
    return SS.t('common.not_computed');
  }

  // ------------------------------------------------------------------------------------------- page parts
  function readings(parent) {
    var box = el('div', 'study-readings', undefined, parent);
    el('h2', 'study-readings-title', SS.t('study.readings.label'), box);
    var ul = el('ul', 'study-readings-list', undefined, box);
    el('li', 'study-reading study-reading-reference', SS.t('lane.reference'), ul);
    el('li', 'study-reading study-reading-ai', SS.t('lane.ai'), ul);
    var ni = el('li', 'study-reading study-reading-no-interview', undefined, ul);
    el('span', '', SS.t('lane.no_interview'), ni);
    el('span', 'study-gloss', SS.t('lane.no_interview_gloss'), ni);
    return box;
  }

  // The prompt behind the no-interview map, quoted from the file as run, with its hash.
  function prompt(box, doc) {
    var q = el('details', 'study-prompt', undefined, box);
    q.setAttribute('data-part', 'reading3_prompt');
    q.setAttribute('open', '');   // the gloss says "prompt quoted": the quote is shown, and can be folded away
    el('summary', '', SS.t('study.reading3.summary'), q);
    // every brace in the string is doubled, so empty slots print the file's own braces and fill nothing
    el('pre', 'mono study-prompt-text', SS.t('study.reading3.prompt', {}), q);
    el('p', 'study-prompt-note', SS.t('study.reading3.note'), q);
    var sha = SS.get(doc, 'run.reading3_prompt.sha256');
    if (sha !== undefined) {
      var line = el('p', 'study-prompt-hash', undefined, q);
      el('span', '', SS.t('study.reading3.hash') + ' ', line);
      el('code', 'mono study-hash', fmtValue(sha), line).setAttribute('data-key', 'run.reading3_prompt.sha256');
    }
    return q;
  }

  // ------------------------------------------------------------------------------------------- the four findings
  // A cell of the file by row, name and setting (x; null = a cell with no x): {path, value} or null.
  function cellAt(doc, rowId, name, x) {
    var rows = Array.isArray(doc.rows) ? doc.rows : [];
    for (var i = 0; i < rows.length; i++) {
      if (!rows[i] || rows[i].row !== rowId || !Array.isArray(rows[i].cells)) continue;
      for (var j = 0; j < rows[i].cells.length; j++) {
        var c = rows[i].cells[j];
        var cx = c && c.x !== undefined && c.x !== null ? c.x : null;
        if (c && c.name === name && cx === x && typeof c.value === 'number') {
          return { path: 'rows.' + i + '.cells.' + j, value: c.value, ci_lo: c.ci_lo, ci_hi: c.ci_hi };
        }
      }
    }
    return null;
  }
  // Shares as percentages with one decimal ("39.4%"), differences as points ("24", "6.5"); never ".394".
  function pct1(v) { return (Math.round(v * 1000) / 10).toFixed(1) + '%'; }
  function pct0(v) { return String(Math.round(v * 100)) + '%'; }
  function pts0(v) { return String(Math.round(v * 100)); }
  function pts1(v) { return (Math.round(v * 1000) / 10).toFixed(1); }

  // SS.study.findings(doc) -> [{id, text, keys}] for the findings whose cells are all in the file, in order.
  function findings(doc) {
    var out = [];
    var ai = cellAt(doc, 'lead', 'ai_recall', 'primary');
    var ni = cellAt(doc, 'lead', 'no_interview_recall', 'primary');
    var added = cellAt(doc, 'lead', 'listening_added', 'primary');
    if (ai && ni && added) {
      out.push({ id: 'added', keys: [added.path, ai.path, ni.path],
        text: SS.t('study.finding.added', { added: pts0(added.value), ai: pct1(ai.value), ni: pct1(ni.value) }) });
    }
    var omitted = cellAt(doc, 1, 'omitted', null);
    if (omitted) {
      out.push({ id: 'omitted', keys: [omitted.path], text: SS.t('study.finding.omitted', { omitted: pct1(omitted.value) }) });
    }
    var size = cellAt(doc, 2, 'ai_per_endorsed', null);
    var boxed = cellAt(doc, 'lead', 'rarefied_ai_minus_prior_only_recall', 'primary');
    if (size && boxed && typeof boxed.ci_lo === 'number' && typeof boxed.ci_hi === 'number') {
      out.push({ id: 'size', keys: [size.path, boxed.path],
        text: SS.t('study.finding.size', { size: pct0(size.value), boxed: pts1(boxed.value), lo: pts1(boxed.ci_lo), hi: pts1(boxed.ci_hi) }) });
    }
    var flat = cellAt(doc, 5, 'flattened', 'primary');
    var against = cellAt(doc, 5, 'counterfactual', 'primary');
    if (flat && against) {
      // Council 30 (MUST 2): the two counts are disjoint; the danger case (drawn as received although the AI's own
      // quote says the service never came) is said whenever it is above 0, and "no case" only when both are 0.
      var nf = SS.profile.fmt.num(flat.value, 'count');
      var na = SS.profile.fmt.num(against.value, 'count');
      var text;
      if (flat.value === 0 && against.value === 0) text = SS.t('study.finding.flattened.zero');
      else if (flat.value === 0) text = SS.t('study.finding.flattened.against_only', { n: na });
      else {
        text = SS.t('study.finding.flattened', { n: nf }) + ' ' +
          (against.value === 0 ? SS.t('study.finding.flattened.none') : SS.t('study.finding.flattened.some', { n: na }));
      }
      out.push({ id: 'flattened', keys: [flat.path, against.path], text: text });
    }
    return out;
  }

  function findingsBlock(parent, doc) {
    var list = findings(doc);
    var sec = el('section', 'study-findings', undefined, parent);
    sec.setAttribute('data-part', 'findings');
    el('h2', 'study-section-title', SS.t('study.findings.title'), sec);
    var ol = el('ol', 'study-findings-list', undefined, sec);
    list.forEach(function (f) {
      var li = el('li', 'study-finding', f.text, ol);
      li.setAttribute('data-finding', f.id);
      li.setAttribute('data-keys', f.keys.join(' '));
    });
    var todo = el('section', 'study-todo', undefined, parent);
    todo.setAttribute('data-part', 'todo');
    el('h2', 'study-section-title', SS.t('study.todo.title'), todo);
    el('p', '', SS.t('study.todo.body'), todo);
    return sec;
  }

  function meta(parent, doc) {
    var dl = el('dl', 'study-meta', undefined, parent);
    dl.setAttribute('aria-label', SS.t('study.meta.label'));
    function row(labelKey, keyPath, text, mono) {
      var d = el('div', 'study-meta-row', undefined, dl);
      el('dt', '', SS.t(labelKey), d);
      el('dd', mono ? 'mono' : '', text, d).setAttribute('data-key', keyPath);
    }
    var pop = SS.get(doc, 'population.label');
    row('study.meta.population', 'population.label',
      typeof pop === 'string' ? SS.t('study.meta.population_value', { label: pop }) : SS.t('common.not_computed'));
    var ref = SS.get(doc, 'reference.type');
    row('study.meta.reference', 'reference.type',
      has('study.meta.reference.' + kp(ref)) ? SS.t('study.meta.reference.' + kp(ref)) : fmtValue(ref));
    row('study.meta.schema', 'schema', String(doc.schema), true);
    row('study.meta.state', 'state', SS.t('study.meta.state.' + kp(doc.state)));
  }

  function panelNav(parent, ps) {
    var nav = el('nav', 'study-nav', undefined, parent);
    nav.setAttribute('aria-label', SS.t('study.panels.label'));
    el('h2', 'study-nav-title', SS.t('study.panels.label'), nav);
    var ol = el('ol', '', undefined, nav);
    ps.forEach(function (p) {
      var a = el('a', '', has(p.title_key) ? SS.t(p.title_key) : p.label_key, el('li', '', undefined, ol));
      a.setAttribute('href', '#' + p.id);
    });
  }

  function tag(sec, p) {
    sec.setAttribute('data-study-panel', String(p.index));
    sec.setAttribute('id', p.id);
  }

  function missingPanel(parent, p) {
    var s = el('section', 'pf-row study-missing', undefined, parent);
    s.setAttribute('data-row', String(p.row));
    tag(s, p);
    var h = el('header', 'pf-row-head', undefined, s);
    el('h3', 'pf-title', has(p.title_key) ? SS.t(p.title_key) : p.label_key, h);
    el('p', 'study-not-in-file', SS.t('study.panel.not_in_file'), s);
    return s;
  }

  function runPanel(parent, doc, p) {
    if (!p.present) return missingPanel(parent, p);
    var s = el('section', 'pf-row study-run', undefined, parent);
    s.setAttribute('data-row', 'run');
    tag(s, p);
    var h = el('header', 'pf-row-head', undefined, s);
    el('h3', 'pf-title', SS.t('study.run.title'), h);
    el('p', 'study-run-intro', SS.t('study.run.intro'), h);
    el('p', 'study-run-limit', SS.t('study.run.limit'), s).setAttribute('data-part', 'limit');
    var fields = runFields(doc);
    var groups = el('div', 'study-run-groups', undefined, s);
    RUN_GROUPS.forEach(function (g) {
      var mine = fields.filter(function (f) { return f.group === g[0]; });
      if (!mine.length) return;
      var box = el('div', 'study-run-group study-run-' + g[0], undefined, groups);
      el('h4', '', SS.t('study.run.group.' + g[0]), box);
      var dl = null;
      var lastModel = null;
      mine.forEach(function (f) {
        if (f.model !== undefined && f.model !== lastModel) {
          lastModel = f.model;
          var rk = 'study.run.model.role.' + kp(f.role);
          el('p', 'study-run-role', has(rk) ? SS.t(rk) : String(f.role === undefined ? '' : f.role), box);
          dl = null;
        }
        if (!dl) dl = el('dl', 'study-run-list', undefined, box);
        var d = el('div', 'study-run-row', undefined, dl);
        el('dt', '', f.n !== undefined ? SS.t(f.label_key, { n: f.n }) : SS.t(f.label_key), d);
        var mono = MONO_KEYS.test(f.key) && typeof f.value === 'string' && f.value !== PLACEHOLDER && f.reason === undefined;
        var dd = el('dd', mono ? 'mono study-hash' : '', fmtValue(f.value, f.reason), d);
        dd.setAttribute('data-key', f.key);
        if (f.value === PLACEHOLDER) dd.setAttribute('data-placeholder', 'true');
      });
    });
    return s;
  }

  function profilePanels(parent, doc, ps) {
    var host = root.document.createElement('div');   // detached: the rendered rows are moved out of it
    // council 30 (01 NOTE, council 23 D4): the four "yield per box" lines beside the lead (derived: true, arithmetic the
    // study's paper does not use, the equal-size "+10.4 points" among them) are left out; the box-for-box +6.5 stays.
    // The file keeps them, and the Results page and the report already leave them out.
    var opts = { gloss: false, derived: false };
    if (SS.strip && typeof SS.strip.drill === 'function') opts.drill = SS.strip.drill;
    SS.profile.render(host, doc, opts);
    var wrap = null;
    kids(host).forEach(function (c) {
      if (!wrap && /\bpf-profile\b/.test(c.getAttribute('class') || '')) wrap = c;
    });
    var byRow = {};
    kids(wrap).forEach(function (sec) { byRow[sec.getAttribute('data-row')] = sec; });
    var out = el('div', 'pf-profile study-panels', undefined, parent);
    out.setAttribute('data-schema', String(doc.schema));
    ps.forEach(function (p) {
      if (p.row === 'run') { runPanel(out, doc, p); return; }
      var sec = p.present ? byRow[String(p.row)] : null;
      if (!sec) { missingPanel(out, p); return; }
      tag(sec, p);
      out.appendChild(sec);
    });
    return out;
  }

  function status(parent, s) {
    var box = el('div', 'study-status', undefined, parent);
    box.setAttribute('data-status', s);
    box.setAttribute('data-part', 'status');
    box.setAttribute('role', 'status');
    el('h2', '', SS.t('study.state.' + s + '.title'), box);
    el('p', '', SS.t('study.state.' + s + '.body'), box);
    return box;
  }

  function render(rootNode, doc) {
    clear(rootNode);
    var s = state(doc);
    rootNode.setAttribute('data-state', s);
    var page = el('div', 'study-wrap', undefined, rootNode);
    var head = el('header', 'study-head', undefined, page);
    el('h1', '', SS.t('study.title'), head);
    if (s !== 'reported') {
      status(head, s);
      if (s === 'not_yet_run') {
        el('p', 'study-intro', SS.t('study.intro'), head);
        prompt(readings(head), doc);
      }
      return rootNode;
    }
    el('p', 'study-intro', SS.t('study.intro'), head);
    readings(head);
    findingsBlock(page, doc);
    // Everything else, closed by default and still in the page (every cell stays in the DOM).
    var det = el('details', 'study-details', undefined, page);
    det.setAttribute('data-part', 'technical');
    el('summary', 'study-details-summary', SS.t('study.details.summary'), det);
    var body = el('div', 'study-details-body', undefined, det);
    el('p', 'study-details-note', SS.t('study.details.note'), body);
    prompt(body, doc);
    meta(body, doc);
    var ps = panels(doc);
    panelNav(body, ps);
    profilePanels(body, doc, ps);
    return rootNode;
  }

  SS.study = { state: state, panels: panels, runFields: runFields, findings: findings, render: render, data: data };

  // ------------------------------------------------------------------------------------------- mount
  function mount() {
    var d = root.document;
    var node = d.getElementById('study-root');
    if (!node) return;
    render(node, data());
    var target = String(root.location && root.location.hash || '').replace(/^#/, '');
    if (/^panel-\d+$/.test(target)) {
      var t = d.getElementById(target);
      // a panel sits inside the closed "Technical details": open it before scrolling
      for (var p = t; p; p = p.parentNode) if (p.tagName === 'DETAILS' && p.className === 'study-details') p.setAttribute('open', '');
      if (t && typeof t.scrollIntoView === 'function') t.scrollIntoView();
    }
  }
  if (root.document && typeof root.document.getElementById === 'function') {
    if (root.document.readyState === 'loading') root.document.addEventListener('DOMContentLoaded', mount);
    else mount();
  }
})();
