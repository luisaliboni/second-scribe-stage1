/* Setup step "import" (Task 33; spec 6.5 step 3 and 4 "pairing"): bring transcripts (.docx, .txt) and maps
   (.vsdx, .drawio, .excalidraw, .mmd, the CSV/XLSX template) into the project, then confirm the pairing table.
   Each file is sent as base64 to the server, which copies it into the project folder, hashes it and logs it.
   PDF and images are refused by the server with the reason. Files pair by name (stem); a transcript without a map can
   only take reference B or C.
   Item 24 (23 Oct): each picker is also a drop area. Files dropped on it go through the same path as chosen ones
   (sendAll, so the server's checks and messages are the same, PDF and images refused with the reason); a dropped
   folder is not opened and gets one line saying so. The area is lit while files are dragged over it; the file
   input inside still works by click and keyboard. A file dropped beside the areas is ignored instead of making the
   browser leave the app.
   Item 25 (23 Oct): with 12 transcripts imported, the 12 result lines pushed everything below them out of view, so the
   maps sent further down looked as if nothing had happened. Each picker now shows its own result inside its box: a
   status line ("Importing 3 of 12…", then "12 of 12 files imported." and how many files of that kind the project
   holds) and a line only for a file that needs attention (refused, replaced, identical). The pairing table sits
   right under the pickers and is reloaded after each file and after the batch, whatever the outcome.
   Item 26 (23 Oct): each file in the table has a Remove button. It asks in the page (a row under the file, "Remove
   the file" or "Keep it"; no browser dialog), then POST /api/import/remove {kind, pid}: the server deletes the
   project's copy, clears the pairing and the labels or map check of that participant, and logs the removal. The
   person's own file is never touched. Refused while a run is going (the server's sentence shows in that row).
   Classic script; queues itself for SS.setup. */
(function () {
  'use strict';
  var root = typeof window !== 'undefined' ? window : globalThis;
  var SS = root.SS = root.SS || {};
  var ACCEPT = {
    transcript: '.docx,.txt',
    map: '.vsdx,.drawio,.excalidraw,.mmd,.csv,.xlsx,.json'
  };

  function el(doc, tag, cls, text) {
    var n = doc.createElement(tag);
    if (cls) n.setAttribute('class', cls);
    if (text !== undefined) n.textContent = text;
    return n;
  }

  function clear(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
  }

  // A data: URL from FileReader -> the base64 part after the comma.
  function base64Of(dataUrl) {
    var s = String(dataUrl || '');
    var i = s.indexOf(',');
    return i >= 0 ? s.slice(i + 1) : '';
  }

  function readFile(file) {
    return new Promise(function (resolve, reject) {
      var reader = new root.FileReader();
      reader.onload = function () { resolve(base64Of(reader.result)); };
      reader.onerror = function () { reject(new Error(SS.t('setup.import.read_failed'))); };
      reader.readAsDataURL(file);
    });
  }

  // Sends the files one after another. `out` is the picker's result area ({status, list}): the status line counts
  // them, and a file gets its own line in `out.list` only when it needs attention (refused, replaced, identical).
  // The pairing table reloads after each file, so the files show in it as they arrive. Resolves to {ok, total}.
  function sendAll(doc, kind, files, out) {
    var chain = Promise.resolve();
    var total = files.length;
    var ok = 0;
    var done = 0;
    function progress() {
      out.status.setAttribute('data-state', 'busy');
      out.status.textContent = SS.t('setup.import.progress', { n: String(done + 1), total: String(total) });
    }
    Array.prototype.forEach.call(files, function (file) {
      chain = chain.then(function () {
        progress();
        return readFile(file).then(function (b64) {
          return SS.api.post('/api/import', { kind: kind, name: file.name, data_b64: b64 });
        }).then(function (reply) {
          ok += 1;
          var replaced = reply && reply.replaced;
          var same = reply && reply.identical_to && reply.identical_to.length ? reply.identical_to : null;
          if (!replaced && !same) return;
          var li = line(doc, file.name, 'true');
          li.appendChild(el(doc, 'span', 'ss-setup-import-result', replaced
            ? SS.t('setup.import.replaced', { name: replaced }) : SS.t('setup.import.done')));
          if (same) {                                                           // a warning only; the file is kept
            li.appendChild(el(doc, 'span', 'ss-setup-identical', ' ' +
              SS.t('setup.import.identical_warning', { name: same.join(', ') })));
          }
          out.list.appendChild(li);
        }).then(null, function (err) {
          var li = line(doc, file.name, 'false');
          li.appendChild(el(doc, 'span', 'ss-setup-import-result ss-setup-error', (err && err.message) || SS.t('api.failed')));
          out.list.appendChild(li);
        }).then(function () {
          done += 1;
          return SS.setupImport.reloadTable();
        });
      });
    });
    return chain.then(function () {
      out.status.setAttribute('data-state', ok === total ? 'done' : 'partial');
      out.status.textContent = SS.tn('setup.import.batch_done', total, { ok: String(ok), total: String(total) }) +
        (ok < total ? ' ' + SS.tn('setup.import.batch_failed', total - ok, { n: String(total - ok) }) : '');
      held(out);
      return { ok: ok, total: total };
    });
  }

  function line(doc, name, ok) {
    var li = el(doc, 'li', 'ss-setup-import-line');
    li.setAttribute('data-ok', ok);
    li.appendChild(el(doc, 'span', 'mono', name));
    return li;
  }

  // After a batch, the status line also says how many files of this kind the project now holds (from the table).
  function held(out) {
    if (!out.kind || typeof lastCounts[out.kind] !== 'number') return;
    out.status.textContent += ' ' + SS.t('setup.import.held_' + out.kind, { n: String(lastCounts[out.kind]) });
  }

  // Item 24: what a drop holds -> {files: [File], folders: [name]}. With DataTransferItem entries a folder is named
  // and left out; without them (older browsers) every dropped thing is sent and the server judges it.
  function dropped(dt) {
    var out = { files: [], folders: [] };
    if (!dt) return out;
    var items = dt.items;
    if (items && items.length && typeof items[0].webkitGetAsEntry === 'function') {
      Array.prototype.forEach.call(items, function (item) {
        if (item.kind !== 'file') return;
        var entry = item.webkitGetAsEntry();
        if (entry && entry.isDirectory) { out.folders.push(entry.name); return; }
        var f = item.getAsFile();
        if (f) out.files.push(f);
      });
      return out;
    }
    Array.prototype.forEach.call(dt.files || [], function (f) { out.files.push(f); });
    return out;
  }
  function hasFiles(dt) {
    var types = dt && dt.types;
    if (!types) return false;
    return Array.prototype.indexOf.call(types, 'Files') >= 0;
  }

  function folderLine(doc, name, out) {
    var li = line(doc, name, 'false');
    li.appendChild(el(doc, 'span', 'ss-setup-import-result ss-setup-error', SS.t('setup.import.drop_folder')));
    out.list.appendChild(li);
  }

  // Clears the picker's result area before a new batch.
  function reset(out) {
    clear(out.list);
    out.status.textContent = '';
    out.status.setAttribute('data-state', 'idle');
  }

  function dropArea(doc, box, kind, out) {
    var depth = 0;
    function light(on) { box.setAttribute('data-over', on ? 'true' : 'false'); }
    light(false);
    box.addEventListener('dragenter', function (ev) {
      if (!hasFiles(ev && ev.dataTransfer)) return;
      if (ev.preventDefault) ev.preventDefault();
      depth += 1;
      light(true);
    });
    box.addEventListener('dragover', function (ev) {
      if (!hasFiles(ev && ev.dataTransfer)) return;
      if (ev.preventDefault) ev.preventDefault();
      if (ev.dataTransfer) ev.dataTransfer.dropEffect = 'copy';
      light(true);
    });
    box.addEventListener('dragleave', function () {
      depth = Math.max(0, depth - 1);
      if (!depth) light(false);
    });
    box.addEventListener('drop', function (ev) {
      if (ev && ev.preventDefault) ev.preventDefault();
      if (ev && ev.stopPropagation) ev.stopPropagation();
      depth = 0;
      light(false);
      var got = dropped(ev && ev.dataTransfer);
      reset(out);
      got.folders.forEach(function (name) { folderLine(doc, name, out); });
      if (!got.files.length) return null;
      return sendAll(doc, kind, got.files, out);
    });
  }

  // A file dropped beside the drop areas would make the browser open it in place of the app.
  var guarded = false;
  function guardPage(doc) {
    if (guarded || !doc || typeof doc.addEventListener !== 'function') return;
    guarded = true;
    ['dragover', 'drop'].forEach(function (type) {
      doc.addEventListener(type, function (ev) {
        if (hasFiles(ev && ev.dataTransfer) && ev.preventDefault) ev.preventDefault();
      });
    });
  }

  function picker(doc, kind) {
    var box = el(doc, 'div', 'ss-setup-field ss-setup-picker ss-setup-drop');
    box.setAttribute('data-kind', kind);
    var id = 'ss-setup-import-' + kind;
    var label = el(doc, 'label', 'ss-setup-label', SS.t('setup.import.' + kind + '_label'));
    label.setAttribute('for', id);
    var input = el(doc, 'input', 'ss-setup-file');
    input.setAttribute('id', id);
    input.setAttribute('type', 'file');
    input.setAttribute('multiple', 'multiple');
    input.setAttribute('accept', ACCEPT[kind]);
    var hint = el(doc, 'p', 'ss-setup-hint', SS.t('setup.import.' + kind + '_hint'));
    hint.setAttribute('id', id + '-hint');
    input.setAttribute('aria-describedby', id + '-hint');
    // Item 25: the result of this picker's imports, inside its own box, so it shows where the person is looking.
    var status = el(doc, 'p', 'ss-setup-import-status');
    status.setAttribute('role', 'status');
    status.setAttribute('aria-live', 'polite');
    status.setAttribute('data-state', 'idle');
    var list = el(doc, 'ul', 'ss-setup-import-list');
    var out = { kind: kind, status: status, list: list };
    input.addEventListener('change', function () {
      var files = input.files || [];
      if (!files.length) return;
      var chosen = Array.prototype.slice.call(files);
      reset(out);
      sendAll(doc, kind, chosen, out).then(function () { input.value = ''; });
    });
    box.appendChild(label);
    box.appendChild(el(doc, 'p', 'ss-setup-drop-text', SS.t('setup.import.drop')));
    box.appendChild(input);
    box.appendChild(hint);
    box.appendChild(status);
    box.appendChild(list);
    dropArea(doc, box, kind, out);
    return box;
  }

  // Item 26: the Remove button of one file, and the question it asks in a row under the file's row.
  var removedNote = '';
  function removeButton(doc, tbody, tr, kind, r) {
    var name = r[kind];
    var b = el(doc, 'button', 'ss-setup-remove', SS.t('setup.import.remove'));
    b.setAttribute('type', 'button');
    b.setAttribute('data-kind', kind);
    b.setAttribute('data-pid', r.pid);
    b.setAttribute('aria-label', SS.t('setup.import.remove_label', { name: name }));
    b.addEventListener('click', function () {
      if (tr.ssAsk) return;
      tr.ssAsk = ask(doc, tbody, tr, kind, r, b);
    });
    return b;
  }

  function ask(doc, tbody, tr, kind, r, opener) {
    var row = el(doc, 'tr', 'ss-setup-remove-ask');
    row.setAttribute('data-kind', kind);
    row.setAttribute('data-pid', r.pid);
    var cell = el(doc, 'td');
    cell.setAttribute('colspan', '4');
    var q = el(doc, 'p', 'ss-setup-remove-question', SS.t('setup.import.remove_ask_' + kind, { name: r[kind], pid: r.pid }));
    q.setAttribute('id', 'ss-setup-remove-q-' + kind + '-' + r.pid);
    cell.appendChild(q);
    var yes = el(doc, 'button', 'ss-setup-remove-yes', SS.t('setup.import.remove_yes'));
    yes.setAttribute('type', 'button');
    yes.setAttribute('aria-describedby', q.getAttribute('id'));
    var no = el(doc, 'button', 'ss-setup-remove-no', SS.t('setup.import.remove_no'));
    no.setAttribute('type', 'button');
    var errorBox = el(doc, 'p', 'ss-setup-error');
    errorBox.setAttribute('role', 'alert');
    var buttons = el(doc, 'div', 'ss-setup-remove-buttons');
    buttons.appendChild(yes);
    buttons.appendChild(no);
    cell.appendChild(buttons);
    cell.appendChild(errorBox);
    row.appendChild(cell);
    var next = tr.nextSibling || null;
    if (next && tbody.insertBefore) tbody.insertBefore(row, next); else tbody.appendChild(row);
    function close() {
      if (row.parentNode && row.parentNode.removeChild) row.parentNode.removeChild(row);
      else tbody.removeChild(row);
      tr.ssAsk = null;
      if (opener.focus) opener.focus();
    }
    no.addEventListener('click', close);
    yes.addEventListener('click', function () {
      yes.disabled = true;
      no.disabled = true;
      errorBox.textContent = '';
      SS.api.post('/api/import/remove', { kind: kind, pid: r.pid }).then(function (reply) {
        var shown = (reply && reply.removed && reply.removed.name) || r[kind];
        removedNote = SS.t('setup.import.removed', { name: shown });
        return SS.setupImport.reloadTable();
      }, function (err) {
        yes.disabled = false;
        no.disabled = false;
        errorBox.textContent = (err && err.message) || SS.t('api.failed');
      });
    });
    if (no.focus) no.focus();
    return row;
  }

  function refCell(doc, opts) {
    if (!opts || !opts.length) return el(doc, 'td', 'ss-setup-muted', SS.t('setup.import.no_transcript'));
    return el(doc, 'td', '', opts.join(' · '));
  }

  var lastCounts = {};     // files of each kind in the last table drawn (item 25: the pickers' status line)

  function drawTable(doc, box, table) {
    clear(box);
    var rows = (table && table.rows) || [];
    lastCounts = {
      transcript: rows.filter(function (r) { return !!r.transcript; }).length,
      map: rows.filter(function (r) { return !!r.map; }).length
    };
    if (!rows.length) {
      box.appendChild(el(doc, 'p', 'ss-setup-note', SS.t('setup.import.empty')));
      return;
    }
    box.appendChild(el(doc, 'h3', 'ss-setup-subtitle', SS.t('setup.import.pairing_title')));
    if (removedNote) {                                   // item 26: what the last removal did, once
      var said = el(doc, 'p', 'ss-setup-ok ss-setup-removed', removedNote);
      said.setAttribute('role', 'status');
      box.appendChild(said);
      removedNote = '';
    }
    box.appendChild(el(doc, 'p', 'ss-setup-line ss-setup-import-counts', SS.t('setup.import.counts', {
      t: String(lastCounts.transcript), m: String(lastCounts.map) })));
    box.appendChild(el(doc, 'p', 'ss-setup-hint', SS.t('setup.import.pairing_hint')));
    var t = el(doc, 'table', 'ss-setup-table');
    var head = el(doc, 'tr');
    ['pid', 'transcript', 'map', 'references'].forEach(function (k) {
      var th = el(doc, 'th', '', SS.t('setup.import.col_' + k));
      th.setAttribute('scope', 'col');
      head.appendChild(th);
    });
    var thead = el(doc, 'thead');
    thead.appendChild(head);
    t.appendChild(thead);
    var body = el(doc, 'tbody');
    rows.forEach(function (r) {
      var tr = el(doc, 'tr');
      tr.setAttribute('data-paired', r.paired ? 'true' : 'false');
      tr.appendChild(el(doc, 'td', 'mono', r.pid));
      var tcell = el(doc, 'td', r.transcript ? 'mono' : 'ss-setup-muted', r.transcript || SS.t('setup.import.none'));
      if (r.transcript) tcell.appendChild(removeButton(doc, body, tr, 'transcript', r));
      if (r.identical_to && r.identical_to.length) {                          // a warning only; nothing is excluded
        tr.setAttribute('data-identical', 'true');
        var warn = el(doc, 'div', 'ss-setup-identical', SS.t('setup.import.identical', { name: r.identical_to.join(', ') }));
        warn.setAttribute('role', 'note');
        tcell.appendChild(warn);
      }
      tr.appendChild(tcell);
      var mcell = el(doc, 'td', r.map ? 'mono' : 'ss-setup-muted', r.map || SS.t('setup.import.none'));
      if (r.map) mcell.appendChild(removeButton(doc, body, tr, 'map', r));
      tr.appendChild(mcell);
      tr.appendChild(refCell(doc, r.reference_options));
      body.appendChild(tr);
    });
    t.appendChild(body);
    var scroll = el(doc, 'div', 'ss-setup-table-wrap');
    scroll.appendChild(t);
    box.appendChild(scroll);
    var status = el(doc, 'p', table.confirmed ? 'ss-setup-ok' : 'ss-setup-note',
      SS.t(table.confirmed ? 'setup.import.pairing_confirmed' : 'setup.import.pairing_unconfirmed'));
    status.setAttribute('role', 'status');
    box.appendChild(status);
    if (!table.confirmed) {
      var button = el(doc, 'button', 'primary', SS.t('setup.import.confirm_pairing'));
      button.setAttribute('type', 'button');
      var errorBox = el(doc, 'p', 'ss-setup-error');
      errorBox.setAttribute('role', 'alert');
      button.addEventListener('click', function () {
        button.disabled = true;
        errorBox.textContent = '';
        SS.api.post('/api/pairing/confirm', { table_sha256: table.table_sha256 }).then(function () {
          return SS.setup.refresh();
        }, function (err) {
          button.disabled = false;
          errorBox.textContent = (err && err.message) || SS.t('api.failed');
        });
      });
      box.appendChild(button);
      box.appendChild(errorBox);
    }
  }

  var current = null;      // {doc, box} of the table on show

  function reloadTable() {
    if (!current) return Promise.resolve();
    var c = current;
    return SS.api.get('/api/pairing').then(function (table) {
      if (current === c) drawTable(c.doc, c.box, table);
    }).then(null, function (err) {
      if (current !== c) return;
      clear(c.box);
      c.box.appendChild(el(c.doc, 'p', 'ss-setup-error', (err && err.message) || SS.t('api.failed')));
    });
  }

  function render(container) {
    var doc = container.ownerDocument || root.document;
    clear(container);
    container.appendChild(el(doc, 'p', 'ss-setup-intro', SS.t('setup.import.intro')));
    var refusal = el(doc, 'p', 'ss-setup-hint', SS.t('setup.import.refused_note'));
    container.appendChild(refusal);
    container.appendChild(el(doc, 'p', 'ss-setup-hint ss-setup-name-tip', SS.t('setup.import.name_tip')));
    guardPage(doc);
    var pickers = el(doc, 'div', 'ss-setup-forms');
    pickers.appendChild(picker(doc, 'transcript'));
    pickers.appendChild(picker(doc, 'map'));
    container.appendChild(pickers);
    var links = el(doc, 'p', 'ss-setup-hint');
    var ex = el(doc, 'a', '', SS.t('setup.import.example_link'));
    ex.setAttribute('href', '/static/examples/labelled-transcript.txt');
    ex.setAttribute('download', 'labelled-transcript.txt');
    links.appendChild(ex);
    links.appendChild(doc.createTextNode ? doc.createTextNode(' · ') : el(doc, 'span', '', ' · '));
    var tpl = el(doc, 'a', '', SS.t('setup.import.template_link'));
    tpl.setAttribute('href', '/static/templates/map-template.xlsx');
    tpl.setAttribute('download', 'map-template.xlsx');
    links.appendChild(tpl);
    container.appendChild(links);
    var box = el(doc, 'div', 'ss-setup-pairing');
    container.appendChild(box);
    current = { doc: doc, box: box };
    box.appendChild(el(doc, 'p', 'ss-shell-note', SS.t('shell.loading')));
    return reloadTable();
  }

  SS.setupImport = { base64Of: base64Of, drawTable: drawTable, reloadTable: reloadTable, dropped: dropped };
  (SS.setupQueue = SS.setupQueue || []).push(['import', {
    order: 30, titleKey: 'setup.import.title', needsProject: true, render: render
  }]);
})();
