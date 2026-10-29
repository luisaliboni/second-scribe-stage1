'use strict';
// Task 28: the site shell's theme toggle (static/js/site-shell.js) and the Try it page script (static/js/site-try.js).
// Fixtures: tests/fixtures/views/mini.view.json (SYNTHETIC, test_pattern) and tests/fixtures/profiles/profile-synth.json.
// site-shell.js and site-try.js are not in the viewer's fixed load order, so they are run into the harness context
// after it (as the page loads them: site-shell.js in the head, site-try.js last).
const test = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const { load, ROOT, JS_DIR } = require('./harness');

const VIEW = JSON.parse(fs.readFileSync(path.join(ROOT, 'tests', 'fixtures', 'views', 'mini.view.json'), 'utf8'));
const SYNTH = JSON.parse(fs.readFileSync(path.join(ROOT, 'tests', 'fixtures', 'profiles', 'profile-synth.json'), 'utf8'));
const EN = JSON.parse(fs.readFileSync(path.join(ROOT, 'second_scribe', 'app', 'strings', 'en.json'), 'utf8'));
const clone = (o) => JSON.parse(JSON.stringify(o));

function run(env, file) {
  vm.runInContext(fs.readFileSync(path.join(JS_DIR, file), 'utf8'), env.window, { filename: file });
}

function boot(data, opts) {
  opts = opts || {};
  const env = load();
  const doc = env.window.document;
  const make = doc.createElement;
  doc.createElement = (tag) => {
    const n = make(tag);
    n.listeners = {};
    n.addEventListener = (type, fn) => { (n.listeners[type] = n.listeners[type] || []).push(fn); };
    n.focus = () => {};
    let text = n.textContent;
    Object.defineProperty(n, 'textContent', {
      get: () => text,
      set: (v) => { text = v; if (v === '') n.children.length = 0; },
    });
    return n;
  };
  const host = doc.createElement('div');
  const button = doc.createElement('button');
  button.setAttribute('aria-pressed', 'false');
  const byId = { 'tryit-viewer': host, 'site-theme': button };
  let profileHost = null;
  let finding = null;
  if (opts.page) {                 // the slots of pages/try.html (item 11)
    profileHost = doc.createElement('div');
    finding = doc.createElement('p');
    finding.hidden = true;
    byId['tryit-profile'] = profileHost;
    byId['tryit-finding'] = finding;
  }
  doc.getElementById = (id) => (Object.prototype.hasOwnProperty.call(byId, id) ? byId[id] : null);
  doc.readyState = 'complete';
  env.window.location.hash = opts.hash || '';
  if (opts.storage !== undefined) env.window.localStorage = opts.storage;
  if (opts.systemDark !== undefined) env.window.matchMedia = () => ({ matches: opts.systemDark });
  env.window.SS_DATA = data;
  return { env, host, button, doc, profileHost, finding };
}
function walk(node, fn) { fn(node); (node.children || []).forEach((c) => walk(c, fn)); }
function allText(root) {
  const out = [];
  walk(root, (n) => { if (n.textContent) out.push(n.textContent); });
  return out.join('\n');
}
function count(text, needle) { return text.split(needle).length - 1; }

function memoryStorage(initial) {
  const m = Object.assign({}, initial || {});
  return {
    getItem: (k) => (Object.prototype.hasOwnProperty.call(m, k) ? m[k] : null),
    setItem: (k, v) => { m[k] = String(v); },
    dump: () => m,
  };
}

// ------------------------------------------------------------------------------------------------- Try it

test('try: mounts the viewer with the synthetic band, the test-pattern label and the profile', () => {
  const b = boot({ tryit: clone(VIEW), tryit_profile: clone(SYNTH) });
  run(b.env, 'site-try.js');
  assert.deepStrictEqual(b.env.errors, []);
  const t = allText(b.host);
  assert.strictEqual(count(t, EN['band.synthetic']), 1);
  assert.strictEqual(count(t, EN['band.test_pattern']), 1);
  const sections = [];
  walk(b.host, (n) => { if (n.attributes && n.attributes['data-section']) sections.push(n.attributes['data-section']); });
  assert.ok(sections.indexOf('profile') >= 0, 'profile section drawn: ' + sections.join(','));
  assert.ok(sections.indexOf('views') >= 0);
});

test('try (item 11): the finding in words first, the maps next, the whole profile in the Technical details slot', () => {
  const b = boot({ tryit: clone(VIEW), tryit_profile: clone(SYNTH) }, { page: true });
  run(b.env, 'site-try.js');
  assert.deepStrictEqual(b.env.errors, []);
  // the two numbers come from the profile's lead row (x primary), as percentages
  assert.strictEqual(b.finding.hidden, false);
  assert.strictEqual(b.finding.textContent, EN['try.finding'].replace('{ai}', '90.9%').replace('{none}', '32.2%'));
  assert.ok(!/\.\d{3}/.test(b.finding.textContent), 'no proportion as a decimal in the open text');
  // the viewer host holds the bands and the maps, no profile
  const sections = [];
  walk(b.host, (n) => { if (n.attributes && n.attributes['data-section']) sections.push(n.attributes['data-section']); });
  assert.deepStrictEqual(sections.filter((x) => ['bands', 'profile', 'views'].indexOf(x) >= 0), ['bands', 'views']);
  // the profile slot holds the whole profile, with no inner "Show the full profile"
  const rows = [];
  let inner = 0;
  walk(b.profileHost, (n) => {
    if (n.tagName === 'DETAILS' && /(^| )pf-full( |$)/.test(n.attributes['class'] || '')) inner++;
    if (n.attributes && n.attributes['data-row'] !== undefined && /pf-row /.test((n.attributes['class'] || '') + ' ')) rows.push(n.attributes['data-row']);
  });
  assert.strictEqual(inner, 0);
  assert.ok(rows.indexOf('lead') >= 0 && rows.indexOf('ledger') >= 0 && rows.indexOf('9') >= 0, rows.join(','));
  // the no-interview gloss moves with the lanes, once on the page
  assert.strictEqual(count(allText(b.host), EN['lane.no_interview_gloss']), 1);
  assert.strictEqual(count(allText(b.profileHost), EN['lane.no_interview_gloss']), 0);
});

test('try: a profile without both recall numbers leaves the finding hidden', () => {
  const prof = clone(SYNTH);
  prof.rows[0].cells = prof.rows[0].cells.filter((c) => c.name !== 'no_interview_recall');
  const b = boot({ tryit: clone(VIEW), tryit_profile: prof }, { page: true });
  run(b.env, 'site-try.js');
  assert.deepStrictEqual(b.env.errors, []);
  assert.strictEqual(b.finding.hidden, true);
  assert.strictEqual(b.finding.textContent, '');
  assert.strictEqual(b.env.SS.tryit.finding(prof), null);
});

test('try: without the page slot the profile is compact: the lead first, the rest in a closed details, then the lanes', () => {
  const b = boot({ tryit: clone(VIEW), tryit_profile: clone(SYNTH) });
  run(b.env, 'site-try.js');
  assert.deepStrictEqual(b.env.errors, []);
  const dets = [];
  walk(b.host, (n) => { if (n.tagName === 'DETAILS' && /(^| )pf-full( |$)/.test(n.attributes['class'] || '')) dets.push(n); });
  assert.strictEqual(dets.length, 1, 'one "Show the full profile"');
  assert.strictEqual(dets[0].attributes.open, undefined, 'closed');
  assert.strictEqual(dets[0].children[0].textContent, EN['try.profile.full']);
  // order inside the viewer: bands, profile (lead, then the details), views (the lanes)
  const order = [];
  walk(b.host, (n) => {
    if (n.attributes && n.attributes['data-section']) order.push(n.attributes['data-section']);
    if (n === dets[0]) order.push('details');
  });
  assert.deepStrictEqual(order.filter((x) => ['bands', 'profile', 'details', 'views'].indexOf(x) >= 0),
    ['bands', 'profile', 'details', 'views']);
  // only the lead block is outside the details
  const outside = [];
  const walkOut = (n) => {
    if (n === dets[0]) return;
    if (n.attributes && n.attributes['data-section'] === 'profile') collect(n);
    (n.children || []).forEach(walkOut);
  };
  const collect = (n) => {
    if (n === dets[0]) return;
    if (n.attributes && n.attributes['data-row'] !== undefined) outside.push(n.attributes['data-row']);
    (n.children || []).forEach(collect);
  };
  walkOut(b.host);
  assert.deepStrictEqual(outside, ['lead']);
});

test('try: the viewer without the compact option still draws the whole profile (the app Results page)', () => {
  const b = boot({ tryit: clone(VIEW), tryit_profile: clone(SYNTH) });
  const root = b.env.window;
  root.SS.viewer.mount(b.host, clone(VIEW), { profile: clone(SYNTH) });
  assert.deepStrictEqual(b.env.errors, []);
  let dets = 0;
  const rows = [];
  walk(b.host, (n) => {
    if (n.tagName === 'DETAILS' && /(^| )pf-full( |$)/.test(n.attributes['class'] || '')) dets++;
    if (n.attributes && n.attributes['data-row'] !== undefined && /pf-row /.test((n.attributes['class'] || '') + ' ')) rows.push(n.attributes['data-row']);
  });
  assert.strictEqual(dets, 0);
  assert.ok(rows.indexOf('lead') >= 0 && rows.indexOf('ledger') >= 0 && rows.indexOf('9') >= 0, rows.join(','));
});

test('try: without a profile the viewer still mounts (no profile section)', () => {
  const b = boot({ tryit: clone(VIEW) });
  run(b.env, 'site-try.js');
  assert.deepStrictEqual(b.env.errors, []);
  const sections = [];
  walk(b.host, (n) => { if (n.attributes && n.attributes['data-section']) sections.push(n.attributes['data-section']); });
  assert.strictEqual(sections.indexOf('profile'), -1);
  assert.ok(allText(b.host).indexOf(EN['band.test_pattern']) >= 0);
});

test('try: no data file leaves a plain message, never a blank or a throw', () => {
  const b = boot({});
  run(b.env, 'site-try.js');
  assert.ok(allText(b.host).indexOf(EN['try.no_data']) >= 0);
});

test('try: SS.tryit.start returns the viewer handle', () => {
  const b = boot({ tryit: clone(VIEW), tryit_profile: clone(SYNTH) });
  run(b.env, 'site-try.js');
  assert.strictEqual(typeof b.env.SS.tryit.start, 'function');
});

// ------------------------------------------------------------------------------------------------- theme

test('theme: follows the system until chosen, and the toggle flips and remembers', () => {
  const storage = memoryStorage();
  const b = boot({}, { storage, systemDark: true });
  run(b.env, 'site-shell.js');
  const de = b.doc.documentElement;
  assert.strictEqual(de.getAttribute('data-theme'), null, 'no forced theme before a choice');
  assert.strictEqual(b.button.getAttribute('aria-pressed'), 'true', 'system dark shows pressed');
  b.button.listeners.click[0]();
  assert.strictEqual(de.getAttribute('data-theme'), 'light');
  assert.strictEqual(b.button.getAttribute('aria-pressed'), 'false');
  assert.strictEqual(storage.dump()['ss-theme'], 'light');
  b.button.listeners.click[0]();
  assert.strictEqual(de.getAttribute('data-theme'), 'dark');
  assert.strictEqual(storage.dump()['ss-theme'], 'dark');
});

test('theme: a remembered choice is applied at load', () => {
  const b = boot({}, { storage: memoryStorage({ 'ss-theme': 'dark' }), systemDark: false });
  run(b.env, 'site-shell.js');
  assert.strictEqual(b.doc.documentElement.getAttribute('data-theme'), 'dark');
  assert.strictEqual(b.button.getAttribute('aria-pressed'), 'true');
});

test('theme: storage that throws is ignored; the page still works', () => {
  const bad = { getItem: () => { throw new Error('denied'); }, setItem: () => { throw new Error('denied'); } };
  const b = boot({}, { storage: bad, systemDark: false });
  run(b.env, 'site-shell.js');
  b.button.listeners.click[0]();   // dark is the default, so the toggle goes to light
  assert.strictEqual(b.doc.documentElement.getAttribute('data-theme'), 'light');
  assert.deepStrictEqual(b.env.errors, []);
});

test('theme: #theme=dark in the hash forces dark and is not remembered (screenshots)', () => {
  const storage = memoryStorage();
  const b = boot({}, { storage, systemDark: false, hash: '#map=X&theme=dark' });
  run(b.env, 'site-shell.js');
  assert.strictEqual(b.doc.documentElement.getAttribute('data-theme'), 'dark');
  assert.deepStrictEqual(storage.dump(), {});
});

test('theme: a stored value that is not light or dark is ignored', () => {
  const b = boot({}, { storage: memoryStorage({ 'ss-theme': 'purple' }), systemDark: false });
  run(b.env, 'site-shell.js');
  assert.strictEqual(b.doc.documentElement.getAttribute('data-theme'), null);
  assert.strictEqual(b.button.getAttribute('aria-pressed'), 'true');   // ignored, so the default (dark) shows
});
