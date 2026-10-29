/* Setup step "Model check" (Task 35; spec 6.5 step 7; Part 5b, items 33, 35, 36): the model check of the pair on six
   of the shipped synthetic interviews (the reconstructor's part, then the judge's once it is loaded, then the result,
   with no override when it fails). Before it starts the step says the calls by part and the time (GET
   /api/model-check `plan`); while it runs, one progress line per part, a line saying what it is doing now, and a
   warning when the model server is not answering (`check.progress.server`); when both models share one Ollama
   server the app may switch them itself (`swap_allowed`, POST /api/model-check/continue {auto: true}).
   How long a run takes is estimated on the Run screen, beside Start; this file keeps the drawing of an estimate's
   result (`estimateResult`, with the optional price per million tokens for a remote role, kept in `prices`, this
   page's memory only: never sent, saved or put in storage). Classic script; queues itself for SS.setup
   (app-setup.js). */
(function () {
  'use strict';
  var root = typeof window !== 'undefined' ? window : globalThis;
  var SS = root.SS = root.SS || {};
  var EXITS = ['verify_failed', 'refused', 'fixture_missing', 'stopped', 'crashed'];
  var SERVER = ['timeout', 'no_connection', 'transport', 'no_reply'];
  var POLL_MS = 1500;
  var RUN_HASH = '#instructions';
  var prices = {};
  var uid = 0;
  var timer = null;

  function el(doc, tag, cls, text) {
    var n = doc.createElement(tag);
    if (cls) n.setAttribute('class', cls);
    if (text !== undefined) n.textContent = text;
    return n;
  }

  function clear(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
  }

  function button(doc, key, cls) {
    var b = el(doc, 'button', cls || 'primary', SS.t(key));
    b.setAttribute('type', 'button');
    return b;
  }

  function errorBox(doc) {
    var box = el(doc, 'p', 'ss-setup-error');
    box.setAttribute('role', 'alert');
    return box;
  }

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

  // Seconds -> "3 h 20 min", "45 min", "under a minute".
  function duration(s) {
    if (typeof s !== 'number' || !isFinite(s) || s < 0) return SS.fmt.value(null);
    var m = Math.round(s / 60);
    if (m < 1) return SS.t('setup.estimate.under_minute');
    if (m < 60) return SS.t('setup.estimate.minutes', { m: m });
    return SS.t('setup.estimate.hours', { h: Math.floor(m / 60), m: m % 60 });
  }

  function thousands(n) {
    return String(Math.round(n)).replace(/\B(?=(\d{3})+(?!\d))/g, ',');
  }

  function roleWord(role) {
    return SS.t('setup.models.role_word.' + role);
  }

  function runTitle() {
    return SS.t('instructions.title');
  }

  // The "model server is not answering" line from a progress `server` ({code, seconds}), or null.
  function serverLine(doc, server) {
    if (!server || typeof server.code !== 'string') return null;
    var m = /^http_(\d{3})$/.exec(server.code);
    var key = m ? 'jobs.server.http' : 'jobs.server.' + (SERVER.indexOf(server.code) >= 0 ? server.code : 'transport');
    var p = el(doc, 'p', 'ss-check-server', SS.t(key, { status: m ? m[1] : '', time: duration(server.seconds) }));
    p.setAttribute('role', 'alert');
    return p;
  }

  // --- the model check -------------------------------------------------------------------------------------
  function stopPolling() {
    if (timer !== null) {
      root.clearTimeout(timer);
      timer = null;
    }
  }

  function poll(box) {
    stopPolling();
    if (typeof root.setTimeout !== 'function') return;
    timer = root.setTimeout(function () {
      timer = null;
      if (box.isConnected === false) return;     // the step was redrawn or left
      SS.api.get('/api/model-check').then(function (reply) {
        drawCheck(box, reply || {});
      }, function () { /* the next refresh shows it */ });
    }, POLL_MS);
  }

  // Before the check starts: the calls by part, the synthetic cases, the time, and what the user can do meanwhile.
  function planBlock(doc, plan) {
    var b = el(doc, 'div', 'ss-check-plan');
    if (!plan || !Array.isArray(plan.parts)) return b;
    var by = {};
    plan.parts.forEach(function (p) { if (p && p.role) by[p.role] = p; });
    var recon = by.recon ? by.recon.total : 0;
    var judge = by.judge && by.judge.total ? by.judge.total : 0;
    b.appendChild(el(doc, 'p', '', judge
      ? SS.t('setup.check.before.calls', { total: plan.total, recon: recon, judge: judge })
      : SS.t('setup.check.before.calls_no_judge', { recon: recon })));
    if (typeof plan.cases === 'number') b.appendChild(el(doc, 'p', '', SS.t('setup.check.before.cases', { cases: plan.cases })));
    var t = plan.time;
    b.appendChild(el(doc, 'p', '', t && typeof t.low_s === 'number' && typeof t.high_s === 'number'
      ? SS.t('setup.check.before.time', { low: duration(t.low_s), high: duration(t.high_s) })
      : SS.t('setup.check.before.time_unknown')));
    if (plan.one_server && judge) b.appendChild(el(doc, 'p', '', SS.t('setup.check.before.one_server')));
    b.appendChild(el(doc, 'p', 'ss-setup-hint', SS.t('setup.check.before.meanwhile')));
    b.appendChild(el(doc, 'p', 'ss-setup-hint', SS.t('setup.check.before.once')));
    return b;
  }

  function nowLine(doc, p) {
    var code = p.now;
    var text = null;
    if (code === 'model_calls') {
      text = SS.t(p.part === 'J' ? 'setup.check.now.model_calls_judge' : 'setup.check.now.model_calls_recon');
    } else if (code === 'determinism') {
      text = SS.t('setup.check.now.determinism', { k: typeof p.determinism_k === 'number' ? p.determinism_k : '' });
    } else if (['starting', 'finishing', 'judge_check', 'switching_models'].indexOf(code) >= 0) {
      text = SS.t('setup.check.now.' + code);
    }
    if (text === null) return null;
    var n = el(doc, 'p', 'ss-check-now', text);
    n.setAttribute('data-now', code);
    return n;
  }

  // While it runs (and while it waits for the judge): one bar and line for all, one line per part, the "now" line
  // and the server warning.
  function progressBlock(doc, p) {
    var b = el(doc, 'div', 'ss-check-progress');
    if (typeof p.done === 'number' && typeof p.total === 'number' && p.total > 0) {
      var bar = el(doc, 'progress', 'ss-check-bar');
      bar.setAttribute('max', String(p.total));
      bar.setAttribute('value', String(Math.min(p.done, p.total)));
      bar.setAttribute('aria-label', SS.t('setup.check.progress', { done: p.done, total: p.total }));
      b.appendChild(bar);
      b.appendChild(el(doc, 'p', 'ss-setup-hint', SS.t('setup.check.progress', { done: p.done, total: p.total })));
    }
    if (Array.isArray(p.parts) && p.parts.length) {
      var ul = el(doc, 'ul', 'ss-check-parts');
      p.parts.forEach(function (part) {
        if (!part || typeof part.done !== 'number' || typeof part.total !== 'number' || !part.total) return;
        var li = el(doc, 'li', 'ss-check-part', SS.t(part.role === 'judge' ? 'setup.check.part.judge' : 'setup.check.part.recon',
          { done: part.done, total: part.total }));
        li.setAttribute('data-part', String(part.part || ''));
        li.setAttribute('data-done', part.done >= part.total ? 'true' : 'false');
        if (part.part === p.part) li.setAttribute('aria-current', 'step');
        ul.appendChild(li);
      });
      b.appendChild(ul);
    }
    var now = nowLine(doc, p);
    if (now) b.appendChild(now);
    var server = serverLine(doc, p.server);
    if (server) b.appendChild(server);
    return b;
  }

  function nextLine(doc) {
    var p = el(doc, 'p', 'ss-check-next');
    p.appendChild(el(doc, 'strong', '', SS.t('setup.check.next') + ' '));
    var a = el(doc, 'a', '', SS.t('setup.check.next_link', { run: runTitle() }));
    a.setAttribute('href', RUN_HASH);
    p.appendChild(a);
    return p;
  }

  function continueWith(box, reply, err, btn, body, status) {
    var doc = box.ownerDocument || root.document;
    var busy = body.auto === true ? nowLine(doc, { now: 'switching_models' }) : null;
    if (busy) status.appendChild(busy);
    act(btn, err, function () {
      return SS.api.post('/api/model-check/continue', body).then(function (r) {
        drawCheck(box, Object.assign({}, reply, r));
      }, function (e) {
        if (busy && (busy.parentNode === status || Array.prototype.indexOf.call(status.children || [], busy) >= 0)) {
          status.removeChild(busy);
        }
        throw e;
      });
    });
  }

  function drawCheck(box, reply) {
    var doc = box.ownerDocument || root.document;
    var c = reply.check || { state: 'not_run' };
    var p = c.progress || {};
    stopPolling();
    clear(box);
    box.setAttribute('data-state', c.state);
    box.appendChild(el(doc, 'h3', 'ss-check-head', SS.t('setup.check.heading')));
    box.appendChild(el(doc, 'p', 'ss-setup-hint', SS.t('setup.check.what')));
    var err = errorBox(doc);
    var status = el(doc, 'div', 'ss-check-status');
    status.setAttribute('role', 'status');
    status.setAttribute('aria-live', 'polite');
    box.appendChild(status);
    if (c.state === 'not_run' || c.state === 'error') {
      if (c.state === 'error') {
        var e = el(doc, 'div', 'ss-check-notice');
        e.appendChild(el(doc, 'p', '', SS.t('setup.check.error')));
        var code = EXITS.indexOf(c.error) >= 0 ? c.error : 'crashed';
        e.appendChild(el(doc, 'p', 'ss-setup-hint', SS.t('setup.check.error_code',
          { code: SS.t('setup.check.exit.' + code) })));
        status.appendChild(e);
      } else {
        status.appendChild(el(doc, 'p', '', SS.t('setup.check.not_run')));
      }
      box.appendChild(planBlock(doc, reply.plan));
      var start = button(doc, 'setup.check.start');
      start.addEventListener('click', function () {
        act(start, err, function () {
          return SS.api.post('/api/model-check/start', {}).then(function (r) {
            drawCheck(box, Object.assign({}, reply, r));
          });
        });
      });
      box.appendChild(start);
    } else if (c.state === 'q_running' || c.state === 'j_running') {
      status.appendChild(el(doc, 'p', 'ss-check-running',
        SS.t(c.state === 'q_running' ? 'setup.check.running_q' : 'setup.check.running_j')));
      status.appendChild(progressBlock(doc, p));
      poll(box);
    } else if (c.state === 'needs_swap') {
      var switching = p.now === 'switching_models';
      if (!switching) {
        var n = el(doc, 'div', 'ss-check-notice');
        n.appendChild(el(doc, 'p', '', SS.t('setup.check.needs_swap', { model: reply.judge_model || '' })));
        if (reply.swap_allowed === true) {
          n.appendChild(el(doc, 'p', '', SS.t('setup.check.swap.auto_note', { model: reply.judge_model || '' })));
        }
        n.appendChild(el(doc, 'p', 'ss-setup-hint', SS.t('setup.check.swap.howto', { model: reply.judge_model || '' })));
        status.appendChild(n);
      }
      status.appendChild(progressBlock(doc, p));
      if (!switching) {
        var row = el(doc, 'div', 'ss-check-actions');
        if (reply.swap_allowed === true) {
          var auto = button(doc, 'setup.check.swap.auto');
          auto.setAttribute('data-action', 'swap_auto');
          auto.addEventListener('click', function () { continueWith(box, reply, err, auto, { auto: true }, status); });
          row.appendChild(auto);
        }
        var go = button(doc, 'setup.check.continue', reply.swap_allowed === true ? 'ss-check-continue' : 'primary');
        go.setAttribute('data-action', 'continue');
        go.addEventListener('click', function () { continueWith(box, reply, err, go, {}, status); });
        row.appendChild(go);
        box.appendChild(row);
      }
      if (switching) poll(box);                   // reopened while the app switches the models: show when it ends
    } else if (c.state === 'passed') {
      status.appendChild(el(doc, 'p', 'ss-check-passed', SS.t('setup.check.passed')));
      box.appendChild(nextLine(doc));
    } else if (c.state === 'failed') {
      var f = el(doc, 'div', 'ss-check-notice ss-check-failed');
      f.appendChild(el(doc, 'p', 'ss-check-failed-head', SS.t('setup.check.failed')));
      if (Array.isArray(c.failures) && c.failures.length) {
        f.appendChild(el(doc, 'p', 'ss-setup-hint', SS.t('setup.check.failed_codes')));
        var list = el(doc, 'ul', 'ss-check-codes');
        c.failures.forEach(function (fc) {
          var item = el(doc, 'li', '');
          item.appendChild(el(doc, 'code', 'mono', String(fc)));
          list.appendChild(item);
        });
        f.appendChild(list);
      }
      f.appendChild(el(doc, 'p', '', SS.t('setup.check.failed_note', { step: SS.t('setup.models.title') })));
      status.appendChild(f);
    }
    if (c.state !== 'passed') box.appendChild(timeMoved(doc));   // passed: the Next line links to Run instead
    box.appendChild(err);
  }

  // --- the time estimate -------------------------------------------------------------------------------------
  function costLines(doc, box, est) {
    (est.remote_roles || []).forEach(function (role) {
      var tokens = (est.tokens || {})[role];
      if (typeof tokens !== 'number') return;
      uid += 1;
      var id = 'ss-check-price-' + role + '-' + uid;
      var wrap = el(doc, 'div', 'ss-setup-field ss-check-cost');
      var label = el(doc, 'label', 'ss-setup-label', SS.t('setup.estimate.cost_label', { role: roleWord(role) }));
      label.setAttribute('for', id);
      var input = el(doc, 'input', 'ss-setup-input ss-check-price');
      input.setAttribute('id', id);
      input.setAttribute('type', 'number');
      input.setAttribute('min', '0');
      input.setAttribute('step', 'any');
      input.setAttribute('inputmode', 'decimal');
      input.setAttribute('autocomplete', 'off');
      if (prices[role] !== undefined) input.value = String(prices[role]);
      var out = el(doc, 'p', 'ss-check-cost-line');
      out.setAttribute('aria-live', 'polite');
      function update() {
        var price = parseFloat(input.value);
        if (!isFinite(price) || price < 0) {
          delete prices[role];
          out.textContent = '';
          return;
        }
        prices[role] = price;
        out.textContent = SS.t('setup.estimate.cost', { tokens: thousands(tokens), role: roleWord(role),
          amount: (tokens / 1e6 * price).toFixed(2) });
      }
      input.addEventListener('input', update);
      wrap.appendChild(label);
      wrap.appendChild(input);
      wrap.appendChild(out);
      box.appendChild(wrap);
      update();
    });
    if ((est.remote_roles || []).length) box.appendChild(el(doc, 'p', 'ss-setup-hint', SS.t('setup.estimate.cost_note')));
  }

  function drawResult(doc, box, est) {
    clear(box);
    if (!est) return;
    var low = duration(est.low_s);
    var high = duration(est.high_s);
    var under = SS.t('setup.estimate.under_minute');
    var time;
    if (high === under) time = SS.t('setup.estimate.result_short');
    else if (low === high) time = SS.t('setup.estimate.result_one', { time: high });
    else if (low === under) time = SS.t('setup.estimate.result_upto', { time: high });
    else time = SS.t('setup.estimate.result', { low: low, high: high });
    box.appendChild(el(doc, 'p', 'ss-check-time', time));
    var calls = est.calls || {};
    box.appendChild(el(doc, 'p', '', est.tokens && est.tokens.judge !== undefined
      ? SS.t('setup.estimate.calls', { q: thousands(calls.Q || 0), j: thousands(calls.J || 0) })
      : SS.t('setup.estimate.calls_no_judge', { q: thousands(calls.Q || 0) })));
    var w = est.words || {};
    box.appendChild(el(doc, 'p', 'ss-setup-hint', SS.t('setup.estimate.basis',
      { seconds: est.probe_s, demo: w.demo, median: w.median })));
    if (est.includes_check) box.appendChild(el(doc, 'p', 'ss-setup-hint', SS.t('setup.estimate.includes_check')));
    costLines(doc, box, est);
  }

  // Where the time estimate went (the Run screen, beside Start), for a check that has not passed yet.
  function timeMoved(doc) {
    var p = el(doc, 'p', 'ss-setup-hint ss-check-time-moved', SS.t('setup.check.time_moved', { run: runTitle() }) + ' ');
    var a = el(doc, 'a', '', SS.t('setup.check.go_run', { run: runTitle() }));
    a.setAttribute('href', RUN_HASH);
    p.appendChild(a);
    return p;
  }

  function draw(container, reply) {
    var doc = container.ownerDocument || root.document;
    clear(container);
    container.appendChild(el(doc, 'p', 'ss-setup-intro', SS.t('setup.check.intro')));
    if (!reply.ready) {
      var n = el(doc, 'p', 'ss-setup-note', SS.t('setup.check.not_ready', { step: SS.t('setup.models.title') }));
      container.appendChild(n);
      if (reply.problem) container.appendChild(el(doc, 'p', 'ss-setup-hint', reply.problem));
      return;
    }
    var check = el(doc, 'section', 'ss-check-box');
    container.appendChild(check);
    drawCheck(check, reply);
  }

  function render(container) {
    var doc = container.ownerDocument || root.document;
    stopPolling();
    clear(container);
    container.appendChild(el(doc, 'p', 'ss-shell-note', SS.t('setup.check.loading')));
    return SS.api.get('/api/model-check').then(function (reply) {
      draw(container, reply || {});
    }, function (err) {
      clear(container);
      container.appendChild(el(doc, 'p', 'ss-setup-error', (err && err.message) || SS.t('api.failed')));
    });
  }

  SS.setupCheck = { render: render, draw: draw, duration: duration, prices: prices, serverLine: serverLine,
    // The Run screen draws an estimate's result with this (time, calls, basis, the optional price lines).
    estimateResult: function (box, est) { drawResult(box.ownerDocument || root.document, box, est); },
    reset: function () {
      stopPolling();
      Object.keys(prices).forEach(function (k) { delete prices[k]; });
    } };

  (SS.setupQueue = SS.setupQueue || []).push(['check', {
    order: 70, titleKey: 'setup.check.title', needsProject: true, render: render
  }]);
})();
