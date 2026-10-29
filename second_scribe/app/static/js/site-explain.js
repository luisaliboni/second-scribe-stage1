/* SS.explain: the Explain page's six scenes (plan Task 29 as amended by V5).
     1 interview      someone tells their journey in an interview
     2 endorsed       the interviewer draws the map live and the participant endorses it
     3 ai             the AI reads the transcript, alone, and draws its map
     4 no_interview   the same model draws the no-interview map: domain, role and phases given, no interview
     5 side_by_side   the three readings side by side, each measured against the same transcript
     6 own            run it on your own interviews, on your computer; labs pool profiles, never interviews
   Scenes 1-5 draw the shipped synthetic case SYN-OW03 (window.SS_DATA.explain, built by explain_data.py) under
   the SYNTHETIC band, as an animation drawn from the answer key (no illustration caption since the author's item 4).
   Scene 3 first shows the interviewer's mapping talk struck out, removed before the AI reads (council 30).
   No number is shown anywhere in the animation, and no null result is animated as a finding.
   Reduced motion (the media query, or data-motion="reduce" on <html>) shows the six scenes as still frames in a
   column. A hash "#scene=<id>" opens that scene, complete and paused.
   Classic script after core.js. Text goes into the DOM through textContent and attributes only. */
(function () {
  'use strict';
  var root = typeof window !== 'undefined' ? window : globalThis;
  var SS = root.SS = root.SS || {};
  var hasOwn = Object.prototype.hasOwnProperty;

  function t(key, slots) { return SS.t(key, slots); }
  function speaker(x) { return x && x.speaker === 'participant' ? t('explain.speaker.participant') : t('explain.speaker.interviewer'); }

  var TRY_HREF = 'try.html#map=SYN-OW03&rung=rung2';
  var IDS = ['interview', 'endorsed', 'ai', 'no_interview', 'side_by_side', 'own'];
  // Full keys only (never built by concatenation), so a test can list every key the page reads.
  var TAB_KEY = {
    interview: 'explain.tab.interview', endorsed: 'explain.tab.endorsed',
    ai: 'explain.tab.ai', no_interview: 'explain.tab.no_interview',
    side_by_side: 'explain.tab.side_by_side', own: 'explain.tab.own'
  };
  var CAPTION_KEY = {
    interview: 'explain.scene.interview', endorsed: 'explain.scene.endorsed', ai: 'explain.scene.ai',
    no_interview: 'explain.scene.no_interview', side_by_side: 'explain.scene.side_by_side', own: 'explain.scene.own'
  };
  var FRAME_MS = 650;
  var HOLD_MS = 3400;


  // ------------------------------------------------------------------ small DOM helpers (stub-friendly)
  function doc() { return root.document; }

  function el(tag, cls, text) {
    var n = doc().createElement(tag);
    if (cls) n.setAttribute('class', cls);
    if (text !== undefined && text !== null) n.textContent = String(text);
    return n;
  }

  function add(parent) {
    for (var i = 1; i < arguments.length; i++) if (arguments[i]) parent.appendChild(arguments[i]);
    return parent;
  }

  // append one child and return the CHILD (add() returns the parent)
  function kid(parent, node) { parent.appendChild(node); return node; }

  function classes(n) { return ((n.getAttribute && n.getAttribute('class')) || '').split(/\s+/).filter(Boolean); }

  function addClass(n, c) {
    var list = classes(n);
    if (list.indexOf(c) < 0) { list.push(c); n.setAttribute('class', list.join(' ')); }
  }

  function removeClass(n, c) {
    var list = classes(n);
    var i = list.indexOf(c);
    if (i >= 0) { list.splice(i, 1); n.setAttribute('class', list.join(' ')); }
  }

  // ------------------------------------------------------------------ case helpers
  function byPhase(phases, boxes) {
    var out = {};
    phases.forEach(function (p) { out[p] = []; });
    boxes.forEach(function (b) {
      if (!hasOwn.call(out, b.phase)) out[b.phase] = [];
      out[b.phase].push(b);
    });
    return out;
  }

  function missingTurn(kase) {
    var m = (kase.reference || []).filter(function (r) { return r.missing && r.turn !== null; })[0];
    return m ? m.turn : null;
  }

  // The turns scene 1 shows: the opening, then the turn behind the box marked missing and the question before it.
  function excerpt(kase) {
    var turns = kase.transcript || [];
    var head = [];
    for (var i = 0; i < Math.min(6, turns.length); i++) head.push(i);
    var m = missingTurn(kase);
    var tail = [];
    if (m !== null && m >= head.length) {
      if (m - 1 >= head.length) tail.push(m - 1);
      tail.push(m);
    }
    return { head: head, tail: tail };
  }

  // SS.explain.rows(case): one row per concept, grouped by phase, with the box each reading drew for it (or
  // null). added = the AI's map from the transcript has it and the no-interview map does not.
  function rows(kase) {
    var order = [];
    var by = {};
    ['reference', 'ai', 'no_interview'].forEach(function (lane) {
      (kase[lane] || []).forEach(function (b) {
        if (!hasOwn.call(by, b.concept)) {
          by[b.concept] = { concept: b.concept, phase: b.phase, reference: null, ai: null, no_interview: null };
          order.push(b.concept);
        }
        by[b.concept][lane] = b;
      });
    });
    var phases = kase.phases || [];
    var list = order.map(function (c) { return by[c]; });
    list.forEach(function (r, i) { r._i = i; });
    list.sort(function (a, b) {
      var pa = phases.indexOf(a.phase), pb = phases.indexOf(b.phase);
      return pa !== pb ? pa - pb : a._i - b._i;
    });
    return list.map(function (r) {
      return { concept: r.concept, phase: r.phase, reference: r.reference, ai: r.ai, no_interview: r.no_interview,
               added: !!r.ai && !r.no_interview };
    });
  }

  // ------------------------------------------------------------------ scenes
  // SS.explain.scenes(case) -> [{id, caption_key, frames, band, try_href}]. frames are keys, one
  // per step of the animation; the builders below make one element per key.
  function scenes(kase) {
    var ex = excerpt(kase);
    var f1 = ex.head.map(function (i) { return 'turn:' + i; });
    if (ex.tail.length) f1 = f1.concat(['gap'], ex.tail.map(function (i) { return 'turn:' + i; }));
    var f2 = (kase.reference || []).map(function (b) { return 'box:' + b.id; });
    if ((kase.reference || []).some(function (b) { return b.missing; })) f2.push('missing');
    f2.push('stamp');
    // scene 3: first the mapping-talk turns, struck out (removed before the AI reads), then the AI's boxes
    var f3 = (kase.mapping_talk || []).map(function (i) { return 'removed:' + i; })
      .concat((kase.ai || []).map(function (b) { return 'row:' + b.id; }));
    var f4 = ['given'].concat((kase.no_interview || []).map(function (b) { return 'box:' + b.id; }));
    var f5 = ['grid', 'marks', 'added'];
    var f6 = ['computer', 'profile', 'pool'];
    var frames = [f1, f2, f3, f4, f5, f6];
    return IDS.map(function (id, i) {
      return { id: id, caption_key: CAPTION_KEY[id], frames: frames[i], band: i < 5, try_href: TRY_HREF };
    });
  }

  function boxEl(b, lane, opts) {
    opts = opts || {};
    var cls = 'ss-x-box ss-x-' + lane;
    if (b.missing) cls += ' ss-x-missing';
    var n = el('div', cls);
    n.dataset.label = b.label;
    n.setAttribute('data-label', b.label);
    add(n, el('span', 'ss-x-box-label', b.label));
    if (opts.mark && b.turn !== null && b.turn !== undefined) {
      addClass(n, 'ss-x-attested');
      var mk = el('span', 'ss-x-mark', '“”');
      mk.setAttribute('title', t('explain.side.attested'));
      mk.setAttribute('aria-label', t('explain.side.attested'));
      add(n, mk);
    }
    return n;
  }

  function phaseColumns(kase, boxes, lane, reg, keyOf) {
    var wrap = el('div', 'ss-x-phases');
    var groups = byPhase(kase.phases || [], boxes);
    Object.keys(groups).forEach(function (p) {
      var col = add(el('div', 'ss-x-phase'), el('div', 'ss-x-phase-name', p));
      var list = kid(col, el('div', 'ss-x-phase-boxes'));
      groups[p].forEach(function (b) {
        var n = boxEl(b, lane);
        reg(keyOf(b), n);
        add(list, n);
      });
      add(wrap, col);
    });
    return wrap;
  }

  var BUILD = {
    interview: function (kase, reg) {
      var card = el('div', 'ss-x-transcript');
      var ex = excerpt(kase);
      function turn(i) {
        var x = kase.transcript[i];
        var n = el('p', 'ss-x-turn ss-x-' + x.speaker);
        add(n, el('span', 'ss-x-speaker', speaker(x)),
          el('span', 'ss-x-said', x.text));
        reg('turn:' + i, n);
        return n;
      }
      ex.head.forEach(function (i) { add(card, turn(i)); });
      if (ex.tail.length) {
        var gap = el('p', 'ss-x-gap', t('explain.transcript_gap'));
        reg('gap', gap);
        add(card, gap);
        ex.tail.forEach(function (i) { add(card, turn(i)); });
      }
      return card;
    },

    endorsed: function (kase, reg) {
      var wrap = el('div', 'ss-x-mapwrap');
      add(wrap, phaseColumns(kase, kase.reference || [], 'reference', reg, function (b) { return 'box:' + b.id; }));
      (kase.reference || []).forEach(function (b) {
        if (!b.missing) return;
        var tag = el('span', 'ss-x-tag ss-x-missing-tag', t('explain.missing_tag'));
        reg('missing', tag);
        reg.lookup('box:' + b.id).appendChild(tag);
      });
      var stamp = el('p', 'ss-x-stamp', t('explain.endorsed_stamp'));
      reg('stamp', stamp);
      return add(wrap, stamp);
    },

    ai: function (kase, reg) {
      var list = el('div', 'ss-x-reads');
      // council 30 (MUST 1): the interviewer's mapping talk is removed before the AI reads (glossary.ai_map)
      (kase.mapping_talk || []).forEach(function (i) {
        var x = kase.transcript[i];
        if (!x) return;
        var row = el('div', 'ss-x-read ss-x-removed');
        var q = el('blockquote', 'ss-x-quote-wrap');
        add(q, el('span', 'ss-x-quote-who', t('explain.quote_from', { speaker: speaker(x) })),
          el('s', 'ss-x-quote ss-x-quote-removed', x.text));
        add(row, q, el('span', 'ss-x-arrow', '→'), el('span', 'ss-x-tag ss-x-removed-tag', t('explain.removed_tag')));
        reg('removed:' + i, row);
        add(list, row);
      });
      (kase.ai || []).forEach(function (b) {
        var row = el('div', 'ss-x-read');
        var x = kase.transcript[b.turn] || { speaker: 'participant', text: '' };
        var q = el('blockquote', 'ss-x-quote-wrap');
        add(q, el('span', 'ss-x-quote-who', t('explain.quote_from', {
          speaker: speaker(x) })),
          el('span', 'ss-x-quote', x.text));
        var box = boxEl(b, 'ai');
        add(box, el('span', 'ss-x-box-phase', b.phase));
        if (b.missing) add(box, el('span', 'ss-x-tag ss-x-ai-missing-tag', t('explain.ai_missing_tag')));
        add(row, q, el('span', 'ss-x-arrow', '→'), box);
        reg('row:' + b.id, row);
        add(list, row);
      });
      return list;
    },

    no_interview: function (kase, reg) {
      var wrap = el('div', 'ss-x-ni');
      var given = el('dl', 'ss-x-given');
      add(given, el('p', 'ss-x-given-title', t('explain.given.title')),
        el('dt', null, t('explain.given.domain')), el('dd', null, kase.domain),
        el('dt', null, t('explain.given.role')), el('dd', null, kase.role),
        el('dt', null, t('explain.given.phases')), el('dd', null, (kase.phases || []).join(' · ')),
        el('p', 'ss-x-given-none', t('explain.given.none')));
      reg('given', given);
      add(wrap, given);
      var map = add(el('div', 'ss-x-ni-map'), el('p', 'ss-x-lane-name', t('lane.no_interview')));
      add(map, phaseColumns(kase, kase.no_interview || [], 'no_interview', reg, function (b) { return 'box:' + b.id; }));
      return add(wrap, map);
    },

    side_by_side: function (kase, reg) {
      var outer = el('div', 'ss-x-side');
      var marks = kid(outer, el('div', 'ss-x-side-marks'));
      var added = kid(marks, el('div', 'ss-x-side-added'));
      reg('grid', outer);
      reg('marks', marks);
      reg('added', added);
      var grid = kid(added, el('div', 'ss-x-grid'));
      var head = kid(grid, el('div', 'ss-x-grow ss-x-ghead'));
      add(head, el('span', 'ss-x-gphase', t('explain.side.phase')),
        el('span', 'ss-x-gcell', t('lane.reference')), el('span', 'ss-x-gcell', t('lane.ai')),
        el('span', 'ss-x-gcell', t('lane.no_interview')));
      var last = null;
      rows(kase).forEach(function (r) {
        var row = el('div', 'ss-x-grow' + (r.added ? ' ss-x-added' : ''));
        add(row, el('span', 'ss-x-gphase', r.phase === last ? '' : r.phase));
        last = r.phase;
        ['reference', 'ai', 'no_interview'].forEach(function (lane) {
          var cell = el('span', 'ss-x-gcell');
          if (r[lane]) add(cell, boxEl(r[lane], lane, { mark: true }));
          add(row, cell);
        });
        add(grid, row);
      });
      // the key sits inside both reveal wrappers, so each line appears with its own frame
      add(added, add(el('div', 'ss-x-side-key'),
        add(el('p', 'ss-x-key-mark'), el('span', 'ss-x-mark', '“”'), el('span', null, t('explain.side.attested'))),
        add(el('p', 'ss-x-key-added'), el('span', 'ss-x-swatch-added'), el('span', null, t('explain.side.added'))),
        el('p', 'ss-x-key-read', t('explain.side.how_to_read'))));
      return outer;
    },

    own: function (kase, reg) {
      var wrap = el('div', 'ss-x-own');
      var pc = el('div', 'ss-x-computer');
      var docs = el('div', 'ss-x-docs');
      for (var i = 0; i < 3; i++) add(docs, el('span', 'ss-x-doc'));
      var mini = el('div', 'ss-x-mini');
      ['reference', 'ai', 'no_interview'].forEach(function (lane) { add(mini, el('span', 'ss-x-mini-lane ss-x-' + lane)); });
      add(pc, el('p', 'ss-x-own-title', t('explain.own.computer')), docs, mini, el('p', 'ss-x-own-note', t('explain.own.interviews')));
      reg('computer', pc);
      var profile = add(el('div', 'ss-x-profile'), el('span', 'ss-x-arrow', '→'),
        add(el('span', 'ss-x-card'), el('span', 'ss-x-card-lines'), el('span', null, t('explain.own.profile'))));
      reg('profile', profile);
      var pool = add(el('div', 'ss-x-pool'), el('p', 'ss-x-own-title', t('explain.own.pool')));
      [t('explain.own.lab_you'), t('explain.own.lab_other'), t('explain.own.lab_other')].forEach(function (name, k) {
        add(pool, add(el('span', 'ss-x-card' + (k === 0 ? ' ss-x-card-you' : '')), el('span', 'ss-x-card-lines'), el('span', null, name)));
      });
      // why pooling helps (author's item 5), shown with the pooled drawing
      add(pool, el('p', 'ss-x-pool-why', t('explain.own.pool_why')));
      reg('pool', pool);
      return add(wrap, pc, profile, pool);
    }
  };

  // SS.explain.renderScene(scene, case, complete) -> <section>. complete=false leaves every frame after the
  // opening one pending (hidden), for the player to reveal one by one.
  function renderScene(scene, kase, complete) {
    var sec = el('section', 'ss-x-scene ss-x-scene-' + scene.id);
    sec.setAttribute('data-scene', scene.id);
    sec.setAttribute('aria-label', t(TAB_KEY[scene.id]));
    if (scene.band) {
      var band = el('p', 'ss-x-band', t('band.synthetic'));
      add(sec, band);
    }
    add(sec, el('h3', 'ss-x-caption', t(scene.caption_key)));
    if (scene.id === 'own') add(sec, el('p', 'ss-x-note', t('explain.scene.own_note')));
    // why the wished-for service is shown (author's item 6); no illustration caption since item 4
    if (scene.id === 'endorsed') add(sec, el('p', 'ss-x-note', t('explain.scene.endorsed_note')));
    var map = {};
    var reg = function (key, node) { map[key] = node; };
    reg.lookup = function (key) { return map[key]; };
    var body = BUILD[scene.id](kase, reg);
    add(sec, add(el('div', 'ss-x-visual'), body));
    var link = el('a', 'ss-x-try', t(scene.id === 'own' ? 'explain.try_link_own' : 'explain.try_link'));
    link.setAttribute('href', scene.try_href || TRY_HREF);
    add(sec, add(el('p', 'ss-x-try-row'), link));
    var frames = scene.frames.map(function (key, i) {
      var n = map[key];
      if (!n) throw new Error('site-explain: no element for frame ' + key + ' of ' + scene.id);
      addClass(n, 'ss-x-f');
      n.setAttribute('data-reveal', key === 'marks' ? 'marks' : key === 'added' ? 'added' : 'self');
      if (!complete && i > 0) addClass(n, 'ss-x-pending');
      return n;
    });
    sec._ssFrames = frames;
    return sec;
  }

  function setFrames(sec, upto) {
    (sec._ssFrames || []).forEach(function (n, i) {
      if (i <= upto) removeClass(n, 'ss-x-pending'); else addClass(n, 'ss-x-pending');
    });
  }

  // The legend under the stage: the three readings, named once, with the no-interview gloss once on the page.
  function legend() {
    var box = add(el('div', 'ss-x-legend'), el('p', 'ss-x-legend-title', t('explain.legend.title')));
    var list = kid(box, el('ul', 'ss-x-legend-list'));
    function item(sample, label, gloss) {
      var li = add(el('li'), sample, el('span', 'ss-x-legend-label', label));
      if (gloss) add(li, el('span', 'ss-x-legend-gloss', gloss));
      return add(list, li);
    }
    item(el('span', 'ss-x-sample ss-x-reference'), t('lane.reference'));
    item(el('span', 'ss-x-sample ss-x-ai'), t('lane.ai'));
    item(el('span', 'ss-x-sample ss-x-no_interview'), t('lane.no_interview'), t('lane.no_interview_gloss'));
    item(el('span', 'ss-x-sample ss-x-reference ss-x-missing'), t('explain.legend.missing'));
    return box;
  }

  // SS.explain.renderAll(case, {reduced}) -> the stage. Reduced: six still scenes in a column, no controls.
  // Otherwise one scene at a time with controls; every scene starts complete until the player runs.
  function renderAll(kase, opts) {
    opts = opts || {};
    var list = scenes(kase);
    var stage = el('div', 'ss-x-stage' + (opts.reduced ? ' ss-x-still' : ' ss-x-live'));
    var sceneEls = list.map(function (s) { return renderScene(s, kase, true); });
    var controls = null;
    var tabs = [];
    if (!opts.reduced) {
      controls = el('div', 'ss-x-controls');
      var nav = el('div', 'ss-x-tabs');
      nav.setAttribute('role', 'group');
      nav.setAttribute('aria-label', t('explain.scene_nav'));
      list.forEach(function (s, i) {
        var b = el('button', 'ss-x-tab', t(TAB_KEY[s.id]));
        b.setAttribute('type', 'button');
        b.setAttribute('data-index', String(i));
        tabs.push(b);
        add(nav, b);
      });
      var prev = el('button', 'ss-x-prev', '←');
      prev.setAttribute('type', 'button');
      prev.setAttribute('aria-label', t('explain.prev'));
      prev.setAttribute('title', t('explain.prev'));
      var play = el('button', 'ss-x-play primary', t('explain.pause'));
      play.setAttribute('type', 'button');
      var next = el('button', 'ss-x-next', '→');
      next.setAttribute('type', 'button');
      next.setAttribute('aria-label', t('explain.next'));
      next.setAttribute('title', t('explain.next'));
      var buttons = add(el('div', 'ss-x-buttons'), prev, play, next);
      add(controls, nav, buttons);
      add(stage, controls);
      stage._ssButtons = { prev: prev, play: play, next: next };
    }
    var screen = kid(stage, el('div', 'ss-x-screen'));
    sceneEls.forEach(function (s, i) {
      if (!opts.reduced && i > 0) s.hidden = true;
      add(screen, s);
    });
    add(stage, legend());
    stage._ssScenes = sceneEls;
    stage._ssTabs = tabs;
    if (tabs.length) tabs[0].setAttribute('aria-current', 'step');
    return stage;
  }

  // SS.explain.player(stage, scenes, {autoplay}) -> {show(i, complete), step(), play(), stop(), playing()}
  function player(stage, list, opts) {
    opts = opts || {};
    var sceneEls = stage._ssScenes || [];
    var tabs = stage._ssTabs || [];
    var btn = stage._ssButtons || {};
    var cur = 0;
    var at = 0;
    var timer = null;
    var running = false;

    function label() {
      if (!btn.play) return;
      var end = cur === sceneEls.length - 1 && at >= (sceneEls[cur]._ssFrames || []).length - 1;
      btn.play.textContent = running ? t('explain.pause') : (end ? t('explain.replay') : t('explain.play'));
    }

    function show(i, complete) {
      cur = Math.max(0, Math.min(sceneEls.length - 1, i));
      sceneEls.forEach(function (s, k) { s.hidden = k !== cur; });
      tabs.forEach(function (b, k) {
        if (k === cur) b.setAttribute('aria-current', 'step'); else b.removeAttribute('aria-current');
      });
      at = complete ? (sceneEls[cur]._ssFrames || []).length - 1 : 0;
      setFrames(sceneEls[cur], at);
      label();
    }

    function step() {
      var n = (sceneEls[cur]._ssFrames || []).length;
      if (at >= n - 1) return false;
      at += 1;
      setFrames(sceneEls[cur], at);
      label();
      return true;
    }

    function tick() {
      timer = null;
      if (!running) return;
      if (step()) { timer = root.setTimeout(tick, FRAME_MS); return; }
      if (cur >= sceneEls.length - 1) { stop(); return; }
      timer = root.setTimeout(function () {
        timer = null;
        if (!running) return;
        show(cur + 1, false);
        timer = root.setTimeout(tick, FRAME_MS);
      }, HOLD_MS);
    }

    function play() {
      if (running) return;
      running = true;
      var n = (sceneEls[cur]._ssFrames || []).length;
      if (at >= n - 1) {
        if (cur >= sceneEls.length - 1) show(0, false); else show(cur + 1, false);
      }
      label();
      timer = root.setTimeout(tick, FRAME_MS);
    }

    function stop() {
      running = false;
      if (timer !== null) { root.clearTimeout(timer); timer = null; }
      label();
    }

    function manual(i) { stop(); show(i, true); }

    if (btn.play) {
      btn.play.addEventListener('click', function () { if (running) stop(); else play(); });
      btn.prev.addEventListener('click', function () { manual(cur - 1); });
      btn.next.addEventListener('click', function () { manual(cur + 1); });
    }
    tabs.forEach(function (b, k) { b.addEventListener('click', function () { manual(k); }); });

    if (opts.start !== undefined && opts.start >= 0) show(opts.start, true);
    else if (opts.autoplay) { show(0, false); play(); }
    else show(0, true);
    label();
    return { show: show, step: step, play: play, stop: stop, playing: function () { return running; } };
  }

  function sceneFromHash(hash) {
    var m = /(?:^#|&)scene=([a-z_]+)(?:&|$)/.exec(String(hash || ''));
    return m ? IDS.indexOf(m[1]) : -1;
  }

  // ------------------------------------------------------------------ page wiring (browser only)
  function fillKeys(scope) {
    var nodes = scope.querySelectorAll('[data-ss-t]');
    for (var i = 0; i < nodes.length; i++) nodes[i].textContent = t(nodes[i].getAttribute('data-ss-t'));
  }

  function wireCopy(scope) {
    var btn = scope.querySelector('[data-ss-copy]');
    var code = scope.querySelector('.ss-x-cmd');
    var status = scope.querySelector('.ss-x-copy-status');
    if (!btn || !code) return;
    function selectText() {
      try {
        var range = doc().createRange();
        range.selectNodeContents(code);
        var sel = root.getSelection();
        sel.removeAllRanges();
        sel.addRange(range);
      } catch (e) { /* nothing to select with */ }
      if (status) status.textContent = t('explain.copy_fallback');
    }
    btn.addEventListener('click', function () {
      var text = code.textContent;
      var clip = root.navigator && root.navigator.clipboard;
      if (clip && typeof clip.writeText === 'function') {
        clip.writeText(text).then(function () {
          if (status) status.textContent = t('explain.copied');
        }, selectText);
      } else {
        selectText();
      }
    });
  }

  function reducedWanted() {
    var html = doc().documentElement;
    if (html && html.getAttribute && html.getAttribute('data-motion') === 'reduce') return true;
    try {
      return !!(root.matchMedia && root.matchMedia('(prefers-reduced-motion: reduce)').matches);
    } catch (e) {
      return false;
    }
  }

  var live = null;

  function mountStage(host, kase) {
    if (live) { live.stop(); live = null; }
    while (host.firstChild) host.removeChild(host.firstChild);
    var reduced = reducedWanted();
    var stage = renderAll(kase, { reduced: reduced });
    host.appendChild(stage);
    if (reduced) return;
    var start = sceneFromHash(root.location && root.location.hash);
    live = player(stage, scenes(kase), { autoplay: start < 0, start: start });
  }

  function mount() {
    var scope = doc().querySelector('[data-ss-explain]');
    if (!scope) return;
    fillKeys(scope);
    wireCopy(scope);
    var host = scope.querySelector('[data-ss-explain-stage]');
    var kase = root.SS_DATA && root.SS_DATA.explain;
    if (!host || !kase) return;
    mountStage(host, kase);
    try {
      var mq = root.matchMedia && root.matchMedia('(prefers-reduced-motion: reduce)');
      if (mq && mq.addEventListener) mq.addEventListener('change', function () { mountStage(host, kase); });
    } catch (e) { /* no media query support */ }
  }

  SS.explain = {
    IDS: IDS, TRY_HREF: TRY_HREF, scenes: scenes, rows: rows, excerpt: excerpt, renderScene: renderScene,
    renderAll: renderAll, player: player, sceneFromHash: sceneFromHash, mount: mount
  };

  var d = doc();
  if (d && typeof d.querySelector === 'function') {
    if (d.readyState === 'loading') d.addEventListener('DOMContentLoaded', mount);
    else mount();
  }
})();
