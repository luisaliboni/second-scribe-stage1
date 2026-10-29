/* Setup step "rule" (Task 33; spec 6.5 step 4, section 7; plan V12): per map format in this project, how a box or
   arrow is marked missing. Defaults: red outline or text (Visio, draw.io, Excalidraw), a "missing" class (Mermaid),
   the status column (the template). For Visio, "red, including red set by a master shape or style" reads red that
   the drawing inherits. POST /api/missing-rule; the parsed-map step shows what each rule reads.
   Item 28 (23 Oct): beside "Colours that count as red", the list as removable chips (swatch and hex), a native colour
   picker that adds a colour, and "Colours found in your maps" (GET /api/missing-rule/colors: the outline, text and
   fill colours of the imported maps' boxes, with how many boxes use each) as chips to tick. The hex list stays
   editable and is what is sent; chips and ticks follow it.
   Classic script; queues itself for SS.setup. */
(function () {
  'use strict';
  var root = typeof window !== 'undefined' ? window : globalThis;
  var SS = root.SS = root.SS || {};
  var COLOR_KINDS = { red_line_or_text: 1, red_fill: 1 };
  var MAX_COLORS = 8;           // as the server (importer.MAX_COLORS)
  var HEX = /^#[0-9a-f]{6}$/;
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

  // The suffix of a map file name, as the server reads it (.mapgraph.json counts as one).
  function suffixOf(name) {
    var low = String(name || '').toLowerCase();
    if (/\.mapgraph\.json$/.test(low) && low.length > '.mapgraph.json'.length) return '.mapgraph.json';
    var i = low.lastIndexOf('.');
    return i > 0 ? low.slice(i) : '';
  }

  function words(text) {
    return String(text || '').split(/[\s,]+/).filter(function (s) { return s; });
  }

  // The request body for one format's form state.
  function body(suffix, kind, colorsText, classText, inherit) {
    var b = { suffix: suffix, kind: kind };
    if (COLOR_KINDS[kind] && words(colorsText).length) b.colors = words(colorsText);
    if (kind === 'mermaid_class' && words(classText).length) b.class_names = words(classText);
    if (suffix === '.vsdx' && kind === 'red_line_or_text') b.inherit = !!inherit;
    return b;
  }

  function paint(swatch, hex) {
    swatch.setAttribute('data-color', hex);
    if (swatch.style && HEX.test(hex)) swatch.style.backgroundColor = hex;
  }

  // The hex list as lower-case codes, in order, without repeats (what the chips and ticks show).
  function listOf(text) {
    var seen = {};
    return words(text).map(function (c) { return c.toLowerCase(); })
      .filter(function (c) { if (seen[c]) return false; seen[c] = true; return true; });
  }

  // How a found colour is used: "4 boxes: outline 1, fill 3".
  function usage(c) {
    var places = ['line', 'text', 'fill'].filter(function (p) { return c[p] > 0; }).map(function (p) {
      return SS.t('setup.rule.place.' + p, { n: String(c[p]) });
    });
    return SS.tn('setup.rule.found_boxes', c.boxes, { n: String(c.boxes) }) + (places.length ? ': ' + places.join(', ') : '');
  }

  // The colour list of one format: the editable hex field, its chips, the picker and the colours found in the maps.
  function colorField(doc, suffix, rule, found) {
    var cw = el(doc, 'div', 'ss-setup-field ss-setup-colors');
    var cid = 'ss-setup-rule-colors-' + uid;
    var cl = el(doc, 'label', 'ss-setup-label', SS.t('setup.rule.colors_label'));
    cl.setAttribute('for', cid);
    var input = el(doc, 'input', 'ss-setup-input mono');
    input.setAttribute('id', cid);
    input.setAttribute('type', 'text');
    input.setAttribute('spellcheck', 'false');
    input.value = (rule.colors || []).join(', ');
    var chips = el(doc, 'ul', 'ss-setup-chips');
    chips.setAttribute('aria-label', SS.t('setup.rule.chips_label'));
    var pickRow = el(doc, 'div', 'ss-setup-pick');
    var pid = cid + '-pick';
    var picker = el(doc, 'input', 'ss-setup-color-pick');
    picker.setAttribute('type', 'color');
    picker.setAttribute('id', pid);
    picker.value = '#ff0000';
    var pl = el(doc, 'label', '', SS.t('setup.rule.pick'));
    pl.setAttribute('for', pid);
    pickRow.appendChild(picker);
    pickRow.appendChild(pl);
    var full = el(doc, 'p', 'ss-setup-hint ss-setup-colors-full');
    full.setAttribute('role', 'status');
    var ticks = [];

    function write(list) {
      input.value = list.join(', ');
      sync();
    }
    function add(hex) {
      var list = listOf(input.value);
      hex = String(hex || '').toLowerCase();
      if (!HEX.test(hex) || list.indexOf(hex) >= 0) { sync(); return; }
      if (list.length >= MAX_COLORS) {
        sync();
        full.textContent = SS.t('setup.rule.colors_full', { n: String(MAX_COLORS) });
        return;
      }
      write(list.concat([hex]));
    }
    function drop(hex) {
      write(listOf(input.value).filter(function (c) { return c !== hex; }));
    }
    function sync() {
      var list = listOf(input.value);
      full.textContent = '';
      clear(chips);
      list.forEach(function (hex) {
        var li = el(doc, 'li', 'ss-setup-chip');
        li.setAttribute('data-color', hex);
        var sw = el(doc, 'span', 'ss-setup-swatch');
        sw.setAttribute('aria-hidden', 'true');
        paint(sw, hex);
        li.appendChild(sw);
        li.appendChild(el(doc, 'span', 'mono', hex));
        var x = el(doc, 'button', 'ss-setup-chip-remove', '\u00d7');
        x.setAttribute('type', 'button');
        x.setAttribute('aria-label', SS.t('setup.rule.chip_remove', { color: hex }));
        x.addEventListener('click', function () { drop(hex); });
        li.appendChild(x);
        chips.appendChild(li);
      });
      ticks.forEach(function (t) {
        t.checked = list.indexOf(t.getAttribute('data-color')) >= 0;
      });
    }

    input.addEventListener('input', sync);
    picker.addEventListener('change', function () { add(picker.value); });
    cw.appendChild(cl);
    cw.appendChild(input);
    cw.appendChild(el(doc, 'p', 'ss-setup-hint', SS.t('setup.rule.colors_hint')));
    cw.appendChild(chips);
    cw.appendChild(pickRow);
    cw.appendChild(full);

    var f = found && found.formats && found.formats[suffix];
    if (f) {
      var fb = el(doc, 'fieldset', 'ss-setup-fieldset ss-setup-found');
      fb.appendChild(el(doc, 'legend', 'ss-setup-label', SS.t('setup.rule.found_title')));
      var colors = f.colors || [];
      if (colors.length) {
        fb.appendChild(el(doc, 'p', 'ss-setup-hint', SS.t('setup.rule.found_hint')));
        var ul = el(doc, 'ul', 'ss-setup-chips ss-setup-found-list');
        colors.forEach(function (c, i) {
          var li = el(doc, 'li', 'ss-setup-chip ss-setup-found-chip');
          var id = cid + '-found-' + i;
          var box = el(doc, 'input');
          box.setAttribute('type', 'checkbox');
          box.setAttribute('id', id);
          box.setAttribute('data-color', c.color);
          box.addEventListener('change', function () {
            if (box.checked) add(c.color);
            else drop(c.color);
          });
          ticks.push(box);
          var lab = el(doc, 'label', '');
          lab.setAttribute('for', id);
          var sw = el(doc, 'span', 'ss-setup-swatch');
          sw.setAttribute('aria-hidden', 'true');
          paint(sw, c.color);
          lab.appendChild(sw);
          lab.appendChild(el(doc, 'span', 'mono', c.color));
          lab.appendChild(el(doc, 'span', 'ss-setup-found-use', usage(c)));
          li.appendChild(box);
          li.appendChild(lab);
          ul.appendChild(li);
        });
        fb.appendChild(ul);
        if (f.more > 0) fb.appendChild(el(doc, 'p', 'ss-setup-hint', SS.tn('setup.rule.found_more', f.more, { n: String(f.more) })));
        if (suffix === '.vsdx') fb.appendChild(el(doc, 'p', 'ss-setup-hint', SS.t('setup.rule.found_vsdx')));
      } else {
        fb.appendChild(el(doc, 'p', 'ss-setup-note', SS.t('setup.rule.found_none')));
      }
      if (f.unreadable > 0) {
        fb.appendChild(el(doc, 'p', 'ss-setup-hint', SS.tn('setup.rule.found_unreadable', f.unreadable, { n: String(f.unreadable) })));
      }
      cw.appendChild(fb);
    }
    sync();
    return { node: cw, input: input };
  }

  function formatCard(doc, suffix, rule, kinds, found) {
    uid += 1;
    var box = el(doc, 'section', 'ss-setup-card');
    box.setAttribute('data-suffix', suffix);
    box.appendChild(el(doc, 'h3', 'ss-setup-subtitle', SS.t('setup.rule.format', { suffix: suffix })));
    var fs = el(doc, 'fieldset', 'ss-setup-fieldset');
    fs.appendChild(el(doc, 'legend', 'ss-setup-label', SS.t('setup.rule.legend')));
    var radios = [];
    var inheritBox = null;
    kinds.forEach(function (kind, i) {
      var row = el(doc, 'div', 'ss-setup-check');
      var input = el(doc, 'input');
      var id = 'ss-setup-rule-' + uid + '-' + i;
      input.setAttribute('type', 'radio');
      input.setAttribute('name', 'ss-setup-rule-' + uid);
      input.setAttribute('id', id);
      input.setAttribute('value', kind);
      if (rule.kind === kind && !(suffix === '.vsdx' && kind === 'red_line_or_text' && rule.inherit)) {
        input.setAttribute('checked', 'checked');
        input.checked = true;
      }
      var label = el(doc, 'label', '', SS.t('setup.rule.kind.' + kind) + (i === 0 ? ' ' + SS.t('setup.rule.default') : ''));
      label.setAttribute('for', id);
      row.appendChild(input);
      row.appendChild(label);
      fs.appendChild(row);
      radios.push(input);
      if (suffix === '.vsdx' && kind === 'red_line_or_text') {
        var r2 = el(doc, 'div', 'ss-setup-check');
        var inp2 = el(doc, 'input');
        var id2 = id + '-inherit';
        inp2.setAttribute('type', 'radio');
        inp2.setAttribute('name', 'ss-setup-rule-' + uid);
        inp2.setAttribute('id', id2);
        inp2.setAttribute('value', 'red_line_or_text+inherit');
        if (rule.kind === kind && rule.inherit) {
          inp2.setAttribute('checked', 'checked');
          inp2.checked = true;
        }
        var l2 = el(doc, 'label', '', SS.t('setup.rule.kind.red_inherit'));
        l2.setAttribute('for', id2);
        r2.appendChild(inp2);
        r2.appendChild(l2);
        fs.appendChild(r2);
        radios.push(inp2);
        inheritBox = inp2;
      }
    });
    box.appendChild(fs);
    var extra = el(doc, 'div', 'ss-setup-row');
    var colors = null;
    var classes = null;
    if (kinds.some(function (k) { return COLOR_KINDS[k]; })) {
      var field = colorField(doc, suffix, rule, found);
      colors = field.input;
      extra.appendChild(field.node);
    }
    if (kinds.indexOf('mermaid_class') >= 0) {
      var kw = el(doc, 'div', 'ss-setup-field');
      var kid = 'ss-setup-rule-classes-' + uid;
      var kl = el(doc, 'label', 'ss-setup-label', SS.t('setup.rule.classes_label'));
      kl.setAttribute('for', kid);
      classes = el(doc, 'input', 'ss-setup-input mono');
      classes.setAttribute('id', kid);
      classes.setAttribute('type', 'text');
      classes.setAttribute('spellcheck', 'false');
      classes.value = (rule.class_names || []).join(', ');
      kw.appendChild(kl);
      kw.appendChild(classes);
      extra.appendChild(kw);
    }
    if (extra.firstChild) box.appendChild(extra);
    var button = el(doc, 'button', '', SS.t('setup.rule.save'));
    button.setAttribute('type', 'button');
    var status = el(doc, 'p', 'ss-setup-note');
    status.setAttribute('role', 'status');
    var errorBox = el(doc, 'p', 'ss-setup-error');
    errorBox.setAttribute('role', 'alert');
    button.addEventListener('click', function () {
      var picked = radios.filter(function (r) { return r.checked; })[0];
      if (!picked) return;
      var inherit = picked === inheritBox;
      var kind = inherit ? 'red_line_or_text' : picked.getAttribute('value');
      errorBox.textContent = '';
      status.textContent = '';
      button.disabled = true;
      SS.api.post('/api/missing-rule', body(suffix, kind, colors && colors.value, classes && classes.value, inherit))
        .then(function () {
          button.disabled = false;
          status.textContent = SS.t('setup.rule.saved');
        }, function (err) {
          button.disabled = false;
          errorBox.textContent = (err && err.message) || SS.t('api.failed');
        });
    });
    box.appendChild(button);
    box.appendChild(status);
    box.appendChild(errorBox);
    return box;
  }

  function draw(container, view, table, found) {
    var doc = container.ownerDocument || root.document;
    clear(container);
    container.appendChild(el(doc, 'p', 'ss-setup-intro', SS.t('setup.rule.intro')));
    var used = {};
    ((table && table.rows) || []).forEach(function (r) { if (r.map) used[suffixOf(r.map)] = true; });
    var suffixes = Object.keys(view.options || {}).filter(function (s) { return used[s]; });
    if (!suffixes.length) {
      container.appendChild(el(doc, 'p', 'ss-setup-note', SS.t('setup.rule.no_maps')));
      return;
    }
    suffixes.forEach(function (s) {
      container.appendChild(formatCard(doc, s, view.rules[s] || {}, view.options[s] || [], found));
    });
    if (used['.mapgraph.json']) container.appendChild(el(doc, 'p', 'ss-setup-hint', SS.t('setup.rule.mapgraph_note')));
  }

  function render(container) {
    var doc = container.ownerDocument || root.document;
    clear(container);
    container.appendChild(el(doc, 'p', 'ss-shell-note', SS.t('shell.loading')));
    // the colours found are a help: if they cannot be read, the step works without them
    var found = SS.api.get('/api/missing-rule/colors').then(null, function () { return null; });
    return Promise.all([SS.api.get('/api/missing-rule'), SS.api.get('/api/pairing'), found]).then(function (got) {
      draw(container, got[0] || {}, got[1] || {}, got[2]);
    }, function (err) {
      clear(container);
      container.appendChild(el(doc, 'p', 'ss-setup-error', (err && err.message) || SS.t('api.failed')));
    });
  }

  SS.setupRule = { suffixOf: suffixOf, body: body, draw: draw, listOf: listOf, usage: usage };
  (SS.setupQueue = SS.setupQueue || []).push(['rule', {
    order: 40, titleKey: 'setup.rule.title', needsProject: true, render: render
  }]);
})();
