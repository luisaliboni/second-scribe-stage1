'use strict';
const test = require('node:test');
const assert = require('node:assert');
const { load } = require('./harness');

const NOT_COMPUTED = 'not computed';

test('strings come from the real en.json', () => {
  const { SS } = load();
  assert.strictEqual(SS.t('common.not_computed'), NOT_COMPUTED);
});

test('fmt_value_not_computed_never_zero', () => {
  const { SS } = load();
  for (const missing of [undefined, null, { status: 'not_run' }, { status: 'x', n: 3 }, NaN]) {
    const out = SS.fmt.value(missing);
    assert.strictEqual(out, NOT_COMPUTED);
    assert.notStrictEqual(out, '0');
  }
  // a recorded zero is a value, not a missing one
  assert.strictEqual(SS.fmt.value(0), '0');
  assert.strictEqual(SS.fmt.value(0.25), '0.25');
  assert.strictEqual(SS.fmt.value('abc'), 'abc');
  // the other formatters never turn a missing input into 0 either
  assert.strictEqual(SS.fmt.pct(undefined), NOT_COMPUTED);
  assert.strictEqual(SS.fmt.pct(null), NOT_COMPUTED);
  assert.strictEqual(SS.fmt.pct({ status: 'x' }), NOT_COMPUTED);
  assert.strictEqual(SS.fmt.count(undefined, 12), NOT_COMPUTED);
  assert.strictEqual(SS.fmt.count(3, null), NOT_COMPUTED);
  assert.strictEqual(SS.fmt.ci(undefined, 5), NOT_COMPUTED);
  assert.strictEqual(SS.fmt.ci(1, { status: 'x' }), NOT_COMPUTED);
  assert.strictEqual(SS.fmt.pct(0), '0%');
  assert.strictEqual(SS.fmt.count(0, 12), '0 of 12');
});

test('fmt_count_pct_ci_shapes', () => {
  const { SS } = load();
  assert.strictEqual(SS.fmt.count(3, 12), '3 of 12');
  assert.strictEqual(SS.fmt.pct(0.42), '42%');
  assert.strictEqual(SS.fmt.pct(0.285), '29%');
  assert.strictEqual(SS.fmt.pct(1), '100%');
  assert.strictEqual(SS.fmt.ci(3, 8), '3–8 points');
  assert.strictEqual(SS.fmt.ci(3.24, 7.86), '3.2–7.9 points');
  assert.strictEqual(SS.fmt.ci(-2, 5), '-2–5 points');
});

test('get_missing_path_undefined', () => {
  const { SS } = load();
  const doc = { a: { b: { c: 7, z: 0, n: null }, list: [{ x: 1 }] } };
  assert.strictEqual(SS.get(doc, 'a.b.c'), 7);
  assert.strictEqual(SS.get(doc, 'a.b.z'), 0); // present zero stays zero
  assert.strictEqual(SS.get(doc, 'a.b.n'), null);
  assert.strictEqual(SS.get(doc, 'a.list.0.x'), 1);
  for (const path of ['a.b.q', 'a.q.c', 'q', 'a.b.c.d', 'a.b.n.x', '', 'a..b']) {
    assert.strictEqual(SS.get(doc, path), undefined, path);
  }
  // never throws, whatever the input
  for (const bad of [undefined, null, 0, 'str', 12, true]) {
    assert.doesNotThrow(() => SS.get(bad, 'a.b'));
    assert.strictEqual(SS.get(bad, 'a.b'), undefined);
  }
  for (const badPath of [undefined, null, 5, {}]) {
    assert.strictEqual(SS.get(doc, badPath), undefined);
  }
  // inherited members are not data
  for (const p of ['constructor', 'a.constructor', 'a.__proto__', 'toString', 'a.b.hasOwnProperty']) {
    assert.strictEqual(SS.get(doc, p), undefined, p);
  }
  // characters of a string are not steps
  assert.strictEqual(SS.get({ s: 'abc' }, 's.length'), undefined);
});

test('view_load_rejects_v2', () => {
  const { SS } = load();
  const msg = 'unsupported view data major version';
  const rejects = (doc) => assert.throws(() => SS.view.load(doc), (e) => e.message === msg);
  rejects({ schema: 'view-data/2' });
  rejects({ schema: 'view-data/10' });
  rejects({ schema: 'view-data/0' });
  rejects({ schema: 'orgsci-profile/v2' });
  rejects({ schema: 1 });
  rejects({});
  rejects(null);
  rejects(undefined);
  rejects('view-data/1');
  const ok = { schema: 'view-data/1', x: 1 };
  assert.strictEqual(SS.view.load(ok), ok); // returns the same document
  const minor = { schema: 'view-data/1.2' };
  assert.strictEqual(SS.view.load(minor), minor);
});

test('t_slots', () => {
  const { SS, errors } = load({ strings: {
    'test.one': 'Hello {name}, you have {n} items ({n} again)',
    'test.braces': 'literal {{braces}} and {x}',
    'test.plain': 'no slots here',
  } });
  assert.strictEqual(SS.t('test.one', { name: 'Ana', n: 3 }), 'Hello Ana, you have 3 items (3 again)');
  assert.strictEqual(SS.t('test.one', { name: 'Ana', n: 0 }), 'Hello Ana, you have 0 items (0 again)');
  assert.strictEqual(SS.t('test.plain'), 'no slots here');
  assert.strictEqual(SS.t('test.plain', { unused: 1 }), 'no slots here');
  // same escape rule as Python's str.format_map
  assert.strictEqual(SS.t('test.braces', { x: 'y' }), 'literal {braces} and y');
  // a slot value is inserted once, never re-expanded
  assert.strictEqual(SS.t('test.one', { name: '{n}', n: 1 }), 'Hello {n}, you have 1 items (1 again)');
  assert.deepStrictEqual(errors, []);
  // a missing slot stays visible and is reported
  assert.strictEqual(SS.t('test.one', { name: 'Ana' }), 'Hello Ana, you have {n} items ({n} again)');
  assert.ok(errors.some((e) => e.includes('n')), errors.join('|'));
});

test('t_unknown_key_returns_key_and_logs', () => {
  const { SS, errors } = load();
  assert.strictEqual(SS.t('no.such.key'), 'no.such.key');
  assert.strictEqual(errors.length, 1);
  assert.ok(errors[0].includes('no.such.key'));
  // inherited object members are not keys
  assert.strictEqual(SS.t('constructor'), 'constructor');
  assert.strictEqual(SS.t('toString'), 'toString');
  assert.strictEqual(errors.length, 3);
});

test('t_without_string_table_does_not_throw', () => {
  const { SS, errors } = load({ strings: null });
  assert.strictEqual(SS.t('common.not_computed'), 'common.not_computed');
  assert.strictEqual(SS.fmt.value(undefined), 'common.not_computed');
  assert.ok(errors.length >= 1);
});

test('bus_on_emit_off', () => {
  const { SS, errors } = load();
  const seen = [];
  const off = SS.bus.on('x', (p) => seen.push(['a', p]));
  SS.bus.on('x', (p) => seen.push(['b', p]));
  SS.bus.on('y', (p) => seen.push(['y', p]));
  SS.bus.emit('x', 1);
  assert.deepStrictEqual(seen, [['a', 1], ['b', 1]]);
  off();
  SS.bus.emit('x', 2);
  assert.deepStrictEqual(seen.slice(2), [['b', 2]]);
  SS.bus.emit('nobody-listens', 3); // no throw
  // a throwing handler does not stop the others and is reported
  SS.bus.on('z', () => { throw new Error('boom'); });
  SS.bus.on('z', (p) => seen.push(['z2', p]));
  SS.bus.emit('z', 9);
  assert.deepStrictEqual(seen[seen.length - 1], ['z2', 9]);
  assert.ok(errors.some((e) => e.includes('boom')));
  // a handler that unsubscribes itself during emit does not skip the next one
  const order = [];
  let offSelf;
  offSelf = SS.bus.on('w', () => { order.push(1); offSelf(); });
  SS.bus.on('w', () => order.push(2));
  SS.bus.emit('w');
  SS.bus.emit('w');
  assert.deepStrictEqual(order, [1, 2, 2]);
});
