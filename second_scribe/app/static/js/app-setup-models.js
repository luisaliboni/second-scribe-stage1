/* Setup step "models" (Task 34; spec 6.5 step 6, 8, 8.1): for the reconstructor and the judge, where the model
   runs, the address check (with the network opt-in or the cloud declaration), the API key (kept in the server's
   memory only), model discovery with cloud models refused, the family, the connection test, and the confirmation
   of the two families. Classic script; queues itself for SS.setup (app-setup.js).

   Typed values live in `drafts` (per role, in this page only) so a redraw keeps them; an API key never does: its
   input is cleared as soon as it is sent.

   Item 29 (23 Oct): the family rule with an example pair above the two roles; a server on this computer or the
   network may take an optional key ("This server asks for an API key"), sent as for an API. Item 31: "List the
   models" fills a compact picker: a search field that narrows the list as you type, a list of fixed height that
   scrolls inside, a count, and each model of the same family as the other role marked. Picking a model fills the
   model id and family in place, so the list keeps its filter and its scroll.

   Part 5c (item 29, "Quick setup"): under the family rule, the two roles side by side, each with where the model
   runs (this computer, a server on the network, a cloud API), the address, an optional key, the model picker and the
   family, and one "Test both" that saves each role when it changed and tests both connections, with each result
   beside its role. Same routes as the detailed forms, which stay below in a closed "Advanced options" (the key
   variable, Change, Remove), so opt-ins, declarations and the family rule are enforced and logged as before. Item 37:
   "List the models", "Use this model", "Allow this address" and "Record the declaration" say why they wait. */
(function () {
  'use strict';
  var root = typeof window !== 'undefined' ? window : globalThis;
  var SS = root.SS = root.SS || {};
  var ROLES = ['recon', 'judge'];
  var KINDS = ['local', 'openai_compat', 'anthropic'];
  var CLASSES = ['loopback', 'lan', 'tailscale', 'remote'];
  var CONNECTION = ['timeout', 'unreachable', 'malformed', 'auth', 'not_found', 'http', 'redirect'];
  var defaults = {};        // the address each kind starts with, from GET /api/models
  var drafts = {};
  var uid = 0;

  function draft(role) {
    if (!drafts[role]) {
      drafts[role] = { editing: false, kind: 'local', url: defaults.local || '', info: null, list: null, model: '',
        family: '', detected: null };
    }
    return drafts[role];
  }

  function el(doc, tag, cls, text) {
    var n = doc.createElement(tag);
    if (cls) n.setAttribute('class', cls);
    if (text !== undefined) n.textContent = text;
    return n;
  }

  function clear(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
  }

  function nextId(name) {
    uid += 1;
    return 'ss-models-' + name + '-' + uid;
  }

  function roleWord(role) {
    return SS.t('setup.models.role_word.' + role);
  }

  function errorBox(doc) {
    var box = el(doc, 'p', 'ss-setup-error');
    box.setAttribute('role', 'alert');
    return box;
  }

  function button(doc, key, primary) {
    var b = el(doc, 'button', primary ? 'primary' : 'ss-models-secondary', SS.t(key));
    b.setAttribute('type', 'button');
    return b;
  }

  // Runs fn() (a promise) with the button disabled; a refusal shows the server's message in `box`.
  function act(btn, box, fn) {
    box.textContent = '';
    btn.disabled = true;
    return fn().then(function (v) {
      btn.disabled = false;
      return v;
    }, function (err) {
      btn.disabled = false;
      box.textContent = (err && err.message) || SS.t('api.failed');
    });
  }

  // opts.message (item 30): the result, shown next to the control used.
  function refresh(opts) {
    return SS.setup && SS.setup.refresh ? SS.setup.refresh(opts) : Promise.resolve();
  }

  function said(key, slots) {
    return function () { return refresh({ message: SS.t(key, slots) }); };
  }

  function input(doc, opts) {
    var id = nextId(opts.name);
    var wrap = el(doc, 'div', 'ss-setup-field');
    var label = el(doc, 'label', 'ss-setup-label', SS.t(opts.labelKey));
    label.setAttribute('for', id);
    var box = el(doc, 'input', 'ss-setup-input' + (opts.mono ? ' mono' : ''));
    box.setAttribute('id', id);
    box.setAttribute('name', opts.name);
    box.setAttribute('type', opts.type || 'text');
    box.setAttribute('autocomplete', 'off');
    box.setAttribute('spellcheck', 'false');
    if (opts.value) box.value = opts.value;
    wrap.appendChild(label);
    wrap.appendChild(box);
    if (opts.hint) {
      var hint = el(doc, 'p', 'ss-setup-hint', opts.hint);
      hint.setAttribute('id', id + '-hint');
      box.setAttribute('aria-describedby', id + '-hint');
      wrap.appendChild(hint);
    }
    if (opts.onInput) box.addEventListener('input', function () { opts.onInput(box.value); });
    return { wrap: wrap, input: box };
  }

  function check(doc, labelText, onChange) {
    var row = el(doc, 'div', 'ss-setup-check');
    var id = nextId('check');
    var box = el(doc, 'input');
    box.setAttribute('type', 'checkbox');
    box.setAttribute('id', id);
    var label = el(doc, 'label', '', labelText);
    label.setAttribute('for', id);
    row.appendChild(box);
    row.appendChild(label);
    box.addEventListener('change', function () { onChange(box.checked); });
    return { row: row, input: box };
  }

  function line(doc, keyText, value, mono) {
    var p = el(doc, 'p', 'ss-setup-line');
    p.appendChild(el(doc, 'span', 'ss-setup-key', keyText));
    p.appendChild(el(doc, mono ? 'code' : 'span', mono ? 'mono' : '', value));
    return p;
  }

  function classText(cls) {
    return CLASSES.indexOf(cls) >= 0 ? SS.t('setup.models.class.' + cls) : '';
  }

  function connectionKey(code) {
    return 'models.connection.' + (CONNECTION.indexOf(code) >= 0 ? code : 'http');
  }

  function providerWord(kind) {
    return SS.t('setup.models.provider.' + (kind === 'anthropic' ? 'anthropic' : 'openai_compat'));
  }

  function matching(list, role, host) {
    return (Array.isArray(list) ? list : []).filter(function (x) { return x.role === role && x.host === host; });
  }

  // --- the address and what it needs ----------------------------------------------------------------------
  function classify(role, d) {
    d = d || draft(role);
    return SS.api.post('/api/models/classify', { role: role, kind: d.kind, url: d.url }).then(function (info) {
      d.info = info;
      d.list = null;
      return info;
    });
  }

  function optinBox(doc, role, info, d) {
    var box = el(doc, 'div', 'ss-setup-warning ss-models-optin');
    box.setAttribute('role', 'alert');
    box.appendChild(el(doc, 'h4', 'ss-models-subhead', SS.t('setup.models.optin_title')));
    box.appendChild(el(doc, 'p', '', SS.t('setup.models.optin_warning', { host: info.host })));
    var allow = button(doc, 'setup.models.optin_allow', true);
    allow.disabled = true;
    var allowWhy = SS.place.why(doc, nextId('why'), SS.t('setup.models.why.optin_tick'));     // item 37
    allow.setAttribute('aria-describedby', allowWhy.getAttribute('id'));
    var c = check(doc, SS.t('setup.models.optin_understood', { host: info.host, role: roleWord(role) }),
      function (on) { allow.disabled = !on; allowWhy.hidden = on; });
    box.appendChild(c.row);
    var err = errorBox(doc);
    allow.addEventListener('click', function () {
      if (!c.input.checked) return;
      act(allow, err, function () {
        return SS.api.post('/api/models/optin', { role: role, host: info.host, understood: true })
          .then(function () { return classify(role, d); }).then(said('setup.models.done.optin', { host: info.host }));
      });
    });
    box.appendChild(allow);
    box.appendChild(allowWhy);
    box.appendChild(err);
    return box;
  }

  function declarationBox(doc, role, info, d) {
    d = d || draft(role);
    var kind = d.kind;
    var box = el(doc, 'div', 'ss-setup-warning ss-models-declaration');
    box.setAttribute('role', 'alert');
    box.appendChild(el(doc, 'h4', 'ss-models-subhead', SS.t('setup.models.decl_title')));
    box.appendChild(el(doc, 'p', '', SS.t('setup.models.decl_text',
      { provider: providerWord(kind), host: info.host, role: roleWord(role) })));
    var submit = button(doc, 'setup.models.decl_submit', true);
    submit.disabled = true;
    var basis = input(doc, { name: 'basis', labelKey: 'setup.models.decl_basis_label',
      hint: SS.t('setup.models.decl_basis_hint') });
    var ticked = false;
    var submitWhy = SS.place.why(doc, nextId('why'), SS.t('setup.models.why.decl_tick'));     // item 37
    submit.setAttribute('aria-describedby', submitWhy.getAttribute('id'));
    function update() {
      submit.disabled = !(ticked && basis.input.value.trim());
      submitWhy.hidden = !submit.disabled;
    }
    basis.input.addEventListener('input', update);
    box.appendChild(basis.wrap);
    var c = check(doc, SS.t('setup.models.decl_tick'), function (on) { ticked = on; update(); });
    box.appendChild(c.row);
    var err = errorBox(doc);
    submit.addEventListener('click', function () {
      if (!ticked || !basis.input.value.trim()) return;
      act(submit, err, function () {
        return SS.api.post('/api/models/declaration', { role: role,
          provider: kind === 'anthropic' ? 'anthropic' : 'openai_compat', host: info.host,
          basis: basis.input.value.trim(), ticked: true })
          .then(function () { return classify(role, d); }).then(said('setup.models.done.declaration'));
      });
    });
    box.appendChild(submit);
    box.appendChild(submitWhy);
    box.appendChild(err);
    return box;
  }

  // Item 37: why "List the models" and "Use this model" wait (null when they can be used).
  function waitKey(info) {
    if (!info) return 'setup.models.why.check';
    if (info.allowed) return null;
    if (info.needs === 'optin') return 'setup.models.why.optin';
    if (info.needs === 'declaration') return info.https_ok ? 'setup.models.why.declaration' : 'setup.models.why.https';
    return 'setup.models.why.check';
  }

  function keyBox(doc, role, m) {
    var box = el(doc, 'div', 'ss-models-key');
    var key = input(doc, { name: 'api-key', labelKey: 'setup.models.key_label', type: 'password', mono: true,
      hint: SS.t('setup.models.key_hint') });
    box.appendChild(key.wrap);
    var err = errorBox(doc);
    var useKey = button(doc, 'setup.models.key_save', false);
    useKey.addEventListener('click', function () {
      var value = key.input.value;
      key.input.value = '';
      act(useKey, err, function () {
        return SS.api.post('/api/models/key', { role: role, key: value })
          .then(said('setup.models.done.key', { role: roleWord(role) }));
      });
    });
    box.appendChild(useKey);
    var envName = input(doc, { name: 'key-env', labelKey: 'setup.models.key_env_label', mono: true });
    box.appendChild(envName.wrap);
    var useEnv = button(doc, 'setup.models.key_env_save', false);
    useEnv.addEventListener('click', function () {
      act(useEnv, err, function () {
        return SS.api.post('/api/models/key', { role: role, env_name: envName.input.value.trim() })
          .then(function (doc2) {
            if (doc2 && doc2.key && doc2.key.env_present === false) {
              throw new Error(SS.t('setup.models.key_env_missing'));
            }
            return said('setup.models.done.key', { role: roleWord(role) })();
          });
      });
    });
    box.appendChild(useEnv);
    var set = !!(m.keys && m.keys[role]);
    box.appendChild(el(doc, 'p', 'ss-setup-note', SS.t(set ? 'setup.models.key_set' : 'setup.models.key_unset')));
    if (set) {
      var forget = button(doc, 'setup.models.key_clear', false);
      forget.addEventListener('click', function () {
        act(forget, err, function () {
          return SS.api.post('/api/models/key', { role: role, clear: true })
            .then(said('setup.models.done.key_clear', { role: roleWord(role) }));
        });
      });
      box.appendChild(forget);
    }
    box.appendChild(err);
    return box;
  }

  // --- the list of models ---------------------------------------------------------------------------------
  var TRAILING = /[\d._-]+$/;

  // As the server's families_differ: one family when either name, without trailing digits and marks, begins the other.
  function sameFamily(a, b) {
    var sa = typeof a === 'string' ? a.trim().toLowerCase().replace(TRAILING, '') : '';
    var sb = typeof b === 'string' ? b.trim().toLowerCase().replace(TRAILING, '') : '';
    if (!sa || !sb) return false;
    return sa.indexOf(sb) === 0 || sb.indexOf(sa) === 0;
  }

  function otherRole(role) {
    return role === 'recon' ? 'judge' : 'recon';
  }

  // The family of the other role when it is set, else null.
  function otherFamily(role, m) {
    var o = m && m.roles && m.roles[otherRole(role)];
    return o && o.family ? o.family : null;
  }

  function matches(item, q) {
    if (!q) return true;
    var hay = (item.name + ' ' + (item.family || '') + ' ' + (item.family_hint || '')).toLowerCase();
    return q.toLowerCase().split(/\s+/).every(function (w) { return !w || hay.indexOf(w) >= 0; });
  }

  // onPick(model) fills the model id and family fields in place.
  // d: the draft that holds the list (the role's form, or its Quick setup column).
  function modelList(doc, role, m, onPick, d) {
    d = d || draft(role);
    var box = el(doc, 'div', 'ss-models-picker');
    var fid = nextId('filter');
    var lid = nextId('list');
    var fl = el(doc, 'label', 'ss-setup-label', SS.t('setup.models.filter_label'));
    fl.setAttribute('for', fid);
    var filter = el(doc, 'input', 'ss-setup-input ss-models-filter');
    filter.setAttribute('id', fid);
    filter.setAttribute('type', 'search');
    filter.setAttribute('autocomplete', 'off');
    filter.setAttribute('spellcheck', 'false');
    filter.setAttribute('aria-controls', lid);
    filter.setAttribute('aria-describedby', fid + '-hint');
    filter.value = d.filter || '';
    var fh = el(doc, 'p', 'ss-setup-hint', SS.t('setup.models.filter_hint'));
    fh.setAttribute('id', fid + '-hint');
    box.appendChild(fl);
    box.appendChild(filter);
    box.appendChild(fh);
    var fam = otherFamily(role, m);
    if (fam) {
      box.appendChild(el(doc, 'p', 'ss-setup-note ss-models-other', SS.t('setup.models.other_family',
        { other: roleWord(otherRole(role)), family: fam })));
    }
    var count = el(doc, 'p', 'ss-setup-hint ss-models-count');
    count.setAttribute('role', 'status');
    box.appendChild(count);
    var list = el(doc, 'ul', 'ss-models-list');
    list.setAttribute('id', lid);
    list.setAttribute('aria-label', SS.t('setup.models.list_label'));
    var group = nextId('pick');
    var rows = [];
    d.list.forEach(function (x) {
      var li = el(doc, 'li', 'ss-models-item');
      var id = nextId('model');
      var radio = el(doc, 'input');
      radio.setAttribute('type', 'radio');
      radio.setAttribute('name', group);
      radio.setAttribute('id', id);
      radio.setAttribute('value', x.name);
      var label = el(doc, 'label', 'ss-models-item-label');
      label.setAttribute('for', id);
      label.appendChild(el(doc, 'span', 'mono ss-models-name', x.name));
      var xf = x.family || x.family_hint;
      if (xf) label.appendChild(el(doc, 'span', 'ss-models-fam', xf));
      if (x.remote) {
        radio.disabled = true;
        li.setAttribute('data-cloud', 'true');
        label.appendChild(el(doc, 'span', 'ss-models-why', SS.t('setup.models.cloud_refused')));
      } else {
        if (fam && sameFamily(xf, fam)) {
          li.setAttribute('data-same-family', 'true');
          label.appendChild(el(doc, 'span', 'ss-models-why ss-models-same', SS.t('setup.models.same_as_other',
            { other: roleWord(otherRole(role)) })));
        }
        if (d.model === x.name) radio.checked = true;
        radio.addEventListener('change', function () {
          d.model = x.name;
          d.family = x.family || x.family_hint || '';
          d.detected = x.family || x.family_hint || null;
          onPick(x);
        });
      }
      li.appendChild(radio);
      li.appendChild(label);
      list.appendChild(li);
      rows.push({ li: li, radio: radio, item: x });
    });
    function apply() {
      var q = String(filter.value || '').trim();
      d.filter = filter.value;
      var shown = 0;
      rows.forEach(function (r) {
        var on = matches(r.item, q);
        r.li.hidden = !on;
        if (on) shown += 1;
      });
      count.textContent = shown
        ? SS.t('setup.models.list_count', { shown: String(shown), total: String(rows.length) })
        : SS.t('setup.models.list_none', { q: q });
    }
    filter.addEventListener('input', apply);
    // Down arrow from the search field goes to the list: the checked model if shown, else the first one shown.
    filter.addEventListener('keydown', function (ev) {
      if (!ev || ev.key !== 'ArrowDown') return;
      var open = rows.filter(function (r) { return !r.li.hidden && !r.radio.disabled; });
      var to = open.filter(function (r) { return r.radio.checked; })[0] || open[0];
      if (!to) return;
      if (ev.preventDefault) ev.preventDefault();
      if (to.radio.focus) to.radio.focus();
    });
    apply();
    box.appendChild(list);
    return box;
  }

  function form(doc, role, entry, m) {
    var d = draft(role);
    var f = el(doc, 'div', 'ss-setup-form ss-models-form');
    var kinds = el(doc, 'fieldset', 'ss-models-kinds');
    kinds.appendChild(el(doc, 'legend', 'ss-setup-label', SS.t('setup.models.kind_label')));
    var group = nextId('kind');
    KINDS.forEach(function (k) {
      var row = el(doc, 'div', 'ss-setup-check');
      var id = nextId('kind-' + k);
      var radio = el(doc, 'input');
      radio.setAttribute('type', 'radio');
      radio.setAttribute('name', group);
      radio.setAttribute('id', id);
      radio.setAttribute('value', k);
      if (d.kind === k) radio.checked = true;
      radio.addEventListener('change', function () {
        if (d.kind === k) return;
        d.kind = k;
        d.url = defaults[k] || '';
        d.info = null;
        d.list = null;
        refresh();
      });
      var label = el(doc, 'label', '', SS.t('setup.models.kind.' + k));
      label.setAttribute('for', id);
      row.appendChild(radio);
      row.appendChild(label);
      kinds.appendChild(row);
    });
    f.appendChild(kinds);

    var url = input(doc, { name: 'url', labelKey: 'setup.models.url_label', mono: true, value: d.url,
      hint: SS.t(d.kind === 'local' ? 'setup.models.url_hint_local' : 'setup.models.url_hint_api'),
      onInput: function (v) { d.url = v; d.info = null; d.list = null; } });
    f.appendChild(url.wrap);
    var err = errorBox(doc);
    var checkBtn = button(doc, 'setup.models.check', false);
    checkBtn.addEventListener('click', function () {
      d.url = url.input.value;
      act(checkBtn, err, function () { return classify(role).then(refresh); });
    });
    f.appendChild(checkBtn);

    var info = d.info;
    if (info) {
      f.appendChild(el(doc, 'p', 'ss-setup-line ss-models-class', classText(info.address_class)));
      if (info.needs === 'optin') {
        if (info.allowed) {
          f.appendChild(el(doc, 'p', 'ss-setup-note',
            SS.t('setup.models.optin_done', { host: info.host, role: roleWord(role) })));
        } else {
          f.appendChild(optinBox(doc, role, info));
        }
      } else if (info.needs === 'declaration') {
        if (!info.https_ok) {
          f.appendChild(el(doc, 'p', 'ss-setup-error', SS.t('setup.models.https_needed')));
        } else if (info.allowed) {
          matching(m.declarations, role, info.host).slice(-1).forEach(function (dec) {
            f.appendChild(el(doc, 'p', 'ss-setup-note', SS.t('setup.models.decl_done',
              { role: roleWord(role), provider: providerWord(d.kind), host: dec.host, basis: dec.basis })));
          });
        } else {
          f.appendChild(declarationBox(doc, role, info));
        }
      }
    }
    if (d.kind !== 'local') {
      f.appendChild(keyBox(doc, role, m));
    } else {
      // item 29: a server on this computer or the network may ask for a key (vLLM or MTPLX started with --api-key)
      var opt = el(doc, 'details', 'ss-setup-details ss-models-key-optional');
      if (m.keys && m.keys[role]) opt.setAttribute('open', 'open');
      opt.appendChild(el(doc, 'summary', '', SS.t('setup.models.key_optional')));
      opt.appendChild(el(doc, 'p', 'ss-setup-hint', SS.t('setup.models.key_optional_hint')));
      opt.appendChild(keyBox(doc, role, m));
      f.appendChild(opt);
    }

    var listBtn = button(doc, 'setup.models.list', false);
    listBtn.disabled = !(info && info.allowed);
    listBtn.addEventListener('click', function () {
      act(listBtn, err, function () {
        return SS.api.post('/api/models/discover', { role: role, kind: d.kind, url: d.url }).then(function (doc2) {
          d.list = Array.isArray(doc2.models) ? doc2.models : [];
          return refresh();
        });
      });
    });
    f.appendChild(listBtn);
    var wait = waitKey(info);
    if (wait) {
      var listWhy = SS.place.why(doc, nextId('why'), SS.t(wait));
      listBtn.setAttribute('aria-describedby', listWhy.getAttribute('id'));
      f.appendChild(listWhy);
    }
    if (d.list) {
      f.appendChild(modelList(doc, role, m, function (x) {
        model.input.value = d.model;
        family.input.value = d.family;
        familyHint.textContent = d.detected ? SS.t('setup.models.family_detected', { family: d.detected })
          : SS.t('setup.models.family_none');
      }));
    } else {
      f.appendChild(el(doc, 'p', 'ss-setup-hint', SS.t('setup.models.list_empty')));
    }

    var model = input(doc, { name: 'model', labelKey: 'setup.models.model_label', mono: true, value: d.model,
      hint: d.kind === 'local' ? undefined : SS.t('setup.models.model_hint_api'),
      onInput: function (v) { d.model = v; } });
    f.appendChild(model.wrap);
    var family = input(doc, { name: 'family', labelKey: 'setup.models.family_label', value: d.family,
      hint: d.detected ? SS.t('setup.models.family_detected', { family: d.detected }) : SS.t('setup.models.family_none'),
      onInput: function (v) { d.family = v; } });
    f.appendChild(family.wrap);
    var familyHint = family.wrap.children[family.wrap.children.length - 1];
    var use = button(doc, 'setup.models.use', true);
    use.disabled = !(info && info.allowed);
    use.addEventListener('click', function () {
      d.model = model.input.value;
      d.family = family.input.value;
      act(use, err, function () {
        return SS.api.post('/api/models/set', { role: role, kind: d.kind, url: d.url, model: d.model.trim(),
          family: d.family.trim() }).then(function () {
          d.editing = false;
          d.list = null;
          return said('setup.models.done.model', { role: roleWord(role), model: d.model.trim() })();
        });
      });
    });
    var actions = el(doc, 'div', 'ss-models-actions');
    actions.appendChild(use);
    if (entry) {
      var cancel = button(doc, 'setup.models.cancel', false);
      cancel.addEventListener('click', function () { d.editing = false; refresh(); });
      actions.appendChild(cancel);
    }
    f.appendChild(actions);
    if (wait) {
      var useWhy = SS.place.why(doc, nextId('why'), SS.t(wait));
      use.setAttribute('aria-describedby', useWhy.getAttribute('id'));
      f.appendChild(useWhy);
    }
    f.appendChild(err);
    return f;
  }

  // --- a role that is set ---------------------------------------------------------------------------------
  function connectionBlock(doc, entry) {
    var c = entry.connection;
    if (!c) return el(doc, 'p', 'ss-setup-note', SS.t('setup.models.untested'));
    if (!c.ok) {
      var bad = el(doc, 'p', 'ss-setup-error', SS.t(connectionKey(c.error)));
      return bad;
    }
    if (c.thinking_seen) {
      var warn = el(doc, 'div', 'ss-setup-warning ss-models-thinking');
      warn.setAttribute('role', 'alert');
      warn.appendChild(el(doc, 'p', '', SS.t('setup.models.thinking_ignored')));
      return warn;
    }
    var ok = el(doc, 'div', 'ss-models-tested');
    ok.appendChild(el(doc, 'p', 'ss-setup-line', SS.t('setup.models.test_ok', { ms: String(c.ms) })));
    if (!c.reply_ok) ok.appendChild(el(doc, 'p', 'ss-setup-note', SS.t('setup.models.test_reply_odd')));
    return ok;
  }

  function current(doc, role, entry, m) {
    var box = el(doc, 'div', 'ss-setup-current ss-models-current');
    box.appendChild(el(doc, 'p', 'ss-setup-lead', SS.t('setup.models.current',
      { model: entry.model, family: entry.family })));
    box.appendChild(line(doc, SS.t('setup.models.where'), entry.host, true));
    box.appendChild(el(doc, 'p', 'ss-setup-note ss-models-class', classText(entry.address_class)));
    var digest = line(doc, SS.t('setup.models.digest'),
      entry.digest ? entry.digest.slice(0, 16) + '\u2026' : SS.t('setup.models.no_digest'), !!entry.digest);
    if (entry.digest) digest.setAttribute('title', entry.digest);
    box.appendChild(digest);
    if (entry.kind !== 'local' || entry.key_set) {
      box.appendChild(line(doc, SS.t('setup.models.key_state'),
        SS.t(entry.key_set ? 'setup.models.key_set' : 'setup.models.key_unset')));
    }
    matching(m.optins, role, entry.host).slice(0, 1).forEach(function (o) {
      box.appendChild(el(doc, 'p', 'ss-setup-note',
        SS.t('setup.models.optin_done', { host: o.host, role: roleWord(role) })));
    });
    if (entry.address_class === 'remote') {
      matching(m.declarations, role, entry.host).slice(-1).forEach(function (dec) {
        box.appendChild(el(doc, 'p', 'ss-setup-note', SS.t('setup.models.decl_done',
          { role: roleWord(role), provider: providerWord(entry.kind), host: dec.host, basis: dec.basis })));
      });
    }
    box.appendChild(connectionBlock(doc, entry));
    var err = errorBox(doc);
    var actions = el(doc, 'div', 'ss-models-actions');
    var test = button(doc, 'setup.models.test', true);
    test.addEventListener('click', function () {
      test.textContent = SS.t('setup.models.testing');
      act(test, err, function () {
        return SS.api.post('/api/models/test', { role: role }).then(refresh);
      }).then(function () { test.textContent = SS.t('setup.models.test'); });
    });
    actions.appendChild(test);
    var change = button(doc, 'setup.models.change', false);
    change.addEventListener('click', function () {
      var d = draft(role);
      d.editing = true;
      d.kind = entry.kind;
      d.url = entry.url;
      d.model = entry.model;
      d.family = entry.family;
      d.detected = entry.family_detected;
      d.info = null;
      d.list = null;
      refresh();
    });
    actions.appendChild(change);
    var remove = button(doc, 'setup.models.remove', false);
    remove.addEventListener('click', function () {
      act(remove, err, function () {
        return SS.api.post('/api/models/clear', { role: role }).then(said('setup.models.done.removed', { role: roleWord(role) }));
      });
    });
    actions.appendChild(remove);
    box.appendChild(actions);
    box.appendChild(err);
    return box;
  }

  function roleCard(doc, role, m) {
    var card = el(doc, 'section', 'ss-models-role');
    card.setAttribute('data-role', role);
    var h = el(doc, 'h3', 'ss-setup-subtitle', SS.t('setup.models.role.' + role));
    h.setAttribute('id', 'ss-models-role-' + role);
    card.setAttribute('aria-labelledby', 'ss-models-role-' + role);
    card.appendChild(h);
    card.appendChild(el(doc, 'p', 'ss-setup-hint ss-models-role-hint', SS.t('setup.models.role.' + role + '_hint')));
    var entry = m.roles && m.roles[role];
    if (entry && !draft(role).editing) {
      card.appendChild(current(doc, role, entry, m));
    } else {
      card.appendChild(form(doc, role, entry, m));
    }
    return card;
  }

  // --- Quick setup (item 29): the two roles side by side, one "Test both" --------------------------------------
  // Each column holds where the model runs, the address, an optional key, the model picker and the family, and the
  // result of its connection test. It uses the same routes as the detailed forms below, so every opt-in, declaration,
  // family rule and refusal works and is logged exactly as there; a key goes to the server's memory and the field is
  // emptied at once (it is never kept in the page).
  var WHERE = ['computer', 'network', 'cloud'];
  var quick = {};

  function hostOf(url) {
    var m = /^[a-z][a-z0-9+.-]*:\/\/([^/:?#]+)/i.exec(String(url || '').trim());
    return m ? m[1].toLowerCase() : '';
  }

  // Where -> the server's kind: this computer and a server on the network are "local" (an OpenAI-compatible or Ollama
  // server); a cloud API is OpenAI-compatible, or Anthropic's own when the address is Anthropic's.
  function quickKind(q) {
    if (q.where !== 'cloud') return 'local';
    return /(^|\.)anthropic\.com$/.test(hostOf(q.url)) ? 'anthropic' : 'openai_compat';
  }

  function whereDefault(where) {
    if (where === 'computer') return defaults.local || '';
    if (where === 'cloud') return defaults.openai_compat || '';
    return '';
  }

  function qdraft(role, m) {
    if (!quick[role]) {
      var e = m && m.roles && m.roles[role];
      var where = e ? (e.kind !== 'local' ? 'cloud' : (e.address_class === 'loopback' ? 'computer' : 'network')) : 'computer';
      quick[role] = { where: where, url: e ? e.url : whereDefault(where), model: e ? e.model : '',
        family: e ? e.family : '', detected: e ? e.family_detected || null : null, info: null, list: null,
        filter: '', error: '' };
    }
    quick[role].kind = quickKind(quick[role]);
    return quick[role];
  }

  // The saved entry when it is what this column shows (same model and kind), else null.
  function savedFor(q, entry) {
    if (!entry || !q.model) return null;
    return entry.model === q.model.trim() && entry.kind === quickKind(q) ? entry : null;
  }

  // Empties the key field at once and returns what it held (the key lives in this call only, never in the page).
  function takeKey(keyInput) {
    var value = keyInput && keyInput.value ? keyInput.value : '';
    if (keyInput) keyInput.value = '';
    return value;
  }

  function sendKey(role, value) {
    if (!value) return Promise.resolve();
    return SS.api.post('/api/models/key', { role: role, key: value });
  }

  function quickColumn(doc, role, m) {
    var q = qdraft(role, m);
    var entry = m.roles && m.roles[role];
    var col = el(doc, 'section', 'ss-quick-role');
    col.setAttribute('data-quick', role);
    var hid = nextId('quick-title');
    var h = el(doc, 'h4', 'ss-quick-title', SS.t('setup.models.role.' + role));
    h.setAttribute('id', hid);
    col.setAttribute('aria-labelledby', hid);
    col.appendChild(h);
    col.appendChild(el(doc, 'p', 'ss-setup-hint', SS.t('setup.models.role.' + role + '_hint')));

    var where = el(doc, 'fieldset', 'ss-quick-where');
    where.appendChild(el(doc, 'legend', 'ss-setup-label', SS.t('setup.models.kind_label')));
    var group = nextId('where');
    WHERE.forEach(function (w) {
      var row = el(doc, 'div', 'ss-setup-check');
      var id = nextId('where-' + w);
      var radio = el(doc, 'input');
      radio.setAttribute('type', 'radio');
      radio.setAttribute('name', group);
      radio.setAttribute('id', id);
      radio.setAttribute('value', w);
      if (q.where === w) radio.checked = true;
      radio.addEventListener('change', function () {
        if (q.where === w) return;
        q.where = w;
        q.url = whereDefault(w);
        q.info = null;
        q.list = null;
        q.error = '';
        refresh();
      });
      var label = el(doc, 'label', '', SS.t('setup.models.quick.where.' + w));
      label.setAttribute('for', id);
      row.appendChild(radio);
      row.appendChild(label);
      where.appendChild(row);
    });
    col.appendChild(where);

    var url = input(doc, { name: 'quick-url', labelKey: 'setup.models.url_label', mono: true, value: q.url,
      hint: SS.t('setup.models.quick.url_hint.' + q.where),
      onInput: function (v) { q.url = v; q.info = null; q.list = null; q.kind = quickKind(q); } });
    col.appendChild(url.wrap);
    var key = input(doc, { name: 'quick-key', type: 'password', mono: true,
      labelKey: q.where === 'cloud' ? 'setup.models.key_label' : 'setup.models.quick.key_optional',
      hint: SS.t(m.keys && m.keys[role] ? 'setup.models.quick.key_set' : 'setup.models.key_hint') });
    col.appendChild(key.wrap);

    var err = errorBox(doc);
    var listBtn = button(doc, 'setup.models.list', false);
    listBtn.addEventListener('click', function () {
      q.url = url.input.value;
      q.kind = quickKind(q);
      q.error = '';
      var typed = takeKey(key.input);
      act(listBtn, err, function () {
        return sendKey(role, typed).then(function () { return classify(role, q); }).then(function (info) {
          if (!info || !info.allowed) return refresh();          // the opt-in or the declaration shows in this column
          return SS.api.post('/api/models/discover', { role: role, kind: q.kind, url: q.url }).then(function (doc2) {
            q.list = Array.isArray(doc2.models) ? doc2.models : [];
            return refresh();
          });
        });
      });
    });
    var row = el(doc, 'div', 'ss-models-actions');
    row.appendChild(listBtn);
    col.appendChild(row);

    var info = q.info;
    if (info) {
      col.appendChild(el(doc, 'p', 'ss-setup-line ss-models-class', classText(info.address_class)));
      if (info.needs === 'optin' && !info.allowed) col.appendChild(optinBox(doc, role, info, q));
      else if (info.needs === 'declaration' && !info.https_ok) {
        col.appendChild(el(doc, 'p', 'ss-setup-error', SS.t('setup.models.https_needed')));
      } else if (info.needs === 'declaration' && !info.allowed) col.appendChild(declarationBox(doc, role, info, q));
    }

    var model = null;
    var family = null;
    var familyHint = null;
    if (q.list) {
      col.appendChild(modelList(doc, role, m, function () {
        model.input.value = q.model;
        family.input.value = q.family;
        familyHint.textContent = q.detected ? SS.t('setup.models.family_detected', { family: q.detected })
          : SS.t('setup.models.family_none');
      }, q));
    }
    model = input(doc, { name: 'quick-model', labelKey: 'setup.models.model_label', mono: true, value: q.model,
      onInput: function (v) { q.model = v; } });
    col.appendChild(model.wrap);
    family = input(doc, { name: 'quick-family', labelKey: 'setup.models.family_label', value: q.family,
      hint: q.detected ? SS.t('setup.models.family_detected', { family: q.detected }) : SS.t('setup.models.family_none'),
      onInput: function (v) { q.family = v; } });
    col.appendChild(family.wrap);
    familyHint = family.wrap.children[family.wrap.children.length - 1];
    col.appendChild(err);

    // the result of this role's connection test, beside the role
    var res = el(doc, 'div', 'ss-quick-result');
    res.setAttribute('role', 'status');
    res.setAttribute('data-part', 'quick-result');
    var saved = savedFor(q, entry);
    if (q.error) {
      var bad = el(doc, 'p', 'ss-setup-error', q.error);
      res.appendChild(bad);
    } else if (saved) {
      res.appendChild(connectionBlock(doc, saved));
    } else {
      res.appendChild(el(doc, 'p', 'ss-setup-note', SS.t('setup.models.untested')));
    }
    col.appendChild(res);
    col.ssKey = key.input;
    col.ssFields = { url: url.input, model: model.input, family: family.input };
    return col;
  }

  // One role of "Test both": save the role when the column differs from what is saved (the server refuses what
  // the detailed form would refuse), then test its connection. The column keeps the refusal, if any.
  function testRole(role, col, m, typed) {
    var q = quick[role];
    q.url = col.ssFields.url.value;
    q.model = col.ssFields.model.value;
    q.family = col.ssFields.family.value;
    q.kind = quickKind(q);
    q.error = '';
    var fail = function (e) { q.error = (e && e.message) || SS.t('api.failed'); };
    return sendKey(role, typed).then(function () {
      if (!q.model.trim()) { q.error = SS.t('setup.models.quick.no_model'); return null; }
      return classify(role, q).then(function (info) {
        if (!info || !info.allowed) {
          var wait = waitKey(info);
          q.error = SS.t(wait || 'setup.models.why.check');
          return null;
        }
        var entry = m.roles && m.roles[role];
        var same = entry && entry.model === q.model.trim() && entry.kind === q.kind && entry.host === info.host &&
          String(entry.family || '') === q.family.trim();
        var set = same ? Promise.resolve() : SS.api.post('/api/models/set', { role: role, kind: q.kind, url: q.url,
          model: q.model.trim(), family: q.family.trim() });
        return set.then(function () {
          return SS.api.post('/api/models/test', { role: role });
        }).then(function (r) {
          if (r && r.result && !r.result.ok && r.message) q.error = r.message;
          return r;
        });
      });
    }).then(null, fail);
  }

  function quickSetup(doc, m) {
    var box = el(doc, 'section', 'ss-models-quick');
    box.setAttribute('aria-labelledby', 'ss-models-quick-title');
    var h = el(doc, 'h3', 'ss-setup-subtitle', SS.t('setup.models.quick.title'));
    h.setAttribute('id', 'ss-models-quick-title');
    box.appendChild(h);
    box.appendChild(el(doc, 'p', 'ss-setup-hint', SS.t('setup.models.quick.intro')));
    var cols = el(doc, 'div', 'ss-quick-roles');
    var made = {};
    ROLES.forEach(function (role) {
      made[role] = quickColumn(doc, role, m);
      cols.appendChild(made[role]);
    });
    box.appendChild(cols);
    var rf = quick.recon && quick.recon.family;
    var jf = quick.judge && quick.judge.family;
    if (rf && jf && sameFamily(rf, jf)) {
      // the family rule, said before the test: the server still decides (a same-family pair runs AI maps only)
      box.appendChild(el(doc, 'p', 'ss-setup-error ss-quick-family', SS.t('setup.models.quick.same_family')));
    }
    var err = errorBox(doc);
    var row = el(doc, 'div', 'ss-quick-actions');
    var both = button(doc, 'setup.models.quick.test_both', true);
    both.addEventListener('click', function () {
      both.textContent = SS.t('setup.models.testing');
      var typed = { recon: takeKey(made.recon.ssKey), judge: takeKey(made.judge.ssKey) };   // both fields emptied now
      act(both, err, function () {
        return testRole('recon', made.recon, m, typed.recon).then(function () {
          return testRole('judge', made.judge, m, typed.judge);
        })
          .then(function () { return refresh({ message: SS.t('setup.models.quick.tested') }); });
      }).then(function () { both.textContent = SS.t('setup.models.quick.test_both'); });
    });
    row.appendChild(both);
    box.appendChild(row);
    box.appendChild(err);
    return box;
  }

  // --- the pair -------------------------------------------------------------------------------------------
  function pair(doc, m) {
    var box = el(doc, 'section', 'ss-models-pair');
    box.appendChild(el(doc, 'h3', 'ss-setup-subtitle', SS.t('setup.models.pair_title')));
    var recon = m.roles && m.roles.recon;
    var judge = m.roles && m.roles.judge;
    if (!recon) return box;
    box.appendChild(el(doc, 'p', 'ss-setup-line', judge
      ? SS.t('setup.models.pair_both', { recon: recon.family, judge: judge.family })
      : SS.t('setup.models.pair_recon_only', { recon: recon.family })));
    if (m.families_confirmed) {
      box.appendChild(el(doc, 'p', 'ss-setup-note', SS.t('setup.models.confirmed')));
      if (m.notice) {
        var n = el(doc, 'div', 'ss-models-notice');
        n.setAttribute('role', 'status');
        n.appendChild(el(doc, 'p', '', m.notice));
        box.appendChild(n);
      } else {
        box.appendChild(el(doc, 'p', 'ss-setup-line', SS.t('setup.models.two_family')));
      }
      return box;
    }
    box.appendChild(el(doc, 'p', 'ss-setup-hint', SS.t('setup.models.pair_hint')));
    var err = errorBox(doc);
    var confirm = button(doc, 'setup.models.confirm', true);
    confirm.addEventListener('click', function () {
      var body = { recon: recon.family };
      if (judge) body.judge = judge.family;
      act(confirm, err, function () { return SS.api.post('/api/models/confirm-families', body).then(refresh); });
    });
    box.appendChild(confirm);
    box.appendChild(err);
    return box;
  }

  function todo(doc, m) {
    var probs = Array.isArray(m.problems) ? m.problems : [];
    var box = el(doc, 'section', 'ss-models-todo');
    if (!probs.length) {
      box.appendChild(el(doc, 'p', 'ss-setup-lead', SS.t('setup.models.ready')));
      return box;
    }
    box.appendChild(el(doc, 'h3', 'ss-setup-subtitle', SS.t('setup.models.problems_title')));
    var list = el(doc, 'ul', 'ss-models-problems');
    probs.forEach(function (p) {
      var li = el(doc, 'li', '');
      li.setAttribute('data-code', p.code);
      if (p.role) li.appendChild(el(doc, 'span', 'ss-models-problem-role', SS.t('setup.models.role.' + p.role) + ': '));
      li.appendChild(el(doc, 'span', '', p.message || p.code));
      list.appendChild(li);
    });
    box.appendChild(list);
    return box;
  }

  function draw(container, m) {
    var doc = container.ownerDocument || root.document;
    clear(container);
    if (m.defaults && typeof m.defaults === 'object') defaults = m.defaults;
    container.appendChild(el(doc, 'p', 'ss-setup-intro', SS.t('setup.models.intro')));
    container.appendChild(el(doc, 'p', 'ss-setup-lead ss-models-rule', SS.t('setup.models.family_rule')));
    container.appendChild(quickSetup(doc, m));
    // item 29: the detailed forms stay, folded, for the advanced options (the key variable, Change, Remove...)
    var adv = el(doc, 'details', 'ss-setup-details ss-models-advanced');
    adv.appendChild(el(doc, 'summary', '', SS.t('setup.models.quick.advanced')));
    var roles = el(doc, 'div', 'ss-models-roles');
    ROLES.forEach(function (role) { roles.appendChild(roleCard(doc, role, m)); });
    adv.appendChild(roles);
    container.appendChild(adv);
    container.appendChild(pair(doc, m));
    container.appendChild(todo(doc, m));
  }

  function render(container) {
    var doc = container.ownerDocument || root.document;
    clear(container);
    container.appendChild(el(doc, 'p', 'ss-shell-note', SS.t('setup.models.loading')));
    return SS.api.get('/api/models').then(function (reply) {
      draw(container, (reply && reply.models) || {});
    }, function (err) {
      clear(container);
      container.appendChild(el(doc, 'p', 'ss-setup-error', (err && err.message) || SS.t('api.failed')));
    });
  }

  SS.setupModels = { render: render, draw: draw, drafts: drafts, quick: quick, quickKind: quickKind,
    sameFamily: sameFamily, matches: matches, reset: function () {
      Object.keys(drafts).forEach(function (k) { delete drafts[k]; });
      Object.keys(quick).forEach(function (k) { delete quick[k]; });
    } };

  (SS.setupQueue = SS.setupQueue || []).push(['models', {
    order: 60, titleKey: 'setup.models.title', needsProject: true, render: render
  }]);
})();
