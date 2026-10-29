/* The blind mode-B Coding screen (Task 37; spec 6.7, artboard AppCoding). Classic script; needs core.js, api.js and
   app-shell.js. The hash is #coding/<pid>; #coding alone lists the transcripts whose reference is B.

   Blindness (F8): this screen shows the transcript, the researcher's own marks and nothing else. It calls only
   GET /api/references, GET /api/coding?pid=, GET /api/coding/suggest?prefix= and POST /api/coding/mark|unmark|tick|
   lock (Task 36), never a route that serves results, the no-interview map or an AI map (the first such view served
   logs the reveal), and it draws only the fields it names below, so anything else a reply carried never reaches
   the page. The stated limit is drawn from the reply's `limit` (certificate.BLINDNESS_LIMIT), never retyped.

   A mark is tied to its span: SS.coding.selection(root, sel?) reads the browser selection (or `sel`, any object with
   rangeCount and getRangeAt) and gives {turn, start, end, text} inside ONE turn, or null. start and end count code
   points into the parsed turn text, as Python slices it, so the server can check text == turn.text[start:end].
   The span rule (at least `min_words` words, or the whole turn, the kit's quote rule) is checked here with the kit's word count and
   again by the server, whose message is shown when it refuses.

   Part 5c (item 38): saving or removing a mark redraws through SS.place.keep: the transcript stays where it was
   on screen, the focus goes to the saved mark's Remove button (or to the next mark after a removal) and the result
   shows beside it. The lock keeps its own status line and focus (2.2 issue 6). Item 37: the "kind" choices of a
   mark are off while "Happened" is chosen, and a line under them says so. */
(function () {
  'use strict';
  var root = typeof window !== 'undefined' ? window : globalThis;
  var SS = root.SS = root.SS || {};
  var TICK = 15;            // seconds of visible time sent per POST /api/coding/tick
  var MAX_TICK = 3600;      // the server's cap per tick
  var SPEAKERS = ['participant', 'interviewer', 'unknown'];
  var CHAR_MAP = { ' ': ' ', ' ': ' ', ' ': ' ', '‘': "'", '’': "'", '“': '"',
    '”': '"', '–': '-', '—': '-', '…': '...' };       // textnorm._CHAR_MAP
  var mounted = null;       // {main, sub} of the last render
  var ctx = null;           // the drawn coding page: {main, data, form, notice, ...}
  var timer = null;         // the visible timer of the open coding
  var justLocked = null;     // pid whose lock the reader has just confirmed: the next draw shows the status line there
  var lockInfo = {};        // pid -> lock reply {sha256, n_marks}, shown after the lock

  function el(doc, tag, cls, text) {
    var n = doc.createElement(tag);
    if (cls) n.setAttribute('class', cls);
    if (text !== undefined) n.textContent = text;
    return n;
  }

  function clear(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
  }

  function docOf(node) {
    return node.ownerDocument || root.document;
  }

  function hasClass(n, c) {
    return !!n && n.nodeType === 1 && typeof n.getAttribute === 'function' &&
      (' ' + (n.getAttribute('class') || '') + ' ').indexOf(' ' + c + ' ') >= 0;
  }

  function nextOf(n) {
    var list = n.parentNode ? n.parentNode.childNodes : [];
    for (var i = 0; i < list.length; i++) if (list[i] === n) return list[i + 1] || null;
    return null;
  }

  function placeAfter(anchor, node) {
    if (!anchor || !anchor.parentNode) return;
    anchor.parentNode.insertBefore(node, nextOf(anchor));
  }

  function cpLen(s) {
    return Array.from(s).length;
  }

  function isInt(v) {
    return typeof v === 'number' && isFinite(v) && Math.floor(v) === v;
  }

  function hms(sec) {
    var s = Math.max(0, Math.floor(sec));
    function two(x) { return (x < 10 ? '0' : '') + x; }
    return two(Math.floor(s / 3600)) + ':' + two(Math.floor((s % 3600) / 60)) + ':' + two(s % 60);
  }

  // The kit's word count (textnorm.tokens): NFKC, its character map, lower case, then [a-z0-9']+ runs with
  // leading and trailing apostrophes stripped.
  function tokens(s) {
    if (typeof s !== 'string' || !s) return [];
    var t = s.normalize ? s.normalize('NFKC') : s;
    t = t.replace(/[\xa0\u2007\u202f\u2018\u2019\u201c\u201d\u2013\u2014\u2026]/g, function (c) { return CHAR_MAP[c]; });
    var runs = t.toLowerCase().match(/[a-z0-9']+/g) || [];
    return runs.filter(function (w) { return w.replace(/^'+|'+$/g, '') !== ''; });
  }
  function wordCount(s) { return tokens(s).length; }
  // The kit's whole-turn exception (provenance.eligible / locate_quote): a span shorter than min_words is still
  // allowed when its word sequence is the word sequence of the whole turn.
  function isWholeTurn(spanText, turnText) {
    var a = tokens(spanText);
    return a.length > 0 && a.join(' ') === tokens(turnText).join(' ');
  }

  // --- selection --------------------------------------------------------------------------------------------------

  function textLen(n) {
    if (n.nodeType === 3) return (n.nodeValue || '').length;
    var total = 0;
    for (var i = 0; i < n.childNodes.length; i++) total += textLen(n.childNodes[i]);
    return total;
  }

  function fullText(n) {
    if (n.nodeType === 3) return n.nodeValue || '';
    var out = '';
    for (var i = 0; i < n.childNodes.length; i++) out += fullText(n.childNodes[i]);
    return out;
  }

  // UTF-16 offset of (node, offset) inside the turn text element `textEl`.
  function offsetIn(textEl, node, offset) {
    var total = 0;
    function walk(n) {
      if (n === node) {
        if (n.nodeType === 3) total += Math.min(offset, (n.nodeValue || '').length);
        else for (var i = 0; i < offset && i < n.childNodes.length; i++) total += textLen(n.childNodes[i]);
        return true;
      }
      if (n.nodeType === 3) {
        total += (n.nodeValue || '').length;
        return false;
      }
      for (var j = 0; j < n.childNodes.length; j++) if (walk(n.childNodes[j])) return true;
      return false;
    }
    return walk(textEl) ? total : null;
  }

  function within(n, ancestor) {
    for (var p = n; p; p = p.parentNode) if (p === ancestor) return true;
    return false;
  }

  // {textEl, wrap, offset} for a range boundary inside one of rootEl's turns, or null.
  function locate(rootEl, node, offset) {
    var textEl = null;
    var wrap = null;
    for (var p = node; p; p = p.parentNode) {
      if (!textEl && hasClass(p, 'ss-coding-text')) textEl = p;
      if (hasClass(p, 'ss-coding-turn')) { wrap = p; break; }
    }
    if (!wrap || !within(wrap, rootEl)) return null;
    if (!textEl) {
      // the boundary sits in the speaker label or on the turn element itself
      for (var i = 0; i < wrap.childNodes.length; i++) if (hasClass(wrap.childNodes[i], 'ss-coding-text')) textEl = wrap.childNodes[i];
      if (!textEl) return null;
      var after = false;
      if (node === wrap) {
        var idx = -1;
        for (var k = 0; k < wrap.childNodes.length; k++) if (wrap.childNodes[k] === textEl) idx = k;
        after = offset > idx;
      }
      return { textEl: textEl, wrap: wrap, offset: after ? textLen(textEl) : 0 };
    }
    var off = offsetIn(textEl, node, offset);
    return off === null ? null : { textEl: textEl, wrap: wrap, offset: off };
  }

  function readSelection(rootEl, sel) {
    var s = sel !== undefined ? sel : (typeof root.getSelection === 'function' ? root.getSelection() : null);
    if (!s || !s.rangeCount || typeof s.getRangeAt !== 'function') return { refused: 'empty' };
    var r = s.getRangeAt(0);
    if (!r || (r.startContainer === r.endContainer && r.startOffset === r.endOffset)) return { refused: 'empty' };
    var a = locate(rootEl, r.startContainer, r.startOffset);
    var b = locate(rootEl, r.endContainer, r.endOffset);
    if (!a || !b) return { refused: 'outside' };
    if (a.textEl !== b.textEl) return { refused: 'cross_turn', at: a.wrap };
    var turn = parseInt(a.textEl.getAttribute('data-turn'), 10);
    if (!isInt(turn) || turn < 0) return { refused: 'outside' };
    var full = fullText(a.textEl);
    var s16 = Math.min(a.offset, b.offset);
    var e16 = Math.max(a.offset, b.offset);
    while (s16 < e16 && /\s/.test(full.charAt(s16))) s16 += 1;
    while (e16 > s16 && /\s/.test(full.charAt(e16 - 1))) e16 -= 1;
    if (s16 >= e16) return { refused: 'empty' };
    var text = full.slice(s16, e16);
    var start = cpLen(full.slice(0, s16));
    return { span: { turn: turn, start: start, end: start + cpLen(text), text: text }, at: a.wrap };
  }

  // --- the visible timer ------------------------------------------------------------------------------------------

  function Timer(pid, seconds) {
    var self = this;
    self.pid = pid;
    self.seconds = seconds;
    self.pending = 0;
    self.display = null;
    self.page = null;
    self.suspended = false;
    self.id = typeof root.setInterval === 'function' ? root.setInterval(function () { self.step(); }, 1000) : null;
  }
  Timer.prototype.attach = function (display, page) {
    this.display = display;
    this.page = page;
    this.suspended = false;
    this.show();
  };
  Timer.prototype.show = function () {
    if (this.display) this.display.textContent = hms(this.seconds);
  };
  Timer.prototype.step = function () {
    if (this.id === null || this.suspended) return;
    if (this.page && this.page.isConnected === false) {
      this.stop();
      return;
    }
    if (root.document && root.document.hidden) return;
    this.seconds += 1;
    this.pending += 1;
    this.show();
    if (this.pending >= TICK) this.flush();
  };
  Timer.prototype.flush = function () {
    var self = this;
    if (!self.pending) return Promise.resolve();
    var s = Math.min(self.pending, MAX_TICK);
    self.pending -= s;
    return SS.api.post('/api/coding/tick', { pid: self.pid, seconds: s }).then(function (doc) {
      if (doc && isInt(doc.seconds)) {
        self.seconds = doc.seconds + self.pending;
        self.show();
      }
    }, function (err) {
      if (!err || (err.status !== 404 && err.status !== 409)) self.pending += s;
    });
  };
  // Stop counting (no more ticks after this).
  Timer.prototype.halt = function () {
    if (this.id !== null && typeof root.clearInterval === 'function') root.clearInterval(this.id);
    this.id = null;
    if (timer === this) timer = null;
  };
  Timer.prototype.stop = function () {
    this.halt();
    return this.flush();
  };

  // --- drawing ----------------------------------------------------------------------------------------------------

  function codingOf(d) {
    d = d && typeof d === 'object' ? d : {};
    var turns = (Array.isArray(d.turns) ? d.turns : []).map(function (t) {
      t = t && typeof t === 'object' ? t : {};
      return { speaker: SPEAKERS.indexOf(t.speaker) >= 0 ? t.speaker : 'unknown',
        text: typeof t.text === 'string' ? t.text : '' };
    });
    var marks = (Array.isArray(d.marks) ? d.marks : []).filter(function (m) {
      return m && isInt(m.turn) && m.turn >= 0 && m.turn < turns.length && isInt(m.start) && isInt(m.end) &&
        m.start >= 0 && m.start < m.end;
    }).map(function (m) {
      return { mark_id: typeof m.mark_id === 'string' ? m.mark_id : '', turn: m.turn, start: m.start, end: m.end,
        text: typeof m.text === 'string' ? m.text : '', status: m.status === 'missing' ? 'missing' : 'happened',
        kind: typeof m.missing_kind === 'string' ? m.missing_kind : '',
        step: typeof m.step === 'string' ? m.step : '', phase: typeof m.phase === 'string' ? m.phase : '' };
    });
    var strs = function (a) { return (Array.isArray(a) ? a : []).filter(function (x) { return typeof x === 'string' && x; }); };
    return { pid: typeof d.pid === 'string' ? d.pid : '', type: typeof d.type === 'string' ? d.type : null,
      turns: turns, marks: marks, seconds: isInt(d.seconds) ? d.seconds : 0, locked: d.locked === true,
      changed: d.changed_after_lock === true, phases: strs(d.phases), kinds: strs(d.missing_kinds),
      minWords: isInt(d.min_words) ? d.min_words : null, limit: typeof d.limit === 'string' ? d.limit : '' };
  }

  function refsOf(d) {
    var list = d && Array.isArray(d.references) ? d.references : [];
    var g = d && d.gate && typeof d.gate === 'object' ? d.gate : {};
    return {
      b: list.filter(function (r) { return r && typeof r.pid === 'string' && r.type === 'B'; }).map(function (r) {
        var c = r.coding && typeof r.coding === 'object' ? r.coding : {};
        return { pid: r.pid, nMarks: isInt(c.n_marks) ? c.n_marks : 0, locked: c.locked === true };
      }),
      waiting: Array.isArray(g.waiting) ? g.waiting.filter(function (p) { return typeof p === 'string'; }) : []
    };
  }

  function kindLabel(kind) {
    var key = 'coding.kind.' + kind.replace(/ /g, '_');
    return SS_has(key) ? SS.t(key) : kind;
  }

  function SS_has(key) {
    var table = root.SS_STRINGS;
    return !!table && Object.prototype.hasOwnProperty.call(table, key);
  }

  // The turn text, cut into plain text nodes and <mark> segments where the researcher's marks fall.
  function turnText(doc, i, text, marks) {
    var p = el(doc, 'p', 'ss-coding-text');
    p.setAttribute('data-turn', String(i));
    var cps = Array.from(text);
    var mine = marks.filter(function (m) { return m.turn === i; });
    var cuts = [0, cps.length];
    mine.forEach(function (m) {
      cuts.push(Math.min(m.start, cps.length));
      cuts.push(Math.min(m.end, cps.length));
    });
    cuts = cuts.sort(function (a, b) { return a - b; }).filter(function (c, k, arr) { return k === 0 || c !== arr[k - 1]; });
    for (var k = 0; k + 1 < cuts.length; k++) {
      var a = cuts[k];
      var b = cuts[k + 1];
      var piece = cps.slice(a, b).join('');
      var over = mine.filter(function (m) { return m.start <= a && m.end >= b; });
      if (!over.length) {
        p.appendChild(doc.createTextNode(piece));
        continue;
      }
      var hasH = over.some(function (m) { return m.status === 'happened'; });
      var hasM = over.some(function (m) { return m.status === 'missing'; });
      var hl = el(doc, 'mark', 'ss-coding-hl');
      hl.setAttribute('data-status', hasH && hasM ? 'both' : (hasM ? 'missing' : 'happened'));
      hl.appendChild(doc.createTextNode(piece));
      p.appendChild(hl);
    }
    return p;
  }

  function transcript(doc, c) {
    var box = el(doc, 'div', 'ss-coding-turns');
    box.setAttribute('aria-label', SS.t('coding.transcript_label', { pid: c.pid }));
    c.turns.forEach(function (t, i) {
      var w = el(doc, 'div', 'ss-coding-turn');
      w.setAttribute('data-speaker', t.speaker);
      w.appendChild(el(doc, 'span', 'ss-coding-speaker', SS.t('coding.speaker.' + t.speaker)));
      w.appendChild(turnText(doc, i, t.text, c.marks));
      box.appendChild(w);
    });
    return box;
  }

  function markItem(doc, m, open) {
    var li = el(doc, 'li', 'ss-coding-mark');
    li.setAttribute('data-status', m.status);
    var head = el(doc, 'div', 'ss-coding-mark-head');
    head.appendChild(el(doc, 'span', 'ss-coding-mark-step', m.step));
    head.appendChild(el(doc, 'span', 'ss-coding-mark-status', m.status === 'missing'
      ? (m.kind ? SS.t('coding.status_missing_kind', { kind: kindLabel(m.kind) }) : SS.t('coding.status_missing'))
      : SS.t('coding.status_happened')));
    li.appendChild(head);
    li.appendChild(el(doc, 'p', 'ss-coding-mark-quote', m.text));
    var foot = el(doc, 'div', 'ss-coding-mark-foot');
    foot.appendChild(el(doc, 'span', 'mono', SS.t('coding.mark_where', { turn: String(m.turn + 1) })));
    if (open) {
      var rm = el(doc, 'button', 'ss-coding-remove', SS.t('coding.remove'));
      rm.setAttribute('type', 'button');
      rm.setAttribute('aria-label', SS.t('coding.remove_label', { step: m.step }));
      rm.addEventListener('click', function () {
        rm.disabled = true;
        SS.api.post('/api/coding/unmark', { pid: ctx.data.pid, mark_id: m.mark_id }).then(function () {
          return SS.coding.refresh({ message: SS.t('coding.removed', { step: m.step }) });
        }, function (err) {
          rm.disabled = false;
          ctx.marksError.textContent = (err && err.message) || SS.t('api.failed');
        });
      });
      foot.appendChild(rm);
    }
    li.appendChild(foot);
    return li;
  }

  function marksPanel(doc, c, open) {
    var panel = el(doc, 'aside', 'ss-coding-marks');
    panel.setAttribute('aria-labelledby', 'ss-coding-marks-title');
    var head = el(doc, 'div', 'ss-coding-marks-head');
    var h2 = el(doc, 'h2', 'ss-coding-marks-title', SS.t('coding.marks_title'));
    h2.setAttribute('id', 'ss-coding-marks-title');
    head.appendChild(h2);
    head.appendChild(el(doc, 'span', 'mono', SS.tn('coding.marks_count', c.marks.length, { n: String(c.marks.length) })));
    panel.appendChild(head);
    panel.appendChild(el(doc, 'p', 'ss-coding-marks-rule', SS.t('coding.marks_rule')));
    var errorBox = el(doc, 'p', 'ss-setup-error');
    errorBox.setAttribute('role', 'alert');
    panel.appendChild(errorBox);
    var phases = c.phases.slice();
    c.marks.forEach(function (m) { if (phases.indexOf(m.phase) < 0) phases.push(m.phase); });
    phases.forEach(function (ph) {
      panel.appendChild(el(doc, 'h3', 'ss-coding-phase', ph));
      var inPhase = c.marks.filter(function (m) { return m.phase === ph; })
        .sort(function (a, b) { return (a.turn - b.turn) || (a.start - b.start); });
      if (!inPhase.length) {
        panel.appendChild(el(doc, 'p', 'ss-coding-empty', SS.t('coding.no_marks')));
        return;
      }
      var ul = el(doc, 'ul', 'ss-coding-mark-list');
      inPhase.forEach(function (m) { ul.appendChild(markItem(doc, m, open)); });
      panel.appendChild(ul);
    });
    panel.appendChild(el(doc, 'p', 'ss-coding-marks-note', SS.t('coding.phases_note')));
    return { panel: panel, error: errorBox };
  }

  var uid = 0;

  function radio(doc, name, value, labelText) {
    uid += 1;
    var id = name + '-' + uid;
    var lab = el(doc, 'label', 'ss-coding-choice');
    lab.setAttribute('for', id);
    var input = el(doc, 'input');
    input.setAttribute('type', 'radio');
    input.setAttribute('name', name);
    input.setAttribute('id', id);
    input.setAttribute('value', value);
    lab.appendChild(input);
    lab.appendChild(el(doc, 'span', '', labelText));
    return { label: lab, input: input };
  }

  // The mark form, shown after the turn that holds the selected passage.
  function markForm(doc, c) {
    uid += 1;
    var n = uid;
    var f = el(doc, 'div', 'ss-coding-form');
    f.setAttribute('role', 'group');
    f.setAttribute('aria-labelledby', 'ss-coding-form-title-' + n);
    f.hidden = true;
    var title = el(doc, 'p', 'ss-coding-form-title');
    title.setAttribute('id', 'ss-coding-form-title-' + n);
    f.appendChild(title);
    var quote = el(doc, 'blockquote', 'ss-coding-form-quote');
    f.appendChild(quote);

    var stepWrap = el(doc, 'div', 'ss-coding-field');
    var stepLabel = el(doc, 'label', 'ss-setup-label', SS.t('coding.step_label'));
    stepLabel.setAttribute('for', 'ss-coding-step-' + n);
    var step = el(doc, 'input', 'ss-setup-input');
    step.setAttribute('id', 'ss-coding-step-' + n);
    step.setAttribute('name', 'step');
    step.setAttribute('type', 'text');
    step.setAttribute('autocomplete', 'off');
    step.setAttribute('maxlength', '200');
    step.setAttribute('list', 'ss-coding-steps-' + n);
    step.setAttribute('aria-describedby', 'ss-coding-step-hint-' + n);
    var list = el(doc, 'datalist');
    list.setAttribute('id', 'ss-coding-steps-' + n);
    var hint = el(doc, 'p', 'ss-setup-hint', SS.t('coding.step_hint'));
    hint.setAttribute('id', 'ss-coding-step-hint-' + n);
    stepWrap.appendChild(stepLabel);
    stepWrap.appendChild(step);
    stepWrap.appendChild(list);
    stepWrap.appendChild(hint);
    f.appendChild(stepWrap);

    var phaseWrap = el(doc, 'div', 'ss-coding-field');
    var phaseLabel = el(doc, 'label', 'ss-setup-label', SS.t('coding.phase_label'));
    phaseLabel.setAttribute('for', 'ss-coding-phase-' + n);
    var phase = el(doc, 'select', 'ss-setup-input');
    phase.setAttribute('id', 'ss-coding-phase-' + n);
    phase.setAttribute('name', 'phase');
    c.phases.forEach(function (ph) {
      var o = el(doc, 'option', '', ph);
      o.setAttribute('value', ph);
      phase.appendChild(o);
    });
    phase.value = c.phases.length ? c.phases[0] : '';
    phaseWrap.appendChild(phaseLabel);
    phaseWrap.appendChild(phase);
    f.appendChild(phaseWrap);

    var markSet = el(doc, 'fieldset', 'ss-coding-set');
    markSet.appendChild(el(doc, 'legend', 'ss-setup-label', SS.t('coding.mark_label')));
    var hap = radio(doc, 'ss-coding-status-' + n, 'happened', SS.t('coding.happened'));
    var mis = radio(doc, 'ss-coding-status-' + n, 'missing', SS.t('coding.missing'));
    hap.label.setAttribute('data-status', 'happened');
    mis.label.setAttribute('data-status', 'missing');
    markSet.appendChild(hap.label);
    markSet.appendChild(mis.label);
    f.appendChild(markSet);

    var kindSet = el(doc, 'fieldset', 'ss-coding-set');
    kindSet.appendChild(el(doc, 'legend', 'ss-setup-label', SS.t('coding.kind_label')));
    var kinds = [radio(doc, 'ss-coding-kind-' + n, '', SS.t('coding.kind.none'))];
    c.kinds.forEach(function (k) { kinds.push(radio(doc, 'ss-coding-kind-' + n, k, kindLabel(k))); });
    kinds.forEach(function (k) { kindSet.appendChild(k.label); });
    f.appendChild(kindSet);

    var errorBox = el(doc, 'p', 'ss-setup-error');
    errorBox.setAttribute('role', 'alert');
    f.appendChild(errorBox);
    var actions = el(doc, 'div', 'ss-coding-actions');
    var cancel = el(doc, 'button', '', SS.t('coding.cancel'));
    cancel.setAttribute('type', 'button');
    var save = el(doc, 'button', 'primary', SS.t('coding.save'));
    save.setAttribute('type', 'button');
    actions.appendChild(cancel);
    actions.appendChild(save);
    f.appendChild(actions);

    // item 37: the kinds are off while "Happened" is chosen, and a line says why
    var kindWhy = el(doc, 'p', 'ss-setup-hint ss-coding-kind-why', SS.t('coding.kind_why'));
    kindWhy.setAttribute('id', 'ss-coding-kind-why-' + n);
    kindSet.appendChild(kindWhy);
    function syncKinds() {
      var on = mis.input.checked;
      kinds.forEach(function (k) {
        k.input.disabled = !on;
        if (on) {
          k.label.removeAttribute('data-disabled');
          k.input.removeAttribute('aria-describedby');
        } else {
          k.label.setAttribute('data-disabled', 'true');
          k.input.setAttribute('aria-describedby', kindWhy.getAttribute('id'));
        }
      });
      kindWhy.hidden = on;
    }
    hap.input.addEventListener('change', syncKinds);
    mis.input.addEventListener('change', syncKinds);

    var seq = 0;
    var pending = null;
    step.addEventListener('input', function () {
      if (pending !== null && typeof root.clearTimeout === 'function') root.clearTimeout(pending);
      var mine = ++seq;
      pending = root.setTimeout(function () {
        pending = null;
        SS.api.get('/api/coding/suggest', { prefix: step.value }).then(function (doc2) {
          if (mine !== seq) return;
          clear(list);
          (doc2 && Array.isArray(doc2.names) ? doc2.names : []).forEach(function (name) {
            if (typeof name !== 'string') return;
            var o = el(doc, 'option');
            o.setAttribute('value', name);
            list.appendChild(o);
          });
        }, function () {});
      }, 200);
    });

    var api = {
      node: f,
      open: function (span, at, words) {
        api.span = span;
        title.textContent = SS.t('coding.form_title', { turn: String(span.turn + 1), words: String(words) });
        quote.textContent = span.text;
        errorBox.textContent = '';
        hap.input.checked = true;
        mis.input.checked = false;
        kinds[0].input.checked = true;
        for (var i = 1; i < kinds.length; i++) kinds[i].input.checked = false;
        syncKinds();
        placeAfter(at, f);
        f.hidden = false;
        if (typeof step.focus === 'function') step.focus();
      },
      close: function () {
        api.span = null;
        f.hidden = true;
      },
      span: null
    };

    cancel.addEventListener('click', function () { api.close(); });
    save.addEventListener('click', function () {
      var span = api.span;
      if (!span) return;
      var name = String(step.value || '').replace(/\s+/g, ' ').trim();
      if (!name) {
        errorBox.textContent = SS.t('coding.error.bad_step');
        return;
      }
      var body = { pid: c.pid, turn: span.turn, start: span.start, end: span.end, text: span.text,
        status: mis.input.checked ? 'missing' : 'happened', step: name, phase: phase.value };
      if (mis.input.checked) {
        kinds.forEach(function (k) { if (k.input.checked && k.input.getAttribute('value')) body.missing_kind = k.input.getAttribute('value'); });
      }
      errorBox.textContent = '';
      save.disabled = true;
      SS.api.post('/api/coding/mark', body).then(function () {
        save.disabled = false;
        step.value = '';
        api.close();
        if (typeof root.getSelection === 'function') {
          var s = root.getSelection();
          if (s && typeof s.removeAllRanges === 'function') s.removeAllRanges();
        }
        // the form is closed after the redraw: the focus goes to the new mark's Remove button, the result beside it
        var label = SS.t('coding.remove_label', { step: name });
        return SS.coding.refresh({ message: SS.t('coding.saved', { step: name }), target: function (main) {
          var hit = null;
          (function walk(n) {
            var c = n && n.children;
            for (var i = 0; c && i < c.length && !hit; i++) {
              if (c[i].tagName === 'BUTTON' && c[i].getAttribute('aria-label') === label) hit = c[i];
              else walk(c[i]);
            }
          })(main);
          return hit;
        } });
      }, function (err) {
        save.disabled = false;
        errorBox.textContent = (err && err.message) || SS.t('api.failed');
      });
    });
    return api;
  }

  function lockBar(doc, c) {
    var bar = el(doc, 'section', 'ss-coding-lockbar');
    var text = el(doc, 'div', 'ss-coding-lockbar-text');
    text.appendChild(el(doc, 'p', 'ss-coding-lockbar-title', SS.t('coding.lock_title')));
    bar.appendChild(text);
    var lockBtn = el(doc, 'button', 'ss-coding-lock', SS.t('coding.lock_button'));
    lockBtn.setAttribute('type', 'button');
    bar.appendChild(lockBtn);
    var confirm = null;
    lockBtn.addEventListener('click', function () {
      if (confirm) return;
      confirm = el(doc, 'div', 'ss-coding-confirm');
      confirm.setAttribute('role', 'alertdialog');
      confirm.setAttribute('aria-labelledby', 'ss-coding-confirm-title');
      var h = el(doc, 'p', 'ss-coding-confirm-title', SS.t('coding.confirm_title', { pid: c.pid }));
      h.setAttribute('id', 'ss-coding-confirm-title');
      confirm.appendChild(h);
      confirm.appendChild(el(doc, 'p', '', SS.t('coding.confirm_body')));
      if (c.limit) confirm.appendChild(el(doc, 'p', 'ss-coding-limit', SS.t('coding.limit_label') + ' ' + c.limit));
      var errorBox = el(doc, 'p', 'ss-setup-error');
      errorBox.setAttribute('role', 'alert');
      var row = el(doc, 'div', 'ss-coding-actions');
      var no = el(doc, 'button', '', SS.t('coding.confirm_no'));
      no.setAttribute('type', 'button');
      var yes = el(doc, 'button', 'ss-coding-lock', SS.t('coding.confirm_yes'));
      yes.setAttribute('type', 'button');
      row.appendChild(no);
      row.appendChild(yes);
      confirm.appendChild(row);
      confirm.appendChild(errorBox);
      bar.appendChild(confirm);
      lockBtn.hidden = true;
      if (typeof yes.focus === 'function') yes.focus();
      no.addEventListener('click', function () {
        bar.removeChild(confirm);
        confirm = null;
        lockBtn.hidden = false;
      });
      yes.addEventListener('click', function () {
        yes.disabled = true;
        no.disabled = true;
        errorBox.textContent = '';
        var t = timer && timer.pid === c.pid ? timer : null;
        if (t) t.halt();
        (t ? t.flush() : Promise.resolve()).then(function () {
          return SS.api.post('/api/coding/lock', { pid: c.pid });
        }).then(function (doc2) {
          lockInfo[c.pid] = { sha256: doc2 && typeof doc2.sha256 === 'string' ? doc2.sha256 : '',
            n: doc2 && isInt(doc2.n_marks) ? doc2.n_marks : c.marks.length };
          justLocked = c.pid;
          return SS.coding.refresh();
        }, function (err) {
          yes.disabled = false;
          no.disabled = false;
          errorBox.textContent = (err && err.message) || SS.t('api.failed');
          if (t && !timer) {
            timer = new Timer(c.pid, t.seconds + t.pending);
            timer.attach(t.display, t.page);
          }
        });
      });
    });
    return bar;
  }

  function navLink(doc, target, key) {
    if (target === undefined) {
      var off = el(doc, 'span', 'ss-coding-navlink', SS.t(key));
      off.setAttribute('aria-disabled', 'true');
      return off;
    }
    var a = el(doc, 'a', 'ss-coding-navlink', SS.t(key));
    a.setAttribute('href', '#coding/' + encodeURIComponent(target));
    return a;
  }

  function header(doc, c, refs) {
    var head = el(doc, 'header', 'ss-coding-header');
    var titles = el(doc, 'div', 'ss-coding-titles');
    titles.appendChild(el(doc, 'p', 'ss-kicker', SS.t('coding.kicker')));
    var h1 = el(doc, 'h1', 'ss-coding-title', SS.t('coding.heading') + ' ');
    h1.appendChild(el(doc, 'span', 'mono ss-coding-pid', c.pid));
    titles.appendChild(h1);
    head.appendChild(titles);
    var pids = refs.b.map(function (r) { return r.pid; });
    var k = pids.indexOf(c.pid);
    if (k >= 0 && pids.length > 1) {
      var nav = el(doc, 'nav', 'ss-coding-nav');
      nav.setAttribute('aria-label', SS.t('coding.nav_label'));
      nav.appendChild(navLink(doc, k > 0 ? pids[k - 1] : undefined, 'coding.prev'));
      nav.appendChild(el(doc, 'span', 'mono ss-coding-position',
        SS.t('coding.position', { k: String(k + 1), n: String(pids.length) })));
      nav.appendChild(navLink(doc, k + 1 < pids.length ? pids[k + 1] : undefined, 'coding.next'));
      head.appendChild(nav);
    }
    return head;
  }

  function blindBanner(doc, c) {
    var b = el(doc, 'div', 'ss-coding-blind');
    var chips = el(doc, 'ul', 'ss-coding-chips');
    ['coding.hidden_ai', 'coding.hidden_no_interview', 'coding.hidden_results'].forEach(function (key) {
      chips.appendChild(el(doc, 'li', 'ss-coding-chip', SS.t(key)));
    });
    b.appendChild(chips);
    var words = el(doc, 'div', 'ss-coding-blind-text');
    words.appendChild(el(doc, 'p', '', SS.t('coding.blind')));
    if (c.limit) {
      var lim = el(doc, 'p', 'ss-coding-limit');
      lim.appendChild(el(doc, 'strong', '', SS.t('coding.limit_label')));
      lim.appendChild(doc.createTextNode(' ' + c.limit));
      words.appendChild(lim);
    }
    b.appendChild(words);
    return b;
  }

  function gateLine(doc, refs) {
    return el(doc, 'p', 'ss-coding-gate', refs.waiting.length
      ? SS.t('coding.gate_waiting', { pids: refs.waiting.join(', ') }) : SS.t('coding.gate_locked'));
  }

  function drawPick(main, refs) {
    var doc = docOf(main);
    clear(main);
    var page = el(doc, 'div', 'ss-coding');
    page.appendChild(el(doc, 'p', 'ss-kicker', SS.t('coding.kicker')));
    page.appendChild(el(doc, 'h1', 'ss-coding-title', SS.t('coding.pick_title')));
    if (!refs.b.length) {               // nothing to code (scout F4): say so, and point to the run
      page.appendChild(el(doc, 'p', 'ss-shell-note', SS.t('coding.none')));
      var go = el(doc, 'a', 'ss-setup-back ss-coding-next', SS.t('coding.next_run'));
      go.setAttribute('href', '#instructions');
      page.appendChild(go);
      main.appendChild(page);
      return;
    }
    page.appendChild(el(doc, 'p', 'ss-setup-intro', SS.t('coding.pick_intro')));
    var ul = el(doc, 'ul', 'ss-coding-pick');
    refs.b.forEach(function (r) {
      var li = el(doc, 'li', 'ss-coding-pick-item');
      li.setAttribute('data-locked', r.locked ? 'true' : 'false');
      var a = el(doc, 'a', 'mono', r.pid);
      a.setAttribute('href', '#coding/' + encodeURIComponent(r.pid));
      li.appendChild(a);
      li.appendChild(el(doc, 'span', 'ss-coding-pick-state',
        SS.tn(r.locked ? 'coding.state_locked' : 'coding.state_open', r.nMarks, { n: String(r.nMarks) })));
      ul.appendChild(li);
    });
    page.appendChild(ul);
    page.appendChild(gateLine(doc, refs));
    main.appendChild(page);
  }

  function drawCoding(main, c, refs) {
    var doc = docOf(main);
    clear(main);
    ctx = null;
    var page = el(doc, 'div', 'ss-coding');
    page.appendChild(header(doc, c, refs));
    if (c.type !== 'B' && !c.locked) {
      page.appendChild(el(doc, 'p', 'ss-shell-note', SS.t('coding.not_b')));
      var go = el(doc, 'a', 'ss-setup-back', SS.t('coding.go_reference'));
      go.setAttribute('href', '#reference');
      page.appendChild(go);
      main.appendChild(page);
      return;
    }
    var open = !c.locked;
    page.appendChild(blindBanner(doc, c));

    var time = el(doc, 'div', 'ss-coding-time');
    time.appendChild(el(doc, 'span', 'ss-coding-time-label', SS.t('coding.time_label')));
    var clock = el(doc, 'span', 'mono ss-coding-clock', hms(c.seconds));
    clock.setAttribute('role', 'timer');
    time.appendChild(clock);
    time.appendChild(el(doc, 'span', 'ss-coding-time-note', SS.t('coding.time_note')));
    page.appendChild(time);

    if (!open) {
      var done = el(doc, 'div', 'ss-coding-locked');
      done.appendChild(el(doc, 'p', 'ss-coding-locked-title', SS.t('coding.locked_title')));
      var info = lockInfo[c.pid];
      if (info && info.sha256) {
        done.appendChild(el(doc, 'p', 'mono ss-coding-record',
          SS.tn('coding.locked_record', info.n, { sha256: info.sha256, n: String(info.n) })));
      }
      if (c.changed) done.appendChild(el(doc, 'p', 'ss-setup-error', SS.t('coding.error.coding_changed')));
      done.appendChild(gateLine(doc, refs));
      page.appendChild(done);
    }

    var grid = el(doc, 'div', 'ss-coding-grid');
    var reader = el(doc, 'section', 'ss-coding-reader');
    var top = el(doc, 'div', 'ss-coding-reader-top');
    if (open) top.appendChild(el(doc, 'p', 'ss-coding-how', SS.t('coding.how')));
    var legend = el(doc, 'p', 'ss-coding-legend');
    var lh = el(doc, 'span', 'ss-coding-key');
    lh.setAttribute('data-status', 'happened');
    lh.textContent = SS.t('coding.legend_happened');
    var lm = el(doc, 'span', 'ss-coding-key');
    lm.setAttribute('data-status', 'missing');
    lm.textContent = SS.t('coding.legend_missing');
    legend.appendChild(lh);
    legend.appendChild(lm);
    top.appendChild(legend);
    reader.appendChild(top);
    var turns = transcript(doc, c);
    reader.appendChild(turns);
    grid.appendChild(reader);
    var marks = marksPanel(doc, c, open);
    grid.appendChild(marks.panel);
    page.appendChild(grid);

    // 2.2 issue 6: the lock bar was at the foot of the page, and after "Lock it" the confirmation above the transcript was
    // out of sight. A status line takes the bar's place: "Locked; logged with hash 3f9a1c2b7d4e…", or "Locked and
    // logged" when the reply carried no hash. It exists only in the session that locked the coding.
    var lockStatus = null;
    if (!open && lockInfo[c.pid]) {
      var sha = lockInfo[c.pid].sha256;
      lockStatus = el(doc, 'p', 'ss-setup-ok ss-coding-lock-status',
        sha ? SS.t('coding.lock_status', { hash: sha.slice(0, 12) + '\u2026' }) : SS.t('coding.lock_status_nohash'));
      lockStatus.setAttribute('role', 'status');
      lockStatus.setAttribute('tabindex', '-1');
      page.appendChild(lockStatus);
    }

    var form = null;
    var notice = el(doc, 'p', 'ss-coding-notice');
    notice.setAttribute('role', 'status');
    if (open) {
      form = markForm(doc, c);
      turns.appendChild(form.node);
      page.appendChild(lockBar(doc, c));
      turns.addEventListener('mouseup', function () { SS.coding.capture(main); });
      turns.addEventListener('keyup', function () { SS.coding.capture(main); });
    }
    main.appendChild(page);
    if (lockStatus && justLocked === c.pid) {
      // the focused "Lock it" button is gone: keep keyboard focus here and bring the line into sight
      justLocked = null;
      if (typeof lockStatus.focus === 'function') {
        try { lockStatus.focus({ preventScroll: true }); } catch (e) { lockStatus.focus(); }
      }
      if (typeof lockStatus.scrollIntoView === 'function') {
        var calm = false;
        try { calm = !!(root.matchMedia && root.matchMedia('(prefers-reduced-motion: reduce)').matches); } catch (e) { calm = false; }
        try { lockStatus.scrollIntoView({ block: 'nearest', behavior: calm ? 'auto' : 'smooth' }); }
        catch (e) { lockStatus.scrollIntoView(); }
      }
    }
    ctx = { main: main, data: c, form: form, notice: notice, marksError: marks.error };

    if (open) {
      if (!timer || timer.pid !== c.pid) {
        if (timer) timer.stop();
        timer = new Timer(c.pid, c.seconds);
      }
      timer.attach(clock, page);
    } else if (timer && timer.pid === c.pid) {
      timer.stop();
    }
  }

  function showNotice(message, at) {
    ctx.notice.textContent = message;
    if (at) placeAfter(at, ctx.notice);
  }

  SS.coding = {
    wordCount: wordCount,
    readSelection: readSelection,
    // {turn, start, end, text} for a selection inside one turn of `rootEl`'s transcript, else null.
    selection: function selection(rootEl, sel) {
      var r = readSelection(rootEl, sel);
      return r.span || null;
    },
    // Reads the selection and opens the mark form for it, or says why it cannot be marked.
    capture: function capture(rootEl, sel) {
      if (!ctx || !ctx.form || ctx.main !== rootEl) return null;
      var r = readSelection(rootEl, sel);
      if (r.refused === 'cross_turn') {
        ctx.form.close();
        showNotice(SS.t('coding.cross_turn'), r.at);
        return null;
      }
      if (!r.span) return null;
      var words = wordCount(r.span.text);
      var turnObj = ctx.data.turns[r.span.turn];
      if (ctx.data.minWords !== null && words < ctx.data.minWords &&
          !(turnObj && isWholeTurn(r.span.text, turnObj.text))) {
        ctx.form.close();
        showNotice(SS.t('coding.error.span_too_short', { min_words: String(ctx.data.minWords) }), r.at);
        return null;
      }
      showNotice('', null);
      if (ctx.notice.parentNode) ctx.notice.parentNode.removeChild(ctx.notice);
      ctx.form.open(r.span, r.at, words);
      return r.span;
    },
    // The shell's render: #coding lists the B transcripts, #coding/<pid> opens one.
    render: function render(main, sub) {
      var pid = '';
      try {
        pid = decodeURIComponent(String(sub || '').split('/')[0]);
      } catch (e) {
        pid = '';
      }
      // the same visit keeps its object (refresh's still() compares it); each draw gets its own number, so an older
      // reply never draws over a newer one
      if (!mounted || mounted.main !== main || mounted.sub !== sub) {
        SS.place.forget(main);
        mounted = { main: main, sub: sub, draw: 0 };
      }
      var visit = mounted;
      var draw = ++visit.draw;
      SS.place.watch(main);
      if (timer) {
        if (timer.pid !== pid) timer.stop(); else timer.suspended = true;
      }
      if (!main.firstChild) main.appendChild(el(docOf(main), 'p', 'ss-shell-note', SS.t('shell.loading')));
      var refsP = SS.api.get('/api/references');
      var codingP = pid ? SS.api.get('/api/coding', { pid: pid }) : Promise.resolve(null);
      return Promise.all([refsP, codingP]).then(function (got) {
        if (mounted !== visit || visit.draw !== draw) return;
        if (!pid) drawPick(main, refsOf(got[0]));
        else drawCoding(main, codingOf(got[1]), refsOf(got[0]));
      }, function (err) {
        if (mounted !== visit || visit.draw !== draw) return;
        if (timer && timer.pid === pid) timer.stop();
        var doc = docOf(main);
        clear(main);
        main.appendChild(el(doc, 'p', 'ss-shell-note', (err && err.message) || SS.t('api.failed')));
        var go = el(doc, 'a', 'ss-setup-back', SS.t(err && (err.code === 'no_project' ||
          err.code === 'sync_folder_unacknowledged') ? 'reference.go_setup' : 'coding.go_reference'));
        go.setAttribute('href', err && (err.code === 'no_project' || err.code === 'sync_folder_unacknowledged')
          ? '#setup' : '#reference');
        main.appendChild(go);
      });
    },
    // Fetch again and redraw in place (after a mark, an unmark or the lock). With opts (a mark saved or removed) the
    // place is kept (item 38): opts.message beside the control, opts.target(main) the control to focus.
    refresh: function refresh(opts) {
      if (!mounted) return Promise.resolve();
      var m = mounted;
      if (!opts) return SS.coding.render(m.main, m.sub);
      return SS.place.keep(m.main, function () { return SS.coding.render(m.main, m.sub); }, {
        message: opts.message, error: opts.error,
        target: typeof opts.target === 'function' ? function () { return opts.target(m.main); } : undefined,
        still: function () { return mounted === m; }
      });
    }
  };

  if (SS.shell && typeof SS.shell.register === 'function') {
    SS.shell.register('coding', { order: 30, titleKey: 'coding.title', render: SS.coding.render });
  }
})();
