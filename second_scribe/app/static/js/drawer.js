/* SS.drawer: the evidence drawer (Task 27, R20: every number drills down to a quote and a log line).
     chain(view, state)        the links behind the chosen step, in this order:
                                 map (pid, step, who marked it)  ->  quote (verbatim, with speaker)  ->  judge label  ->
                                 log line (request_id, record_index)  ->  record hash (record_sha256)  ->  verify status.
                               A label reused from the judge cache (evidence.judge_cached) says so on the log line,
                               which then names the earlier identical judge request it came from.
                               A link the data lacks reads "not available" (never 0, never "not computed").
                               A step the AI drew but whose own quote was not found reads "Not attested by its own quote".
                               For study-public data only two links exist: the number and its source key (no quote, no log).
     itemsBehind(view, spec)   the steps behind a number: the reference-missing steps with how the AI drew each at spec.rung
                               (spec.map limits it to one map). Nothing is counted or rated here, only listed.
     runInfo(view)             the run block: run id, code version (sha256, or "not recorded": absent and all-zero values
                               both read that way, never zeros) and the log check (status word, checked time, and the
                               error codes of a check that failed, shown in mono)
     model(view, state, opts)  everything the panel shows, no DOM.
     render(root, view, state, opts)
                               opts.behind = {rung}: also list the steps behind a number; clicking one emits "item:open".
                               opts.status: the plain sentence of the box the reader chose (SS.lanes.sentenceFor, item
                               18); it leads the chain, before the quote and the log line.
   Reference type B: the panel titles read "researcher-marked absences" (the *.mode_b keys). The fixed note is the
   viewer's to print, once per screen.
   Data goes into the page through textContent and attributes only. Classic script; attaches to window.SS
   (after core, encoding, a11y, ledger). */
(function () {
  'use strict';
  var root = typeof window !== 'undefined' ? window : globalThis;
  var SS = root.SS = root.SS || {};
  var hasOwn = Object.prototype.hasOwnProperty;

  var STEP_KEYS = ['map', 'quote', 'judge', 'log', 'hash', 'verify'];
  var FATE_KEY = { kept: 'ledger.cell.kept', leaked: 'ledger.cell.leaked', missed: 'ledger.cell.never_drawn' };
  var VERIFY_KNOWN = { not_run: 1, ok: 1, passed: 1, failed: 1 };

  function isString(v) { return typeof v === 'string' && v !== ''; }
  // A recorded value. 0 is recorded; undefined, null, '', NaN and {status: ...} stand-ins are not.
  function recorded(v) {
    if (v === undefined || v === null || v === '') return false;
    if (typeof v === 'number') return isFinite(v);
    if (typeof v === 'object') return false;
    return true;
  }
  function maps(view) { return view && Array.isArray(view.maps) ? view.maps : []; }
  function mapOf(view, pid) {
    var list = maps(view);
    for (var i = 0; i < list.length; i++) if (list[i] && list[i].pid === pid) return list[i];
    return null;
  }
  function itemOf(map, id) {
    var items = map && Array.isArray(map.items) ? map.items : [];
    for (var i = 0; i < items.length; i++) if (items[i] && items[i].item_id === id) return items[i];
    return null;
  }
  function evidenceAt(item, rung) {
    var ev = item && item.evidence;
    if (ev === null || typeof ev !== 'object' || !hasOwn.call(ev, rung)) return null;
    var e = ev[rung];
    return e !== null && typeof e === 'object' && !Array.isArray(e) ? e : null;
  }
  function modeB(view) { return !!view && view.reference_type === 'B'; }
  function notAvailable() { return SS.t('drawer.not_available'); }

  function link(step, value, extra) {
    var l = { step: step, title: SS.t('drawer.step.' + step), value: value, missing: false, mono: false };
    if (extra) for (var k in extra) if (hasOwn.call(extra, k)) l[k] = extra[k];
    return l;
  }
  function gone(step) { return link(step, notAvailable(), { missing: true }); }

  // {value, errors}: the status in words (with the check time when the log was checked), and the error codes the
  // check returned (strings, as recorded). null when the file records no status.
  function verifyOf(verify) {
    if (verify === null || typeof verify !== 'object' || !recorded(verify.status)) return null;
    var status = String(verify.status);
    var word = hasOwn.call(VERIFY_KNOWN, status) ? SS.t('drawer.verify.' + status) : status;
    var value = word;
    // a log that was not checked has no check time worth printing
    if (status !== 'not_run' && recorded(verify.checked_at)) value = SS.t('drawer.verify_value', { status: word, checked_at: String(verify.checked_at) });
    var errors = Array.isArray(verify.errors) ? verify.errors.filter(isString).map(String) : [];
    return { value: value, errors: errors };
  }

  // The run block. The code hash is absent until the kit records it (and a stand-in of zeros is not a hash):
  // "not recorded", never zeros.
  function runInfo(view) {
    var run = view && view.run && typeof view.run === 'object' ? view.run : {};
    var code = isString(run.code_sha256) && !/^0+$/.test(run.code_sha256) ? run.code_sha256 : null;
    return {
      id: isString(run.run_id) ? run.run_id : null,
      code: code ? { value: code, missing: false } : { value: SS.t('drawer.run.code_none'), missing: true },
      verify: verifyOf(run.verify)
    };
  }

  // study-public data keeps no maps, items or quotes: only the number and the key it came from.
  function studyChain(s) {
    var num = recorded(s.number) ? link('number', String(s.number)) : gone('number');
    var key = isString(s.source_key) ? link('source', s.source_key, { mono: true }) : gone('source');
    return [num, key];
  }

  // SS.drawer.chain(view, state) -> [{step, value, title, missing, mono}]; [] when no step is chosen.
  function chain(view, state) {
    var s = state && typeof state === 'object' ? state : {};
    if (view && view.source === 'study-public') return studyChain(s);
    if (!isString(s.item)) return [];
    var map = mapOf(view, s.map);
    var item = itemOf(map, s.item);
    if (!item) return [];
    var rung = s.rung;
    var ev = evidenceAt(item, rung);
    var b = modeB(view);
    var label = item.label === undefined || item.label === null ? '' : String(item.label);
    var status = SS.t('a11y.item.ref_' + (item.reference_status === 'missing' ? 'missing' : 'happened') + (b ? '_b' : ''));
    var out = [link('map', SS.t('drawer.map_value', { pid: map.pid, label: label, status: status }))];

    // quote
    if (SS.encoding.fateAt(item, rung) === 'unattested') {
      out.push(link('quote', SS.t('lane.quote_not_found'), { missing: true }));
    } else if (ev && recorded(ev.quote)) {
      out.push(link('quote', recorded(ev.speaker) ?
        SS.t('drawer.quote_value', { quote: String(ev.quote), speaker: String(ev.speaker) }) :
        SS.t('drawer.quote_value_no_speaker', { quote: String(ev.quote) })));
    } else {
      out.push(gone('quote'));
    }
    // judge label
    out.push(ev && recorded(ev.judge_label) ? link('judge', String(ev.judge_label)) : gone('judge'));
    // log line: both parts are needed; record_index 0 is a recorded value. A label the judge cache served
    // (judge_cached: answered once for an identical element and quote, possibly in another map) says it was reused.
    out.push(ev && recorded(ev.request_id) && recorded(ev.record_index) ?
      link('log', SS.t(ev.judge_cached === true ? 'drawer.log_value_cached' : 'drawer.log_value',
        { request_id: String(ev.request_id), record_index: String(ev.record_index) }), { mono: true }) :
      gone('log'));
    // record hash
    out.push(ev && recorded(ev.record_sha256) ? link('hash', String(ev.record_sha256), { mono: true }) : gone('hash'));
    // verify status of the whole run, and the codes of a check that failed
    var vv = runInfo(view).verify;
    out.push(vv === null ? gone('verify') : link('verify', vv.value, { errors: vv.errors }));
    return out;
  }

  // The steps behind a number: every step the reference marks missing, with the AI's fate for it at spec.rung.
  function itemsBehind(view, spec) {
    var sp = spec && typeof spec === 'object' ? spec : {};
    var ran = view && Array.isArray(view.rungs) ? view.rungs : [];
    var out = [];
    maps(view).forEach(function (map) {
      if (!map || (isString(sp.map) && map.pid !== sp.map)) return;
      (Array.isArray(map.items) ? map.items : []).forEach(function (item) {
        if (!item || item.reference_status !== 'missing') return;
        var raw = SS.encoding.fateAt(item, sp.rung);
        var fate;
        if (ran.indexOf(sp.rung) < 0 || raw === undefined || raw === null) fate = 'not_run';
        else if (raw === 'kept' || raw === 'leaked' || raw === 'missed' || raw === 'unattested') fate = raw;
        else fate = 'unknown';
        var text;
        if (fate === 'unattested') text = SS.t('lane.quote_not_found');
        else if (fate === 'not_run') text = SS.t('ledger.cell.not_run');
        else if (hasOwn.call(FATE_KEY, fate)) text = SS.t(FATE_KEY[fate]);
        else text = SS.t('ledger.cell.unknown');
        out.push({
          map: map.pid, item_id: item.item_id, fate: fate, text: text,
          label: item.label === undefined || item.label === null ? '' : String(item.label)
        });
      });
    });
    return out;
  }

  function rungName(r) {
    return /^(rung1|rung2|placebo|rung3)$/.test(String(r)) ? SS.t('views.rung.' + r) : String(r);
  }

  // SS.drawer.model(view, state, opts) -> {modeB, title, hint, item, steps, run, behind}
  function model(view, state, opts) {
    var s = state && typeof state === 'object' ? state : {};
    var o = opts && typeof opts === 'object' ? opts : {};
    var b = modeB(view);
    var steps = chain(view, s);
    var m = {
      modeB: b,
      title: SS.t(b ? 'drawer.title.mode_b' : 'drawer.title'),
      steps: steps,
      run: runInfo(view),
      item: steps.length && isString(s.item) ? { map: s.map, item_id: s.item, rung: s.rung } : null,
      hint: steps.length ? null : SS.t('drawer.empty'),
      behind: null
    };
    if (o.behind && typeof o.behind === 'object' && isString(o.behind.rung)) {
      var rows = itemsBehind(view, { rung: o.behind.rung });
      m.behind = {
        rung: o.behind.rung,
        title: SS.t(b ? 'drawer.behind.title.mode_b' : 'drawer.behind.title', { rung: rungName(o.behind.rung) }),
        rows: rows,
        empty: rows.length ? null : SS.t('drawer.behind.none')
      };
    }
    return m;
  }

  function el(tag, cls, text, parent) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null && text !== '') n.textContent = text;
    if (parent) parent.appendChild(n);
    return n;
  }

  function errorCodes(dd, errors) {
    (errors || []).forEach(function (code) { el('code', 'ss-drawer-error-code mono', code, dd); });
  }

  // The run block: the run id, the code version and, when no step is chosen (the chain has its own), the log check.
  // opts.verify === false leaves the log check out.
  function renderRun(parent, info, opts) {
    var o = opts && typeof opts === 'object' ? opts : {};
    var box = el('div', 'ss-drawer-run', undefined, parent);
    box.setAttribute('data-part', 'run');
    el('h4', 'ss-drawer-run-title', SS.t('drawer.run.title'), box);
    var list = el('div', 'ss-drawer-run-list', undefined, box);
    function row(key, title, value, mono, missing, errors) {
      var r = el('div', 'ss-drawer-run-row' + (missing ? ' is-missing' : ''), undefined, list);
      r.setAttribute('data-run', key);
      el('span', 'ss-drawer-run-key', title, r);
      var val = el('span', 'ss-drawer-run-val' + (mono && !missing ? ' mono' : '') + (missing ? ' muted' : ''), value, r);
      errorCodes(val, errors);
    }
    if (info.id) row('id', SS.t('drawer.run.id'), info.id, true, false);
    row('code', SS.t('drawer.run.code'), info.code.value, true, info.code.missing);
    if (o.verify !== false) {
      if (info.verify) row('verify', SS.t('drawer.run.verify'), info.verify.value, false, false, info.verify.errors);
      else row('verify', SS.t('drawer.run.verify'), notAvailable(), false, true);
    }
    return box;
  }

  // SS.drawer.render(root, view, state, opts) -> the model it drew.
  function render(rootEl, view, state, opts) {
    var s = state && typeof state === 'object' ? state : {};
    var o = opts && typeof opts === 'object' ? opts : {};
    var m = model(view, s, o);
    rootEl.textContent = '';
    var box = el('div', 'ss-drawer', undefined, rootEl);
    box.setAttribute('role', 'region');
    box.setAttribute('aria-label', m.title);
    var head = el('div', 'ss-drawer-head', undefined, box);
    el('h3', 'ss-drawer-title', m.title, head);
    if (m.item) {
      var close = el('button', 'ss-drawer-close', SS.t('drawer.close'), head);
      close.setAttribute('type', 'button');
      close.addEventListener('click', function () {
        if (SS.state && typeof SS.state.set === 'function') SS.state.set({ item: null });
      });
    }
    var body = el('div', 'ss-drawer-body', undefined, box);
    body.setAttribute('aria-live', 'polite');
    if (m.hint) {
      el('p', 'ss-drawer-empty muted', m.hint, body);
    } else {
      if (m.item && isString(o.status)) {
        var said = el('p', 'ss-drawer-status', o.status, body);
        said.setAttribute('data-part', 'status');
      }
      var dl = el('dl', 'ss-drawer-chain', undefined, body);
      m.steps.forEach(function (c, i) {
        var row = el('div', 'ss-drawer-link' + (c.missing ? ' is-missing' : ''), undefined, dl);
        row.setAttribute('data-step', c.step);
        var dt = el('dt', 'ss-drawer-step', c.title, row);
        dt.setAttribute('data-n', String(i + 1));
        var dd = el('dd', 'ss-drawer-value ss-v-' + c.step + (c.mono && !c.missing ? ' mono' : '') + (c.missing ? ' muted' : ''), c.value, row);
        errorCodes(dd, c.errors);
      });
    }
    // opts.run === false (council 30, seat 02 F3): the page prints the run block in its Technical details, so the panel
    // opens on the hint alone and a chosen step shows only its own chain
    if (o.run !== false) renderRun(box, m.run, { verify: !m.steps.length });
    if (m.behind) {
      var sec = el('div', 'ss-drawer-behind', undefined, box);
      sec.setAttribute('data-behind-rung', m.behind.rung);
      var bh = el('div', 'ss-drawer-behind-head', undefined, sec);
      el('h4', 'ss-drawer-behind-title', m.behind.title, bh);
      var hide = el('button', 'ss-drawer-behind-close', SS.t('drawer.behind.close'), bh);
      hide.setAttribute('type', 'button');
      hide.addEventListener('click', function () { SS.bus.emit('number:close', {}); });
      if (m.behind.empty) {
        el('p', 'ss-drawer-empty muted', m.behind.empty, sec);
      } else {
        var ul = el('ul', 'ss-drawer-behind-list', undefined, sec);
        m.behind.rows.forEach(function (r) {
          var li = el('li', '', undefined, ul);
          var btn = el('button', 'ss-drawer-behind-item', undefined, li);
          btn.setAttribute('type', 'button');
          btn.setAttribute('data-item-id', r.item_id);
          btn.setAttribute('data-map', r.map);
          btn.setAttribute('data-fate', r.fate);
          el('span', 'ss-drawer-behind-label', r.label, btn);
          el('span', 'ss-drawer-behind-fate', r.text, btn);
          el('span', 'ss-drawer-behind-map mono', r.map, btn);
          btn.addEventListener('click', function () {
            SS.bus.emit('item:open', { item_id: r.item_id, map: r.map, rung: m.behind.rung, lane: 'ai' });
          });
        });
      }
    }
    return m;
  }

  SS.drawer = {
    STEPS: STEP_KEYS.slice(), chain: chain, itemsBehind: itemsBehind, runInfo: runInfo, model: model,
    renderRun: renderRun, render: render
  };
})();
