/* Setup step "labels" (Task 33; spec 3.1 "Speaker labels (F5)", 6.5 steps 2-3; Review Focus 1): for each transcript,
   the speaker labels the kit's detector finds with their turn counts; the user says which label is the participant,
   which the interviewer, and picks the participant's role. POST /api/labels/confirm re-reads the transcript with
   those labels and refuses one where the speakers still cannot be told apart. Only labels and counts reach the
   page, never a turn's words.
   Item 27 (23 Oct): at the top, a "Standard for all transcripts" form: the participant label, the interviewer label
   and one rule per role of the study context ("file names containing OW: owner"). "Apply to all" saves it in the
   project (POST /api/labels/standard; it stays for files imported later) and the table below then shows every
   transcript filled from it, with its turn counts, each row still editable. A row the standard cannot fill (a label
   not found, no rule or two rules matching the file name, speakers not told apart) is flagged for a choice by hand.
   "Confirm all shown" sends one POST /api/labels/confirm per filled row, so each confirmation is still logged on its
   own; nothing is confirmed without that click. Classic script; queues itself for SS.setup. */
(function () {
  'use strict';
  var root = typeof window !== 'undefined' ? window : globalThis;
  var SS = root.SS = root.SS || {};
  var uid = 0;
  var PROBLEMS = ['participant_missing', 'interviewer_missing', 'not_told_apart', 'no_rule', 'many_rules', 'unreadable'];
  var lastRun = null;      // {said, errors: {pid: message}} of the last "Confirm all shown", shown once after redraw

  function el(doc, tag, cls, text) {
    var n = doc.createElement(tag);
    if (cls) n.setAttribute('class', cls);
    if (text !== undefined) n.textContent = text;
    return n;
  }

  function clear(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
  }

  // How the kit and the server compare speaker labels: punctuation and spaces dropped, lowercase. A confirmed label
  // is stored as it was sent, so "PT" can stand for the detected "[PT]" (2.2: the dropdowns showed blank for it).
  function norm(s) {
    return String(s === undefined || s === null ? '' : s).replace(/[^\p{L}\p{N}]+/gu, '').toLowerCase();
  }

  // The option value that stands for the confirmed label: the exact value when there is one, else the first whose
  // normalised form is the same; '' when the confirmed label is none of the options.
  function optionFor(options, chosen) {
    if (!chosen) return '';
    var exact = options.filter(function (o) { return o.value === chosen; })[0];
    if (exact) return exact.value;
    var key = norm(chosen);
    var loose = key ? options.filter(function (o) { return norm(o.value) === key; })[0] : null;
    return loose ? loose.value : '';
  }

  // A dropdown with its label; in the table the label is read by screen readers only (the column head shows it).
  function select(doc, labelKey, options, chosen, placeholderKey, loose, slots) {
    if (loose) chosen = optionFor(options, chosen);
    uid += 1;
    var id = 'ss-setup-labels-' + uid;
    var wrap = el(doc, 'div', 'ss-setup-field ss-setup-cellfield');
    var label = el(doc, 'label', 'ss-visually-hidden', SS.t(labelKey, slots));
    label.setAttribute('for', id);
    var sel = el(doc, 'select', 'ss-setup-select');
    sel.setAttribute('id', id);
    var none = el(doc, 'option', '', SS.t(placeholderKey || 'setup.labels.pick'));
    none.setAttribute('value', '');
    sel.appendChild(none);
    options.forEach(function (o) {
      var opt = el(doc, 'option', '', o.text);
      opt.setAttribute('value', o.value);
      if (o.value === chosen) opt.setAttribute('selected', 'selected');
      sel.appendChild(opt);
    });
    if (chosen) sel.value = chosen;
    wrap.appendChild(label);
    wrap.appendChild(sel);
    return { wrap: wrap, input: sel };
  }

  function formatName(fmt) {
    return SS.t('setup.labels.format.' + (['bracket', 'first_token', 'none'].indexOf(fmt) >= 0 ? fmt : 'none'));
  }

  // --- the standard form (item 27) ------------------------------------------------------------------------------

  function textField(doc, labelKey, value, hintKey) {
    uid += 1;
    var id = 'ss-setup-std-' + uid;
    var wrap = el(doc, 'div', 'ss-setup-field');
    var label = el(doc, 'label', 'ss-setup-label', SS.t(labelKey));
    label.setAttribute('for', id);
    var input = el(doc, 'input', 'ss-setup-input mono');
    input.setAttribute('id', id);
    input.setAttribute('type', 'text');
    input.setAttribute('maxlength', '40');
    input.setAttribute('autocomplete', 'off');
    input.setAttribute('spellcheck', 'false');
    if (hintKey) input.setAttribute('placeholder', SS.t(hintKey));
    input.value = value || '';
    wrap.appendChild(label);
    wrap.appendChild(input);
    return { wrap: wrap, input: input };
  }

  // The body POST /api/labels/standard takes, from the form's values; a rule with no text is left out.
  function standardBody(participant, interviewer, pieces) {
    var rules = [];
    Object.keys(pieces).forEach(function (role) {
      var c = String(pieces[role] || '').trim();
      if (c) rules.push({ contains: c, role: role });
    });
    return { participant: String(participant || '').trim(), interviewer: String(interviewer || '').trim(), rules: rules };
  }

  function standardForm(doc, std, roles) {
    var box = el(doc, 'section', 'ss-setup-card ss-setup-standard');
    box.setAttribute('aria-labelledby', 'ss-setup-std-title');
    var h = el(doc, 'h3', 'ss-setup-subtitle', SS.t('setup.labels.std.title'));
    h.setAttribute('id', 'ss-setup-std-title');
    box.appendChild(h);
    box.appendChild(el(doc, 'p', 'ss-setup-hint ss-setup-std-hint', SS.t('setup.labels.std.hint')));
    var row = el(doc, 'div', 'ss-setup-row');
    var part = textField(doc, 'setup.labels.std.participant', std && std.participant, 'setup.labels.std.participant_eg');
    var iv = textField(doc, 'setup.labels.std.interviewer', std && std.interviewer, 'setup.labels.std.interviewer_eg');
    row.appendChild(part.wrap);
    row.appendChild(iv.wrap);
    box.appendChild(row);
    var set = el(doc, 'fieldset', 'ss-setup-fieldset ss-setup-std-rules');
    set.appendChild(el(doc, 'legend', 'ss-setup-label', SS.t('setup.labels.std.rules')));
    var pieces = {};
    var names = Object.keys(roles);
    names.forEach(function (role) {
      var saved = ((std && std.rules) || []).filter(function (r) { return r.role === role; })[0];
      uid += 1;
      var id = 'ss-setup-std-rule-' + uid;
      var line = el(doc, 'div', 'ss-setup-std-rule');
      line.setAttribute('data-role', role);
      var lab = el(doc, 'label', 'ss-setup-std-rule-text', SS.t('setup.labels.std.rule_before'));
      lab.setAttribute('for', id);
      var input = el(doc, 'input', 'ss-setup-input mono ss-setup-std-piece');
      input.setAttribute('id', id);
      input.setAttribute('type', 'text');
      input.setAttribute('maxlength', '40');
      input.setAttribute('autocomplete', 'off');
      input.setAttribute('spellcheck', 'false');
      input.value = saved ? saved.contains : '';
      line.appendChild(lab);
      line.appendChild(input);
      line.appendChild(el(doc, 'span', 'ss-setup-std-rule-role', SS.t('setup.labels.std.rule_after', { role: role })));
      set.appendChild(line);
      pieces[role] = input;
    });
    if (!names.length) set.appendChild(el(doc, 'p', 'ss-setup-note', SS.t('setup.labels.needs_roles')));
    box.appendChild(set);
    var apply = el(doc, 'button', 'primary ss-setup-std-apply', SS.t('setup.labels.std.apply'));
    apply.setAttribute('type', 'button');
    var errorBox = el(doc, 'p', 'ss-setup-error');
    errorBox.setAttribute('role', 'alert');
    apply.addEventListener('click', function () {
      apply.disabled = true;
      errorBox.textContent = '';
      var values = {};
      Object.keys(pieces).forEach(function (r) { values[r] = pieces[r].value; });
      SS.api.post('/api/labels/standard', standardBody(part.input.value, iv.input.value, values)).then(function () {
        return SS.setup.refresh();
      }, function (err) {
        apply.disabled = false;
        errorBox.textContent = (err && err.message) || SS.t('api.failed');
      });
    });
    box.appendChild(apply);
    box.appendChild(errorBox);
    if (std) box.appendChild(el(doc, 'p', 'ss-setup-hint', SS.t('setup.labels.std.saved')));
    return box;
  }

  // --- the table ------------------------------------------------------------------------------------------------

  function flagText(fill, std) {
    var said = (fill.problems || []).filter(function (p) { return PROBLEMS.indexOf(p) >= 0; }).map(function (p) {
      return SS.t('setup.labels.flag.' + p, { participant: std ? std.participant : '', interviewer: std ? std.interviewer : '' });
    });
    return said.length ? SS.t('setup.labels.flag.lead') + ' ' + said.join(' ') : '';
  }

  // One transcript's row: what the detector found, the three choices, and what state the row is in.
  // `fill` is what the standard proposes for this transcript (or null); a confirmed row shows its confirmed choices.
  function row(doc, prev, roles, fill, std, err) {
    var tr = el(doc, 'tr', 'ss-setup-labels-row');
    tr.setAttribute('data-pid', prev.pid);
    var cands = prev.candidates || [];
    var conf = prev.confirmed;
    var first = el(doc, 'td', 'ss-setup-labels-who');
    first.appendChild(el(doc, 'div', 'mono ss-setup-labels-pid', prev.pid));
    if (prev.format === 'none' || !cands.length) {     // a format the detector found is plain from the labels shown
      first.appendChild(el(doc, 'div', 'ss-setup-hint ss-setup-labels-format', SS.t('setup.labels.detected', { format: formatName(prev.format) })));
    }
    if (cands.length) {
      var list = el(doc, 'ul', 'ss-setup-labels-list');
      cands.forEach(function (c) {
        var li = el(doc, 'li', '');
        li.appendChild(el(doc, 'code', 'mono', c.shown || c.label));
        li.appendChild(el(doc, 'span', 'ss-setup-muted', ' ' + SS.tn('setup.labels.turns', c.turns, { n: String(c.turns) })));
        list.appendChild(li);
      });
      first.appendChild(list);
    }
    tr.appendChild(first);
    var options = cands.map(function (c) { return { value: c.label, text: c.shown || c.label }; });
    var useFill = !conf && fill;
    var slots = { pid: prev.pid };
    var part = select(doc, 'setup.labels.participant_for', options,
      conf ? conf.participant : (useFill ? fill.participant : ''), null, true, slots);
    var iv = select(doc, 'setup.labels.interviewer_for', options,
      conf ? conf.interviewer : (useFill ? fill.interviewer : ''), null, true, slots);
    var role = select(doc, 'setup.labels.role_for', Object.keys(roles).map(function (r) {
      return { value: r, text: r };
    }), conf ? conf.role : (useFill ? fill.role : ''), 'setup.labels.pick_role', false, slots);   // 2.2 issue 5
    [part, iv, role].forEach(function (f) {
      var td = el(doc, 'td', 'ss-setup-labels-choice');
      td.appendChild(f.wrap);
      tr.appendChild(td);
    });
    var state = el(doc, 'td', 'ss-setup-labels-state');
    var written = function (key) {
      var hit = cands.filter(function (c) { return c.label === key; })[0] ||
        cands.filter(function (c) { return norm(c.label) === norm(key); })[0];
      return hit && hit.shown ? hit.shown : key;
    };
    if (conf) {
      tr.setAttribute('data-state', 'confirmed');
      var ok = el(doc, 'p', 'ss-setup-ok', SS.t('setup.labels.confirmed', {
        participant: written(conf.participant), interviewer: written(conf.interviewer), role: conf.role,
        np: SS.tn('setup.labels.n_turns', conf.turns.participant, { n: String(conf.turns.participant) }),
        ni: SS.tn('setup.labels.n_turns', conf.turns.interviewer, { n: String(conf.turns.interviewer) })
      }));
      ok.setAttribute('role', 'status');
      state.appendChild(ok);
    } else if (fill) {
      var flag = flagText(fill, std);
      if (flag) {
        tr.setAttribute('data-state', 'flagged');
        state.appendChild(el(doc, 'p', 'ss-setup-flag', flag));
      } else {
        tr.setAttribute('data-state', 'filled');
        var t = fill.turns || {};
        state.appendChild(el(doc, 'p', 'ss-setup-line ss-setup-filled', SS.t('setup.labels.filled', {
          participant: written(fill.participant), interviewer: written(fill.interviewer),
          np: SS.tn('setup.labels.n_turns', t.participant || 0, { n: String(t.participant || 0) }),
          ni: SS.tn('setup.labels.n_turns', t.interviewer || 0, { n: String(t.interviewer || 0) })
        })));
      }
    } else {
      tr.setAttribute('data-state', 'open');
    }
    (prev.warnings || []).forEach(function (w) {
      state.appendChild(el(doc, 'p', 'ss-setup-warning ss-setup-warning-inline', w.message));
    });
    var button = el(doc, 'button', conf ? '' : 'ss-setup-labels-one', SS.t(conf ? 'setup.labels.reconfirm' : 'setup.labels.confirm'));
    button.setAttribute('type', 'button');
    var errorBox = el(doc, 'p', 'ss-setup-error', err || '');
    errorBox.setAttribute('role', 'alert');
    function body() {
      return { pid: prev.pid, participant: part.input.value, interviewer: iv.input.value, role: role.input.value };
    }
    button.addEventListener('click', function () {
      errorBox.textContent = '';
      button.disabled = true;
      SS.api.post('/api/labels/confirm', body()).then(function () {
        return SS.setup.refresh();
      }, function (e) {
        button.disabled = false;
        errorBox.textContent = (e && e.message) || SS.t('api.failed');
      });
    });
    state.appendChild(button);
    state.appendChild(errorBox);
    if (prev.format === 'none') {
      var help = el(doc, 'p', 'ss-setup-hint', SS.t('setup.labels.none_help'));
      var a = el(doc, 'a', '', SS.t('setup.import.example_link'));
      a.setAttribute('href', prev.example_url || '/static/examples/labelled-transcript.txt');
      a.setAttribute('download', 'labelled-transcript.txt');
      help.appendChild(el(doc, 'span', '', ' '));
      help.appendChild(a);
      state.appendChild(help);
    }
    tr.appendChild(state);
    // what "Confirm all shown" reads: the row's choices as they are on screen now
    tr.ssRow = {
      pid: prev.pid, body: body, errorBox: errorBox,
      ready: function () {
        var b = body();
        if (!b.participant || !b.interviewer || !b.role) return false;
        if (!conf) return true;
        return norm(b.participant) !== norm(conf.participant) || norm(b.interviewer) !== norm(conf.interviewer) ||
          b.role !== conf.role;
      }
    };
    return tr;
  }

  function confirmAll(doc, rows, button, said) {
    var todo = rows.filter(function (r) { return r.ssRow.ready(); });
    button.disabled = true;
    said.textContent = '';
    var errors = {};
    var ok = 0;
    var chain = Promise.resolve();
    todo.forEach(function (tr) {
      chain = chain.then(function () {
        tr.ssRow.errorBox.textContent = '';
        return SS.api.post('/api/labels/confirm', tr.ssRow.body()).then(function () { ok += 1; }, function (e) {
          errors[tr.ssRow.pid] = (e && e.message) || SS.t('api.failed');
        });
      });
    });
    return chain.then(function () {
      var refused = Object.keys(errors).length;
      lastRun = {
        said: SS.tn('setup.labels.all_done', ok, { n: String(ok) }) +
          (refused ? ' ' + SS.tn('setup.labels.all_refused', refused, { n: String(refused) }) : ''),
        errors: errors
      };
      return SS.setup.refresh();
    });
  }

  function draw(container, table, previews, roles, stdDoc) {
    var doc = container.ownerDocument || root.document;
    var std = stdDoc && stdDoc.standard ? stdDoc.standard : null;
    var fills = {};
    ((stdDoc && stdDoc.rows) || []).forEach(function (f) { fills[f.pid] = f; });
    var run = lastRun;
    lastRun = null;
    clear(container);
    container.appendChild(el(doc, 'p', 'ss-setup-intro', SS.t('setup.labels.intro')));
    if (!Object.keys(roles).length) {
      container.appendChild(el(doc, 'p', 'ss-setup-note', SS.t('setup.labels.needs_roles')));
    }
    if (!table.confirmed) {
      container.appendChild(el(doc, 'p', 'ss-setup-note', SS.t('setup.labels.needs_pairing')));
    }
    if (!previews.length) {
      container.appendChild(el(doc, 'p', 'ss-setup-note', SS.t('setup.labels.empty')));
      return;
    }
    container.appendChild(standardForm(doc, std, roles));
    var done = previews.filter(function (p) { return p.preview && p.preview.confirmed; }).length;
    var count = el(doc, 'p', 'ss-setup-lead', SS.t('setup.labels.progress', {
      done: String(done), total: String(previews.length) }));
    count.setAttribute('role', 'status');
    container.appendChild(count);
    if (run && run.said) {
      var report = el(doc, 'p', 'ss-setup-ok ss-setup-labels-report', run.said);
      report.setAttribute('role', 'status');
      container.appendChild(report);
    }
    var t = el(doc, 'table', 'ss-setup-table ss-setup-labels-table');
    var head = el(doc, 'tr');
    ['transcript', 'participant', 'interviewer', 'role', 'state'].forEach(function (k) {
      var th = el(doc, 'th', '', SS.t('setup.labels.col_' + k));
      th.setAttribute('scope', 'col');
      head.appendChild(th);
    });
    var thead = el(doc, 'thead');
    thead.appendChild(head);
    t.appendChild(thead);
    var tbody = el(doc, 'tbody');
    var rows = [];
    previews.forEach(function (p) {
      if (p.error) {
        var bad = el(doc, 'tr', 'ss-setup-labels-row');
        bad.setAttribute('data-pid', p.pid);
        bad.setAttribute('data-state', 'unreadable');
        bad.appendChild(el(doc, 'td', 'mono', p.pid));
        var cell = el(doc, 'td', 'ss-setup-error', p.error);
        cell.setAttribute('colspan', '4');
        bad.appendChild(cell);
        tbody.appendChild(bad);
        return;
      }
      var tr = row(doc, p.preview, roles, fills[p.pid] || null, std, run && run.errors[p.pid]);
      rows.push(tr);
      tbody.appendChild(tr);
    });
    t.appendChild(tbody);
    var scroll = el(doc, 'div', 'ss-setup-table-wrap');
    scroll.appendChild(t);
    container.appendChild(scroll);
    var all = el(doc, 'button', 'primary ss-setup-labels-all', SS.t('setup.labels.confirm_all'));
    all.setAttribute('type', 'button');
    var said = el(doc, 'p', 'ss-setup-note ss-setup-labels-all-note');
    said.setAttribute('role', 'status');
    all.addEventListener('click', function () { return confirmAll(doc, rows, all, said); });
    var foot = el(doc, 'div', 'ss-setup-labels-foot');
    foot.appendChild(all);
    foot.appendChild(el(doc, 'p', 'ss-setup-hint', SS.t('setup.labels.confirm_all_hint')));
    foot.appendChild(said);
    container.appendChild(foot);
  }

  function render(container) {
    var doc = container.ownerDocument || root.document;
    clear(container);
    container.appendChild(el(doc, 'p', 'ss-shell-note', SS.t('shell.loading')));
    var standard = SS.api.get('/api/labels/standard').then(null, function () { return null; });
    return Promise.all([SS.api.get('/api/pairing'), SS.api.get('/api/context'), standard]).then(function (got) {
      var table = got[0] || {};
      var roles = (got[1] && got[1].context && got[1].context.roles) || {};
      var rows = (table.rows || []).filter(function (r) { return !!r.transcript; });
      return Promise.all(rows.map(function (r) {
        return SS.api.get('/api/labels', { pid: r.pid }).then(function (prev) {
          return { pid: r.pid, preview: prev };
        }, function (err) {
          return { pid: r.pid, error: (err && err.message) || SS.t('api.failed') };
        });
      })).then(function (previews) { draw(container, table, previews, roles, got[2]); });
    }, function (err) {
      clear(container);
      container.appendChild(el(doc, 'p', 'ss-setup-error', (err && err.message) || SS.t('api.failed')));
    });
  }

  SS.setupLabels = { draw: draw, standardBody: standardBody };
  (SS.setupQueue = SS.setupQueue || []).push(['labels', {
    order: 35, titleKey: 'setup.labels.title', needsProject: true, render: render
  }]);
})();
