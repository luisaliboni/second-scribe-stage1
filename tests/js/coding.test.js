'use strict';
// Task 37: the Reference screen (app-reference.js) and the blind mode-B Coding screen (app-coding.js); spec 6.6, 6.7.
// The app's scripts load in the server's order (core, api, shell, then app-*.js sorted by name) into a vm context
// with a small stub DOM (parent links and text nodes, so selections can be built) and a fake fetch that answers
// like Task 36's routes.
//
// Blindness is the invariant under test: these screens only ever ask for the routes in ALLOWED, never /api/view
// or anything under /api/results/ (the first results view served logs the reveal), and they render only the
// fields they know, so anything else a reply might carry never reaches the page.
const test = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ROOT = path.resolve(__dirname, '..', '..');
const JS = path.join(ROOT, 'second_scribe', 'app', 'static', 'js');
const STRINGS = JSON.parse(fs.readFileSync(path.join(ROOT, 'second_scribe', 'app', 'strings', 'en.json'), 'utf8'));
const LIMIT = 'blindness is enforced by this interface and recorded in the log order; it is not cryptographic, ' +
  'so someone could open the run folder by hand.';
const SENTINEL = 'ZEBRAQUARTZ AI DERIVED SENTINEL';

// --- a small DOM -------------------------------------------------------------------------------------------------

function textNode(s) {
  return { nodeType: 3, nodeValue: String(s), get textContent() { return this.nodeValue; }, parentNode: null };
}

function makeNode(tag) {
  const node = {
    nodeType: 1, tagName: String(tag).toUpperCase(), childNodes: [], attributes: {}, listeners: {},
    parentNode: null, disabled: false, value: '', checked: false, hidden: false, _text: '',
    get children() { return node.childNodes.filter((c) => c.nodeType === 1); },
    get firstChild() { return node.childNodes[0] || null; },
    get isConnected() {
      let n = node;
      while (n.parentNode) n = n.parentNode;
      return n.tagName === 'BODY';
    },
    get textContent() { return node._text + node.childNodes.map((c) => c.textContent).join(''); },
    set textContent(v) { node.childNodes.forEach((c) => { c.parentNode = null; }); node.childNodes = []; node._text = String(v); },
    setAttribute(k, v) { node.attributes[k] = String(v); },
    getAttribute(k) { return Object.prototype.hasOwnProperty.call(node.attributes, k) ? node.attributes[k] : null; },
    removeAttribute(k) { delete node.attributes[k]; },
    appendChild(c) {
      if (c.parentNode) c.parentNode.removeChild(c);
      c.parentNode = node; node.childNodes.push(c); return c;
    },
    insertBefore(c, ref) {
      if (!ref) return node.appendChild(c);
      if (c.parentNode) c.parentNode.removeChild(c);
      c.parentNode = node; node.childNodes.splice(node.childNodes.indexOf(ref), 0, c); return c;
    },
    removeChild(c) { node.childNodes.splice(node.childNodes.indexOf(c), 1); c.parentNode = null; return c; },
    addEventListener(type, fn) { (node.listeners[type] = node.listeners[type] || []).push(fn); },
    fire(type, extra) { (node.listeners[type] || []).forEach((fn) => fn(Object.assign({ preventDefault() {} }, extra || {}))); },
    focus() {},
    scrollIntoView() {},
  };
  return node;
}

function all(node, pred, out) {
  out = out || [];
  if (node.nodeType === 1) {
    if (pred(node)) out.push(node);
    node.childNodes.forEach((c) => all(c, pred, out));
  }
  return out;
}
const text = (node) => node.textContent;
const cls = (n, c) => (n.attributes.class || '').split(' ').indexOf(c) >= 0;
const byClass = (node, c) => all(node, (n) => cls(n, c));
const byTag = (node, tag) => all(node, (n) => n.tagName === tag.toUpperCase());
const button = (node, label) => {
  const hits = byTag(node, 'button').filter((b) => b.textContent === label);
  assert.ok(hits.length, 'no button ' + label + ' in: ' + text(node).slice(0, 400));
  return hits[0];
};
const textNodes = (node, out) => {
  out = out || [];
  node.childNodes.forEach((c) => (c.nodeType === 3 ? out.push(c) : textNodes(c, out)));
  return out;
};

// --- loading the app's scripts -----------------------------------------------------------------------------------

function appScripts() {
  const screens = fs.readdirSync(JS).filter((f) => /^app-[a-z0-9-]+\.js$/.test(f) && f !== 'app-shell.js').sort();
  return ['core.js', 'api.js', 'app-shell.js'].concat(screens);
}

function reply(status, doc) {
  return { ok: status >= 200 && status < 300, status, text: () => Promise.resolve(JSON.stringify(doc)) };
}

// Routes these two screens may call (Task 36). Anything else is a test failure.
const ALLOWED = ['GET /api/session', 'GET /api/references', 'POST /api/references', 'POST /api/references/bulk',
  'GET /api/coding', 'POST /api/coding/mark', 'POST /api/coding/unmark', 'POST /api/coding/tick',
  'POST /api/coding/lock', 'GET /api/coding/suggest'];

function load(opts) {
  opts = opts || {};
  const calls = [];
  const errors = [];
  const intervals = [];
  const body = makeNode('body');
  const sandbox = {
    console: { error: (...a) => errors.push(a.map(String).join(' ')), log() {}, warn() {} },
    location: { hash: '', search: '' },
    SS_STRINGS: Object.assign({}, STRINGS, opts.strings || {}),
    document: { createElement: makeNode, createTextNode: textNode, addEventListener() {}, body, hidden: false },
    setTimeout: (fn) => { fn(); return 0; },
    clearTimeout() {},
    setInterval: (fn) => { intervals.push(fn); return intervals.length; },
    clearInterval: (id) => { intervals[id - 1] = null; },
    fetch: (url, init) => {
      const u = String(url);
      calls.push({ url: u, method: init.method, body: init.body ? JSON.parse(init.body) : undefined });
      if (u === '/api/session') return Promise.resolve(reply(200, { csrf: 'tok' }));
      return Promise.resolve((opts.answer || (() => reply(404, { error: 'not_found' })))(u, init, calls));
    },
  };
  sandbox.window = sandbox;
  const ctx = vm.createContext(sandbox);
  for (const f of appScripts()) vm.runInContext(fs.readFileSync(path.join(JS, f), 'utf8'), ctx, { filename: f });
  const main = makeNode('main');
  body.appendChild(main);
  const tickAll = (n) => { for (let i = 0; i < n; i++) intervals.forEach((fn) => fn && fn()); };
  return { SS: sandbox.SS, calls, errors, main, body, tickAll, intervals, doc: sandbox.document };
}

const tick = () => new Promise((r) => setImmediate(r));
async function settle() { for (let i = 0; i < 20; i++) await tick(); }

// --- fixtures shaped like Task 36's replies ----------------------------------------------------------------------

const TURNS = [
  { idx: 0, speaker: 'interviewer', text: 'Could you start from the beginning?' },
  { idx: 1, speaker: 'participant', text: 'After that came the how-to pages on the city website, which helped a lot.' },
  { idx: 2, speaker: 'interviewer', text: 'How did that go?' },
  { idx: 3, speaker: 'participant',
    text: 'The café 🙂 owner said the commissary agreement was signed at the counter that same afternoon.' },
];

function codingDoc(extra) {
  return Object.assign({
    pid: 'SYN-OW03', type: 'B', turns: TURNS,
    marks: [{ mark_id: 'mark-0001', turn: 1, start: 20, end: 56, text: 'how-to pages on the city website, wh',
      status: 'happened', missing_kind: null, step: 'Read the how-to pages', phase: 'Planning' }],
    seconds: 75, locked: false, changed_after_lock: false, phases: ['Planning', 'Permits', 'Inspections'],
    missing_kinds: ['never came', 'wished for', 'denied'], min_words: 8, limit: LIMIT,
  }, extra || {});
}

function refsDoc(extra) {
  return Object.assign({
    references: [
      { pid: 'MAP-A', type: 'A', map_confirmed: true, coding: { n_marks: 0, locked: false } },
      { pid: 'SYN-OW03', type: 'B', map_confirmed: false, coding: { n_marks: 1, locked: false } },
      { pid: 'SYN-OW05', type: 'B', map_confirmed: true, coding: { n_marks: 4, locked: true } },
      { pid: 'SYN-OF01', type: 'C', map_confirmed: false, coding: { n_marks: 0, locked: false } },
      { pid: 'SYN-OF02', type: null, map_confirmed: true, coding: { n_marks: 0, locked: false } },
    ],
    gate: { open: false, waiting: ['SYN-OW03'], revealed: false },
  }, extra || {});
}

// A fake server for both screens. `state` lets a test change what later replies say.
function server(state) {
  state = state || {};
  return (url, init) => {
    const method = init.method;
    const p = url.split('?')[0];
    if (method === 'GET' && p === '/api/references') return reply(200, state.refs || refsDoc());
    if (method === 'GET' && p === '/api/coding') return reply(200, state.coding || codingDoc());
    if (method === 'GET' && p === '/api/coding/suggest') return reply(200, { names: ['Read the how-to pages'] });
    if (method === 'POST' && p === '/api/coding/mark') {
      if (state.markError) return reply(422, state.markError);
      return reply(200, { mark: {}, n_marks: 2 });
    }
    if (method === 'POST' && p === '/api/coding/unmark') return reply(200, { n_marks: 0 });
    if (method === 'POST' && p === '/api/coding/tick') return reply(200, { seconds: 90 });
    if (method === 'POST' && p === '/api/coding/lock') {
      if (state.lockError) return reply(422, state.lockError);
      state.coding = codingDoc({ locked: true });
      return reply(200, state.lockReply || { sha256: 'c'.repeat(64), n_marks: 1 });
    }
    if (method === 'POST' && p === '/api/references') return reply(200, { pid: 'x', type: 'A', changed: true, gate: {} });
    if (method === 'POST' && p === '/api/references/bulk') return reply(200, { set: ['SYN-OF02'], gate: {} });
    return reply(404, { error: 'not_found', message: 'no route' });
  };
}

function turnEl(main, i) {
  const hits = all(main, (n) => n.getAttribute('data-turn') === String(i) && cls(n, 'ss-coding-text'));
  assert.strictEqual(hits.length, 1, 'turn ' + i + ' text element');
  return hits[0];
}

// A Selection-like object between (nodeA, offA) and (nodeB, offB).
function sel(a, offA, b, offB) {
  return { rangeCount: 1, isCollapsed: a === b && offA === offB,
    getRangeAt: () => ({ startContainer: a, startOffset: offA, endContainer: b, endOffset: offB }) };
}

async function openCoding(opts) {
  const env = load(Object.assign({ answer: server(opts && opts.state) }, opts || {}));
  env.SS.coding.render(env.main, 'SYN-OW03');
  await settle();
  return env;
}

// --- tests -------------------------------------------------------------------------------------------------------

test('both screens register with the shell, after Setup', () => {
  const { SS, errors } = load();
  assert.deepStrictEqual(errors, []);
  const ids = Array.from(SS.shell.list());
  assert.ok(ids.indexOf('setup') < ids.indexOf('reference'), ids.join(','));
  assert.ok(ids.indexOf('reference') < ids.indexOf('coding'), ids.join(','));
});

test('selection_offsets_match_text: offsets are code points into the turn, across highlight segments', async () => {
  const { SS, main } = await openCoding();
  // turn 1 is split by the stored mark into three text nodes; select from inside the highlight to the last one
  const t1 = turnEl(main, 1);
  const nodes = textNodes(t1);
  assert.ok(nodes.length >= 3, 'the mark splits the turn into segments');
  const full = TURNS[1].text;
  assert.strictEqual(nodes.map((n) => n.nodeValue).join(''), full, 'the DOM holds the turn text exactly');
  const s = SS.coding.selection(main, sel(nodes[1], 4, nodes[2], 8));
  assert.ok(s, 'a selection inside one turn is accepted');
  assert.strictEqual(s.turn, 1);
  assert.strictEqual(Array.from(full).slice(s.start, s.end).join(''), s.text);
  assert.strictEqual(s.text, full.slice(20 + 4, 56 + 8).trim());

  // turn 3 has an emoji before the selection: start/end count code points (Python's str), not UTF-16 units
  const t3 = turnEl(main, 3);
  const n3 = textNodes(t3)[0];
  const t = TURNS[3].text;
  const from = t.indexOf('owner');
  const to = t.indexOf(' that same');
  const s3 = SS.coding.selection(main, sel(n3, from, n3, to));
  assert.strictEqual(s3.text, t.slice(from, to));
  assert.strictEqual(s3.start, from - 1, 'the emoji is one code point but two UTF-16 units');
  assert.strictEqual(Array.from(t).slice(s3.start, s3.end).join(''), s3.text);

  // surrounding spaces are trimmed off, with the offsets moved to match
  const s4 = SS.coding.selection(main, sel(n3, from - 1, n3, to + 1));
  assert.strictEqual(s4.text, t.slice(from, to));
  // an element container with a child index (what a triple click gives) counts the text before that child
  const s5 = SS.coding.selection(main, sel(t1, 0, t1, t1.childNodes.length));
  assert.strictEqual(s5.text, full);
  assert.strictEqual(s5.start, 0);
  // nothing selected, or a caret only
  assert.strictEqual(SS.coding.selection(main, { rangeCount: 0 }), null);
  assert.strictEqual(SS.coding.selection(main, sel(n3, 5, n3, 5)), null);
});

test('S6_whole_short_turn_opens_the_form_a_part_of_it_does_not', async () => {
  // the kit's rule (provenance.eligible): fewer than min_words words only when the span is the whole turn
  const { SS, main } = await openCoding();
  const n2 = textNodes(turnEl(main, 2))[0];
  const t = TURNS[2].text;                                   // 'How did that go?' (4 words)
  SS.coding.capture(main, sel(n2, 0, n2, 3));                // 'How'
  const want = STRINGS['coding.error.span_too_short'].replace('{min_words}', '8');
  assert.ok(text(main).indexOf(want) >= 0, 'a part of a short turn is refused');
  assert.strictEqual(byClass(main, 'ss-coding-form').filter((f) => !f.hidden).length, 0);
  const span = SS.coding.capture(main, sel(n2, 0, n2, t.length));
  assert.ok(span && span.text === t, 'the whole turn is a span');
  assert.strictEqual(byClass(main, 'ss-coding-form').filter((f) => !f.hidden).length, 1, 'the whole short turn opens the form');
  // without its final punctuation it is still every word of the turn (the kit compares word sequences)
  const span2 = SS.coding.capture(main, sel(n2, 0, n2, t.length - 1));
  assert.ok(span2 && span2.text === 'How did that go');
});

test('cross_turn_selection_refused: a selection across two turns gives null and a message', async () => {
  const { SS, main } = await openCoding();
  const a = textNodes(turnEl(main, 1))[0];
  const b = textNodes(turnEl(main, 3))[0];
  assert.strictEqual(SS.coding.selection(main, sel(a, 2, b, 10)), null);
  assert.strictEqual(SS.coding.readSelection(main, sel(a, 2, b, 10)).refused, 'cross_turn');
  // outside the transcript (e.g. in the marks panel) is not a span either
  const outside = textNode('elsewhere');
  makeNode('div').appendChild(outside);
  assert.strictEqual(SS.coding.selection(main, sel(outside, 0, outside, 4)), null);

  SS.coding.capture(main, sel(a, 2, b, 10));
  assert.ok(text(main).indexOf(STRINGS['coding.cross_turn']) >= 0, 'the cross-turn message is shown');
});

test('short_span_message_shown: a short selection shows the span rule with min_words from the reply', async () => {
  for (const minWords of [8, 5]) {
    const { SS, main } = await openCoding({ state: { coding: codingDoc({ min_words: minWords }) } });
    const n3 = textNodes(turnEl(main, 3))[0];
    const t = TURNS[3].text;
    const from = t.indexOf('commissary');
    SS.coding.capture(main, sel(n3, from, n3, from + 'commissary agreement was'.length));   // 3 words
    const want = STRINGS['coding.error.span_too_short'].replace('{min_words}', String(minWords));
    assert.ok(text(main).indexOf(want) >= 0, 'span rule shown for min_words ' + minWords);
    assert.strictEqual(byClass(main, 'ss-coding-form').filter((f) => !f.hidden).length, 0, 'no mark form for a short span');
  }
  // the server's refusal message is shown too (the server is the authority on word counts)
  const message = 'Select at least 8 words. Shorter spans are not used.';
  const { SS, main } = await openCoding({ state: { markError: { error: 'span_too_short', message } } });
  const n3 = textNodes(turnEl(main, 3))[0];
  SS.coding.capture(main, sel(n3, 0, n3, TURNS[3].text.length));
  const form = byClass(main, 'ss-coding-form').filter((f) => !f.hidden)[0];
  assert.ok(form, 'a long enough span opens the mark form');
  const step = byTag(form, 'input').filter((i) => i.getAttribute('name') === 'step')[0];
  step.value = 'Commissary agreement';
  button(form, STRINGS['coding.save']).fire('click');
  await settle();
  assert.ok(text(main).indexOf(message) >= 0, 'the server refusal is shown');
});

test('the word count follows the kit (textnorm.tokens): apostrophes, punctuation, curly quotes, NBSP', () => {
  const { SS } = load();
  assert.strictEqual(SS.coding.wordCount("it's the clerk's 'form' — 2 of them"), 7);
  assert.strictEqual(SS.coding.wordCount('one two’s “three”... ---'), 3);
  assert.strictEqual(SS.coding.wordCount(''), 0);
});

test('saving a mark sends the span, the status, the kind only for Missing, the step and the phase', async () => {
  const { SS, main, calls } = await openCoding();
  const n3 = textNodes(turnEl(main, 3))[0];
  const t = TURNS[3].text;
  SS.coding.capture(main, sel(n3, 0, n3, t.length));
  let form = byClass(main, 'ss-coding-form').filter((f) => !f.hidden)[0];
  const inputs = byTag(form, 'input');
  const step = inputs.filter((i) => i.getAttribute('name') === 'step')[0];
  step.value = '  Commissary agreement ';
  step.fire('input');
  await settle();
  assert.ok(calls.some((c) => c.url.indexOf('/api/coding/suggest?prefix=') === 0), 'suggestions asked from own names');
  const dl = byTag(form, 'datalist')[0];
  assert.deepStrictEqual(byTag(dl, 'option').map((o) => o.getAttribute('value')), ['Read the how-to pages']);
  const missing = inputs.filter((i) => i.getAttribute('value') === 'missing')[0];
  missing.checked = true;
  missing.fire('change');
  const kind = inputs.filter((i) => i.getAttribute('value') === 'wished for')[0];
  assert.strictEqual(kind.disabled, false, 'kinds open for Missing');
  kind.checked = true;
  const phase = byTag(form, 'select')[0];
  phase.value = 'Permits';
  button(form, STRINGS['coding.save']).fire('click');
  await settle();
  const post = calls.filter((c) => c.url === '/api/coding/mark')[0];
  assert.deepStrictEqual(post.body, { pid: 'SYN-OW03', turn: 3, start: 0, end: Array.from(t).length, text: t,
    status: 'missing', missing_kind: 'wished for', step: 'Commissary agreement', phase: 'Permits' });

  // Happened never sends a kind, even when one was ticked before switching
  const env2 = await openCoding();
  const m3 = textNodes(turnEl(env2.main, 3))[0];
  env2.SS.coding.capture(env2.main, sel(m3, 0, m3, t.length));
  form = byClass(env2.main, 'ss-coding-form').filter((f) => !f.hidden)[0];
  const in2 = byTag(form, 'input');
  in2.filter((i) => i.getAttribute('name') === 'step')[0].value = 'Commissary agreement';
  const miss2 = in2.filter((i) => i.getAttribute('value') === 'missing')[0];
  miss2.checked = true; miss2.fire('change');
  in2.filter((i) => i.getAttribute('value') === 'denied')[0].checked = true;
  miss2.checked = false;
  const hap = in2.filter((i) => i.getAttribute('value') === 'happened')[0];
  hap.checked = true; hap.fire('change');
  assert.strictEqual(in2.filter((i) => i.getAttribute('value') === 'denied')[0].disabled, true, 'kinds closed for Happened');
  button(form, STRINGS['coding.save']).fire('click');
  await settle();
  const post2 = env2.calls.filter((c) => c.url === '/api/coding/mark')[0];
  assert.strictEqual(post2.body.status, 'happened');
  assert.ok(!('missing_kind' in post2.body), 'no kind with Happened');
});

test('the stated limit is shown verbatim from the reply, never retyped', async () => {
  const { main } = await openCoding();
  assert.ok(text(main).indexOf(LIMIT) >= 0, 'limit from the reply is on screen');
  const other = 'a different limit text served by the API';
  const env = await openCoding({ state: { coding: codingDoc({ limit: other }) } });
  assert.ok(text(env.main).indexOf(other) >= 0);
  assert.ok(text(env.main).indexOf(LIMIT) < 0, 'nothing on screen retypes the limit');
  for (const f of ['app-coding.js', 'app-reference.js']) {
    assert.ok(fs.readFileSync(path.join(JS, f), 'utf8').indexOf('not cryptographic') < 0, f + ' retypes the limit');
  }
  assert.ok(!Object.values(STRINGS).some((v) => v.indexOf('not cryptographic') >= 0), 'en.json retypes the limit');
});

test('the visible timer counts while the page is shown and adds the time in ticks', async () => {
  const env = await openCoding();
  assert.ok(text(env.main).indexOf('00:01:15') >= 0, 'stored seconds shown');
  env.tickAll(14);
  assert.ok(text(env.main).indexOf('00:01:29') >= 0);
  assert.strictEqual(env.calls.filter((c) => c.url === '/api/coding/tick').length, 0);
  env.tickAll(1);
  await settle();
  const ticks = env.calls.filter((c) => c.url === '/api/coding/tick');
  assert.strictEqual(ticks.length, 1);
  assert.deepStrictEqual(ticks[0].body, { pid: 'SYN-OW03', seconds: 15 });
  env.doc.hidden = true;           // a hidden page does not count
  env.tickAll(30);
  await settle();
  assert.strictEqual(env.calls.filter((c) => c.url === '/api/coding/tick').length, 1);
  env.doc.hidden = false;
  env.tickAll(3);
  env.body.removeChild(env.main);  // the shell left the screen: stop and send what is pending
  env.tickAll(1);
  await settle();
  const last = env.calls.filter((c) => c.url === '/api/coding/tick');
  assert.strictEqual(last.length, 2);
  assert.strictEqual(last[1].body.seconds, 3);
  assert.ok(env.intervals.every((f) => f === null), 'interval cleared');
});

test('Finish and lock asks to confirm, sends pending time, locks, then shows the coding read-only', async () => {
  const state = {};
  const env = await openCoding({ state });
  env.tickAll(4);
  const lockBtn = button(env.main, STRINGS['coding.lock_button']);
  lockBtn.fire('click');
  assert.strictEqual(env.calls.filter((c) => c.url === '/api/coding/lock').length, 0, 'nothing locked before confirm');
  assert.ok(text(env.main).indexOf(STRINGS['coding.confirm_title'].replace('{pid}', 'SYN-OW03')) >= 0);
  button(env.main, STRINGS['coding.confirm_no']).fire('click');
  assert.ok(text(env.main).indexOf(STRINGS['coding.confirm_title'].replace('{pid}', 'SYN-OW03')) < 0, 'back out');
  button(env.main, STRINGS['coding.lock_button']).fire('click');
  button(env.main, STRINGS['coding.confirm_yes']).fire('click');
  await settle();
  const urls = env.calls.map((c) => c.method + ' ' + c.url.split('?')[0]);
  const iTick = urls.lastIndexOf('POST /api/coding/tick');
  const iLock = urls.indexOf('POST /api/coding/lock');
  assert.ok(iTick >= 0 && iTick < iLock, 'pending time is sent before the lock: ' + urls.join(','));
  assert.deepStrictEqual(env.calls[iLock].body, { pid: 'SYN-OW03' });
  assert.ok(text(env.main).indexOf(STRINGS['coding.locked_title']) >= 0, 'locked state shown');
  assert.ok(text(env.main).indexOf('c'.repeat(64)) >= 0, 'the logged sha256 is shown');
  assert.strictEqual(byClass(env.main, 'ss-coding-form').length, 0, 'no mark form once locked');
  assert.ok(!byTag(env.main, 'button').some((b) => b.textContent === STRINGS['coding.remove']), 'no remove once locked');
  assert.ok(!byTag(env.main, 'button').some((b) => b.textContent === STRINGS['coding.lock_button']));

  const refused = 'The step “Fee” in Permits is marked both Happened and Missing. Keep one of them, then lock.';
  const env2 = await openCoding({ state: { lockError: { error: 'conflicting_marks', message: refused } } });
  button(env2.main, STRINGS['coding.lock_button']).fire('click');
  button(env2.main, STRINGS['coding.confirm_yes']).fire('click');
  await settle();
  assert.ok(text(env2.main).indexOf(refused) >= 0, 'lock refusal shown');
});

test('removing a mark asks the server and redraws', async () => {
  const env = await openCoding();
  const rm = button(env.main, STRINGS['coding.remove']);
  rm.fire('click');
  await settle();
  const post = env.calls.filter((c) => c.url === '/api/coding/unmark')[0];
  assert.deepStrictEqual(post.body, { pid: 'SYN-OW03', mark_id: 'mark-0001' });
});

test('a transcript whose reference is not B shows no transcript and points to the Reference screen', async () => {
  const env = await openCoding({ state: { coding: codingDoc({ type: 'A' }) } });
  assert.ok(text(env.main).indexOf(STRINGS['coding.not_b']) >= 0);
  assert.ok(text(env.main).indexOf(TURNS[1].text) < 0);
  assert.strictEqual(env.intervals.length, 0, 'no timer');
});

test('without a pid the screen lists the B transcripts, with links to code each', async () => {
  const env = load({ answer: server() });
  env.SS.coding.render(env.main, '');
  await settle();
  const links = byTag(env.main, 'a').map((a) => a.getAttribute('href'));
  assert.ok(links.indexOf('#coding/SYN-OW03') >= 0 && links.indexOf('#coding/SYN-OW05') >= 0, links.join(','));
  assert.ok(links.indexOf('#coding/MAP-A') < 0);
  const none = load({ answer: server({ refs: refsDoc({ references: [] }) }) });
  none.SS.coding.render(none.main, '');
  await settle();
  assert.ok(text(none.main).indexOf(STRINGS['coding.none']) >= 0);
  // Part 5b, scout F4: nothing to code says so and points to the run, not back to Reference
  assert.ok(STRINGS['coding.none'].indexOf('Nothing to code') === 0);
  const next = byTag(none.main, 'a').filter((a) => a.getAttribute('href') === '#instructions');
  assert.strictEqual(next.length, 1);
  assert.strictEqual(next[0].textContent, STRINGS['coding.next_run']);
  assert.strictEqual(byTag(none.main, 'a').filter((a) => a.getAttribute('href') === '#reference').length, 0);
});

test('reference screen: a radio group per transcript, A only with a confirmed map, the C note, bulk', async () => {
  const env = load({ answer: server() });
  env.SS.reference.render(env.main, '');
  await settle();
  const groups = byTag(env.main, 'fieldset');
  assert.strictEqual(groups.length, 5);
  const radios = (pid) => byTag(groups.filter((g) => text(g).indexOf(pid) >= 0)[0], 'input');
  const a3 = radios('SYN-OW03').filter((r) => r.getAttribute('value') === 'A')[0];
  assert.strictEqual(a3.disabled, true, 'A needs a confirmed map');
  assert.strictEqual(radios('MAP-A').filter((r) => r.getAttribute('value') === 'A')[0].checked, true);
  assert.ok(radios('SYN-OF02').every((r) => !r.checked), 'nothing chosen yet');
  assert.ok(text(env.main).indexOf(STRINGS['reference.c_note']) >= 0, 'C note');
  assert.ok(text(env.main).indexOf(STRINGS['reference.every_mode_title']) >= 0, 'own-quote check in every mode');
  assert.ok(text(env.main).indexOf('SYN-OW03') >= 0 && text(env.main).indexOf(STRINGS['reference.gate_waiting']
    .replace('{pids}', 'SYN-OW03')) >= 0, 'gate line names the open codings');
  const links = byTag(env.main, 'a').map((a) => a.getAttribute('href'));
  assert.ok(links.indexOf('#coding/SYN-OW03') >= 0);

  const c = radios('SYN-OF02').filter((r) => r.getAttribute('value') === 'C')[0];
  c.checked = true;
  c.fire('change');
  await settle();
  const set = env.calls.filter((x) => x.method === 'POST' && x.url === '/api/references')[0];
  assert.deepStrictEqual(set.body, { pid: 'SYN-OF02', type: 'C' });

  button(env.main, STRINGS['reference.bulk']).fire('click');
  await settle();
  const bulk = env.calls.filter((x) => x.url === '/api/references/bulk')[0];
  assert.deepStrictEqual(bulk.body, { confirmed_maps: 'A' });
  assert.ok(text(env.main).indexOf(STRINGS['reference.bulk_done'].replace('{n}', '1')) >= 0);
});

test('reference screen: after the reveal, B is closed for a transcript whose coding was not locked before it', async () => {
  const env = load({ answer: server({ refs: refsDoc({ gate: { open: true, waiting: [], revealed: true } }) }) });
  env.SS.reference.render(env.main, '');
  await settle();
  const g = byTag(env.main, 'fieldset').filter((x) => text(x).indexOf('SYN-OF02') >= 0)[0];
  assert.strictEqual(byTag(g, 'input').filter((r) => r.getAttribute('value') === 'B')[0].disabled, true);
  const g5 = byTag(env.main, 'fieldset').filter((x) => text(x).indexOf('SYN-OW05') >= 0)[0];
  assert.strictEqual(byTag(g5, 'input').filter((r) => r.getAttribute('value') === 'B')[0].disabled, false);
});

test('blindness: the screens never request /api/view or any results route, in any flow', async () => {
  for (const f of ['app-coding.js', 'app-reference.js']) {
    const src = fs.readFileSync(path.join(JS, f), 'utf8');
    assert.ok(!/\/api\/view/.test(src), f + ' names /api/view');
    assert.ok(!/\/api\/results/.test(src), f + ' names a results route');
  }
  const state = {};
  const env = load({ answer: server(state) });
  env.SS.reference.render(env.main, '');
  await settle();
  const c = byTag(env.main, 'input').filter((r) => r.getAttribute('value') === 'C')[0];
  c.checked = true; c.fire('change');
  await settle();
  button(env.main, STRINGS['reference.bulk']).fire('click');
  await settle();
  env.SS.coding.render(env.main, '');
  await settle();
  env.SS.coding.render(env.main, 'SYN-OW03');
  await settle();
  const n3 = textNodes(turnEl(env.main, 3))[0];
  env.SS.coding.capture(env.main, sel(n3, 0, n3, TURNS[3].text.length));
  const form = byClass(env.main, 'ss-coding-form').filter((f) => !f.hidden)[0];
  byTag(form, 'input').filter((i) => i.getAttribute('name') === 'step')[0].value = 'Commissary agreement';
  button(form, STRINGS['coding.save']).fire('click');
  await settle();
  button(env.main, STRINGS['coding.remove']).fire('click');
  await settle();
  env.tickAll(20);
  button(env.main, STRINGS['coding.lock_button']).fire('click');
  button(env.main, STRINGS['coding.confirm_yes']).fire('click');
  await settle();
  env.SS.coding.render(env.main, 'SYN-OW03');     // the locked view
  await settle();
  const seen = env.calls.map((x) => x.method + ' ' + x.url.split('?')[0]);
  ['POST /api/references', 'POST /api/references/bulk', 'POST /api/coding/mark', 'POST /api/coding/unmark',
    'POST /api/coding/tick', 'POST /api/coding/lock', 'GET /api/coding/suggest'].forEach((r) => {
    assert.ok(seen.indexOf(r) >= 0 || r === 'GET /api/coding/suggest', 'flow exercised ' + r);
  });
  seen.forEach((r) => {
    assert.ok(ALLOWED.indexOf(r) >= 0, 'unexpected request ' + r);
    assert.ok(!/\/api\/(view|results)/.test(r), 'results route requested: ' + r);
  });
});

test('blindness: fields a reply should not carry never reach the page', async () => {
  const dirty = codingDoc({ ai_map: { nodes: [{ text: SENTINEL }] }, script: SENTINEL, view: SENTINEL,
    turns: TURNS.map((t) => Object.assign({}, t, { ai: SENTINEL })),
    marks: codingDoc().marks.map((m) => Object.assign({}, m, { judge: SENTINEL })) });
  const refs = refsDoc();
  refs.references[0].ai_steps = SENTINEL;
  refs.results = SENTINEL;
  const env = load({ answer: server({ coding: dirty, refs }) });
  env.SS.coding.render(env.main, 'SYN-OW03');
  await settle();
  assert.ok(text(env.main).indexOf(SENTINEL) < 0, 'coding screen');
  env.SS.reference.render(env.main, '');
  await settle();
  assert.ok(text(env.main).indexOf(SENTINEL) < 0, 'reference screen');
  const attrs = all(env.main, () => true).map((n) => JSON.stringify(n.attributes)).join(' ');
  assert.ok(attrs.indexOf(SENTINEL) < 0);
});

// --- 2.2 issue 5 and issue 6 -------------------------------------------------------------------------------------------------

function spyOn(env) {
  const seen = { focus: [], scroll: [] };
  env.doc.createElement = (t) => {
    const n = makeNode(t);
    n.focus = (o) => seen.focus.push({ node: n, opts: o });
    n.scrollIntoView = (o) => seen.scroll.push({ node: n, opts: o });
    return n;
  };
  return seen;
}

async function lockIt(state, spy) {
  const env = load({ answer: server(state) });
  const seen = spy ? spyOn(env) : null;
  env.SS.coding.render(env.main, 'SYN-OW03');
  await settle();
  button(env.main, STRINGS['coding.lock_button']).fire('click');
  button(env.main, STRINGS['coding.confirm_yes']).fire('click');
  await settle();
  return Object.assign(env, { seen });
}

test('issue 6 after "Lock it" a status line names the hash and takes keyboard focus', async () => {
  const env = await lockIt({}, true);
  const line = byClass(env.main, 'ss-coding-lock-status');
  assert.strictEqual(line.length, 1, 'one status line');
  assert.strictEqual(line[0].textContent, 'Locked; logged with hash cccccccccccc\u2026');
  assert.strictEqual(line[0].getAttribute('role'), 'status');
  assert.ok(!text(line[0]).includes('c'.repeat(13)), 'the line shows a short form; the full hash stays in the record above');
  assert.ok(env.seen.focus.some((f) => f.node === line[0]), 'focus moved to the line (the Lock it button is gone)');
  assert.ok(env.seen.scroll.some((f) => f.node === line[0]), 'the line is brought into sight');
  assert.ok(text(env.main).indexOf('c'.repeat(64)) >= 0, 'the full sha256 is still printed in the lock record');
  assert.deepStrictEqual(env.errors, []);
});

test('issue 6 with no hash in the reply the line says Locked and logged', async () => {
  const env = await lockIt({ lockReply: { n_marks: 1 } });
  const line = byClass(env.main, 'ss-coding-lock-status');
  assert.strictEqual(line.length, 1);
  assert.strictEqual(line[0].textContent, 'Locked and logged');
});

test('issue 6 a coding opened already locked (a later visit) shows no status line', async () => {
  const env = await openCoding({ state: { coding: codingDoc({ locked: true }) } });
  assert.strictEqual(byClass(env.main, 'ss-coding-lock-status').length, 0);
  assert.ok(text(env.main).indexOf(STRINGS['coding.locked_title']) >= 0, 'the locked note itself stays');
});

test('issue 5 one mark reads "1 mark", the others keep the plural', async () => {
  const env = await openCoding({});
  assert.ok(text(env.main).indexOf('1 mark') >= 0 && !/1 marks/.test(text(env.main)), text(env.main).slice(0, 400));
  const two = await openCoding({ state: { coding: codingDoc({ marks: codingDoc().marks.concat([Object.assign({}, codingDoc().marks[0], { mark_id: 'mark-0002' })]) }) } });
  assert.ok(/2 marks/.test(text(two.main)));
  // the list of transcripts: open and locked, one and several
  const list = load({ answer: server({}) });
  list.SS.coding.render(list.main, '');
  await settle();
  const t = text(list.main);
  assert.ok(t.indexOf('open \u00b7 1 mark') >= 0 && !/ 1 marks/.test(t), t);
  assert.ok(t.indexOf('locked \u00b7 4 marks') >= 0, t);
  // the strings directly
  list.SS.coding && assert.strictEqual(list.SS.tn('coding.locked_record', 1, { sha256: 'x', n: 1 }), 'sha256 x \u00b7 1 mark, recorded in the app log');
  assert.strictEqual(list.SS.tn('coding.state_locked', 0, { n: 0 }), 'locked \u00b7 0 marks');
  assert.strictEqual(list.SS.tn('no.such_key_one', 1), 'no.such_key_one');
});

test('issue 5 the Reference screen says "Coding open · 1 mark" and "Coding locked · 4 marks"', async () => {
  const env = load({ answer: server({}) });
  env.SS.reference.render(env.main, '');
  await settle();
  const t = text(env.main);
  assert.ok(t.indexOf('Coding open · 1 mark') >= 0 && !/1 marks/.test(t), t.slice(0, 300));
  assert.ok(t.indexOf('Coding locked · 4 marks') >= 0);
});
