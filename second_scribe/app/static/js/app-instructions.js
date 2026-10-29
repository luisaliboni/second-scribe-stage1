/* The Run screen (Task 40; spec 6.8, artboard AppInstructions; plan V1; Part 5b, scout F5 to F8, F14, F16, item 39).
   The choice of instructions comes first: rung 1 is the authors' original draft or the user's own (shown with the
   wrapper sent after it), rung 2 always runs, rung 3 and its placebo are one package (both texts in full; the harms
   note and a ready-made text for a result with no difference sit in a closed details). Then the preset and the Start
   section: one line on the reference types, the readiness list (GET /api/runs/ready: counts, each with a link to the
   step that fixes it), the one time estimate of the app (Task 35), and Start per reference group, with the reason
   beside a Start that cannot go ahead (a refused start also says why there, never only at the top of the page).
   Then the per-stage progress list (plain stage names, the time left, the model-server warning, items left out at
   the end) with pause/resume and the model-switch prompt (Tasks 38/39). Last, the three readings, named once, with
   the no-interview gloss once and its prompt quoted once, from the as-run file (the same en.json key as The study
   page, study.reading3.prompt).
   Classic script; needs core.js, api.js and app-shell.js.

   Routes: SS.instructions.ROUTES. GET /api/prompts, GET/POST /api/instructions and GET /api/references are Task 40's
   and Task 36's; POST /api/estimate is Task 35's; the run routes are Task 39's (routes/runs.py): the route table
   matches paths exactly, so the run id goes as ?run= (status) or in the body (pause, resume, swap), and
   GET /api/runs/status with no run answers the run in progress (or null), which this screen shows when it opens.
   GET /api/runs/last (routes/runs_last.py) names the latest run of each group and whether it finished (review R8).
   This screen never asks for a result, the no-interview map or an AI map.

   Part 5c (item 38): every redraw (a saved choice, the estimate, Start, each poll of the progress) goes through
   SS.place.redraw: the scroll position stays, the focus stays on the control just used, an opened folded part
   stays open, and a saved choice says so beside it ("Choice saved."; a refusal shows there too). A poll never moves
   the focus when it was outside this screen. */
(function () {
  'use strict';
  var root = typeof window !== 'undefined' ? window : globalThis;
  var SS = root.SS = root.SS || {};
  var ROUTES = {
    prompts: '/api/prompts', instructions: '/api/instructions', references: '/api/references',
    estimate: '/api/estimate',
    start: '/api/runs/start', status: '/api/runs/status', pause: '/api/runs/pause', resume: '/api/runs/resume',
    swap: '/api/runs/swap', last: '/api/runs/last', ready: '/api/runs/ready'
  };
  // A readiness item -> the screen (and Setup step) that fixes it.
  var FIX = {
    context: ['#setup/context', 'setup.context.title'], pairing: ['#setup/import', 'setup.import.title'],
    labels: ['#setup/labels', 'setup.labels.title'], maps: ['#setup/maps', 'setup.maps.title'],
    references: ['#reference', 'reference.title'], coding: ['#coding', 'coding.title'],
    models: ['#setup/models', 'setup.models.title'], model_check: ['#setup/check', 'setup.check.title']
  };
  var SERVER = ['timeout', 'no_connection', 'transport', 'no_reply'];
  var GROUPS = ['A', 'B', 'C'];
  var STAGES = ['Q', 'J', 'M'];
  var RUN_ORDER = ['rung1', 'rung2', 'placebo', 'rung3'];
  var STUDY_ORDER = ['rung1', 'rung2', 'rung3', 'placebo'];     // the order the names are read in (Part 5a)
  var POLL_MS = 2000;
  var screen = null;          // {main, prompts, ins, refs, draft, job, estimate, message, poll}

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

  // The rungs a run runs for a pick: rung 2 always, the placebo with rung 3 (the kit's registry), viewer order.
  function runRungs(picked) {
    var keys = { rung2: true };
    (Array.isArray(picked) ? picked : []).forEach(function (r) { keys[r] = true; });
    if (keys.rung3) keys.placebo = true;
    return RUN_ORDER.filter(function (k) { return keys[k] === true; });
  }

  function insOf(doc) {
    var i = isObj(doc) && isObj(doc.instructions) ? doc.instructions : {};
    return {
      rung1: i.rung1 === 'own' ? 'own' : 'basic',
      rung1_user: isObj(i.rung1_user) ? i.rung1_user : null,
      rungs: Array.isArray(i.rungs) ? i.rungs.filter(function (r) { return typeof r === 'string'; }) : ['rung2'],
      preset: i.preset === 'full' ? 'full' : 'standard'
    };
  }

  function rungPrompt(key) {
    var list = screen && isObj(screen.prompts) && Array.isArray(screen.prompts.rungs) ? screen.prompts.rungs : [];
    for (var i = 0; i < list.length; i++) if (list[i] && list[i].rung === key) return list[i];
    return null;
  }

  // ------------------------------------------------------------------------------------------- pieces
  function promptBlock(doc, p, summaryKey) {
    var d = el(doc, 'details', 'ss-ins-prompt');
    d.appendChild(el(doc, 'summary', '', SS.t(summaryKey || 'instructions.prompt.show')));
    if (!p) {
      d.appendChild(el(doc, 'p', 'ss-shell-note', SS.t('common.not_computed')));
      return d;
    }
    d.setAttribute('data-file', p.file || '');
    d.appendChild(el(doc, 'p', 'mono ss-ins-file', SS.t('instructions.prompt.file', { file: p.file, sha256: p.sha256 })));
    [['system', 'instructions.prompt.system'], ['definitions', 'instructions.prompt.definitions']].forEach(function (pair) {
      var part = p[pair[0]];
      if (!isObj(part)) return;
      d.appendChild(el(doc, 'h5', 'ss-ins-part', SS.t(pair[1])));
      d.appendChild(el(doc, 'pre', 'mono ss-ins-text', part.text));
    });
    if (isObj(p.system) || isObj(p.definitions)) d.appendChild(el(doc, 'h5', 'ss-ins-part', SS.t('instructions.prompt.main')));
    d.appendChild(el(doc, 'pre', 'mono ss-ins-text', p.text));
    return d;
  }

  function checkbox(doc, id, labelText, checked, disabled, onChange) {
    var lab = el(doc, 'label', 'ss-ins-check');
    lab.setAttribute('for', id);
    var input = el(doc, 'input');
    input.setAttribute('type', 'checkbox');
    input.setAttribute('id', id);
    input.checked = checked;
    if (checked) input.setAttribute('checked', 'checked');
    input.disabled = !!disabled;
    if (onChange) input.addEventListener('change', function () { onChange(input.checked === true); });
    lab.appendChild(input);
    lab.appendChild(el(doc, 'span', '', labelText));
    return lab;
  }

  function radio(doc, name, value, labelText, checked, onChange) {
    var id = name + '-' + value;
    var lab = el(doc, 'label', 'ss-ins-radio');
    lab.setAttribute('for', id);
    var input = el(doc, 'input');
    input.setAttribute('type', 'radio');
    input.setAttribute('name', name);
    input.setAttribute('id', id);
    input.setAttribute('value', value);
    input.checked = checked;
    if (checked) input.setAttribute('checked', 'checked');
    input.addEventListener('change', function () { if (input.checked) onChange(value); });
    lab.appendChild(input);
    lab.appendChild(el(doc, 'span', '', labelText));
    return lab;
  }

  function section(doc, part, titleKey) {
    var s = el(doc, 'section', 'ss-ins-section');
    s.setAttribute('data-part', part);
    if (titleKey) s.appendChild(el(doc, 'h2', 'ss-ins-heading', SS.t(titleKey)));
    return s;
  }

  // The three readings, named once; the no-interview gloss once; its prompt quoted once, from the as-run file.
  function readings(doc) {
    var s = section(doc, 'readings');
    var more = el(doc, 'details', 'ss-ins-prompt ss-ins-more');
    more.setAttribute('data-part', 'readings_more');
    more.appendChild(el(doc, 'summary', '', SS.t('instructions.readings.more')));
    more.appendChild(el(doc, 'h2', 'ss-ins-heading', SS.t('instructions.readings.heading')));
    var ul = el(doc, 'ul', 'ss-ins-readings');
    var li = el(doc, 'li', 'ss-ins-reading');
    li.setAttribute('data-lane', 'no_interview');
    li.appendChild(el(doc, 'strong', 'ss-ins-lane', SS.t('lane.no_interview')));
    li.appendChild(el(doc, 'span', 'ss-ins-gloss', ', ' + SS.t('lane.no_interview_gloss') + '. '));
    li.appendChild(el(doc, 'span', '', SS.t('instructions.readings.no_interview_body')));
    ul.appendChild(li);
    li = el(doc, 'li', 'ss-ins-reading');
    li.setAttribute('data-lane', 'ai');
    li.appendChild(el(doc, 'strong', 'ss-ins-lane', SS.t('lane.ai')));
    li.appendChild(el(doc, 'span', '', '. ' + SS.t('instructions.readings.ai_body')));
    ul.appendChild(li);
    li = el(doc, 'li', 'ss-ins-reading');
    li.setAttribute('data-lane', 'reference');
    li.appendChild(el(doc, 'strong', 'ss-ins-lane', SS.t('lane.reference')));
    li.appendChild(el(doc, 'span', '', '. ' + SS.t('instructions.readings.reference_body', { lane_b: SS.t('lane.reference_b') })));
    var noRef = el(doc, 'span', 'ss-ins-note', ' ' + SS.t('instructions.readings.no_reference_c'));
    noRef.setAttribute('data-part', 'reading_c_note');
    li.appendChild(noRef);
    ul.appendChild(li);
    more.appendChild(ul);

    var q = el(doc, 'details', 'ss-ins-prompt ss-ins-reading3');
    q.setAttribute('data-part', 'reading3_prompt');
    q.appendChild(el(doc, 'summary', '', SS.t('study.reading3.summary')));
    q.appendChild(el(doc, 'pre', 'mono ss-ins-text', SS.t('study.reading3.prompt', {})));
    q.appendChild(el(doc, 'p', 'ss-ins-note', SS.t('study.reading3.note')));
    var r3 = screen && isObj(screen.prompts) && isObj(screen.prompts.reading3) ? screen.prompts.reading3 : null;
    if (r3 && typeof r3.sha256 === 'string') {
      var line = el(doc, 'p', 'ss-ins-note');
      line.appendChild(el(doc, 'span', '', SS.t('study.reading3.hash') + ' '));
      line.appendChild(el(doc, 'code', 'mono ss-ins-hash', r3.sha256));
      q.appendChild(line);
    }
    more.appendChild(q);
    s.appendChild(more);
    return s;
  }

  function rung1(doc) {
    var ins = screen.ins;
    var s = section(doc, 'rung1', 'instructions.rung1.heading');
    var on = ins.rungs.indexOf('rung1') >= 0;
    s.appendChild(checkbox(doc, 'ss-ins-rung1', SS.t('instructions.rung1.include'), on, false, function (v) {
      save({ rungs: toggled('rung1', v) });
    }));
    var modes = el(doc, 'div', 'ss-ins-modes');
    modes.setAttribute('role', 'radiogroup');
    modes.setAttribute('aria-label', SS.t('instructions.rung1.heading'));
    var mode = screen.mode || ins.rung1;
    modes.appendChild(radio(doc, 'ss-ins-rung1-mode', 'basic', SS.t('instructions.rung1.basic'), mode === 'basic',
      function () { screen.mode = null; save({ rung1: 'basic' }); }));
    modes.appendChild(radio(doc, 'ss-ins-rung1-mode', 'own', SS.t('instructions.rung1.own'), mode === 'own',
      function () { screen.mode = 'own'; draw(); }));
    s.appendChild(modes);
    if (mode === 'basic') {
      s.appendChild(promptBlock(doc, rungPrompt('rung1')));
      return s;
    }
    var box = el(doc, 'div', 'ss-ins-own');
    box.setAttribute('data-part', 'own_prompt');
    var lab = el(doc, 'label', 'ss-ins-label', SS.t('instructions.rung1.own_label'));
    lab.setAttribute('for', 'ss-ins-own-text');
    box.appendChild(lab);
    var ta = el(doc, 'textarea', 'mono ss-ins-textarea');
    ta.setAttribute('id', 'ss-ins-own-text');
    ta.setAttribute('rows', '8');
    ta.setAttribute('maxlength', '20000');
    ta.value = screen.draft !== null ? screen.draft : (ins.rung1_user && typeof ins.rung1_user.text === 'string' ? ins.rung1_user.text : '');
    ta.addEventListener('input', function () { screen.draft = ta.value; });
    box.appendChild(ta);
    var b = el(doc, 'button', 'primary ss-ins-save', SS.t('instructions.rung1.save'));
    b.setAttribute('type', 'button');
    b.addEventListener('click', function () {
      screen.draft = ta.value;
      save({ rung1: 'own', rung1_text: ta.value }).then(function (ok) { if (ok) screen.draft = null; });
    });
    box.appendChild(b);
    if (ins.rung1 === 'own' && ins.rung1_user && typeof ins.rung1_user.text_sha256 === 'string') {
      box.appendChild(el(doc, 'p', 'mono ss-ins-note', SS.t('instructions.rung1.saved', { sha256: ins.rung1_user.text_sha256 })));
    }
    var w = el(doc, 'div', 'ss-ins-wrapper');
    w.setAttribute('data-part', 'wrapper');
    w.appendChild(el(doc, 'h3', 'ss-ins-subheading', SS.t('instructions.rung1.wrapper_heading')));
    var wrapper = screen.prompts && isObj(screen.prompts.wrapper) ? screen.prompts.wrapper.text : '';
    w.appendChild(el(doc, 'pre', 'mono ss-ins-text', wrapper));
    w.appendChild(el(doc, 'p', 'ss-ins-note', SS.t('instructions.rung1.wrapper_note')));
    box.appendChild(w);
    s.appendChild(box);
    return s;
  }

  function rung2(doc) {
    var s = section(doc, 'rung2', 'instructions.rung2.heading');
    s.appendChild(checkbox(doc, 'ss-ins-rung2', SS.t('instructions.rung2.include'), true, true));
    s.appendChild(el(doc, 'p', 'ss-ins-body', SS.t('instructions.rung2.always')));
    s.appendChild(promptBlock(doc, rungPrompt('rung2')));
    return s;
  }

  function rung3(doc) {
    var s = section(doc, 'rung3', 'instructions.rung3.heading');
    var on = screen.ins.rungs.indexOf('rung3') >= 0;
    s.setAttribute('data-on', on ? 'true' : 'false');
    s.appendChild(checkbox(doc, 'ss-ins-rung3', SS.t('instructions.rung3.include'), on, false, function (v) {
      save({ rungs: toggled('rung3', v) });
    }));
    s.appendChild(el(doc, 'p', 'ss-ins-body', SS.t('instructions.rung3.body')));
    var state = el(doc, 'p', 'ss-ins-placebo', SS.t(on ? 'instructions.rung3.placebo_on' : 'instructions.rung3.placebo_off'));
    state.setAttribute('data-part', 'placebo_state');
    state.setAttribute('data-placebo', on ? 'true' : 'false');
    s.appendChild(state);
    var pair = el(doc, 'div', 'ss-ins-pair');
    pair.appendChild(promptBlock(doc, rungPrompt('rung3'), 'instructions.rung3.remedy_text'));
    pair.appendChild(promptBlock(doc, rungPrompt('placebo'), 'instructions.rung3.placebo_text'));
    s.appendChild(pair);
    var notes = el(doc, 'details', 'ss-ins-prompt ss-ins-notes');
    notes.setAttribute('data-part', 'remedy_notes');
    notes.appendChild(el(doc, 'summary', '', SS.t('instructions.rung3.notes')));
    var harms = el(doc, 'div', 'ss-ins-harms');
    harms.setAttribute('data-part', 'harms');
    harms.appendChild(el(doc, 'h3', 'ss-ins-subheading', SS.t('instructions.rung3.harms_heading')));
    harms.appendChild(el(doc, 'p', '', SS.t('instructions.rung3.harms')));
    notes.appendChild(harms);
    var nul = el(doc, 'div', 'ss-ins-null');
    nul.setAttribute('data-part', 'null_text');
    nul.appendChild(el(doc, 'h3', 'ss-ins-subheading', SS.t('instructions.rung3.null_heading')));
    nul.appendChild(el(doc, 'p', 'ss-ins-note', SS.t('instructions.rung3.null_note')));
    nul.appendChild(el(doc, 'blockquote', 'ss-ins-null-text', SS.t('instructions.rung3.null_text')));
    notes.appendChild(nul);
    s.appendChild(notes);
    return s;
  }

  function preset(doc) {
    var s = section(doc, 'preset', 'instructions.preset.heading');
    var g = el(doc, 'div', 'ss-ins-modes');
    g.setAttribute('role', 'radiogroup');
    g.setAttribute('aria-label', SS.t('instructions.preset.heading'));
    ['standard', 'full'].forEach(function (p) {
      g.appendChild(radio(doc, 'ss-ins-preset', p, SS.t('instructions.preset.' + p), screen.ins.preset === p,
        function () { save({ preset: p }); }));
    });
    s.appendChild(g);
    return s;
  }

  function fmtDuration(seconds) {
    if (typeof seconds !== 'number' || !isFinite(seconds) || seconds < 0) return SS.t('common.not_computed');
    if (seconds < 60) return SS.t('instructions.estimate.under_minute');
    var m = Math.round(seconds / 60);
    if (m < 60) return SS.t('instructions.estimate.minutes', { m: String(m) });
    return SS.t('instructions.estimate.hours', { h: String(Math.floor(m / 60)), m: String(m % 60) });
  }

  // The app's one time estimate, beside Start (Setup no longer has its own): for the instructions chosen above.
  function estimate(doc) {
    var s = el(doc, 'div', 'ss-ins-estimate-box');
    s.setAttribute('data-part', 'estimate');
    s.appendChild(el(doc, 'h3', 'ss-ins-subheading', SS.t('instructions.estimate.heading')));
    s.appendChild(el(doc, 'p', 'ss-ins-note', SS.t('instructions.estimate.note')));
    var out = el(doc, 'p', 'ss-ins-estimate');
    out.setAttribute('role', 'status');
    var detail = null;
    var e = screen.estimate;
    if (e && e.error) out.textContent = e.error;
    else if (e && e.running) out.textContent = SS.t('instructions.estimate.running');
    else if (e && isObj(e.doc)) {
      if (SS.setupCheck && typeof SS.setupCheck.estimateResult === 'function') {
        detail = el(doc, 'div', 'ss-check-result ss-ins-estimate-detail');
        SS.setupCheck.estimateResult(detail, e.doc);
      } else {
        out.textContent = SS.t('instructions.estimate.result', { low: fmtDuration(e.doc.low_s), high: fmtDuration(e.doc.high_s) });
      }
    }
    var b = el(doc, 'button', 'ss-ins-estimate-run', SS.t('instructions.estimate.run'));
    b.setAttribute('type', 'button');
    b.disabled = !!(e && e.running);
    b.addEventListener('click', function () {
      screen.estimate = { running: true };
      draw();
      SS.api.post(ROUTES.estimate, { rungs: screen.ins.rungs.filter(function (r) { return r !== 'placebo'; }) }).then(function (doc2) {
        screen.estimate = { doc: isObj(doc2) ? doc2.estimate : null };
        draw();
      }, function (err) {
        screen.estimate = { error: msg(err) };
        draw();
      });
    });
    s.appendChild(b);
    s.appendChild(out);
    if (detail) s.appendChild(detail);
    return s;
  }

  function groupsOf(refs) {
    var list = isObj(refs) && Array.isArray(refs.references) ? refs.references : [];
    var gate = isObj(refs) && isObj(refs.gate) ? refs.gate : {};
    var waiting = Array.isArray(gate.waiting) ? gate.waiting : [];
    var counts = {};
    list.forEach(function (r) { if (r && GROUPS.indexOf(r.type) >= 0) counts[r.type] = (counts[r.type] || 0) + 1; });
    return GROUPS.filter(function (g) { return counts[g]; }).map(function (g) {
      return { group: g, n: counts[g], blocked: g === 'B' && waiting.length > 0 };
    });
  }

  // The latest run of a group as GET /api/runs/last gave it, unless that run is the one in progress (its own
  // Progress panel shows it).
  function lastOf(group) {
    var list = screen.last;
    if (!Array.isArray(list)) return null;
    for (var i = 0; i < list.length; i++) {
      var r = list[i];
      if (isObj(r) && r.group === group && typeof r.run_id === 'string') {
        return screen.job && screen.job.run_id === r.run_id ? null : r;
      }
    }
    return null;
  }

  function lastRun(doc, last) {
    var box = el(doc, 'div', 'ss-ins-last');
    box.setAttribute('data-group', last.group);
    box.appendChild(el(doc, 'span', 'ss-ins-last-state', SS.t(last.finished ? 'instructions.last.finished' : 'instructions.last.stopped', { run_id: last.run_id })));
    if (last.finished) {
      var a = el(doc, 'a', 'ss-ins-last-link', SS.t('instructions.last.show'));
      a.setAttribute('href', '#results/' + encodeURIComponent(last.run_id));
      box.appendChild(a);
    }
    box.appendChild(el(doc, 'span', 'ss-ins-note', SS.t('instructions.last.again')));
    return box;
  }

  // The latest run of each group, for the Start rows; a failure here is not shown (the rows just say nothing).
  function lastRuns(s) {
    return SS.api.get(ROUTES.last).then(function (doc) {
      if (screen !== s) return;
      s.last = isObj(doc) && Array.isArray(doc.last) ? doc.last : null;
      draw();
    }, function () {});
  }

  // ------------------------------------------------------------------------------------------- readiness
  // One readiness item as its line ("Speaker labels: 0 of 12 confirmed").
  function readyText(item) {
    var k = item.key;
    if (k === 'labels' || k === 'maps' || k === 'references' || k === 'coding') {
      return SS.t('instructions.ready.' + k, { done: String(item.done), total: String(item.total) });
    }
    if (k === 'model_check') {
      var st = ['passed', 'failed', 'missing', 'running'].indexOf(item.state) >= 0 ? item.state : 'missing';
      return SS.t('instructions.ready.model_check.' + st);
    }
    return SS.t('instructions.ready.' + k + (item.ok ? '.ok' : '.no'));
  }

  function fixLink(doc, key) {
    var fix = FIX[key];
    if (!fix) return null;
    var a = el(doc, 'a', 'ss-ins-fix', SS.t('instructions.start.go_step', { step: SS.t(fix[1]) }));
    a.setAttribute('href', fix[0]);
    return a;
  }

  function readyItems() {
    var r = screen && isObj(screen.ready) && Array.isArray(screen.ready.items) ? screen.ready.items : [];
    return r.filter(function (i) { return isObj(i) && typeof i.key === 'string' && FIX[i.key]; });
  }

  function readinessList(doc) {
    var box = el(doc, 'div', 'ss-ins-ready');
    box.setAttribute('data-part', 'ready');
    box.appendChild(el(doc, 'h3', 'ss-ins-subheading', SS.t('instructions.ready.heading')));
    var items = readyItems();
    if (!items.length) {
      box.appendChild(el(doc, 'p', 'ss-ins-note', SS.t(screen.ready === null ? 'instructions.ready.loading' : 'instructions.ready.intro')));
      return box;
    }
    box.appendChild(el(doc, 'p', 'ss-ins-note', SS.t('instructions.ready.intro')));
    var ul = el(doc, 'ul', 'ss-ins-ready-list');
    items.forEach(function (item) {
      var li = el(doc, 'li', 'ss-ins-ready-item');
      li.setAttribute('data-key', item.key);
      li.setAttribute('data-ok', item.ok ? 'true' : 'false');
      li.appendChild(el(doc, 'span', 'ss-ins-ready-mark', SS.t(item.ok ? 'instructions.ready.ok' : 'instructions.ready.needs')));
      li.appendChild(el(doc, 'span', 'ss-ins-ready-text', readyText(item)));
      if (!item.ok) {
        var a = fixLink(doc, item.key);
        if (a) li.appendChild(a);
      }
      ul.appendChild(li);
    });
    box.appendChild(ul);
    return box;
  }

  // Why a group's Start cannot go ahead: the readiness item that fixes it (its line and link), else the fixed
  // message of the refusal. Null when nothing is known against it.
  function reasonOf(doc, group) {
    var why = null;
    var g = blockedBy(group);
    if (g) why = { message: g.message, item: g.item };
    else if (screen.startError && screen.startError.group === group) why = { message: screen.startError.message, item: null };
    if (!why) return null;
    var box = el(doc, 'p', 'ss-ins-reason');
    box.setAttribute('role', 'alert');
    box.setAttribute('data-group', group);
    box.appendChild(el(doc, 'strong', '', SS.t('instructions.start.blocked') + ' '));
    var item = null;
    readyItems().forEach(function (i) { if (i.key === why.item && !i.ok) item = i; });
    var text = item ? readyText(item) + '. ' : (typeof why.message === 'string' && why.message ? why.message + ' ' : '');
    box.appendChild(el(doc, 'span', '', text));
    var a = fixLink(doc, why.item);
    if (a) box.appendChild(a);
    return box;
  }

  // The server's refusal of a group (GET /api/runs/ready), or null when it would start or nothing is known.
  function blockedBy(group) {
    var g = screen && isObj(screen.ready) && isObj(screen.ready.groups) ? screen.ready.groups[group] : null;
    return isObj(g) && g.ready === false ? g : null;
  }

  function refreshReady(s) {
    return SS.api.get(ROUTES.ready).then(function (doc) {
      if (screen !== s) return;
      s.ready = isObj(doc) ? doc : false;
      draw();
    }, function () {
      if (screen !== s) return;
      s.ready = false;                       // unknown: Start stays on and a refusal says why beside it
      draw();
    });
  }

  function start(doc) {
    var s = section(doc, 'start', 'instructions.start.heading');
    s.appendChild(el(doc, 'p', 'ss-ins-note', SS.t('instructions.start.rungs', {
      rungs: listOf(STUDY_ORDER.filter(function (r) { return runRungs(screen.ins.rungs).indexOf(r) >= 0; })
        .map(function (r) {
          return SS.t(r === 'rung1' && screen.ins.rung1 === 'own' ? 'instructions.rung1.own_name' : 'methods.rung.' + r);
        })) })));
    var groups = groupsOf(screen.refs);
    s.appendChild(readinessList(doc));
    s.appendChild(estimate(doc));
    if (!groups.length) {
      s.appendChild(el(doc, 'p', 'ss-shell-note', SS.t('instructions.start.none')));
      var a = el(doc, 'a', 'ss-setup-back', SS.t('instructions.start.go_reference'));
      a.setAttribute('href', '#reference');
      s.appendChild(a);
      return s;
    }
    s.appendChild(el(doc, 'p', 'ss-ins-note ss-ins-groups-note', SS.t('instructions.start.groups_note')));
    var running = !!screen.job && ['running', 'paused', 'needs_swap', 'swapping', 'starting'].indexOf(screen.job.state) >= 0;
    if (running) s.appendChild(el(doc, 'p', 'ss-ins-note ss-ins-busy', SS.t('instructions.start.busy')));
    groups.forEach(function (g) {
      var row = el(doc, 'div', 'ss-ins-start-row');
      row.setAttribute('data-group', g.group);
      row.appendChild(el(doc, 'span', 'ss-ins-group', SS.t('instructions.group.' + g.group.toLowerCase(), { n: String(g.n) })));
      var reason = running ? null : reasonOf(doc, g.group);
      var b = el(doc, 'button', 'primary ss-ins-start', SS.t('instructions.start.button'));
      b.setAttribute('type', 'button');
      b.setAttribute('data-group', g.group);
      b.disabled = running || g.blocked || !!blockedBy(g.group);
      b.addEventListener('click', function () { startRun(g.group); });
      row.appendChild(b);
      if (g.blocked && !reason) row.appendChild(el(doc, 'span', 'ss-ins-note', SS.t('instructions.start.b_waiting')));
      if (reason) row.appendChild(reason);
      var last = lastOf(g.group);
      if (last) row.appendChild(lastRun(doc, last));
      s.appendChild(row);
    });
    return s;
  }

  // "a, b and c" for the run line (the methods paragraph's in-sentence names, which hold no comma).
  function listOf(names) {
    if (names.length < 2) return names.join('');
    return names.slice(0, -1).join(', ') + ' and ' + names[names.length - 1];
  }

  // ------------------------------------------------------------------------------------------- progress
  // job: {run_id, state: starting|running|paused|needs_swap|done|failed, stage, done, total, stages?, message?,
  //       resumable?, server?, left_s?, left_out?} (Task 39's RunJob.status; a failed run with resumable offers
  //       Resume; server: the model server is not answering; left_s: about how long the model stages still need;
  //       left_out: items the server never answered, once finished)
  function renderProgress(container, job) {
    var doc = docOf(container);
    clear(container);
    if (!isObj(job)) return container;
    container.setAttribute('data-state', String(job.state || ''));
    if (typeof job.run_id === 'string') container.appendChild(el(doc, 'p', 'mono ss-ins-run', job.run_id));
    var stages = Array.isArray(job.stages) && job.stages.length ? job.stages : STAGES;
    var at = stages.indexOf(job.stage);
    var finished = job.state === 'done';
    var ol = el(doc, 'ol', 'ss-ins-stages');
    stages.forEach(function (st, i) {
      var li = el(doc, 'li', 'ss-ins-stage');
      li.setAttribute('data-stage', st);
      var status = finished || (at >= 0 && i < at) ? 'done' : (i === at ? 'current' : 'waiting');
      li.setAttribute('data-status', status);
      li.appendChild(el(doc, 'span', 'ss-ins-stage-name', SS.t('instructions.progress.stage.' + st.toLowerCase())));
      var count;
      if (status === 'current' && typeof job.done === 'number' && typeof job.total === 'number') {
        count = SS.t('instructions.progress.count', { done: String(job.done), total: String(job.total) });
      } else {
        count = SS.t('instructions.progress.' + (status === 'done' ? 'stage_done' : (status === 'current' ? 'stage_running' : 'stage_waiting')));
      }
      li.appendChild(el(doc, 'span', 'ss-ins-stage-count', count));
      ol.appendChild(li);
    });
    container.appendChild(ol);
    var stateKey = 'instructions.progress.state.' + (['starting', 'running', 'paused', 'needs_swap', 'done', 'failed'].indexOf(job.state) >= 0 ? job.state : 'running');
    var outN = job.state === 'done' && typeof job.left_out === 'number' && job.left_out > 0 ? job.left_out : 0;
    var text = outN ? SS.tn('instructions.progress.state.done_left_out', outN, { n: String(outN) })
      : SS.t(stateKey, { message: typeof job.message === 'string' ? job.message : '' });
    if (job.state === 'running' && typeof job.left_s === 'number' && job.left_s >= 0) {
      text += ' ' + (job.left_s < 60 ? SS.t('instructions.progress.left_minute')
        : SS.t('instructions.progress.left', { time: fmtDuration(job.left_s) }));
    }
    var line = el(doc, 'p', 'ss-ins-state', text);
    line.setAttribute('role', 'status');
    if (outN) line.setAttribute('data-left-out', String(outN));
    container.appendChild(line);
    var server = serverLine(doc, job.state === 'running' ? job.server : null);
    if (server) container.appendChild(server);
    var actions = el(doc, 'div', 'ss-ins-actions');
    function act(key, route, body) {
      var b = el(doc, 'button', 'ss-ins-act', SS.t(key));
      b.setAttribute('type', 'button');
      b.setAttribute('data-action', key.split('.').pop());
      b.addEventListener('click', function () {
        b.disabled = true;
        SS.api.post(route, body).then(function (doc2) {
          if (screen && isObj(doc2) && isObj(doc2.job)) screen.job = doc2.job;
          // Resume (or a switch) after the poll stopped on a failed run: keep watching the run again.
          if (screen && screen.job && ['running', 'needs_swap', 'swapping', 'starting'].indexOf(screen.job.state) >= 0) {
            startPoll(screen);
          }
          poll();
        }, function (err) {
          if (screen) screen.message = msg(err);
          draw();
        });
      });
      actions.appendChild(b);
    }
    if (job.state === 'running') act('instructions.progress.pause', ROUTES.pause, { run: job.run_id });
    if (job.state === 'paused' || (job.state === 'failed' && job.resumable === true)) {
      act('instructions.progress.resume', ROUTES.resume, { run: job.run_id });
    }
    if (job.state === 'needs_swap') {
      container.appendChild(el(doc, 'p', 'ss-ins-note', SS.t('instructions.progress.swap_note')));
      act('instructions.progress.swap_auto', ROUTES.swap, { run: job.run_id, auto: true });
      act('instructions.progress.swap_done', ROUTES.swap, { run: job.run_id, auto: false });
    }
    if (job.state === 'done') {
      container.appendChild(el(doc, 'p', 'ss-ins-note ss-ins-next', SS.t('instructions.progress.next')));
      var a = el(doc, 'a', 'ss-ins-go-results', SS.t('instructions.progress.go_results'));
      a.setAttribute('href', '#results/' + encodeURIComponent(job.run_id || ''));
      actions.appendChild(a);
    }
    container.appendChild(actions);
    return container;
  }

  // "The model server is not answering" from a job's `server` ({code, seconds}; jobs.LogTail.server), or null.
  function serverLine(doc, server) {
    if (!isObj(server) || typeof server.code !== 'string') return null;
    var m = /^http_(\d{3})$/.exec(server.code);
    var key = m ? 'jobs.server.http' : 'jobs.server.' + (SERVER.indexOf(server.code) >= 0 ? server.code : 'transport');
    var p = el(doc, 'p', 'ss-ins-server', SS.t(key, { status: m ? m[1] : '', time: fmtDuration(server.seconds) }));
    p.setAttribute('role', 'alert');
    p.setAttribute('data-code', server.code);
    return p;
  }

  function stopPoll() {
    if (screen && screen.poll !== null && typeof root.clearInterval === 'function') root.clearInterval(screen.poll);
    if (screen) screen.poll = null;
  }

  function poll() {
    if (!screen || !screen.job || typeof screen.job.run_id !== 'string') return;
    var s = screen;
    SS.api.get(ROUTES.status, { run: s.job.run_id }).then(function (doc) {
      if (screen !== s) return;
      if (isObj(doc) && isObj(doc.job)) s.job = doc.job;
      else if (isObj(doc) && typeof doc.state === 'string') s.job = doc;
      if (s.job && (s.job.state === 'done' || s.job.state === 'failed')) { stopPoll(); lastRuns(s); refreshReady(s); }
      draw();
    }, function (err) {
      if (screen !== s) return;
      stopPoll();
      s.message = msg(err);
      draw();
    });
  }

  function startPoll(s) {
    if (s.job && typeof root.setInterval === 'function') {
      stopPoll();
      s.poll = root.setInterval(poll, POLL_MS);
    }
  }

  // The run in progress when the screen opens (after a reload or an app restart); a failure here is not shown.
  function current(s) {
    return SS.api.get(ROUTES.status).then(function (doc) {
      if (screen !== s || s.job || !isObj(doc) || !isObj(doc.job)) return;
      s.job = doc.job;
      draw();
      if (['running', 'needs_swap', 'swapping', 'starting'].indexOf(s.job.state) >= 0) startPoll(s);
    }, function () {});
  }

  // A refused start says why beside its own Start (item 39), and the readiness list is read again.
  function startRun(group) {
    var s = screen;
    s.message = '';
    s.startError = null;
    var body = { group: group, rungs: s.ins.rungs.slice(), preset: s.ins.preset };
    return SS.api.post(ROUTES.start, body).then(function (doc) {
      if (screen !== s) return;
      var job = isObj(doc) && isObj(doc.job) ? doc.job : doc;
      s.job = isObj(job) ? job : null;
      if (s.job && !s.job.state) s.job.state = 'starting';
      draw();
      startPoll(s);
      lastRuns(s);
    }, function (err) {
      if (screen !== s) return;
      s.startError = { group: group, message: msg(err) };
      draw();
      return refreshReady(s);
    });
  }

  // ------------------------------------------------------------------------------------------- state
  function toggled(rung, on) {
    var set = screen.ins.rungs.filter(function (r) { return r !== rung; });
    if (on) set.push(rung);
    return set;
  }

  // POST the choices with `patch` over the current ones; resolves true when saved.
  function save(patch) {
    var s = screen;
    var body = { rung1: s.ins.rung1, rungs: s.ins.rungs.slice(), preset: s.ins.preset };
    Object.keys(patch).forEach(function (k) { body[k] = patch[k]; });
    s.message = '';
    return SS.api.post(ROUTES.instructions, body).then(function (doc) {
      if (screen !== s) return false;
      s.ins = insOf(doc);
      s.mode = null;
      draw({ message: SS.t('instructions.saved') });
      return true;
    }, function (err) {
      if (screen !== s) return false;
      draw({ error: msg(err) });      // beside the choice just made (item 38), not at the top of the page
      return false;
    });
  }

  // opts.message / opts.error: a line beside the control just used (SS.place).
  function draw(opts) {
    if (!screen) return;
    var main = screen.main;
    SS.place.redraw(main, drawNow, opts);
  }

  function drawNow() {
    var main = screen.main;
    var doc = docOf(main);
    clear(main);
    var page = el(doc, 'div', 'ss-ins');
    page.appendChild(el(doc, 'h1', 'ss-ins-title', SS.t('instructions.title')));
    page.appendChild(el(doc, 'p', 'ss-ins-intro', SS.t('instructions.intro')));
    var err = el(doc, 'p', 'ss-setup-error', screen.message || '');
    err.setAttribute('role', 'alert');
    page.appendChild(err);
    page.appendChild(rung1(doc));
    page.appendChild(rung2(doc));
    page.appendChild(rung3(doc));
    page.appendChild(preset(doc));
    page.appendChild(start(doc));
    var prog = section(doc, 'progress', 'instructions.progress.heading');
    var box = el(doc, 'div', 'ss-ins-progress');
    if (screen.job) renderProgress(box, screen.job);
    else box.appendChild(el(doc, 'p', 'ss-shell-note', SS.t('instructions.progress.none')));
    prog.appendChild(box);
    page.appendChild(prog);
    page.appendChild(readings(doc));
    main.appendChild(page);
  }

  function render(main) {
    stopPoll();
    screen = { main: main, prompts: null, ins: insOf(null), refs: null, draft: null, mode: null, job: null,
      estimate: null, message: '', poll: null, last: null, ready: null, startError: null };
    var s = screen;
    var doc = docOf(main);
    SS.place.forget(main);
    SS.place.watch(main);
    clear(main);
    main.appendChild(el(doc, 'p', 'ss-shell-note', SS.t('shell.loading')));
    return Promise.all([SS.api.get(ROUTES.prompts), SS.api.get(ROUTES.instructions), SS.api.get(ROUTES.references)])
      .then(function (got) {
        if (screen !== s) return;
        s.prompts = got[0];
        s.ins = insOf(got[1]);
        s.refs = got[2];
        draw();
        return Promise.all([current(s), lastRuns(s), refreshReady(s)]);
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

  SS.instructions = {
    ROUTES: ROUTES,
    render: render,
    runRungs: runRungs,
    serverLine: serverLine,
    // The rungs the next run would run, from the choices shown (rung 2 always, the placebo with rung 3).
    selected: function selected() { return screen ? runRungs(screen.ins.rungs) : runRungs([]); },
    renderProgress: renderProgress,
    fmtDuration: fmtDuration
  };

  if (SS.shell && typeof SS.shell.register === 'function') {
    SS.shell.register('instructions', { order: 40, titleKey: 'instructions.title', render: render });
  }
})();
