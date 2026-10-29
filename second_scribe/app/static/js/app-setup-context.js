/* Setup step "context" (Task 33; spec 6.5 step 2): the study's domain sentence, its phases in order and its roles,
   each with a one-line description. Saved with POST /api/context (logged as context_set). The role of each
   transcript is picked with its speaker labels (step "labels"). Classic script; queues itself for SS.setup. */
(function () {
  'use strict';
  var root = typeof window !== 'undefined' ? window : globalThis;
  var SS = root.SS = root.SS || {};

  function el(doc, tag, cls, text) {
    var n = doc.createElement(tag);
    if (cls) n.setAttribute('class', cls);
    if (text !== undefined) n.textContent = text;
    return n;
  }

  function clear(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
  }

  function lines(text) {
    return String(text || '').split(/\r?\n/).map(function (s) { return s.trim(); }).filter(function (s) { return s; });
  }

  // "name: description" per line -> {name: description}; a line without a colon keeps an empty description.
  function parseRoles(text) {
    var out = {};
    lines(text).forEach(function (line) {
      var i = line.indexOf(':');
      var name = (i >= 0 ? line.slice(0, i) : line).trim();
      var desc = i >= 0 ? line.slice(i + 1).trim() : '';
      if (name) out[name] = desc;
    });
    return out;
  }

  function formatRoles(roles) {
    return Object.keys(roles || {}).map(function (k) { return k + ': ' + roles[k]; }).join('\n');
  }

  var uid = 0;
  function field(doc, labelKey, hintKey, multiline, value) {
    uid += 1;
    var id = 'ss-setup-context-' + uid;
    var wrap = el(doc, 'div', 'ss-setup-field');
    var label = el(doc, 'label', 'ss-setup-label', SS.t(labelKey));
    label.setAttribute('for', id);
    var input = el(doc, multiline ? 'textarea' : 'input', 'ss-setup-input' + (multiline ? ' ss-setup-textarea' : ''));
    input.setAttribute('id', id);
    if (!multiline) input.setAttribute('type', 'text');
    else input.setAttribute('rows', '5');
    input.setAttribute('autocomplete', 'off');
    input.value = value || '';
    wrap.appendChild(label);
    wrap.appendChild(input);
    if (hintKey) {
      var hint = el(doc, 'p', 'ss-setup-hint', SS.t(hintKey));
      hint.setAttribute('id', id + '-hint');
      input.setAttribute('aria-describedby', id + '-hint');
      wrap.appendChild(hint);
    }
    return { wrap: wrap, input: input };
  }

  function draw(container, ctx) {
    var doc = container.ownerDocument || root.document;
    clear(container);
    container.appendChild(el(doc, 'p', 'ss-setup-intro', SS.t('setup.context.intro')));
    var domain = field(doc, 'setup.context.domain_label', 'setup.context.domain_hint', false, ctx.domain);
    var phases = field(doc, 'setup.context.phases_label', 'setup.context.phases_hint', true,
      (ctx.phases || []).join('\n'));
    var roles = field(doc, 'setup.context.roles_label', 'setup.context.roles_hint', true, formatRoles(ctx.roles));
    var form = el(doc, 'form', 'ss-setup-form');
    form.setAttribute('novalidate', 'novalidate');
    [domain, phases, roles].forEach(function (f) { form.appendChild(f.wrap); });
    var button = el(doc, 'button', 'primary', SS.t('setup.context.save'));
    button.setAttribute('type', 'submit');
    form.appendChild(button);
    var status = el(doc, 'p', 'ss-setup-note');
    status.setAttribute('role', 'status');
    if (ctx.domain) status.textContent = SS.t('setup.context.saved');
    form.appendChild(status);
    var errorBox = el(doc, 'p', 'ss-setup-error');
    errorBox.setAttribute('role', 'alert');
    form.appendChild(errorBox);
    form.addEventListener('submit', function (ev) {
      if (ev && ev.preventDefault) ev.preventDefault();
      errorBox.textContent = '';
      status.textContent = '';
      button.disabled = true;
      SS.api.post('/api/context', {
        domain: domain.input.value, phases: lines(phases.input.value), roles: parseRoles(roles.input.value)
      }).then(function () {
        button.disabled = false;
        return SS.setup.refresh();
      }, function (err) {
        button.disabled = false;
        errorBox.textContent = (err && err.message) || SS.t('api.failed');
      });
    });
    container.appendChild(form);
  }

  function render(container) {
    var doc = container.ownerDocument || root.document;
    clear(container);
    container.appendChild(el(doc, 'p', 'ss-shell-note', SS.t('shell.loading')));
    return SS.api.get('/api/context').then(function (reply) {
      draw(container, (reply && reply.context) || {});
    }, function (err) {
      clear(container);
      container.appendChild(el(doc, 'p', 'ss-setup-error', (err && err.message) || SS.t('api.failed')));
    });
  }

  SS.setupContext = { parseRoles: parseRoles, lines: lines };
  (SS.setupQueue = SS.setupQueue || []).push(['context', {
    order: 20, titleKey: 'setup.context.title', needsProject: true, render: render
  }]);
})();
