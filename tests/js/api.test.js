'use strict';
// Task 31: SS.api (fetch wrapper: cookie, CSRF header, error shape) and SS.shell (screen registry, hash).
// These scripts are the app's, not the viewer's, so they load here with their own small vm context.
const test = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ROOT = path.resolve(__dirname, '..', '..');
const JS = path.join(ROOT, 'second_scribe', 'app', 'static', 'js');
const STRINGS = JSON.parse(fs.readFileSync(path.join(ROOT, 'second_scribe', 'app', 'strings', 'en.json'), 'utf8'));

function reply(status, doc) {
  const text = typeof doc === 'string' ? doc : JSON.stringify(doc);
  return { ok: status >= 200 && status < 300, status, text: () => Promise.resolve(text) };
}

function load(answer) {
  const calls = [];
  const errors = [];
  const sandbox = {
    console: { error: (...a) => errors.push(a.map(String).join(' ')), log() {}, warn() {} },
    location: { hash: '', search: '' },
    SS_STRINGS: STRINGS,
    fetch: (url, init) => {
      calls.push({ url, init: JSON.parse(JSON.stringify(init)) });
      return Promise.resolve(answer(url, init, calls.length));
    },
  };
  sandbox.window = sandbox;
  const ctx = vm.createContext(sandbox);
  for (const f of ['core.js', 'api.js', 'app-shell.js']) {
    vm.runInContext(fs.readFileSync(path.join(JS, f), 'utf8'), ctx, { filename: f });
  }
  return { SS: sandbox.SS, calls, errors };
}

function okServer(url) {
  if (url === '/api/session') return reply(200, { csrf: 'tok-123' });
  return reply(200, { echoed: url });
}

test('get sends the cookie, no CSRF header, no-store, and builds the query', async () => {
  const { SS, calls } = load(okServer);
  const doc = await SS.api.get('/api/pairing', { pid: 'MAP 1', b: 2, skip: null });
  assert.strictEqual(doc.echoed, '/api/pairing?b=2&pid=MAP%201');
  assert.strictEqual(calls.length, 1);
  const init = calls[0].init;
  assert.strictEqual(init.method, 'GET');
  assert.strictEqual(init.credentials, 'same-origin');
  assert.strictEqual(init.cache, 'no-store');
  assert.strictEqual(init.headers['X-SS-CSRF'], undefined);
  assert.strictEqual(init.body, undefined);
});

test('post fetches the CSRF token once and sends it with a JSON body', async () => {
  const { SS, calls } = load(okServer);
  await SS.api.post('/api/context', { domain: 'courts' });
  await SS.api.post('/api/context');
  const posts = calls.filter((c) => c.init.method === 'POST');
  assert.strictEqual(calls.filter((c) => c.url === '/api/session').length, 1);
  assert.strictEqual(posts.length, 2);
  for (const c of posts) {
    assert.strictEqual(c.init.headers['X-SS-CSRF'], 'tok-123');
    assert.strictEqual(c.init.headers['Content-Type'], 'application/json');
    assert.strictEqual(c.init.credentials, 'same-origin');
  }
  assert.strictEqual(posts[0].init.body, '{"domain":"courts"}');
  assert.strictEqual(posts[1].init.body, '{}');
});

test('a refusal rejects with status, code and the server message', async () => {
  const { SS } = load((url) => (url === '/api/session' ? reply(200, { csrf: 't' })
    : reply(409, { error: 'file_changed', message: 'This file changed since you imported it.' })));
  await assert.rejects(SS.api.post('/api/run', {}), (e) => {
    assert.strictEqual(e.status, 409);
    assert.strictEqual(e.code, 'file_changed');
    assert.strictEqual(e.message, 'This file changed since you imported it.');
    return true;
  });
});

test('a 401 emits api:no_session and forgets the CSRF token', async () => {
  let n = 0;
  const { SS, calls } = load((url) => {
    if (url === '/api/session') return reply(200, { csrf: 'tok-' + (++n) });
    return n === 1 ? reply(401, { error: 'no_session', message: 'No session.' }) : reply(200, {});
  });
  const seen = [];
  SS.bus.on('api:no_session', (e) => seen.push(e.code));
  await assert.rejects(SS.api.post('/api/x', {}), (e) => e.code === 'no_session');
  assert.deepStrictEqual(seen, ['no_session']);
  await SS.api.post('/api/x', {});
  assert.strictEqual(calls.filter((c) => c.url === '/api/session').length, 2);
  assert.strictEqual(calls[calls.length - 1].init.headers['X-SS-CSRF'], 'tok-2');
});

test('non-JSON replies and network failures reject with readable codes', async () => {
  const { SS } = load((url) => (url === '/api/a' ? reply(500, '<html>oops</html>') : reply(200, '[1]')));
  await assert.rejects(SS.api.get('/api/a'), (e) => e.code === 'http_500' && e.message === STRINGS['api.failed']);
  await assert.rejects(SS.api.get('/api/b'), (e) => e.code === 'bad_reply');
  const down = load(() => Promise.reject(new Error('refused')));
  await assert.rejects(down.SS.api.get('/api/c'), (e) => e.code === 'network' && e.message === STRINGS['api.unreachable']);
});

test('paths outside /api/ are refused before any fetch', async () => {
  const { SS, calls } = load(okServer);
  for (const p of ['/static/js/core.js', 'http://evil.example/api/x', '//evil.example/api/', 'api/x', 7]) {
    await assert.rejects(SS.api.get(p), (e) => e.code === 'bad_path');
  }
  assert.strictEqual(calls.length, 0);
});

test('shell registry orders screens and checks definitions', () => {
  const { SS } = load(okServer);
  const r = () => {};
  SS.shell.register('export', { order: 60, titleKey: 'app.name', render: r });
  SS.shell.register('setup', { order: 10, titleKey: 'app.name', render: r });
  SS.shell.register('coding', { order: 30, titleKey: 'app.name', render: r });
  assert.deepStrictEqual(Array.from(SS.shell.list()), ['setup', 'coding', 'export']);
  assert.throws(() => SS.shell.register('setup', { titleKey: 'app.name', render: r }), /already registered/);
  assert.throws(() => SS.shell.register('Bad Id', { titleKey: 'app.name', render: r }), /bad screen id/);
  assert.throws(() => SS.shell.register('x', { titleKey: 'app.name' }), /needs titleKey and render/);
  assert.strictEqual(SS.shell.pick('#coding/MAP-A'), 'coding');
  assert.strictEqual(SS.shell.pick('#nope'), 'setup');
  assert.strictEqual(SS.shell.pick(''), 'setup');
});

test('shell hash parsing', () => {
  const { SS } = load(okServer);
  const p = (h) => JSON.parse(JSON.stringify(SS.shell.parseHash(h)));
  assert.deepStrictEqual(p('#setup/2'), { screen: 'setup', sub: '2' });
  assert.deepStrictEqual(p('#results'), { screen: 'results', sub: '' });
  assert.deepStrictEqual(p(''), { screen: '', sub: '' });
  assert.deepStrictEqual(p('#<img>/x'), { screen: '', sub: 'x' });
  assert.strictEqual(SS.shell.pick('#anything'), '');
});
