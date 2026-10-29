/* SS.gloss: a small info marker beside technical terms (design feedback item 32). Classic script; needs core.js
   (SS.t) at run time, so it may load before or after it.

   The definitions live in en.json as glossary.<key>. TERMS below says which words name which key. A page section is the
   run of content from one heading (h1, h2 or h3) up to the next one, inside the scanned root; content before the first
   heading is the root's leading section. A term gets a marker only at its first appearance in each section, right after
   the term, so lists, tables and details do not start a new section. A paragraph or list item (the closest p, li, dd,
   dt, td, th, label, caption, figcaption, blockquote or h2 to h6) holds at most two markers, and a short text (under 80
   characters) gets at most one. A term in a heading (h2 to h6) is left unmarked when the first paragraph after that
   heading names the same term; the paragraph gets the marker then. A term inside a label gets a <span role="button">
   marker at the end of the label, since a button inside a label is not valid HTML. Text inside links, navigation,
   buttons, summaries, code, the page title, map boxes, quotes, live regions and hidden parts is never marked.
   Pages are drawn by script and redrawn often, so SS.gloss.watch(root) re-reads only the parts that change; the
   section of a changed part is found by walking back through the page to the nearest heading, so a redraw does not
   add a second marker for a term already marked earlier in its section.

   The marker is <button class="ss-gloss" aria-label="Meaning of <term>" aria-describedby=tip>; the tip is
   <span role="tooltip" hidden> right after it, so a screen reader reads the definition when the marker is focused.
   The tip shows on mouse hover (and stays while the pointer moves onto it), on keyboard focus, and on click or tap;
   a second click or tap, a tap elsewhere, or Escape closes it. Nothing is hover-only. While open, the tip is moved
   under <body> with position: fixed, placed below the marker (above when there is no room), inside the window with a
   16px gutter, and never over the marker. Colours come from tokens.css (light and dark). */
(function () {
  'use strict';
  var root = typeof window !== 'undefined' ? window : globalThis;
  var SS = root.SS = root.SS || {};
  var hasOwn = Object.prototype.hasOwnProperty;

  // [key, pattern], in priority order: when two patterns cover the same words, the one listed earlier wins, so a
  // longer phrase is listed before the shorter one inside it. A pattern with whole: true must be the whole text of
  // its text node (the profile's one-word labels "Kept", "Missing"). \x27 is an apostrophe.
  var TERMS = [
    ['attested_recall', /\battested recall\b/i],
    ['not_attested', /\bnot attested by (?:its|their) own quotes?\b/i],
    ['not_attested', /\bnot backed by its own quote\b/i],
    ['attested', /\bbacked by (?:its|their) own quotes?\b/i],
    ['attested', /\battested\b/i],
    ['listening_added', /\bwhat (?:did )?listening add(?:ed)?\b/i],
    ['listening_added', /\blistening (?:to the interview )?added\b/i],
    ['no_interview_script', /\bno-interview scripts?\b/i],
    ['no_interview_map', /\bno-interview maps?\b/i],
    ['no_interview_map', /\bthe same model, without the interview\b/i],
    ['lexical_floor', /\blexical floor\b/i],
    ['lexical_matcher', /\blexical matcher\b/i],
    ['lexical_matcher', /\bword overlap\b/i],
    ['primary_setting', /\bstrict matching rule\b/i],
    ['matcher_setting', /\b(?:matcher|match) settings?\b/i],
    ['matcher_setting', /\bmatching rule\b/i],
    ['primary_setting', /\bprimary (?:matcher )?setting\b/i],
    ['lenient_setting', /\blenient(?: setting)?\b/i],
    ['second_family', /\bsecond(?: judge)? family\b/i],
    ['model_family', /\b(?:model )?famil(?:y|ies)\b/i],
    ['reconstructor', /\breconstructor\b/i],
    ['reconstructor', /\bmapping model\b/i],
    ['judge', /\bjudges?\b/i],
    ['temperature', /\btemperature\b/i],
    ['canonical_names', /\b(?:canonical|standard) names\b/i],
    ['quote_anchoring', /\bquote anchoring\b/i],
    ['quote_anchoring', /\bunanchored\b/i],
    ['modality', /\bmodality\b/i],
    ['mapping_talk', /\bmapping talk\b/i],
    ['official_pathway', /\bofficial pathway\b/i],
    ['reference', /\bin-transcript endorsed steps?\b/i],
    ['endorsed_map', /\binterviewer-drawn, participant-endorsed map\b/i],
    ['endorsed_map', /\b(?:participant-)?endorsed (?:maps?|steps?)\b/i],
    ['ai_map', /\bAI(?:\x27s)? maps?\b/i],
    ['flattened', /\bflattened(?: ideals?)?\b/i],
    ['member_check', /\bmember check(?:s|ing)?\b/i],          // council 30 (seat 02 F5)
    ['wished_for', /\bwished[- ]for\b/i],
    ['wished_for', /\bnever came\b/i],
    ['wished_for', /\bideals?\b/i],
    ['missing_rule', /\bmissing rule\b/i],
    ['missing', /\b(?:marked|drawn)(?: as)? missing\b/i],
    ['missing', /\bmissing marks?\b/i],
    ['missing', /^missing$/i, true],
    ['kept', /^kept$/i, true],
    ['coarsened', /\bcoarsened\b/i],
    ['omitted', /\bomitted\b/i],
    ['omitted', /\bstill left out\b/i],
    ['recall', /\brecall\b/i],
    ['precision', /\bprecision\b/i],
    ['slant', /\bslant\b/i],
    ['slant', /^\s*pull\b/i],
    ['instrument_error', /\binstrument error\b/i],
    ['floor', /\bfloors?\b/i],
    ['interval', /\b9[05]% (?:one-sided )?(?:interval|bound)\b/i],
    ['band', /\bbands?\b/i],
    ['pooled', /\bpooled (?:point|ratio|over)\b/i],
    ['pooling', /\bpool(?:s|ing)? profiles\b/i],
    ['pooling', /\bprofiles pooled\b/i],
    ['pooling', /\bpooling\b/i],
    ['ledger', /\btwo readings of one transcript\b/i],
    ['ledger', /\bledger\b/i],
    ['reference_type', /\breference types?\b/i],
    ['profile', /\bprofiles?\b/i],
    ['declaration_log', /\bdeclaration[- ]logs?\b/i],
    ['chain_head', /\bchain heads?\b/i],
    ['chain_head', /:\s*head\b/i],
    ['hash_chain', /\bhash[- ]chain(?:ed|s)?\b/i],
    ['hash_chain', /\bgate[- ]chain\b/i],
    ['gate', /\bgate-cleared\b/i],
    ['gate', /\bthe gate\b/i],
    ['app_log', /\bapp log\b/i],
    ['speaker_labels', /\bspeaker labels?\b/i],
    ['pairing', /\bpairing\b/i],
    ['model_check', /\bmodel check\b/i],
    ['opt_in', /\ballow a machine on your network\b/i],
    ['opt_in', /\bnetwork opt-in\b/i],
    ['declaration', /\bdeclarations?\b/i],
    ['synthetic', /\bsynthetic (?:interviews?|corpus|case)\b/i],
    ['trap', /\btraps?\b/i],
    ['oracle', /\boracle\b/i],
    ['instruction_ladder', /\binstruction ladder\b/i],
    ['instruction_ladder', /\binstructions given to the AI\b/i],
    ['instruction_ladder', /\b(?:prompt )?rungs\b/i],
    ['instruction_ladder', /\bper rung\b/i],
    ['rung1', /\brung 1\b/i],
    ['rung2', /\brung 2\b/i],
    ['remedy', /\brung 3\b/i],
    ['remedy', /\bremedy\b/i],
    ['placebo', /\bplacebo\b/i]
  ];

  var GLOBAL = TERMS.map(function (t) { return new RegExp(t[1].source, 'gi'); });

  // Never marked: these elements and everything inside them.
  var SKIP_TAGS = { CODE: 1, PRE: 1, KBD: 1, SAMP: 1, TEXTAREA: 1, SELECT: 1, OPTION: 1, BUTTON: 1, SUMMARY: 1,
    A: 1, H1: 1, NAV: 1, STYLE: 1, NOSCRIPT: 1, TITLE: 1, SVG: 1, TEXT: 1 };
  var SKIP_CLASSES = ['ss-gloss', 'ss-gloss-tip', 'ss-visually-hidden', 'ss-item', 'ss-box-status', 'ss-x-box',
    'ss-x-transcript', 'ss-x-quote-wrap', 'ss-x-band', 'ss-x-tabs', 'ss-band', 'pf-rests', 'mono', 'site-hash',
    'site-header', 'ss-shell-header', 'ss-setup-mapdraw', 'ss-setup-arrows', 'ss-coding-turns', 'ss-coding-form-quote',
    'ss-coding-mark-quote', 'ss-setup-labels-who', 'pf-members', 'pf-formula'];
  var SKIP_ROLES = { status: 1, alert: 1, tooltip: 1, log: 1 };
  // A term inside one of these gets its marker after it.
  var HOST_TAGS = { LABEL: 1 };   // a term in a label: the marker ends the label
  // The closest of these around a text is its "paragraph": at most MAX_PER_PARAGRAPH markers fit in one.
  // Lowercase keys (compared with lower-cased tag names): an uppercase two-letter tag name trips the privacy name scan.
  var PARA_TAGS = { p: 1, li: 1, dd: 1, dt: 1, td: 1, th: 1, label: 1, caption: 1, figcaption: 1, blockquote: 1,
    h2: 1, h3: 1, h4: 1, h5: 1, h6: 1 };
  var MAX_PER_PARAGRAPH = 2;
  var SECTION_TAGS = { H1: 1, H2: 1, H3: 1 };   // a heading of one of these starts a page section
  var HEADING_TAGS = { H2: 1, H3: 1, H4: 1, H5: 1, H6: 1 };   // headings whose terms may be left to the first paragraph

  var nextId = 0;
  var open = null;          // the marker whose tip is open
  var reasons = { hover: false, focus: false, pin: false };
  var hoverTimer = null;
  var listening = false;

  function doc() { return root.document; }
  function upper(n) { return n && n.tagName ? String(n.tagName).toUpperCase() : ''; }
  function classes(n) {
    var c = n && typeof n.getAttribute === 'function' ? n.getAttribute('class') : null;
    return c ? String(c).split(/\s+/) : [];
  }
  function hasClass(n, name) { return classes(n).indexOf(name) >= 0; }
  function attr(n, name) { return n && typeof n.getAttribute === 'function' ? n.getAttribute(name) : null; }
  function isElement(n) { return !!n && n.nodeType === 1; }

  function skipped(n) {
    if (!isElement(n)) return false;
    if (hasOwn.call(SKIP_TAGS, upper(n))) return true;
    if (attr(n, 'hidden') !== null || attr(n, 'aria-hidden') === 'true' || attr(n, 'aria-live') !== null) return true;
    if (attr(n, 'data-gloss-skip') !== null || attr(n, 'contenteditable') !== null) return true;
    var role = attr(n, 'role');
    if (role && hasOwn.call(SKIP_ROLES, role)) return true;
    var cs = classes(n);
    for (var i = 0; i < SKIP_CLASSES.length; i++) if (cs.indexOf(SKIP_CLASSES[i]) >= 0) return true;
    return false;
  }

  // Walks from the text node's parent up to (and including) the limit. Returns null when the text must be left alone,
  // else {host, para, heading}: host is the label the marker ends (or null), para the paragraph the text sits in (see
  // PARA_TAGS; else its parent element), heading the h2 to h6 around it (or null).
  function place(text, limit) {
    var host = null;
    var para = null;
    var heading = null;
    for (var n = text.parentNode; n; n = n.parentNode) {
      if (skipped(n)) return null;
      if (!host && hasOwn.call(HOST_TAGS, upper(n))) host = n;
      if (!para && hasOwn.call(PARA_TAGS, upper(n).toLowerCase())) para = n;
      if (!heading && hasOwn.call(HEADING_TAGS, upper(n))) heading = n;
      if (n === limit) break;
    }
    return { host: host, para: para || text.parentNode, heading: heading };
  }

  function contains(a, b) {
    for (var n = b; n; n = n.parentNode) if (n === a) return true;
    return false;
  }

  // ----------------------------------------------------------------------------------------------- page sections

  // A heading that starts a page section. A heading inside a part that is never marked (hidden, navigation, ...) is not
  // on the page, so it starts nothing; an h1 is never marked but always starts a section.
  function isBoundary(n) {
    if (!isElement(n)) return false;
    var tag = upper(n);
    if (!hasOwn.call(SECTION_TAGS, tag)) return false;
    return tag === 'H1' || !skipped(n);
  }

  // The last section heading inside `n` in document order, or null. `memo` (a WeakMap, or null) lasts for one scan.
  function lastBoundary(n, memo) {
    if (!isElement(n)) return null;
    if (isBoundary(n)) return n;
    if (skipped(n)) return null;
    if (memo && memo.has(n)) return memo.get(n);
    var found = null;
    var kids = n.childNodes || [];
    for (var i = kids.length - 1; i >= 0 && !found; i--) found = lastBoundary(kids[i], memo);
    if (memo) memo.set(n, found);
    return found;
  }

  // The element that names the section of `text`: the nearest section heading before it (or around it), found by
  // walking back through the page (previous siblings, each from its last descendant, then the parent) up to `limit`.
  // The scanned part may be only a redrawn piece, so the heading can lie outside it. With no heading in reach it is the
  // limit itself (the leading section).
  function sectionOf(text, limit, memo) {
    var n = text;
    while (n && n !== limit) {
      for (var s = n.previousSibling; s; s = s.previousSibling) {
        var h = lastBoundary(s, memo);
        if (h) return h;
      }
      n = n.parentNode;
      if (n && n !== limit && isBoundary(n)) return n;
    }
    return limit;
  }

  // The next node in document order after `n`, not leaving `limit`; `descend` false skips n's own children.
  function advance(n, limit, descend) {
    if (descend && n.childNodes && n.childNodes.length) return n.childNodes[0];
    while (n && n !== limit) {
      if (n.nextSibling) return n.nextSibling;
      n = n.parentNode;
    }
    return null;
  }

  // Visits the nodes of the section named by `start` (a section heading, or the limit for the leading section) in
  // document order, ending before the next section heading. Parts that are never marked are not entered. `visit`
  // returns true to stop the walk.
  function walkSection(start, limit, visit) {
    var n = start;
    while (n) {
      if (n !== start && isBoundary(n)) return;
      if (visit(n) === true) return;
      n = advance(n, limit, n === start || !(isElement(n) && skipped(n)));
    }
  }

  // The marker of `key` in the section named by `section`, if any. A registry per section keeps the search short; a
  // cached marker counts only while it is still inside the limit (a redraw removes nodes), else the section's own
  // DOM range is searched (markers carry data-gloss), which also finds markers whose heading was redrawn.
  var seen = typeof WeakMap === 'function' ? new WeakMap() : null;
  function markerIn(section, limit, key) {
    var cache = seen ? seen.get(section) : null;
    if (cache && hasOwn.call(cache, key)) {
      var c = cache[key];
      if (contains(limit, c)) return c;
      delete cache[key];
    }
    var hit = null;
    walkSection(section, limit, function (n) {
      if (isElement(n) && hasClass(n, 'ss-gloss') && attr(n, 'data-gloss') === key) { hit = n; return true; }
      return false;
    });
    if (hit) remember(section, key, hit);
    return hit;
  }
  function remember(section, key, marker) {
    if (!seen) return;
    var cache = seen.get(section);
    if (!cache) { cache = {}; seen.set(section, cache); }
    cache[key] = marker;
  }

  // Does the first paragraph after `heading` (the first p before the next h1, h2 or h3) name `key`? Only text that
  // could carry a marker counts: not the tips, not parts that are never marked.
  function firstParagraphHas(heading, limit, key) {
    var para = null;
    walkSection(heading, limit, function (n) {
      if (n !== heading && isElement(n) && upper(n) === 'P' && !skipped(n)) { para = n; return true; }
      return false;
    });
    if (!para) return false;
    for (var x = para.parentNode; x; x = x.parentNode) {
      if (skipped(x)) return false;
      if (x === limit) break;
    }
    var nodes = textNodes(para);
    var joined = '';
    for (var i = 0; i < nodes.length; i++) joined += nodes[i].nodeValue || '';
    var found = matches(joined);
    for (var j = 0; j < found.length; j++) if (found[j].key === key) return true;
    return false;
  }

  // How many markers sit inside `para`.
  function countMarkers(para) {
    var count = 0;
    var stack = [para];
    while (stack.length) {
      var n = stack.pop();
      if (isElement(n) && hasClass(n, 'ss-gloss')) { count++; continue; }
      var kids = n.childNodes || [];
      for (var i = 0; i < kids.length; i++) if (isElement(kids[i])) stack.push(kids[i]);
    }
    return count;
  }

  // [{start, end, key}] in text order; earlier patterns claim their words first.
  function matches(text) {
    var found = [];
    var trimmed = text.replace(/^\s+|\s+$/g, '');
    for (var i = 0; i < TERMS.length; i++) {
      var key = TERMS[i][0];
      var re = TERMS[i][1];
      var m;
      if (TERMS[i][2]) {
        if (!re.test(trimmed)) continue;
        var at = text.indexOf(trimmed);
        m = { start: at, end: at + trimmed.length, key: key };
      } else {
        var g = GLOBAL[i];
        g.lastIndex = 0;
        m = null;
        var r;
        while ((r = g.exec(text)) !== null) {
          var cand = { start: r.index, end: r.index + r[0].length, key: key };
          if (!overlaps(found, cand)) { m = cand; break; }
          if (g.lastIndex === r.index) g.lastIndex++;
        }
      }
      if (m && !overlaps(found, m)) found.push(m);
    }
    found.sort(function (a, b) { return a.start - b.start; });
    return found;
  }
  function overlaps(list, m) {
    for (var i = 0; i < list.length; i++) if (m.start < list[i].end && list[i].start < m.end) return true;
    return false;
  }

  function definition(key) {
    var table = root.SS_STRINGS;
    var k = 'glossary.' + key;
    return table && hasOwn.call(table, k) && typeof table[k] === 'string' && table[k] ? table[k] : null;
  }

  // The marker is a <button>. Inside a label it is a <span role="button" tabindex="0">: a button inside a label is not
  // valid HTML, and the span's click is cancelled, so the label's own control does not change.
  function makeMarker(d, key, term, inLabel) {
    var id = 'ss-gloss-tip-' + (++nextId);
    var b = d.createElement(inLabel ? 'span' : 'button');
    if (inLabel) {
      b.setAttribute('role', 'button');
      b.setAttribute('tabindex', '0');
    } else {
      b.setAttribute('type', 'button');
    }
    b.setAttribute('class', 'ss-gloss');
    b.setAttribute('data-gloss', key);
    var name = /^[A-Z][a-z]/.test(term) ? term.charAt(0).toLowerCase() + term.slice(1) : term;
    b.setAttribute('aria-label', SS.t('glossary.marker_label', { term: name }));
    b.setAttribute('aria-describedby', id);
    var tip = d.createElement('span');
    tip.setAttribute('class', 'ss-gloss-tip');
    tip.setAttribute('role', 'tooltip');
    tip.setAttribute('id', id);
    tip.setAttribute('data-gloss', key);
    tip.setAttribute('hidden', '');
    tip.textContent = definition(key);
    wire(b, tip);
    return { button: b, tip: tip };
  }

  // Splits `text` so that the term (or, for a long term, its last word), the marker and the punctuation that follows
  // them sit together in a span that does not wrap, so the marker never starts a line and never drifts from a comma or
  // a full stop. There is no space in the text between the term and the marker: the gap is the marker's own CSS margin.
  // A closing parenthesis that closes a parenthesis opened before the term goes before the marker ("(rung 2)" then the
  // marker); a run of , . ; : ! ? ) ] that follows goes after the marker, kept right against it. Returns the text node
  // that holds the rest, and `consumed`, the number of characters taken from the start of that rest.
  var CLOSERS = /^[,.;:!?)\]]+(?=\s|$)/;
  function insertInText(d, text, start, end, made) {
    var value = text.nodeValue;
    var termText = value.slice(start, end);
    var lastSpace = termText.lastIndexOf(' ');
    var wordStart = lastSpace >= 0 && termText.length > 14 ? start + lastSpace + 1 : start;
    var rest = value.slice(end);
    var before = value.slice(0, start);
    var open = (before.match(/\(/g) || []).length - (before.match(/\)/g) || []).length;
    var closing = '';
    if (open > 0) {
      var c = /^\)+/.exec(rest);
      if (c) closing = c[0].slice(0, open);
    }
    var trail = '';
    var t = CLOSERS.exec(rest.slice(closing.length));
    if (t) trail = t[0];
    var parent = text.parentNode;
    var after = d.createTextNode(rest.slice(closing.length + trail.length));
    var wrap = d.createElement('span');
    wrap.setAttribute('class', 'ss-gloss-term');
    wrap.appendChild(d.createTextNode(value.slice(wordStart, end) + closing));
    wrap.appendChild(made.button);
    wrap.appendChild(made.tip);
    if (trail) wrap.appendChild(d.createTextNode(trail));
    text.nodeValue = value.slice(0, wordStart);
    parent.insertBefore(wrap, text.nextSibling);
    parent.insertBefore(after, wrap.nextSibling);
    made.button.ssHome = made.button;
    return { node: after, consumed: closing.length + trail.length };
  }

  // Inside a label: the marker ends the label and the tip follows the label, so the label's text stays as it was;
  // the label's control keeps that text as its name.
  function insertAtHost(host, made) {
    var control = labelled(host);
    if (control && control.getAttribute('aria-label') === null && control.getAttribute('aria-labelledby') === null) {
      var name = String(host.textContent || '').replace(/\s+/g, ' ').replace(/^\s+|\s+$/g, '');
      if (name) control.setAttribute('aria-label', name);
    }
    host.appendChild(made.button);
    host.parentNode.insertBefore(made.tip, host.nextSibling);
    made.button.ssHome = host;
    made.button.ssHost = host;
  }

  function labelled(label) {
    var stack = [label];
    while (stack.length) {
      var n = stack.pop();
      var tag = upper(n);
      if (n !== label && (tag === 'INPUT' || tag === 'SELECT' || tag === 'TEXTAREA')) return n;
      var kids = n.childNodes || [];
      for (var i = kids.length - 1; i >= 0; i--) if (isElement(kids[i])) stack.push(kids[i]);
    }
    return null;
  }

  function hostHasMarker(host) {
    var kids = host.childNodes || [];
    for (var i = 0; i < kids.length; i++) if (isElement(kids[i]) && hasClass(kids[i], 'ss-gloss')) return true;
    return false;
  }

  function textNodes(scanRoot) {
    var out = [];
    var stack = [scanRoot];
    while (stack.length) {
      var n = stack.pop();
      if (n.nodeType === 3) { out.push(n); continue; }
      if (isElement(n) && n !== scanRoot && skipped(n)) continue;
      var kids = n.childNodes || [];
      for (var i = kids.length - 1; i >= 0; i--) stack.push(kids[i]);
    }
    return out;
  }

  // A short text (a label, a column head) gets at most one marker, so two markers never crowd a few words.
  var SHORT = 80;

  // Marks the first appearance of each term in each page section under `scanRoot` (an element). `top` is the element
  // that bounds the sections (the watched root; scanRoot itself when omitted). Returns the markers added.
  function scan(scanRoot, top) {
    var d = doc();
    var added = [];
    if (!d || !isElement(scanRoot)) return added;
    var limit = top || scanRoot;
    var memo = typeof WeakMap === 'function' ? new WeakMap() : null;
    var texts = textNodes(scanRoot);
    for (var t = 0; t < texts.length; t++) {
      var text = texts[t];
      if (!text.nodeValue || !/[A-Za-z]/.test(text.nodeValue) || !text.parentNode) continue;
      var where = place(text, limit);
      if (!where) continue;
      var found = matches(text.nodeValue);
      if (!found.length) continue;
      var section = sectionOf(text, limit, memo);
      var inPara = null;   // markers already in the paragraph, counted when first needed
      var short = text.nodeValue.length < SHORT;
      var offset = 0;
      var current = text;
      for (var i = 0; i < found.length; i++) {
        var m = found[i];
        if (!definition(m.key) || markerIn(section, limit, m.key)) continue;
        if (where.heading && firstParagraphHas(where.heading, limit, m.key)) continue;
        if (inPara === null) inPara = countMarkers(where.para);
        if (inPara >= MAX_PER_PARAGRAPH) break;
        if (where.host) {
          if (!hostHasMarker(where.host)) {   // one marker per label
            var madeH = makeMarker(d, m.key, text.nodeValue.slice(m.start, m.end), true);
            insertAtHost(where.host, madeH);
            remember(section, m.key, madeH.button);
            added.push(madeH.button);
          }
          break;
        }
        var made = makeMarker(d, m.key, current.nodeValue.slice(m.start - offset, m.end - offset), false);
        var put = insertInText(d, current, m.start - offset, m.end - offset, made);
        current = put.node;
        offset = m.end + put.consumed;
        remember(section, m.key, made.button);
        added.push(made.button);
        inPara++;
        if (short) break;
      }
    }
    return added;
  }

  // ------------------------------------------------------------------------------------------- show and hide

  // Where the tip goes: below the marker, else above, inside the window with a 16px gutter; never over the marker.
  // a: the marker's rect {left, top, right, bottom, width, height}; size: {width, height}; view: {width, height}.
  function position(a, size, view) {
    var gap = 8;
    var gutter = 16;
    var left = a.left + a.width / 2 - size.width / 2;
    var maxLeft = view.width - gutter - size.width;
    if (maxLeft < gutter) left = Math.max(0, (view.width - size.width) / 2);
    else left = Math.min(Math.max(left, gutter), maxLeft);
    var below = a.bottom + gap;
    var above = a.top - gap - size.height;
    var side;
    if (below + size.height <= view.height - gap) side = 'below';
    else if (above >= gap) side = 'above';
    else side = view.height - a.bottom >= a.top ? 'below' : 'above';
    return { left: Math.round(left), top: Math.round(side === 'below' ? below : above), side: side };
  }

  function rectOf(n) {
    var r = n.getBoundingClientRect();
    return { left: r.left, top: r.top, right: r.right, bottom: r.bottom, width: r.width, height: r.height };
  }

  function layout() {
    if (!open) return;
    var b = open.button;
    var tip = open.tip;
    var a = rectOf(b);
    if (!b.parentNode || !connected(b) || (!a.width && !a.height)) { hide(); return; }
    if (b.ssHost && connected(b.ssHost)) {   // never over the option it explains
      var h = rectOf(b.ssHost);
      a.top = Math.min(a.top, h.top);
      a.bottom = Math.max(a.bottom, h.bottom);
      a.height = a.bottom - a.top;
    }
    var view = { width: root.innerWidth || 0, height: root.innerHeight || 0 };
    var s = rectOf(tip);
    var p = position(a, { width: s.width, height: s.height }, view);
    tip.style.left = p.left + 'px';
    tip.style.top = p.top + 'px';
    tip.setAttribute('data-side', p.side);
  }

  function connected(n) {
    var d = doc();
    for (var x = n; x; x = x.parentNode) if (x === d) return true;
    return false;
  }

  function show(b, tip) {
    if (open && open.button !== b) hide();
    if (!open) {
      open = { button: b, tip: tip, home: tip.parentNode, next: tip.nextSibling };
      var body = doc().body;
      if (body && tip.parentNode !== body) body.appendChild(tip);
      tip.style.visibility = 'hidden';
      tip.removeAttribute('hidden');
      b.setAttribute('data-open', '');
      layout();
      tip.style.visibility = '';
      listen();
      tick();
    }
  }

  function hide() {
    if (!open) return;
    var o = open;
    open = null;
    reasons.hover = reasons.focus = reasons.pin = false;
    if (hoverTimer) { clearTimeout(hoverTimer); hoverTimer = null; }
    o.tip.setAttribute('hidden', '');
    o.button.removeAttribute('data-open');
    // back beside its marker (or its label), so the description stays next to it
    var home = o.button.ssHome || o.button;
    if (home.parentNode) home.parentNode.insertBefore(o.tip, home.nextSibling);
    else if (o.tip.parentNode) o.tip.parentNode.removeChild(o.tip);
  }

  function update(b, tip) {
    if (reasons.hover || reasons.focus || reasons.pin) show(b, tip);
    else if (open && open.button === b) hide();
  }

  function isOpen(b) { return !!open && open.button === b; }

  function wire(b, tip) {
    function enter(e) {
      if (e && e.pointerType && e.pointerType !== 'mouse') return;
      if (hoverTimer) { clearTimeout(hoverTimer); hoverTimer = null; }
      if (!isOpen(b)) { if (open) hide(); reasons.hover = false; reasons.focus = false; reasons.pin = false; }
      reasons.hover = true;
      update(b, tip);
    }
    function leave(e) {
      if (e && e.pointerType && e.pointerType !== 'mouse') return;
      if (!isOpen(b)) return;
      if (hoverTimer) clearTimeout(hoverTimer);
      hoverTimer = setTimeout(function () {
        hoverTimer = null;
        if (!isOpen(b)) return;
        reasons.hover = false;
        update(b, tip);
      }, 250);
    }
    b.addEventListener('pointerenter', enter);
    b.addEventListener('pointerleave', leave);
    tip.addEventListener('pointerenter', enter);
    tip.addEventListener('pointerleave', leave);
    b.addEventListener('focus', function () {
      if (!isOpen(b)) { if (open) hide(); reasons.hover = reasons.pin = false; }
      reasons.focus = true;
      update(b, tip);
    });
    b.addEventListener('blur', function () {
      if (!isOpen(b)) return;
      reasons.focus = false;
      reasons.pin = false;
      update(b, tip);
    });
    if (upper(b) !== 'BUTTON') {
      b.addEventListener('keydown', function (e) {
        if (!e || (e.key !== 'Enter' && e.key !== ' ' && e.key !== 'Spacebar')) return;
        if (typeof e.preventDefault === 'function') e.preventDefault();
        if (typeof e.stopPropagation === 'function') e.stopPropagation();
        toggle();
      });
    }
    function toggle() {
      if (isOpen(b) && reasons.pin) { hide(); return; }
      if (!isOpen(b) && open) hide();
      reasons.pin = true;
      update(b, tip);
    }
    b.addEventListener('click', function (e) {
      if (e && typeof e.preventDefault === 'function') e.preventDefault();
      if (e && typeof e.stopPropagation === 'function') e.stopPropagation();
      toggle();
    });
  }

  function onKey(e) {
    if (!open || !e || (e.key !== 'Escape' && e.key !== 'Esc')) return;
    hide();
    if (typeof e.stopPropagation === 'function') e.stopPropagation();
    if (typeof e.preventDefault === 'function') e.preventDefault();
  }
  function onDown(e) {
    if (!open || !e) return;
    var t = e.target;
    if (t && (contains(open.button, t) || contains(open.tip, t))) return;
    hide();
  }
  function onMove() { layout(); }

  function listen() {
    if (listening) return;
    var d = doc();
    if (!d || typeof d.addEventListener !== 'function') return;
    listening = true;
    d.addEventListener('keydown', onKey, true);
    d.addEventListener('pointerdown', onDown, true);
    d.addEventListener('scroll', onMove, true);
    if (typeof root.addEventListener === 'function') root.addEventListener('resize', onMove);
  }

  // While a tip is open, follow the marker (a scene of the Explain page can move or hide it).
  function tick() {
    if (!open || typeof root.requestAnimationFrame !== 'function') return;
    root.requestAnimationFrame(function () { if (open) { layout(); tick(); } });
  }

  // ------------------------------------------------------------------------------------------- watching a root

  function inGloss(n) {
    for (var x = n; x; x = x.parentNode) if (isElement(x) && (hasClass(x, 'ss-gloss') || hasClass(x, 'ss-gloss-tip'))) return true;
    return false;
  }

  // Scans `scanRoot` now and again after every change under it. Returns a function that stops watching.
  function watch(scanRoot) {
    if (!isElement(scanRoot)) return function () {};
    scan(scanRoot);
    var Observer = root.MutationObserver;
    if (typeof Observer !== 'function') return function () {};
    var pending = [];
    var queued = false;
    function flush() {
      queued = false;
      var list = pending;
      pending = [];
      for (var i = 0; i < list.length; i++) {
        var n = list[i];
        if (!connected(n) || !contains(scanRoot, n)) continue;
        var covered = false;
        for (var j = 0; j < list.length; j++) if (j !== i && list[j] !== n && contains(list[j], n) && connected(list[j])) { covered = true; break; }
        if (!covered) scan(n, scanRoot);
      }
    }
    var obs = new Observer(function (records) {
      for (var i = 0; i < records.length; i++) {
        var r = records[i];
        var target = r.type === 'characterData' ? r.target.parentNode : r.target;
        if (!target || inGloss(target)) continue;
        if (r.type === 'attributes') { if (pending.indexOf(target) < 0) pending.push(target); continue; }
        var ours = true;
        var added = r.addedNodes || [];
        for (var k = 0; k < added.length; k++) if (!inGloss(added[k]) && !(isElement(added[k]) && hasClass(added[k], 'ss-gloss-term'))) ours = false;
        if (r.type === 'childList' && ours) continue;
        if (pending.indexOf(target) < 0) pending.push(target);
      }
      if (pending.length && !queued) { queued = true; setTimeout(flush, 0); }
    });
    obs.observe(scanRoot, { childList: true, subtree: true, characterData: true, attributes: true, attributeFilter: ['hidden'] });
    return function () { obs.disconnect(); };
  }

  SS.gloss = {
    TERMS: TERMS,
    scan: scan,
    watch: watch,
    matches: matches,
    position: position,
    hide: hide,
    openMarker: function () { return open ? open.button : null; }
  };

  // Start on its own: the site's main content, or the app's frame.
  function start() {
    var d = doc();
    if (!d || typeof d.getElementById !== 'function') return;
    var host = d.getElementById('main') || d.getElementById('ss-app');
    if (host) watch(host);
  }
  var d0 = doc();
  if (d0 && typeof d0.addEventListener === 'function' && typeof d0.getElementById === 'function') {
    if (d0.readyState === 'loading') d0.addEventListener('DOMContentLoaded', start);
    else start();
  }
})();
