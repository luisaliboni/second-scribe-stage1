'use strict';
// Task 35, Part 5b: the Setup step "Model check" (app-setup-check.js): the model check's states, and the drawing of an
// estimate's result that the Run screen uses. The app's
// scripts load in the server's order into a vm context with a small stub DOM and a fake fetch.
const test = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ROOT = path.resolve(__dirname, '..', '..');
const JS = path.join(ROOT, 'second_scribe', 'app', 'static', 'js');
const STRINGS = JSON.parse(fs.readFileSync(path.join(ROOT, 'second_scribe', 'app', 'strings', 'en.json'), 'utf8'));

function makeNode(tag) {
  const node = {
    tagName: String(tag).toUpperCase(), children: [], attributes: {}, listeners: {}, textContent: '',
    disabled: false, value: '', checked: false,
    get firstChild() { return node.children[0] || null; },
    setAttribute(k, v) { node.attributes[k] = String(v); },
    getAttribute(k) { return Object.prototype.hasOwnProperty.call(node.attributes, k) ? node.attributes[k] : null; },
    appendChild(c) { node.children.push(c); return c; },
    removeChild(c) { node.children.splice(node.children.indexOf(c), 1); return c; },
    addEventListener(type, fn) { (node.listeners[type] = node.listeners[type] || []).push(fn); },
    fire(type) { (node.listeners[type] || []).forEach((fn) => fn({ preventDefault() {} })); },
  };
  return node;
}

function all(node, pred, out) {
  out = out || [];
  if (pred(node)) out.push(node);
  node.children.forEach((c) => all(c, pred, out));
  return out;
}
const text = (node) => [node.textContent].concat(node.children.map(text)).join(' ');
const byClass = (node, cls) => all(node, (n) => (n.attributes.class || '').split(' ').indexOf(cls) >= 0);
const byTag = (node, tag) => all(node, (n) => n.tagName === tag.toUpperCase());
const buttonOf = (node, key) => byTag(node, 'button').find((b) => b.textContent === STRINGS[key]);
const fill = (s, kw) => s.replace(/\{(\w+)\}/g, (m, k) => (k in kw ? kw[k] : m));

function appScripts() {
  const screens = fs.readdirSync(JS).filter((f) => /^app-[a-z0-9-]+\.js$/.test(f) && f !== 'app-shell.js').sort();
  return ['core.js', 'api.js', 'app-shell.js'].concat(screens);
}

function reply(status, doc) {
  return { ok: status >= 200 && status < 300, status, text: () => Promise.resolve(JSON.stringify(doc)) };
}

function load(routes) {
  const calls = [];
  const errors = [];
  const sandbox = {
    console: { error: (...a) => errors.push(a.map(String).join(' ')), log() {}, warn() {} },
    location: { hash: '', search: '' },
    SS_STRINGS: STRINGS,
    document: { createElement: makeNode, addEventListener() {} },
    fetch: (url, init) => {
      const body = init.body ? JSON.parse(init.body) : undefined;
      calls.push({ url, method: init.method, body });
      if (url === '/api/session') return Promise.resolve(reply(200, { csrf: 'tok' }));
      const fn = routes[url];
      if (!fn) return Promise.resolve(reply(404, { error: 'not_found', message: 'no route ' + url }));
      const out = fn(body, calls);
      return Promise.resolve(Array.isArray(out) ? reply(out[0], out[1]) : reply(200, out));
    },
  };
  sandbox.window = sandbox;
  const ctx = vm.createContext(sandbox);
  for (const f of appScripts()) vm.runInContext(fs.readFileSync(path.join(JS, f), 'utf8'), ctx, { filename: f });
  return { SS: sandbox.SS, calls, errors };
}

const tick = () => new Promise((r) => setTimeout(r, 0));
async function settle() { for (let i = 0; i < 12; i++) await tick(); }


function reply0(check, extra) {
  return Object.assign({ check: Object.assign({ state: 'not_run', pair_sha256: 'c'.repeat(64), error: null,
    progress: null, failures: [], two_roles: true }, check || {}), ready: true, problem: null,
    judge_model: 'gemma-mock:9b' }, extra || {});
}

async function mount(routes) {
  const env = load(routes);
  const container = makeNode('div');
  env.SS.setupCheck.reset();
  await env.SS.setupCheck.render(container);
  await settle();
  return Object.assign(env, { container });
}

test('the step registers at order 70, after the models step', async () => {
  const { SS, errors } = await mount({ '/api/model-check': () => reply0() });
  assert.deepStrictEqual(errors, []);
  const ids = Array.from(SS.setup.list());
  assert.ok(ids.indexOf('check') > ids.indexOf('models'), ids.join(','));
});

test('before the models step is done the step says so and offers nothing to run', async () => {
  const { container } = await mount({ '/api/model-check': () => reply0({}, { ready: false,
    problem: STRINGS['models.error.recon_missing'] }) });
  const t = text(container);
  const modelsTitle = STRINGS['setup.models.title'];
  assert.ok(t.indexOf(fill(STRINGS['setup.check.not_ready'], { step: modelsTitle })) >= 0);
  assert.ok(STRINGS['setup.check.not_ready'].indexOf('{step}') >= 0, 'names the step by its title string');
  assert.ok(!/\b(above|below)\b/i.test(t), 'no direction words: the steps are a list on the left');
  assert.ok(t.indexOf(STRINGS['models.error.recon_missing']) >= 0);
  assert.strictEqual(byTag(container, 'button').length, 0);
});

test('not run: Start posts, then the reconstructor part shows its progress', async () => {
  const { container, calls } = await mount({
    '/api/model-check': () => reply0(),
    '/api/model-check/start': () => reply0({ state: 'q_running', progress: { done: 12, total: 132 } }),
  });
  assert.ok(text(container).indexOf(STRINGS['setup.check.not_run']) >= 0);
  buttonOf(container, 'setup.check.start').fire('click');
  await settle();
  assert.ok(calls.some((c) => c.url === '/api/model-check/start' && c.method === 'POST'));
  const t = text(container);
  assert.ok(t.indexOf(STRINGS['setup.check.running_q']) >= 0);
  assert.ok(t.indexOf(fill(STRINGS['setup.check.progress'], { done: 12, total: 132 })) >= 0);
  const bar = byTag(container, 'progress')[0];
  assert.strictEqual(bar.attributes.max, '132');
  assert.strictEqual(bar.attributes.value, '12');
  assert.strictEqual(buttonOf(container, 'setup.check.start'), undefined);
});

test('needs swap: names the judge model, and Continue posts', async () => {
  const { container, calls } = await mount({
    '/api/model-check': () => reply0({ state: 'needs_swap' }),
    '/api/model-check/continue': () => reply0({ state: 'j_running', progress: { done: 0, total: 443 } }),
  });
  assert.ok(text(container).indexOf(fill(STRINGS['setup.check.needs_swap'], { model: 'gemma-mock:9b' })) >= 0);
  buttonOf(container, 'setup.check.continue').fire('click');
  await settle();
  assert.ok(calls.some((c) => c.url === '/api/model-check/continue'));
  assert.ok(text(container).indexOf(STRINGS['setup.check.running_j']) >= 0);
});

test('passed reads as passed; failed shows the codes and offers no way to run', async () => {
  let env = await mount({ '/api/model-check': () => reply0({ state: 'passed' }) });
  assert.ok(text(env.container).indexOf(STRINGS['setup.check.passed']) >= 0);
  assert.strictEqual(buttonOf(env.container, 'setup.check.start'), undefined);
  env = await mount({ '/api/model-check': () => reply0({ state: 'failed',
    failures: ['canary_wished_low:0', 'model_check_judge_negated:1'] }) });
  const t = text(env.container);
  assert.ok(t.indexOf(STRINGS['setup.check.failed']) >= 0);
  assert.ok(STRINGS['setup.check.failed'].indexOf('larger model') >= 0);
  assert.deepStrictEqual(byTag(byClass(env.container, 'ss-check-codes')[0], 'code').map(text),
    ['canary_wished_low:0', 'model_check_judge_negated:1'], 'one code per line');
  assert.ok(t.indexOf(fill(STRINGS['setup.check.failed_note'], { step: STRINGS['setup.models.title'] })) >= 0);
  assert.ok(!/\b(above|below)\b/i.test(STRINGS['setup.check.failed_note']));
  assert.strictEqual(buttonOf(env.container, 'setup.check.start'), undefined, 'no override');
  assert.strictEqual(buttonOf(env.container, 'setup.check.continue'), undefined);
});

test('an error names the exit in words (never raw output) and offers Start again', async () => {
  const { container } = await mount({ '/api/model-check': () => reply0({ state: 'error', error: 'refused' }) });
  const t = text(container);
  assert.ok(t.indexOf(STRINGS['setup.check.error']) >= 0);
  assert.ok(t.indexOf(fill(STRINGS['setup.check.error_code'], { code: STRINGS['setup.check.exit.refused'] })) >= 0);
  assert.ok(buttonOf(container, 'setup.check.start'));
  const odd = await mount({ '/api/model-check': () => reply0({ state: 'error', error: '<b>raw</b>' }) });
  assert.ok(text(odd.container).indexOf('<b>raw</b>') < 0);
});

// Part 5b: Setup has no estimate of its own any more; the Run screen draws the result with SS.setupCheck.estimateResult.
test('the estimate result (drawn on the Run screen) shows the range, calls and basis; a price stays in the page', async () => {
  const est = { low_s: 7 * 3600, high_s: 16 * 3600 + 30 * 60, per_stage: { Q: 1, J: 2 }, calls: { Q: 1234, J: 5678, M: 0 },
    tokens: { recon: 2500000, judge: 900000 }, words: { median: 9000, demo: 301 }, demo_pid: 'SYN-OF03',
    probe_s: 14.2, remote_roles: ['judge'], range: [0.7, 1.6], includes_check: true };
  const { SS, container, calls } = await mount({ '/api/model-check': () => reply0({ state: 'passed' }) });
  assert.strictEqual(buttonOf(container, 'instructions.estimate.run'), undefined, 'no estimate in Setup');
  assert.strictEqual(byTag(container, 'input').length, 0, 'no rung choice in Setup');
  const box = makeNode('div');
  SS.setupCheck.estimateResult(box, est);
  const t = text(box);
  assert.ok(t.indexOf(fill(STRINGS['setup.estimate.result'], { low: fill(STRINGS['setup.estimate.hours'], { h: 7, m: 0 }),
    high: fill(STRINGS['setup.estimate.hours'], { h: 16, m: 30 }) })) >= 0, t);
  assert.ok(t.indexOf(fill(STRINGS['setup.estimate.calls'], { q: '1,234', j: '5,678' })) >= 0);
  assert.ok(t.indexOf(fill(STRINGS['setup.estimate.basis'], { seconds: 14.2, demo: 301, median: 9000 })) >= 0);
  assert.ok(t.indexOf(STRINGS['setup.estimate.includes_check']) >= 0);
  const price = byClass(box, 'ss-check-price');
  assert.strictEqual(price.length, 1, 'one cost line: the remote judge');
  price[0].value = '3';
  price[0].fire('input');
  assert.ok(text(box).indexOf(fill(STRINGS['setup.estimate.cost'], { tokens: '900,000',
    role: STRINGS['setup.models.role_word.judge'], amount: '2.70' })) >= 0);
  assert.strictEqual(SS.setupCheck.prices.judge, 3);
  assert.ok(calls.every((c) => c.url !== '/api/estimate'), 'Setup never times a call');
  assert.ok(text(box).indexOf(STRINGS['setup.estimate.cost_note']) >= 0);
});

test('durations read in words', () => {
  const { SS } = load({});
  assert.strictEqual(SS.setupCheck.duration(20), STRINGS['setup.estimate.under_minute']);
  assert.strictEqual(SS.setupCheck.duration(45 * 60), fill(STRINGS['setup.estimate.minutes'], { m: 45 }));
  assert.strictEqual(SS.setupCheck.duration(3 * 3600 + 20 * 60), fill(STRINGS['setup.estimate.hours'], { h: 3, m: 20 }));
});

test('a range whose ends read the same (or starts under a minute) reads as one time', async () => {
  const { SS } = load({});
  for (const [low, high, want] of [[10, 25, STRINGS['setup.estimate.result_short']],
    [45 * 60 + 5, 45 * 60 + 20, fill(STRINGS['setup.estimate.result_one'], { time: fill(STRINGS['setup.estimate.minutes'], { m: 45 }) })],
    [20, 3 * 60, fill(STRINGS['setup.estimate.result_upto'], { time: fill(STRINGS['setup.estimate.minutes'], { m: 3 }) })]]) {
    const est = { low_s: low, high_s: high, per_stage: { Q: 1, J: 1 }, calls: { Q: 10, J: 20, M: 0 }, tokens: { recon: 10 },
      words: { median: 300, demo: 301 }, demo_pid: 'SYN-OF03', probe_s: 0.1, remote_roles: [], range: [0.7, 1.6],
      includes_check: false };
    const box = makeNode('div');
    SS.setupCheck.estimateResult(box, est);
    const t = text(box);
    assert.ok(t.indexOf(want) >= 0, t);
    assert.ok(byClass(box, 'ss-check-time').length === 1 && text(byClass(box, 'ss-check-time')[0]) === want, t);
  }
});

// --- Part 5b (items 33, 35, 36; scout F1, F2, F3, F14) ---------------------------------------------------------
const PLAN = { parts: [{ part: 'Q', role: 'recon', calls: 14, determinism: 6, judge_check: 0, total: 20 },
  { part: 'J', role: 'judge', calls: 32, determinism: 6, judge_check: 1, total: 39 }], total: 59,
  time: { low_s: 427, high_s: 976, per_part: { Q: 176, J: 434 }, range: [0.7, 1.6] }, cases: 6, one_server: true };

test('before it starts: the calls by part, the six cases, the time, keep setting up, runs again only on a change', async () => {
  const { container } = await mount({ '/api/model-check': () => reply0({}, { plan: PLAN }) });
  const t = text(container);
  assert.ok(t.indexOf(fill(STRINGS['setup.check.before.calls'], { total: 59, recon: 20, judge: 39 })) >= 0, t);
  assert.ok(t.indexOf(fill(STRINGS['setup.check.before.cases'], { cases: 6 })) >= 0);
  assert.ok(t.indexOf(fill(STRINGS['setup.check.before.time'], { low: fill(STRINGS['setup.estimate.minutes'], { m: 7 }),
    high: fill(STRINGS['setup.estimate.minutes'], { m: 16 }) })) >= 0, t);
  assert.ok(t.indexOf(STRINGS['setup.check.before.one_server']) >= 0);
  assert.ok(t.indexOf(STRINGS['setup.check.before.meanwhile']) >= 0);
  assert.ok(t.indexOf(STRINGS['setup.check.before.once']) >= 0);
  assert.ok(buttonOf(container, 'setup.check.start'));
  const link = byTag(container, 'a').find((a) => a.attributes.href === '#instructions');
  assert.ok(link, 'the time estimate is on the Run screen, linked');
  assert.ok(t.indexOf(fill(STRINGS['setup.check.time_moved'], { run: STRINGS['instructions.title'] })) >= 0);
  const noJudge = await mount({ '/api/model-check': () => reply0({}, { plan: { parts: [PLAN.parts[0]], total: 20,
    time: null, cases: 6, one_server: false } }) });
  const u = text(noJudge.container);
  assert.ok(u.indexOf(fill(STRINGS['setup.check.before.calls_no_judge'], { recon: 20 })) >= 0, u);
  assert.ok(u.indexOf(STRINGS['setup.check.before.time_unknown']) >= 0);
  assert.ok(u.indexOf(STRINGS['setup.check.before.one_server']) < 0);
});

test('while it runs: one line for all, one line per part, what it is doing now, and the server warning', async () => {
  const progress = { done: 23, total: 59, part: 'J', now: 'determinism', determinism_k: 6,
    parts: [{ part: 'Q', role: 'recon', done: 20, total: 20 }, { part: 'J', role: 'judge', done: 3, total: 39 }],
    server: { code: 'http_503', seconds: 125, errors: 2 } };
  const { container } = await mount({ '/api/model-check': () => reply0({ state: 'j_running', progress }, { plan: PLAN }) });
  const t = text(container);
  assert.ok(t.indexOf(fill(STRINGS['setup.check.progress'], { done: 23, total: 59 })) >= 0, t);
  assert.ok(t.indexOf(fill(STRINGS['setup.check.part.recon'], { done: 20, total: 20 })) >= 0);
  assert.ok(t.indexOf(fill(STRINGS['setup.check.part.judge'], { done: 3, total: 39 })) >= 0);
  const parts = byClass(container, 'ss-check-part');
  assert.deepStrictEqual(parts.map((p) => p.attributes['aria-current'] || ''), ['', 'step']);
  assert.ok(t.indexOf(fill(STRINGS['setup.check.now.determinism'], { k: 6 })) >= 0);
  const warn = byClass(container, 'ss-check-server');
  assert.strictEqual(warn.length, 1);
  assert.strictEqual(warn[0].attributes.role, 'alert');
  assert.strictEqual(warn[0].textContent, fill(STRINGS['jobs.server.http'], { status: '503',
    time: fill(STRINGS['setup.estimate.minutes'], { m: 2 }) }));
  for (const [now, part, key] of [['model_calls', 'Q', 'setup.check.now.model_calls_recon'],
    ['model_calls', 'J', 'setup.check.now.model_calls_judge'], ['judge_check', 'J', 'setup.check.now.judge_check'],
    ['finishing', 'Q', 'setup.check.now.finishing']]) {
    const env = await mount({ '/api/model-check': () => reply0({ state: part === 'Q' ? 'q_running' : 'j_running',
      progress: Object.assign({}, progress, { now, part, server: null }) }) });
    assert.ok(text(env.container).indexOf(STRINGS[key]) >= 0, key);
    assert.strictEqual(byClass(env.container, 'ss-check-server').length, 0);
  }
  const quiet = await mount({ '/api/model-check': () => reply0({ state: 'q_running',
    progress: Object.assign({}, progress, { server: { code: 'no_reply', seconds: 400, errors: 0 } }) }) });
  assert.strictEqual(byClass(quiet.container, 'ss-check-server')[0].textContent,
    fill(STRINGS['jobs.server.no_reply'], { time: fill(STRINGS['setup.estimate.minutes'], { m: 7 }) }));
});

test('waiting for the judge on one Ollama server: the app can switch the models, and says how to do it by hand', async () => {
  const progress = { done: 20, total: 59, part: 'J', now: 'waiting_for_judge', determinism_k: 6,
    parts: [{ part: 'Q', role: 'recon', done: 20, total: 20 }, { part: 'J', role: 'judge', done: 0, total: 39 }], server: null };
  const { container, calls } = await mount({
    '/api/model-check': () => reply0({ state: 'needs_swap', progress }, { swap_allowed: true, plan: PLAN }),
    '/api/model-check/continue': () => reply0({ state: 'j_running', progress: Object.assign({}, progress, { now: 'model_calls' }) }),
  });
  const t = text(container);
  assert.ok(t.indexOf(fill(STRINGS['setup.check.swap.auto_note'], { model: 'gemma-mock:9b' })) >= 0, t);
  assert.ok(t.indexOf(fill(STRINGS['setup.check.swap.howto'], { model: 'gemma-mock:9b' })) >= 0);
  assert.ok(buttonOf(container, 'setup.check.continue'));
  buttonOf(container, 'setup.check.swap.auto').fire('click');
  assert.ok(text(container).indexOf(STRINGS['setup.check.now.switching_models']) >= 0, 'says it is switching');
  await settle();
  const post = calls.find((c) => c.url === '/api/model-check/continue');
  assert.deepStrictEqual(post.body, { auto: true });
  assert.ok(text(container).indexOf(STRINGS['setup.check.running_j']) >= 0);
  const manual = await mount({ '/api/model-check': () => reply0({ state: 'needs_swap', progress }, { swap_allowed: false }) });
  assert.strictEqual(buttonOf(manual.container, 'setup.check.swap.auto'), undefined, 'no auto switch when not allowed');
  assert.ok(text(manual.container).indexOf(fill(STRINGS['setup.check.swap.howto'], { model: 'gemma-mock:9b' })) >= 0);
  const refused = await mount({
    '/api/model-check': () => reply0({ state: 'needs_swap', progress }, { swap_allowed: true }),
    '/api/model-check/continue': () => [502, { error: 'swap_failed', message: STRINGS['model_check.error.swap_failed'] }],
  });
  buttonOf(refused.container, 'setup.check.swap.auto').fire('click');
  await settle();
  const u = text(refused.container);
  assert.ok(u.indexOf(STRINGS['model_check.error.swap_failed']) >= 0, u);
  assert.ok(u.indexOf(STRINGS['setup.check.now.switching_models']) < 0, 'the switching line goes when it failed');
});

test('passed: a Next line links to the Run screen', async () => {
  const { container } = await mount({ '/api/model-check': () => reply0({ state: 'passed' }, { plan: PLAN }) });
  const next = byClass(container, 'ss-check-next');
  assert.strictEqual(next.length, 1);
  assert.ok(text(next[0]).indexOf(STRINGS['setup.check.next']) >= 0);
  const a = byTag(next[0], 'a')[0];
  assert.strictEqual(a.attributes.href, '#instructions');
  assert.strictEqual(a.textContent, fill(STRINGS['setup.check.next_link'], { run: STRINGS['instructions.title'] }));
  assert.strictEqual(byClass(container, 'ss-check-time-moved').length, 0, 'one link to Run, not two');
});

test('a check that passes while the page is open drops the "time is on Run" line (one link to Run)', async () => {
  const { container } = await mount({
    '/api/model-check': () => reply0({ state: 'needs_swap' }),
    '/api/model-check/continue': () => reply0({ state: 'passed' }),
  });
  assert.strictEqual(byClass(container, 'ss-check-time-moved').length, 1);
  buttonOf(container, 'setup.check.continue').fire('click');
  await settle();
  assert.strictEqual(byClass(container, 'ss-check-next').length, 1);
  assert.strictEqual(byClass(container, 'ss-check-time-moved').length, 0, 'one link to Run, not two');
});
