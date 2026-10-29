/* Setup step "project" (Task 32; spec 4 and 6.5 step 1): create or open the project folder, the sync-folder
   warning and its acknowledgement, files changed since they were brought in, and the app log's state.
   Item 22 (23 Oct): a "Choose folder…" button beside both folder fields. A browser never gives a page a folder's
   full path, so the button asks the app (POST /api/project/choose-folder) to open the system's own folder window;
   the chosen path only fills the field, and the person still presses Create or Open. GET
   /api/project/folder-dialog says whether this computer has such a window: until it answers yes the buttons stay
   hidden, and a no shows one sentence asking for the full path.
   Classic script; queues itself for SS.setup (app-setup.js). */
(function () {
  'use strict';
  var root = typeof window !== 'undefined' ? window : globalThis;
  var SS = root.SS = root.SS || {};
  var KINDS = ['icloud', 'cloudstorage', 'dropbox', 'onedrive', 'google_drive', 'box', 'pcloud', 'nextcloud', 'mega',
    'sync_com', 'sharepoint'];

  function el(doc, tag, cls, text) {
    var n = doc.createElement(tag);
    if (cls) n.setAttribute('class', cls);
    if (text !== undefined) n.textContent = text;
    return n;
  }

  function clear(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
  }

  function serviceName(kind) {
    return KINDS.indexOf(kind) >= 0 ? SS.t('project.sync.kind.' + kind) : SS.t('project.sync.kind.unknown');
  }

  var uid = 0;
  function field(doc, labelKey, name, hintKey) {
    uid += 1;
    var id = 'ss-setup-project-' + name + '-' + uid;
    var wrap = el(doc, 'div', 'ss-setup-field');
    var label = el(doc, 'label', 'ss-setup-label', SS.t(labelKey));
    label.setAttribute('for', id);
    var input = el(doc, 'input', 'ss-setup-input' + (name === 'name' ? '' : ' mono'));
    input.setAttribute('id', id);
    input.setAttribute('name', name);
    input.setAttribute('type', 'text');
    input.setAttribute('autocomplete', 'off');
    input.setAttribute('spellcheck', 'false');
    wrap.appendChild(label);
    var row = null;
    if (name === 'path') {                     // item 22: the folder field and its "Choose folder…" button
      row = el(doc, 'div', 'ss-setup-pathrow');
      row.appendChild(input);
      wrap.appendChild(row);
    } else {
      wrap.appendChild(input);
    }
    if (hintKey) {
      var hint = el(doc, 'p', 'ss-setup-hint', SS.t(hintKey));
      hint.setAttribute('id', id + '-hint');
      input.setAttribute('aria-describedby', id + '-hint');
      wrap.appendChild(hint);
    }
    return { wrap: wrap, input: input, row: row };
  }

  // Item 22: the "Choose folder…" button of one folder field, hidden until the app says a folder window exists.
  function chooser(doc, f) {
    var row = f.row;
    var button = el(doc, 'button', 'ss-setup-choose', SS.t('setup.project.choose'));
    button.setAttribute('type', 'button');
    button.hidden = true;
    row.appendChild(button);
    var note = el(doc, 'p', 'ss-setup-choose-note');
    note.setAttribute('aria-live', 'polite');
    f.wrap.appendChild(note);
    button.addEventListener('click', function () {
      note.textContent = '';
      button.disabled = true;
      return SS.api.post('/api/project/choose-folder', {}).then(function (doc2) {
        button.disabled = false;
        if (doc2 && typeof doc2.path === 'string' && doc2.path) {
          f.input.value = doc2.path;
          if (f.input.focus) f.input.focus();
        }
      }, function (err) {
        button.disabled = false;
        note.textContent = (err && err.message) || SS.t('setup.project.choose_failed');
      });
    });
    return { button: button, note: note };
  }

  function offerChoosers(choosers) {
    return SS.api.get('/api/project/folder-dialog').then(function (doc) {
      var yes = !!(doc && doc.available === true);
      choosers.forEach(function (c) {
        c.button.hidden = !yes;
        if (!yes) c.note.textContent = SS.t('setup.project.choose_none');
      });
    }, function () { /* no answer: the buttons stay hidden and the fields still take a typed path */ });
  }

  // Sends `body` to `path`; on success refreshes the Setup page, on refusal shows the server's message.
  function submit(button, errorBox, path, body) {
    errorBox.textContent = '';
    button.disabled = true;
    return SS.api.post(path, body).then(function () {
      button.disabled = false;
      return SS.setup.refresh();
    }, function (err) {
      button.disabled = false;
      errorBox.textContent = (err && err.message) || SS.t('api.failed');
    });
  }

  function form(doc, titleKey, fields, buttonKey, onSubmit) {
    var f = el(doc, 'form', 'ss-setup-form');
    f.setAttribute('novalidate', 'novalidate');
    f.appendChild(el(doc, 'h3', 'ss-setup-subtitle', SS.t(titleKey)));
    fields.forEach(function (x) { f.appendChild(x.wrap); });
    var button = el(doc, 'button', 'primary', SS.t(buttonKey));
    button.setAttribute('type', 'submit');
    f.appendChild(button);
    var errorBox = el(doc, 'p', 'ss-setup-error');
    errorBox.setAttribute('role', 'alert');
    f.appendChild(errorBox);
    f.addEventListener('submit', function (ev) {
      if (ev && ev.preventDefault) ev.preventDefault();
      onSubmit(button, errorBox);
    });
    return f;
  }

  function syncBlock(doc, project) {
    var kind = project.sync && project.sync.kind;
    if (!kind) return null;
    var service = serviceName(kind);
    if (project.sync.acknowledged) {
      return el(doc, 'p', 'ss-setup-note', SS.t('setup.project.sync_acknowledged', { service: service }));
    }
    var box = el(doc, 'div', 'ss-setup-warning');
    box.setAttribute('role', 'alert');
    box.appendChild(el(doc, 'h3', 'ss-setup-subtitle', SS.t('setup.project.sync_title')));
    box.appendChild(el(doc, 'p', '', SS.t('setup.project.sync_warning', { service: service })));
    var row = el(doc, 'div', 'ss-setup-check');
    var check = el(doc, 'input');
    check.setAttribute('type', 'checkbox');
    check.setAttribute('id', 'ss-setup-sync-understood');
    var label = el(doc, 'label', '', SS.t('setup.project.sync_understood', { service: service }));
    label.setAttribute('for', 'ss-setup-sync-understood');
    row.appendChild(check);
    row.appendChild(label);
    box.appendChild(row);
    var button = el(doc, 'button', 'primary', SS.t('setup.project.sync_continue'));
    button.setAttribute('type', 'button');
    button.disabled = true;
    var errorBox = el(doc, 'p', 'ss-setup-error');
    errorBox.setAttribute('role', 'alert');
    var why = SS.place.why(doc, 'ss-setup-sync-why', SS.t('setup.project.sync_why'));     // item 37
    button.setAttribute('aria-describedby', 'ss-setup-sync-why');
    check.addEventListener('change', function () {
      button.disabled = !check.checked;
      why.hidden = check.checked;
    });
    button.addEventListener('click', function () {
      if (!check.checked) return;
      submit(button, errorBox, '/api/project/ack-sync', { understood: true });
    });
    box.appendChild(button);
    box.appendChild(why);
    box.appendChild(errorBox);
    return box;
  }

  function current(doc, project) {
    var box = el(doc, 'div', 'ss-setup-current');
    box.appendChild(el(doc, 'p', 'ss-setup-lead', SS.t('setup.project.open_now', { name: project.name })));
    var where = el(doc, 'p', 'ss-setup-line');
    where.appendChild(el(doc, 'span', 'ss-setup-key', SS.t('setup.project.folder')));
    where.appendChild(el(doc, 'code', 'mono', project.root));
    box.appendChild(where);
    var log = project.log || {};
    var logLine = el(doc, 'p', 'ss-setup-line');
    logLine.appendChild(el(doc, 'span', 'ss-setup-key', SS.t('setup.project.log')));
    logLine.appendChild(el(doc, 'span', log.ok ? '' : 'ss-setup-bad',
      SS.t(log.ok ? 'setup.project.log_ok' : 'setup.project.log_bad', { records: String(log.records || 0) })));
    box.appendChild(logLine);
    var sync = syncBlock(doc, project);
    if (sync) box.appendChild(sync);
    var changed = Array.isArray(project.changed_files) ? project.changed_files : [];
    if (changed.length) {
      var warn = el(doc, 'div', 'ss-setup-warning');
      warn.setAttribute('role', 'alert');
      warn.appendChild(el(doc, 'p', '', SS.t('setup.project.changed')));
      var list = el(doc, 'ul', 'ss-setup-files');
      changed.forEach(function (rel) { list.appendChild(el(doc, 'li', 'mono', rel)); });
      warn.appendChild(list);
      box.appendChild(warn);
    }
    return box;
  }

  function render(container, project) {
    var doc = container.ownerDocument || root.document;
    clear(container);
    container.appendChild(el(doc, 'p', 'ss-setup-intro', SS.t('setup.project.intro')));
    if (project) container.appendChild(current(doc, project));

    var cFolder = field(doc, 'setup.project.folder_label', 'path', 'setup.project.folder_hint');
    var cName = field(doc, 'setup.project.name_label', 'name');
    var forms = el(doc, 'div', 'ss-setup-forms');
    forms.appendChild(form(doc, project ? 'setup.project.create_other' : 'setup.project.create_title',
      [cFolder, cName], 'setup.project.create', function (button, errorBox) {
        submit(button, errorBox, '/api/project/create', { path: cFolder.input.value, name: cName.input.value });
      }));
    var oFolder = field(doc, 'setup.project.open_label', 'path');
    forms.appendChild(form(doc, project ? 'setup.project.open_other' : 'setup.project.open_title', [oFolder],
      'setup.project.open', function (button, errorBox) {
        submit(button, errorBox, '/api/project/open', { path: oFolder.input.value });
      }));
    container.appendChild(forms);
    offerChoosers([chooser(doc, cFolder), chooser(doc, oFolder)]);
  }

  (SS.setupQueue = SS.setupQueue || []).push(['project', {
    order: 10, titleKey: 'setup.project.title', needsProject: false, render: render
  }]);
})();
