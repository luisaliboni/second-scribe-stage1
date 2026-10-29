'use strict';
// Loads the viewer's classic scripts into a vm context with a stub window/document (no DOM library).
// Scripts attach themselves to window.SS, exactly as in a browser. Usage:
//   const { SS, errors } = require('./harness').load();            // every script that exists, in order
//   const { SS } = require('./harness').load({ strings: {...} });   // extra/overriding string keys
// Objects built inside the context belong to another realm: compare strings and primitives, or
// JSON round-trip before deepStrictEqual, and check thrown errors by message, not instanceof.
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ROOT = path.resolve(__dirname, '..', '..');
const JS_DIR = path.join(ROOT, 'second_scribe', 'app', 'static', 'js');
const EN_JSON = path.join(ROOT, 'second_scribe', 'app', 'strings', 'en.json');

// The fixed load order of Tasks 22-27 (plan, WP3 intro).
const ORDER = ['core.js', 'layout.js', 'encoding.js', 'a11y.js', 'lanes.js', 'views.js',
  'state.js', 'ledger.js', 'strip.js', 'profile.js', 'drawer.js', 'viewer.js'];
// profile.js (plan V3) replaces the strip as the headline; it needs only core.js.

function makeNode(tag) {
  const node = {
    tagName: String(tag).toUpperCase(), children: [], attributes: {}, dataset: {}, style: {},
    textContent: '',
    setAttribute(k, v) { node.attributes[k] = String(v); },
    getAttribute(k) { return Object.prototype.hasOwnProperty.call(node.attributes, k) ? node.attributes[k] : null; },
    appendChild(c) { node.children.push(c); return c; },
    addEventListener() {},
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
  };
  return node;
}

function makeDocument() {
  return {
    documentElement: makeNode('html'),
    body: makeNode('body'),
    createElement: makeNode,
    createElementNS: (ns, tag) => makeNode(tag),
    createTextNode: (text) => ({ nodeType: 3, textContent: String(text) }),
    addEventListener() {},
  };
}

function load(opts) {
  opts = opts || {};
  let files = opts.files;
  if (!files) files = ORDER.filter((f) => fs.existsSync(path.join(JS_DIR, f)));
  let last = -1;
  for (const f of files) {
    const i = ORDER.indexOf(f);
    if (i < 0) throw new Error('harness: ' + f + ' is not in the fixed load order');
    if (i < last) throw new Error('harness: ' + f + ' is out of the fixed load order');
    last = i;
  }
  const errors = [];
  const logs = [];
  const sandbox = {
    console: {
      error: (...a) => errors.push(a.map(String).join(' ')),
      warn: (...a) => logs.push(a.map(String).join(' ')),
      log: (...a) => logs.push(a.map(String).join(' ')),
    },
    document: makeDocument(),
    location: { hash: '' },
    setTimeout, clearTimeout,
  };
  sandbox.window = sandbox;
  if (opts.strings !== null) {
    const real = JSON.parse(fs.readFileSync(EN_JSON, 'utf8'));
    sandbox.SS_STRINGS = Object.assign({}, real, opts.strings || {});
  }
  const ctx = vm.createContext(sandbox);
  for (const f of files) {
    vm.runInContext(fs.readFileSync(path.join(JS_DIR, f), 'utf8'), ctx, { filename: f });
  }
  return { SS: sandbox.SS, window: sandbox, errors, logs, files };
}

module.exports = { load, ORDER, JS_DIR, ROOT };
