'use strict';
// Task 34: Setup step "models" (app-setup-models.js). The app's scripts load in the server's order into a vm
// context with a small stub DOM and a fake fetch that answers each /api/models route.
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

function state(extra) {
  return Object.assign({ roles: {}, keys: { recon: false, judge: false }, confirmed: null, families_confirmed: false,
    pipeline_mode: null, same_family: null, notice: null, optins: [], declarations: [], problems: [],
    pair_sha256: 'c'.repeat(64),
    defaults: { local: 'loop' + 'back-default', openai_compat: '', anthropic: 'api-default-from-server' } }, extra || {});
}

function role(extra) {
  return Object.assign({ kind: 'local', provider: 'openai', url: 'http://127.0.0.1:11434/v1', host: '127.0.0.1:11434',
    address_class: 'loopback', model: 'gemma3:12b', family: 'gemma', family_detected: 'gemma3', digest: 'd'.repeat(64),
    server: 'ollama', connection: null, key_set: false }, extra || {});
}

// Renders the step; `routes` answers the API. A redraw (SS.setup.refresh) re-renders into the same container.
async function mount(routes) {
  const env = load(routes);
  const container = makeNode('div');
  env.SS.setup.refresh = () => env.SS.setupModels.render(container);
  env.SS.setupModels.reset();
  await env.SS.setupModels.render(container);
  await settle();
  return Object.assign(env, { container });
}

test('the step registers at order 60 after the project step and draws both roles', async () => {
  const { SS, container, errors } = await mount({ '/api/models': () => ({ models: state() }) });
  assert.deepStrictEqual(errors, []);
  const ids = Array.from(SS.setup.list());
  assert.ok(ids.indexOf('models') > ids.indexOf('project'), ids.join(','));
  const cards = byClass(container, 'ss-models-role');
  assert.deepStrictEqual(cards.map((c) => c.attributes['data-role']), ['recon', 'judge']);
  assert.ok(text(container).indexOf(STRINGS['setup.models.role.recon']) >= 0);
  assert.ok(text(container).indexOf(STRINGS['setup.models.role.judge']) >= 0);
  // Part 5c: the detailed forms sit in the closed "Advanced options" under Quick setup; their List still waits
  assert.ok(buttonOf(cards[0], 'setup.models.list').disabled, 'listing waits for the address check');
});

test('a LAN address shows the opt-in, whose button waits for the tick and posts the host', async () => {
  const info = { address_class: 'lan', host: 'gpu-box.lan:11434', endpoint: 'http://gpu-box.lan:11434/v1',
    needs: 'optin', allowed: false, https_ok: false, problem: 'optin_missing' };
  let optinBody = null;
  const { container, calls } = await mount({
    '/api/models': () => ({ models: state() }),
    '/api/models/classify': () => info,
    '/api/models/optin': (b) => { optinBody = b; info.allowed = true; return { optin: b, models: state() }; },
  });
  const card = byClass(container, 'ss-models-role')[0];
  buttonOf(card, 'setup.models.check').fire('click');
  await settle();
  const recon = byClass(container, 'ss-models-role')[0];
  assert.ok(text(recon).indexOf(STRINGS['setup.models.class.lan']) >= 0);
  const box = byClass(recon, 'ss-models-optin')[0];
  assert.ok(box, 'opt-in box shown');
  const allow = buttonOf(box, 'setup.models.optin_allow');
  assert.ok(allow.disabled);
  allow.fire('click');
  await settle();
  assert.strictEqual(optinBody, null, 'nothing sent before the tick');
  const tick = byTag(box, 'input').find((i) => i.attributes.type === 'checkbox');
  tick.checked = true;
  tick.fire('change');
  assert.ok(!allow.disabled);
  allow.fire('click');
  await settle();
  assert.deepStrictEqual(optinBody, { role: 'recon', host: 'gpu-box.lan:11434', understood: true });
  assert.ok(calls.filter((c) => c.url === '/api/models/classify').length >= 2, 'the address is checked again');
  const after = byClass(container, 'ss-models-role')[0];
  assert.ok(text(after).indexOf(fill(STRINGS['setup.models.optin_done'],
    { host: 'gpu-box.lan:11434', role: STRINGS['setup.models.role_word.recon'] })) >= 0);
  assert.ok(!buttonOf(after, 'setup.models.list').disabled);
});

test('a cloud address needs the tick and the basis before the declaration is sent', async () => {
  const info = { address_class: 'remote', host: 'api.anthropic.com', endpoint: 'https://api.anthropic.com/v1',
    needs: 'declaration', allowed: false, https_ok: true, problem: 'declaration_missing' };
  let sent = null;
  const { container } = await mount({
    '/api/models': () => ({ models: state() }),
    '/api/models/classify': () => info,
    '/api/models/declaration': (b) => { sent = b; return { declaration: b, models: state() }; },
  });
  let card = byClass(container, 'ss-models-role')[0];
  const anthropic = byTag(card, 'input').find((i) => i.attributes.value === 'anthropic');
  anthropic.checked = true;
  anthropic.fire('change');
  await settle();
  card = byClass(container, 'ss-models-role')[0];
  const url = byTag(card, 'input').find((i) => i.attributes.name === 'url');
  assert.strictEqual(url.value, 'api-default-from-server');     // the server supplies each kind's address
  buttonOf(card, 'setup.models.check').fire('click');
  await settle();
  card = byClass(container, 'ss-models-role')[0];
  const box = byClass(card, 'ss-models-declaration')[0];
  assert.ok(box);
  assert.ok(text(box).indexOf('Anthropic (api.anthropic.com)') >= 0);
  const submit = buttonOf(box, 'setup.models.decl_submit');
  const tick = byTag(box, 'input').find((i) => i.attributes.type === 'checkbox');
  const basis = byTag(box, 'input').find((i) => i.attributes.name === 'basis');
  tick.checked = true;
  tick.fire('change');
  assert.ok(submit.disabled, 'a tick without a basis is not enough');
  basis.value = 'REB #1234 approves the Anthropic API';
  basis.fire('input');
  assert.ok(!submit.disabled);
  submit.fire('click');
  await settle();
  assert.deepStrictEqual(sent, { role: 'recon', provider: 'anthropic', host: 'api.anthropic.com',
    basis: 'REB #1234 approves the Anthropic API', ticked: true });
  const key = byTag(card, 'input').find((i) => i.attributes.name === 'api-key');
  assert.strictEqual(key.attributes.type, 'password');
});

test('the key is posted once and its input cleared; it never appears in the page', async () => {
  const secret = 'sk' + '-test-' + 'f'.repeat(24);
  let keyBody = null;
  let keySet = false;
  const { container } = await mount({
    '/api/models': () => ({ models: state({ keys: { recon: keySet, judge: false } }) }),
    '/api/models/key': (b) => { keyBody = b; keySet = true; return { role: 'recon', key: { set: true, source: 'pasted' } }; },
  });
  let card = byClass(container, 'ss-models-role')[0];
  const kind = byTag(card, 'input').find((i) => i.attributes.value === 'openai_compat');
  kind.checked = true;
  kind.fire('change');
  await settle();
  card = byClass(container, 'ss-models-role')[0];
  const key = byTag(card, 'input').find((i) => i.attributes.name === 'api-key');
  key.value = secret;
  buttonOf(card, 'setup.models.key_save').fire('click');
  assert.strictEqual(key.value, '', 'cleared as soon as it is sent');
  await settle();
  assert.deepStrictEqual(keyBody, { role: 'recon', key: secret });
  const page = JSON.stringify(container, (k, v) => (k === 'listeners' ? undefined : v));
  assert.ok(page.indexOf(secret) < 0, 'the key is nowhere in the page');
  assert.ok(text(container).indexOf(STRINGS['setup.models.key_set']) >= 0);
});

test('a discovered cloud model is listed but cannot be chosen, with the reason in words', async () => {
  const info = { address_class: 'loopback', host: '127.0.0.1:11434', endpoint: 'http://127.0.0.1:11434/v1',
    needs: null, allowed: true, https_ok: false, problem: null };
  const { container } = await mount({
    '/api/models': () => ({ models: state() }),
    '/api/models/classify': () => info,
    '/api/models/discover': () => ({ server: 'ollama', endpoint: info.endpoint, models: [
      { name: 'gemma3:12b', family: 'gemma3', family_hint: 'gemma', remote: false, digest: 'a'.repeat(64) },
      { name: 'gpt-oss:120b-cloud', family: 'gptoss', family_hint: 'gpt-oss', remote: true, cloud_reason: 'remote_host' },
    ] }),
  });
  buttonOf(byClass(container, 'ss-models-role')[0], 'setup.models.check').fire('click');
  await settle();
  buttonOf(byClass(container, 'ss-models-role')[0], 'setup.models.list').fire('click');
  await settle();
  const items = byClass(container, 'ss-models-item');
  assert.strictEqual(items.length, 2);
  const cloud = items.find((li) => li.attributes['data-cloud'] === 'true');
  assert.ok(cloud);
  assert.ok(byTag(cloud, 'input')[0].disabled);
  assert.ok(text(cloud).indexOf(STRINGS['setup.models.cloud_refused']) >= 0);
  const local = items.find((li) => li !== cloud);
  assert.ok(!byTag(local, 'input')[0].disabled);
  byTag(local, 'input')[0].checked = true;
  byTag(local, 'input')[0].fire('change');
  await settle();
  const card = byClass(container, 'ss-models-role')[0];
  const family = byTag(card, 'input').find((i) => i.attributes.name === 'family');
  assert.strictEqual(family.value, 'gemma3');
  assert.ok(text(card).indexOf(fill(STRINGS['setup.models.family_detected'], { family: 'gemma3' })) >= 0);
});

test('a set role shows its test result; thinking text shows how to pick a variant without it', async () => {
  let conn = null;
  const { container, calls } = await mount({
    '/api/models': () => ({ models: state({ roles: { recon: role({ connection: conn }) } }) }),
    '/api/models/test': () => {
      conn = { ok: true, ms: 840, thinking_seen: true, reply_ok: true, error: null };
      return { result: conn, message: STRINGS['setup.models.thinking_ignored'] };
    },
  });
  let card = byClass(container, 'ss-models-role')[0];
  assert.ok(text(card).indexOf(fill(STRINGS['setup.models.current'], { model: 'gemma3:12b', family: 'gemma' })) >= 0);
  assert.ok(text(card).indexOf(STRINGS['setup.models.untested']) >= 0);
  buttonOf(card, 'setup.models.test').fire('click');
  await settle();
  assert.deepStrictEqual(calls.filter((c) => c.url === '/api/models/test').map((c) => c.body), [{ role: 'recon' }]);
  card = byClass(container, 'ss-models-role')[0];
  assert.ok(byClass(card, 'ss-models-thinking').length === 1);
  assert.ok(text(card).indexOf(STRINGS['setup.models.thinking_ignored']) >= 0);
});

test('same family: the confirmed pair shows the AI-maps-only notice; no control turns it off', async () => {
  let confirmed = false;
  const notice = STRINGS['setup.models.same_family'];
  const { container, calls } = await mount({
    '/api/models': () => ({ models: state({
      roles: { recon: role(), judge: role({ model: 'gemma2:9b', family: 'gemma2' }) },
      families_confirmed: confirmed, pipeline_mode: confirmed ? 'ai_maps_only' : null, same_family: true,
      notice: confirmed ? notice : null,
      problems: confirmed ? [] : [{ code: 'families_unconfirmed', role: null, message: STRINGS['models.error.families_unconfirmed'] }],
    }) }),
    '/api/models/confirm-families': () => { confirmed = true; return { models: state() }; },
  });
  assert.ok(text(container).indexOf(fill(STRINGS['setup.models.pair_both'], { recon: 'gemma', judge: 'gemma2' })) >= 0);
  assert.ok(text(container).indexOf(STRINGS['models.error.families_unconfirmed']) >= 0);
  buttonOf(container, 'setup.models.confirm').fire('click');
  await settle();
  assert.deepStrictEqual(calls.filter((c) => c.url === '/api/models/confirm-families').map((c) => c.body),
    [{ recon: 'gemma', judge: 'gemma2' }]);
  assert.ok(text(container).indexOf(notice) >= 0);
  assert.strictEqual(buttonOf(container, 'setup.models.confirm'), undefined);
  assert.ok(text(container).indexOf(STRINGS['setup.models.ready']) >= 0);
});

test('a refusal shows the server message in the step, in ink', async () => {
  const { container } = await mount({
    '/api/models': () => ({ models: state() }),
    '/api/models/classify': () => [422, { error: 'bad_url', message: STRINGS['models.error.bad_url'] }],
  });
  const card = byClass(container, 'ss-models-role')[0];
  buttonOf(card, 'setup.models.check').fire('click');
  await settle();
  const err = byClass(card, 'ss-setup-error').find((e) => e.textContent);
  assert.ok(err);
  assert.strictEqual(err.textContent, STRINGS['models.error.bad_url']);
});

test('item 30: each saving action asks the redraw to show its result next to the control', async () => {
  let keySet = false;
  const env = await mount({
    '/api/models': () => ({ models: state({ keys: { recon: keySet, judge: false } }) }),
    '/api/models/key': () => { keySet = true; return { role: 'recon', key: { set: true, source: 'pasted' } }; },
  });
  const seen = [];
  const plain = env.SS.setup.refresh;
  env.SS.setup.refresh = (opts) => { seen.push(opts ? opts.message : null); return plain(); };
  let card = byClass(env.container, 'ss-models-role')[0];
  const kind = byTag(card, 'input').find((i) => i.attributes.value === 'openai_compat');
  kind.checked = true;
  kind.fire('change');
  await settle();
  card = byClass(env.container, 'ss-models-role')[0];
  byTag(card, 'input').find((i) => i.attributes.name === 'api-key').value = 'sk' + '-x';
  buttonOf(card, 'setup.models.key_save').fire('click');
  await settle();
  assert.deepStrictEqual(seen, [null, 'Key set for the reconstructor for this session.']);
  const keys = Object.keys(STRINGS).filter((k) => k.indexOf('setup.models.done.') === 0);
  assert.strictEqual(keys.length, 6);
  for (const k of keys) assert.ok(!/—|rather than|\b(first|wrong|fail|failed|error)\b/i.test(STRINGS[k]), k);
});

// --- item 31: the model list is a compact searchable picker; item 29: optional key on a local server ------------
const MANY = [
  { name: 'deepseek/deepseek-r1', family: null, family_hint: 'deepseek', remote: false },
  { name: 'deepseek/deepseek-v3', family: null, family_hint: 'deepseek', remote: false },
  { name: 'qwen/qwen3-235b', family: null, family_hint: 'qwen', remote: false },
  { name: 'meta-llama/llama-3.3-70b', family: null, family_hint: 'llama', remote: false },
  { name: 'google/gemma-3-27b', family: null, family_hint: 'gemma', remote: false },
];
async function listed(extraState) {
  const info = { address_class: 'loopback', host: '127.0.0.1:8000', endpoint: 'http://127.0.0.1:8000/v1',
    needs: null, allowed: true, https_ok: false, problem: null };
  const env = await mount({
    '/api/models': () => ({ models: state(extraState) }),
    '/api/models/classify': () => info,
    '/api/models/discover': () => ({ server: 'openai', endpoint: info.endpoint, models: MANY }),
  });
  buttonOf(byClass(env.container, 'ss-models-role')[1], 'setup.models.check').fire('click');
  await settle();
  buttonOf(byClass(env.container, 'ss-models-role')[1], 'setup.models.list').fire('click');
  await settle();
  const card = byClass(env.container, 'ss-models-role')[1];
  const filter = byTag(card, 'input').find((i) => i.attributes.type === 'search');
  return Object.assign(env, { card, filter });
}

test('item 31: typing narrows the list, with a count; the list has its own scroll box', async () => {
  const { card, filter } = await listed();
  const label = byTag(card, 'label').find((l) => l.attributes.for === filter.attributes.id);
  assert.strictEqual(label.textContent, 'Find a model');
  const list = byClass(card, 'ss-models-list')[0];
  assert.strictEqual(filter.attributes['aria-controls'], list.attributes.id);
  const count = byClass(card, 'ss-models-count')[0];
  assert.strictEqual(count.textContent, '5 of 5 models shown.');
  assert.strictEqual(count.attributes.role, 'status');
  filter.value = 'deepseek';
  filter.fire('input');
  const shown = () => byClass(card, 'ss-models-item').filter((li) => !li.hidden).map((li) => byTag(li, 'input')[0].attributes.value);
  assert.deepStrictEqual(shown(), ['deepseek/deepseek-r1', 'deepseek/deepseek-v3']);
  assert.strictEqual(count.textContent, '2 of 5 models shown.');
  filter.value = 'DeepSeek V3';                                              // words in any case, all must match
  filter.fire('input');
  assert.deepStrictEqual(shown(), ['deepseek/deepseek-v3']);
  filter.value = 'mistral';
  filter.fire('input');
  assert.deepStrictEqual(shown(), []);
  assert.strictEqual(count.textContent, 'No model matches mistral.');
  const css = fs.readFileSync(path.join(ROOT, 'second_scribe', 'app', 'static', 'css', 'app.css'), 'utf8');
  assert.ok(/\.ss-models-list \{[^}]*max-height: 300px;[^}]*overflow-y: auto;/.test(css));
});

test('item 31: picking a model fills the id and family in place; the filter and the list stay', async () => {
  const { card, filter, calls } = await listed();
  filter.value = 'llama';
  filter.fire('input');
  const gets = calls.filter((c) => c.url === '/api/models').length;
  const radio = byTag(card, 'input').find((i) => i.attributes.value === 'meta-llama/llama-3.3-70b');
  radio.checked = true;
  radio.fire('change');
  await settle();
  assert.strictEqual(calls.filter((c) => c.url === '/api/models').length, gets, 'no redraw');
  assert.strictEqual(byTag(card, 'input').find((i) => i.attributes.name === 'model').value, 'meta-llama/llama-3.3-70b');
  assert.strictEqual(byTag(card, 'input').find((i) => i.attributes.name === 'family').value, 'llama');
  assert.ok(text(card).indexOf(fill(STRINGS['setup.models.family_detected'], { family: 'llama' })) >= 0);
  assert.strictEqual(filter.value, 'llama');
  // the down arrow moves from the search field to the checked model
  let focused = null;
  byTag(card, 'input').filter((i) => i.attributes.type === 'radio').forEach((r) => { r.focus = () => { focused = r; }; });
  let prevented = false;
  filter.listeners.keydown.forEach((fn) => fn({ key: 'ArrowDown', preventDefault() { prevented = true; } }));
  assert.strictEqual(focused, radio);
  assert.ok(prevented);
});

test('item 31: models of the same family as the other role are marked, and the other family is named', async () => {
  const { card } = await listed({ roles: { recon: role({ model: 'qwen3:32b', family: 'qwen3' }) } });
  assert.ok(text(card).indexOf('The reconstructor is qwen3. Pick a model from another family.') >= 0);
  const same = byClass(card, 'ss-models-item').filter((li) => li.attributes['data-same-family'] === 'true');
  assert.deepStrictEqual(same.map((li) => byTag(li, 'input')[0].attributes.value), ['qwen/qwen3-235b']);
  assert.ok(text(same[0]).indexOf('Same family as the reconstructor') >= 0);
});

test('item 31: the family test is the server\'s (trailing digits and marks dropped, one name begins the other)', () => {
  const { SS } = load({});
  const s = SS.setupModels.sameFamily;
  assert.strictEqual(s('gemma3', 'gemma2'), true);
  assert.strictEqual(s('qwen2', 'qwen3moe'), true);
  assert.strictEqual(s('Llama-3.3', 'llama'), true);
  assert.strictEqual(s('gemma', 'llama'), false);
  assert.strictEqual(s(null, 'qwen'), false);
});

test('item 29: the family rule with an example pair on top; a local server can take an optional key', async () => {
  let keyBody = null;
  let keySet = false;
  const { container } = await mount({
    '/api/models': () => ({ models: state({ keys: { recon: keySet, judge: false } }) }),
    '/api/models/key': (b) => { keyBody = b; keySet = true; return { role: 'recon', key: { set: true, source: 'pasted' } }; },
  });
  const rule = byClass(container, 'ss-models-rule')[0];
  assert.strictEqual(rule.textContent, STRINGS['setup.models.family_rule']);
  // Part 5c (item 29): the rule, then Quick setup, then the closed "Advanced options" that holds the role cards
  const adv = byClass(container, 'ss-models-advanced')[0];
  assert.ok(adv && adv.tagName === 'DETAILS' && byClass(adv, 'ss-models-roles').length === 1);
  assert.ok(container.children.indexOf(rule) < container.children.indexOf(byClass(container, 'ss-models-quick')[0]));
  assert.ok(container.children.indexOf(byClass(container, 'ss-models-quick')[0]) < container.children.indexOf(adv));
  let card = byClass(container, 'ss-models-role')[0];
  const opt = byClass(card, 'ss-models-key-optional')[0];
  assert.strictEqual(opt.tagName, 'DETAILS');
  assert.strictEqual(opt.attributes.open, undefined, 'closed while no key is set');
  assert.strictEqual(byTag(opt, 'summary')[0].textContent, 'This server asks for an API key');
  byTag(opt, 'input').find((i) => i.attributes.name === 'api-key').value = 'local-' + 'secret';
  buttonOf(opt, 'setup.models.key_save').fire('click');
  await settle();
  assert.deepStrictEqual(keyBody, { role: 'recon', key: 'local-secret' });
  card = byClass(container, 'ss-models-role')[0];
  assert.strictEqual(byClass(card, 'ss-models-key-optional')[0].attributes.open, 'open');
  const keys = ['family_rule', 'key_optional', 'key_optional_hint', 'filter_label', 'filter_hint', 'list_count',
    'list_none', 'other_family', 'same_as_other'].map((k) => 'setup.models.' + k);
  for (const k of keys) assert.ok(STRINGS[k] && !/—|rather than|\b(first|wrong|fail|failed|error)\b/i.test(STRINGS[k]), k);
});

const LAN = ['192', '168', '1', '20'].join('.');   // built, so no private address sits in the file

test('item 29: Quick setup tests both roles at once, each result beside its role; the key never stays in the page', async () => {
  const secret = 'sk' + '-quick-' + 'e'.repeat(24);
  const roles = {};
  const conn = {};
  const { container, calls } = await mount({
    '/api/models': () => ({ models: state({ roles: JSON.parse(JSON.stringify(roles)), keys: { recon: false, judge: !!conn.key } }) }),
    '/api/models/key': (b) => { conn.key = b.key; return { role: b.role, key: { set: true, source: 'pasted' } }; },
    '/api/models/classify': (b) => (String(b.url).indexOf(LAN) >= 0
      ? { address_class: 'private', host: LAN + ':11434', needs: 'optin', allowed: false, https_ok: false, problem: null }
      : { address_class: 'loopback', host: '127.0.0.1:11434', needs: null, allowed: true, https_ok: false, problem: null }),
    '/api/models/set': (b) => { roles[b.role] = role({ model: b.model, family: b.family, kind: b.kind }); return { role: b.role }; },
    '/api/models/test': (b) => {
      roles[b.role].connection = { ok: true, ms: b.role === 'recon' ? 840 : 910, thinking_seen: false, reply_ok: true, error: null };
      return { result: roles[b.role].connection };
    },
  });
  const col = (r) => byClass(container, 'ss-quick-role').find((c) => c.attributes['data-quick'] === r);
  const field = (r, name) => byTag(col(r), 'input').find((i) => i.attributes.name === name);
  assert.strictEqual(byClass(byClass(container, 'ss-quick-roles')[0], 'ss-quick-role').length, 2, 'the two roles side by side');
  for (const r of ['recon', 'judge']) {
    assert.deepStrictEqual(byTag(col(r), 'input').filter((i) => i.attributes.type === 'radio').map((i) => i.attributes.value),
      ['computer', 'network', 'cloud']);
    assert.strictEqual(field(r, 'quick-key').attributes.type, 'password');
  }
  field('recon', 'quick-model').value = 'gemma3:12b';
  field('recon', 'quick-family').value = 'gemma';
  field('judge', 'quick-model').value = 'qwen3:8b';
  field('judge', 'quick-family').value = 'qwen';
  field('judge', 'quick-key').value = secret;
  buttonOf(container, 'setup.models.quick.test_both').fire('click');
  assert.strictEqual(field('judge', 'quick-key').value, '', 'the key field is emptied when the key is sent');
  await settle();
  assert.deepStrictEqual(calls.filter((c) => c.url === '/api/models/test').map((c) => c.body), [{ role: 'recon' }, { role: 'judge' }]);
  assert.deepStrictEqual(calls.filter((c) => c.url === '/api/models/set').map((c) => [c.body.role, c.body.model, c.body.family]),
    [['recon', 'gemma3:12b', 'gemma'], ['judge', 'qwen3:8b', 'qwen']]);
  assert.ok(text(byClass(col('recon'), 'ss-quick-result')[0]).indexOf(fill(STRINGS['setup.models.test_ok'], { ms: '840' })) >= 0);
  assert.ok(text(byClass(col('judge'), 'ss-quick-result')[0]).indexOf(fill(STRINGS['setup.models.test_ok'], { ms: '910' })) >= 0);
  const page = JSON.stringify(container, (k, v) => (k === 'listeners' ? undefined : v));
  assert.ok(page.indexOf(secret) < 0, 'the key is nowhere in the page');

  // a server on the network still needs its opt-in: Test both saves nothing for it and says why, in its column
  const net = byTag(col('judge'), 'input').find((i) => i.attributes.value === 'network');
  net.checked = true;
  net.fire('change');
  await settle();
  field('judge', 'quick-url').value = 'http://' + LAN + ':11434';
  field('judge', 'quick-model').value = 'qwen3:8b';
  field('judge', 'quick-family').value = 'qwen';
  const before = calls.length;
  buttonOf(container, 'setup.models.quick.test_both').fire('click');
  await settle();
  assert.strictEqual(calls.filter((c) => c.url === '/api/models/set' && c.body.role === 'judge').length,
    calls.slice(0, before).filter((c) => c.url === '/api/models/set' && c.body.role === 'judge').length, 'nothing saved without the opt-in');
  assert.ok(calls.every((c) => c.url !== '/api/models/optin'), 'the opt-in is never posted for the person');
  assert.ok(text(byClass(col('judge'), 'ss-quick-result')[0]).indexOf(STRINGS['setup.models.why.optin']) >= 0);
});

test('item 37: an opt-in button waits for its tick and says why, and the reason goes once the box is ticked', async () => {
  const info = { address_class: 'private', host: LAN + ':11434', needs: 'optin', allowed: false, https_ok: false, problem: null };
  const { container } = await mount({
    '/api/models': () => ({ models: state() }),
    '/api/models/classify': () => info,
  });
  const col = byClass(container, 'ss-quick-role')[0];
  const net = byTag(col, 'input').find((i) => i.attributes.value === 'network');
  net.checked = true;
  net.fire('change');
  await settle();
  let c = byClass(container, 'ss-quick-role')[0];
  byTag(c, 'input').find((i) => i.attributes.name === 'quick-url').value = 'http://' + LAN + ':11434';
  buttonOf(c, 'setup.models.list').fire('click');
  await settle();
  c = byClass(container, 'ss-quick-role')[0];
  const box = byClass(c, 'ss-models-optin')[0];
  const allow = buttonOf(box, 'setup.models.optin_allow');
  const why = byClass(box, 'ss-why')[0];
  assert.strictEqual(allow.disabled, true);
  assert.strictEqual(why.textContent || text(why).trim(), text(why).trim());
  assert.ok(text(why).indexOf(STRINGS['setup.models.why.optin_tick']) >= 0);
  assert.strictEqual(allow.attributes['aria-describedby'], why.attributes.id);
  const tick = byTag(box, 'input').find((i) => i.attributes.type === 'checkbox');
  tick.checked = true;
  tick.fire('change');
  assert.strictEqual(allow.disabled, false);
  assert.strictEqual(why.hidden, true);
});
