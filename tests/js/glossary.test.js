'use strict';
// Item 32 (design feedback): the info marker beside technical terms (SS.gloss, static/js/glossary.js) and its
// definitions (en.json glossary.<key>). A small DOM stub with text nodes, insertBefore and events stands in for the
// browser: no DOM library. Objects built inside the vm belong to another realm, so compare strings and numbers.
const test = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const { ROOT, JS_DIR } = require('./harness');

const EN = JSON.parse(fs.readFileSync(path.join(ROOT, 'second_scribe', 'app', 'strings', 'en.json'), 'utf8'));
const GLOSS_KEYS = Object.keys(EN).filter((k) => k.startsWith('glossary.') && k !== 'glossary.marker_label');

// ------------------------------------------------------------------------------------------- a small DOM stub

function makeDom(view) {
  const docListeners = {};
  const document = { nodeType: 9, parentNode: null, childNodes: [], readyState: 'complete' };

  function link(parent, child, index) {
    if (child.parentNode) detach(child);
    child.parentNode = parent;
    parent.childNodes.splice(index, 0, child);
    return child;
  }
  function detach(child) {
    const p = child.parentNode;
    if (!p) return child;
    const i = p.childNodes.indexOf(child);
    if (i >= 0) p.childNodes.splice(i, 1);
    child.parentNode = null;
    return child;
  }
  function siblingOf(n, step) {
    if (!n.parentNode) return null;
    const kids = n.parentNode.childNodes;
    const i = kids.indexOf(n) + step;
    return i >= 0 ? (kids[i] || null) : null;
  }

  function text(value) {
    const t = { nodeType: 3, nodeValue: String(value), parentNode: null };
    Object.defineProperty(t, 'textContent', { get() { return t.nodeValue; } });
    Object.defineProperty(t, 'nextSibling', { get() { return siblingOf(t, 1); } });
    Object.defineProperty(t, 'previousSibling', { get() { return siblingOf(t, -1); } });
    return t;
  }

  function el(tag, attrs, kids) {
    const listeners = {};
    const n = {
      nodeType: 1, tagName: String(tag).toUpperCase(), parentNode: null, childNodes: [], attributes: {}, style: {},
      listeners, rect: null,
      getAttribute(k) { return Object.prototype.hasOwnProperty.call(n.attributes, k) ? n.attributes[k] : null; },
      setAttribute(k, v) { n.attributes[k] = String(v); },
      removeAttribute(k) { delete n.attributes[k]; },
      appendChild(c) { return link(n, c, n.childNodes.length); },
      insertBefore(c, ref) {
        if (c.parentNode === n) detach(c);
        const i = ref ? n.childNodes.indexOf(ref) : -1;
        return link(n, c, i < 0 ? n.childNodes.length : i);
      },
      removeChild(c) { return detach(c); },
      addEventListener(type, fn) { (listeners[type] = listeners[type] || []).push(fn); },
      fire(type, extra) {
        const e = Object.assign({ type, target: n, prevented: false, stopped: false,
          preventDefault() { e.prevented = true; }, stopPropagation() { e.stopped = true; } }, extra || {});
        (listeners[type] || []).slice().forEach((fn) => fn(e));
        return e;
      },
      getBoundingClientRect() {
        const r = n.rect || { left: 0, top: 0, width: 0, height: 0 };
        return { left: r.left, top: r.top, width: r.width, height: r.height, right: r.left + r.width, bottom: r.top + r.height };
      },
    };
    Object.defineProperty(n, 'textContent', {
      get() { return n.childNodes.map((c) => c.textContent).join(''); },
      set(v) { n.childNodes.slice().forEach(detach); if (v !== null && v !== undefined && v !== '') link(n, text(v), 0); },
    });
    Object.defineProperty(n, 'nextSibling', { get() { return siblingOf(n, 1); } });
    Object.defineProperty(n, 'previousSibling', { get() { return siblingOf(n, -1); } });
    Object.entries(attrs || {}).forEach(([k, v]) => n.setAttribute(k, v));
    (kids || []).forEach((c) => n.appendChild(typeof c === 'string' ? text(c) : c));
    return n;
  }

  const body = el('body');
  link(document, body, 0);
  Object.assign(document, {
    body,
    createElement: (tag) => el(tag),
    createTextNode: (v) => text(v),
    addEventListener(type, fn) { (docListeners[type] = docListeners[type] || []).push(fn); },
    fireDoc(type, extra) {
      const e = Object.assign({ type, prevented: false, stopped: false,
        preventDefault() { e.prevented = true; }, stopPropagation() { e.stopped = true; } }, extra || {});
      (docListeners[type] || []).slice().forEach((fn) => fn(e));
      return e;
    },
  });
  const timers = [];
  const sandbox = {
    console, document, SS_STRINGS: EN,
    innerWidth: (view || {}).width || 1440, innerHeight: (view || {}).height || 900,
    setTimeout: (fn) => { timers.push(fn); return timers.length; },
    clearTimeout: (id) => { if (id > 0 && id <= timers.length) timers[id - 1] = () => {}; },
    addEventListener() {},
  };
  sandbox.window = sandbox;
  const ctx = vm.createContext(sandbox);
  for (const f of ['core.js', 'glossary.js']) {
    vm.runInContext(fs.readFileSync(path.join(JS_DIR, f), 'utf8'), ctx, { filename: f });
  }
  return { SS: sandbox.SS, document, body, el, text, timers, win: sandbox };
}

function all(root, pred, out) {
  out = out || [];
  if (pred(root)) out.push(root);
  (root.childNodes || []).forEach((c) => all(c, pred, out));
  return out;
}
const markers = (root) => all(root, (n) => n.nodeType === 1 && (n.getAttribute('class') || '').split(/\s+/).includes('ss-gloss'));
const keysOf = (root) => markers(root).map((m) => m.getAttribute('data-gloss'));
const tipOf = (dom, m) => all(dom.document, (n) => n.nodeType === 1 && n.getAttribute('id') === m.getAttribute('aria-describedby'))[0];

// ------------------------------------------------------------------------------------------- the definitions

test('glossary: every definition exists, is one or two plain sentences, and the label has its slot', () => {
  assert.ok(GLOSS_KEYS.length >= 60, 'the glossary covers the item 32 terms and the other technical terms');
  for (const k of GLOSS_KEYS) {
    const v = EN[k];
    assert.strictEqual(typeof v, 'string', k);
    assert.ok(v.trim().length >= 40, k + ' is not empty');
    // sentences: split after . ! ? followed by a space, but not after "al." (Zhou et al.) or "e.g."
    const sentences = v.replace(/\bet al\./g, 'et al').replace(/\be\.g\./g, 'eg').split(/[.!?](?:\s+|$)/).filter((s) => s.trim());
    assert.ok(sentences.length >= 1 && sentences.length <= 2, k + ' has ' + sentences.length + ' sentences');
    assert.ok(/[.]$/.test(v), k + ' ends with a full stop');
  }
  assert.ok(EN['glossary.marker_label'].includes('{term}'));
});

test('glossary: definitions follow the register and every pinned forbidden-word list', () => {
  // the union of the pinned lists (study, box, setup, importer, wording); "no-interview script" is the profile's
  // term for the model's script set (DECISIONS L67, council 23 M7), allowed as in test_wording.
  const BAN = /—|rather than|second scribe|official script|ground truth|\b(error|errors|erred|mistake|mistakes|missed|wrong|fail|failed|correct|fix|first|replicable|validated|certificate)\b/i;
  for (const k of GLOSS_KEYS.concat(['glossary.marker_label'])) {
    const v = EN[k];
    assert.ok(!BAN.test(v), k + ': ' + v);
    assert.ok(!/\bscript\w*/i.test(v.replace(/no-interview scripts?/gi, '')), k + ' uses "script" outside "no-interview script"');
    assert.ok(!/, not\b/.test(v), k + ': no "X, not Y"');
    assert.ok(!/:\s/.test(v), k + ': no colon-led reveal');
    assert.ok(!/\bsecond scribe\b/i.test(v), k);
  }
});

test('glossary: the rungs say what the appendix says', () => {
  assert.match(EN['glossary.rung1'], /authors' original draft prompts/);
  assert.match(EN['glossary.rung2'], /main run's prompts/);
  assert.match(EN['glossary.remedy'], /Zhou et al\. \(2023\)/);
  assert.match(EN['glossary.placebo'], /length and format/);
  assert.match(EN['glossary.instruction_ladder'], /once/);
});

test('glossary: every term in TERMS points to a definition, and every definition is reachable', () => {
  const { SS } = makeDom();
  const keys = new Set();
  for (const t of SS.gloss.TERMS) {
    assert.ok(Object.prototype.hasOwnProperty.call(EN, 'glossary.' + t[0]), 'glossary.' + t[0] + ' exists');
    keys.add(t[0]);
  }
  for (const k of GLOSS_KEYS) assert.ok(keys.has(k.slice('glossary.'.length)), k + ' has a pattern');
});

test('glossary: matches finds the longer phrase before the shorter one inside it', () => {
  const { SS } = makeDom();
  const m = (s) => JSON.parse(JSON.stringify(SS.gloss.matches(s).map((x) => x.key + '@' + x.start)));
  assert.deepStrictEqual(m('The attested recall of the AI map'), ['attested_recall@4', 'ai_map@27']);
  assert.deepStrictEqual(m('the no-interview map and rung 2'), ['no_interview_map@4', 'rung2@25']);
  assert.deepStrictEqual(m('nothing technical here'), []);
});

// ------------------------------------------------------------------------------------------- placement

test('scan: one marker per term per page section, right after the term, with the definition as its tip', () => {
  const dom = makeDom();
  const { el, SS } = dom;
  const main = el('main', { id: 'main' }, [
    el('section', {}, [el('h2', {}, ['Reading']), el('p', {}, ['The judge reads each AI map and then the judge reads it again, ' +
      'so the judge appears three times in one long sentence about the judge.'])]),
    el('section', {}, [el('h2', {}, ['Checking']), el('p', {}, ['A second section names the judge once more.'])]),
  ]);
  dom.body.appendChild(main);
  const added = SS.gloss.scan(main);
  assert.deepStrictEqual(keysOf(main), ['judge', 'ai_map', 'judge']);
  assert.strictEqual(added.length, 3);
  for (const b of markers(main)) {
    assert.strictEqual(b.tagName, 'BUTTON');
    assert.strictEqual(b.getAttribute('type'), 'button');
    const tip = tipOf(dom, b);
    assert.ok(tip, 'aria-describedby points to the tip');
    assert.strictEqual(tip.getAttribute('role'), 'tooltip');
    assert.strictEqual(tip.getAttribute('hidden'), '');
    assert.strictEqual(tip.textContent, EN['glossary.' + b.getAttribute('data-gloss')]);
    assert.ok(b.parentNode.getAttribute('class') === 'ss-gloss-term', 'the term and its marker do not wrap apart');
    assert.ok(/^Meaning of /.test(b.getAttribute('aria-label')));
  }
  // the text reads the same as before, apart from the tips' own text
  const p = main.childNodes[0].childNodes[1];
  const words = all(p, (n) => n.nodeType === 3).filter((t) => {
    for (let x = t.parentNode; x; x = x.parentNode) if (/ss-gloss-tip/.test((x.getAttribute && x.getAttribute('class')) || '')) return false;
    return true;
  }).map((t) => t.nodeValue).join('');
  assert.ok(words.startsWith('The judge reads each AI map and then the judge'), words);
  // a second scan adds nothing
  assert.strictEqual(SS.gloss.scan(main).length, 0);
});

test('scan: a short text gets one marker; links, navigation, buttons, code, h1, hidden and live parts get none', () => {
  const dom = makeDom();
  const { el, SS } = dom;
  const main = el('main', {}, [
    el('table', {}, [el('th', {}, ['Judge and reconstructor'])]),
    el('section', {}, [
      el('h1', {}, ['The judge']),
      el('a', { href: '#x' }, ['the judge']),
      el('nav', {}, [el('span', {}, ['the judge'])]),
      el('button', {}, ['the judge']),
      el('code', {}, ['judge']),
      el('p', { hidden: '' }, ['the judge']),
      el('p', { 'aria-live': 'polite' }, ['the judge']),
      el('p', { role: 'status' }, ['the judge']),
      el('p', { 'data-gloss-skip': '' }, ['the judge']),
      el('div', { class: 'ss-x-box' }, ['the judge']),
    ]),
  ]);
  dom.body.appendChild(main);
  SS.gloss.scan(main);
  assert.deepStrictEqual(keysOf(main), ['judge'], 'only the table head, and only its first term');
});

test('scan: a term inside a label gets a span marker at the end of the label, and the control keeps its name', () => {
  const dom = makeDom();
  const { el, SS } = dom;
  const input = el('input', { type: 'radio', name: 'rung' });
  const label = el('label', {}, [input, el('span', {}, ['Rung 1'])]);
  const form = el('form', {}, [label]);
  const main = el('main', {}, [form]);
  dom.body.appendChild(main);
  SS.gloss.scan(main);
  const [m] = markers(main);
  assert.ok(m, 'one marker');
  assert.strictEqual(m.tagName, 'SPAN', 'a button inside a label is not valid HTML');
  assert.strictEqual(m.getAttribute('role'), 'button');
  assert.strictEqual(m.getAttribute('tabindex'), '0');
  assert.strictEqual(m.parentNode, label, 'the marker ends the label');
  assert.strictEqual(label.nextSibling.getAttribute('role'), 'tooltip', 'the tip follows the label');
  assert.strictEqual(input.getAttribute('aria-label'), 'Rung 1', 'the radio is named by the label text only');
  assert.strictEqual(m.getAttribute('aria-label'), 'Meaning of rung 1');
  // a click on the marker does not reach the label's control
  const e = m.fire('click');
  assert.ok(e.prevented && e.stopped);
});

test('scan: a section part that is redrawn gets its marker again', () => {
  const dom = makeDom();
  const { el, SS } = dom;
  const sec = el('section', {}, [el('p', {}, ['The placebo matches the remedy in length and format, so only the content of the instruction differs.'])]);
  const main = el('main', {}, [sec]);
  dom.body.appendChild(main);
  SS.gloss.scan(main);
  assert.deepStrictEqual(keysOf(main), ['placebo', 'remedy']);
  sec.textContent = '';
  sec.appendChild(el('p', {}, ['The placebo again.']));
  SS.gloss.scan(main);
  assert.deepStrictEqual(keysOf(main), ['placebo']);
});

// ------------------------------------------------------------------------------------------- sections, paragraphs, headings

// Where each marker sits, as "key:parent tag", in document order.
const whereOf = (root) => markers(root).map((m) => {
  let x = m.parentNode;
  while (x && x.getAttribute('class') === 'ss-gloss-term') x = x.parentNode;
  return m.getAttribute('data-gloss') + ':' + x.tagName.toLowerCase();
});

test('sections: lists, tables, details and fieldsets do not start a new block; one marker per term in an h2 section', () => {
  const dom = makeDom();
  const { el, SS } = dom;
  const main = el('main', {}, [
    el('h2', {}, ['Overview']),
    el('ul', {}, [el('li', {}, ['The judge reads the transcript.'])]),
    el('ul', {}, [el('li', {}, ['The judge reads the transcript again.'])]),
    el('table', {}, [el('tr', {}, [el('td', {}, ['The judge returns a score for each row.'])])]),
    el('details', {}, [el('summary', {}, ['More']), el('p', {}, ['The judge is named here too.'])]),
    el('fieldset', {}, [el('p', {}, ['And the judge once more inside a fieldset.'])]),
    el('section', {}, [el('p', {}, ['A plain section without a heading names the judge again.'])]),
  ]);
  dom.body.appendChild(main);
  SS.gloss.scan(main);
  assert.deepStrictEqual(whereOf(main), ['judge:li']);
});

test('sections: the same term under two different h2 or h3 headings gets two markers; the leading section counts', () => {
  const dom = makeDom();
  const { el, SS } = dom;
  const main = el('main', {}, [
    el('p', {}, ['Before any heading the judge is named.']),
    el('h2', {}, ['First part']),
    el('p', {}, ['In this part the judge is named.']),
    el('p', {}, ['And the judge again in the same part.']),
    el('h3', {}, ['A smaller part']),
    el('p', {}, ['Under an h3 the judge is named.']),
    el('h4', {}, ['Not a new section']),
    el('p', {}, ['An h4 does not start a section, so the judge is not marked again.']),
    el('h2', {}, ['Second part']),
    el('div', {}, [el('div', {}, [el('p', {}, ['Deep inside wrappers the judge is named.'])])]),
  ]);
  dom.body.appendChild(main);
  SS.gloss.scan(main);
  assert.deepStrictEqual(whereOf(main), ['judge:p', 'judge:p', 'judge:p', 'judge:p']);
  // the markers sit in paragraphs 1, 3 (first part), 6 (h3) and the last (second part): nothing else
  const ps = all(main, (n) => n.nodeType === 1 && n.tagName === 'P');
  assert.deepStrictEqual(ps.map((q) => markers(q).length), [1, 1, 0, 1, 0, 1]);
});

test('sections: a heading that is never shown (hidden) starts no section', () => {
  const dom = makeDom();
  const { el, SS } = dom;
  const main = el('main', {}, [
    el('h2', {}, ['Visible']),
    el('p', {}, ['The judge is named here.']),
    el('h2', { hidden: '' }, ['Hidden heading']),
    el('p', {}, ['The judge is named here again.']),
  ]);
  dom.body.appendChild(main);
  SS.gloss.scan(main);
  assert.deepStrictEqual(whereOf(main), ['judge:p']);
});

test('sections: a new h1 (an app screen) starts a new section, although the h1 itself is never marked', () => {
  const dom = makeDom();
  const { el, SS } = dom;
  const app = el('div', { id: 'ss-app' }, [
    el('div', {}, [el('h1', {}, ['The judge screen']), el('p', {}, ['The judge is set up here.'])]),
    el('div', {}, [el('h1', {}, ['Second screen']), el('p', {}, ['The judge is named again here.'])]),
    el('div', {}, [el('h1', {}, ['Third screen']), el('h2', {}, ['Step']), el('p', {}, ['The judge, once more.'])]),
  ]);
  dom.body.appendChild(app);
  SS.gloss.scan(app);
  assert.deepStrictEqual(whereOf(app), ['judge:p', 'judge:p', 'judge:p']);
  assert.strictEqual(markers(app.childNodes[0].childNodes[0]).length, 0, 'the h1 has no marker');
});

test('paragraphs: at most two markers in a paragraph or list item; the terms left over are marked in the next one', () => {
  const dom = makeDom();
  const { el, SS } = dom;
  const four = 'The judge, the reconstructor, the temperature and the recall are all named in this one long paragraph.';
  const main = el('main', {}, [
    el('h2', {}, ['Terms']),
    el('p', {}, [four]),
    el('ul', {}, [el('li', {}, ['Item: the precision, the slant, the ledger and the oracle all sit together in one single list item of this page.'])]),
    el('p', {}, ['The temperature and the recall return in a second long paragraph of ordinary text.']),
    el('p', {}, ['The ', el('b', {}, ['pooling']), ' of runs and the ', el('i', {}, ['floor']), ' and the gate are split across inline tags in this paragraph.']),
  ]);
  dom.body.appendChild(main);
  SS.gloss.scan(main);
  const ps = all(main, (n) => n.nodeType === 1 && (n.tagName === 'P' || n.tagName === 'LI'));
  assert.deepStrictEqual(ps.map((q) => keysOf(q)), [
    ['judge', 'reconstructor'],
    ['precision', 'slant'],
    ['temperature', 'recall'],
    ['pooling', 'floor'],
  ]);
});

test('paragraphs: the cap counts markers already in the paragraph, and a short text still gets one marker', () => {
  const dom = makeDom();
  const { el, SS } = dom;
  const p = el('p', {}, ['The judge and the reconstructor are named first in this paragraph, which is long enough to be normal.']);
  const th = el('th', {}, ['Judge, reconstructor and temperature']);
  const main = el('main', {}, [el('h2', {}, ['Terms']), p, el('table', {}, [el('tr', {}, [th])])]);
  dom.body.appendChild(main);
  SS.gloss.scan(main);
  assert.deepStrictEqual(keysOf(p), ['judge', 'reconstructor']);
  // a later piece of text in the same paragraph (a redraw adds a span) finds the paragraph full
  p.appendChild(el('span', {}, ['The temperature and the recall are added later.']));
  assert.strictEqual(SS.gloss.scan(p, main).length, 0);
  assert.deepStrictEqual(keysOf(p), ['judge', 'reconstructor']);
  // the table head is a short text: one marker, and judge and reconstructor are already marked in the section
  assert.deepStrictEqual(keysOf(th), ['temperature']);
});

test('headings: a term in an h2 is left to the first paragraph when that paragraph names it; kept when it does not', () => {
  const dom = makeDom();
  const { el, SS } = dom;
  const a = el('main', {}, [el('h2', {}, ['Recall']), el('p', {}, ['This section explains recall in plain words and how it is read.'])]);
  dom.body.appendChild(a);
  SS.gloss.scan(a);
  assert.deepStrictEqual(whereOf(a), ['recall:p'], 'the paragraph gets the marker, the heading none');
  assert.strictEqual(markers(a.childNodes[0]).length, 0);

  const b = el('main', {}, [el('h2', {}, ['Recall']), el('p', {}, ['This paragraph says nothing technical at all, only ordinary words.'])]);
  dom.body.appendChild(b);
  SS.gloss.scan(b);
  assert.deepStrictEqual(whereOf(b), ['recall:h2'], 'the heading keeps its marker when the first paragraph lacks the term');

  // only the first paragraph counts, and only up to the next h1, h2 or h3
  const c = el('main', {}, [
    el('h2', {}, ['Recall']), el('p', {}, ['Nothing technical here at all, only ordinary words.']),
    el('p', {}, ['The second paragraph names recall.']),
    el('h2', {}, ['Next']), el('p', {}, ['A paragraph under the next heading names recall too.']),
  ]);
  dom.body.appendChild(c);
  SS.gloss.scan(c);
  assert.deepStrictEqual(whereOf(c), ['recall:h2', 'recall:p']);

  // an h3 follows the same rule
  const d = el('main', {}, [el('h3', {}, ['The judge']), el('p', {}, ['The reconstructor is named here, in a paragraph that is long enough to be normal.'])]);
  dom.body.appendChild(d);
  SS.gloss.scan(d);
  assert.deepStrictEqual(whereOf(d), ['judge:h3', 'reconstructor:p']);

  // the tip text of a marker in the paragraph is not the paragraph's words: the tip of "attested" mentions the judge
  const e = el('main', {}, [el('h3', {}, ['Judge']), el('p', {}, ['This paragraph is about attested quotes and is long enough to be a normal one.'])]);
  dom.body.appendChild(e);
  SS.gloss.scan(e);
  assert.deepStrictEqual(whereOf(e), ['judge:h3', 'attested:p']);
  assert.ok(/judge/i.test(EN['glossary.attested']), 'the premise: that tip names the judge');
  e.removeChild(e.childNodes[0]);
  e.insertBefore(el('h3', {}, ['Judge']), e.childNodes[0]);   // the heading is drawn again, the paragraph keeps its marker
  assert.strictEqual(SS.gloss.scan(e).length, 1);
  assert.deepStrictEqual(whereOf(e), ['judge:h3', 'attested:p']);
});

test('headings: an h1 is still never marked, and a heading term the first paragraph names is not marked on a rescan', () => {
  const dom = makeDom();
  const { el, SS } = dom;
  const main = el('main', {}, [el('h1', {}, ['Recall']), el('h2', {}, ['Precision']), el('p', {}, ['Precision and recall are read together, and this sentence is long enough to be a normal paragraph.'])]);
  dom.body.appendChild(main);
  SS.gloss.scan(main);
  assert.deepStrictEqual(whereOf(main), ['precision:p', 'recall:p']);
  assert.strictEqual(SS.gloss.scan(main).length, 0);
  assert.deepStrictEqual(whereOf(main), ['precision:p', 'recall:p']);
});

test('incremental scan: a redraw does not add a second marker for a term already marked earlier in its section', () => {
  const dom = makeDom();
  const { el, SS } = dom;
  const first = el('p', {}, ['The judge is named early in this section.']);
  const later = el('div', {}, [el('p', {}, ['A paragraph that will be drawn again.'])]);
  const main = el('main', {}, [el('h2', {}, ['Part']), first, later]);
  dom.body.appendChild(main);
  SS.gloss.scan(main);
  assert.deepStrictEqual(whereOf(main), ['judge:p']);

  // the redraw replaces the later subtree; the heading lies outside it, the scan root is the limit (as watch's flush does)
  later.textContent = '';
  later.appendChild(el('p', {}, ['The judge is named again in the redrawn part.']));
  assert.strictEqual(SS.gloss.scan(later, main).length, 0, 'no second marker in the same section');
  assert.deepStrictEqual(whereOf(main), ['judge:p']);

  // the earlier marker goes away (its paragraph is redrawn without the term): the redrawn part now gets the marker
  first.textContent = 'This paragraph no longer names the term.';
  assert.strictEqual(SS.gloss.scan(later, main).length, 1);
  assert.strictEqual(whereOf(main).length, 1);
  assert.ok(markers(later).length === 1, 'the marker is now in the redrawn part');
  // and a further scan adds nothing
  assert.strictEqual(SS.gloss.scan(later, main).length, 0);
});

test('incremental scan: the heading may sit in another branch; a redraw under another heading is its own section', () => {
  const dom = makeDom();
  const { el, SS } = dom;
  const a = el('div', {}, [el('h2', {}, ['Alpha']), el('p', {}, ['The judge is named in alpha.'])]);
  const pb = el('p', {}, ['The judge is named in beta.']);
  const b = el('div', {}, [el('h2', {}, ['Beta']), el('div', {}, [pb])]);
  const c = el('div', {}, [el('div', {}, [el('p', {}, ['Gamma content without a heading of its own.'])])]);
  const main = el('main', {}, [a, b, c]);
  dom.body.appendChild(main);
  SS.gloss.scan(main);
  assert.deepStrictEqual(whereOf(main), ['judge:p', 'judge:p']);
  // redraw the beta paragraph: its own section (beta) has no marker once the old one is gone
  const inner = pb.parentNode;
  inner.textContent = '';
  inner.appendChild(el('p', {}, ['The judge, drawn again in beta.']));
  assert.strictEqual(SS.gloss.scan(inner, main).length, 1, 'alpha has a marker, but beta lost its own');
  assert.deepStrictEqual(whereOf(main), ['judge:p', 'judge:p']);
  // a redraw in the last branch belongs to beta's section: already marked there
  const g = c.childNodes[0];
  g.textContent = '';
  g.appendChild(el('p', {}, ['The judge, in a branch that has no heading.']));
  assert.strictEqual(SS.gloss.scan(g, main).length, 0);
});

test('incremental scan: a redrawn heading finds the markers its section already holds', () => {
  const dom = makeDom();
  const { el, SS } = dom;
  const h = el('h2', {}, ['Part']);
  const main = el('main', {}, [h, el('p', {}, ['The judge is named here, under the first heading.'])]);
  dom.body.appendChild(main);
  SS.gloss.scan(main);
  assert.strictEqual(markers(main).length, 1);
  const h2 = el('h2', {}, ['Part again']);
  main.insertBefore(h2, h);
  main.removeChild(h);
  assert.strictEqual(SS.gloss.scan(main).length, 0, 'the section range is searched for an existing marker');
  assert.strictEqual(markers(main).length, 1);
});

// ------------------------------------------------------------------------------------------- behaviour

function oneMarker(view) {
  const dom = makeDom(view);
  const { el, SS } = dom;
  const p = el('p', {}, ['The judge compares the maps, and this sentence is long enough to be a normal paragraph.']);
  const other = el('button', {}, ['Other control']);
  const main = el('main', {}, [el('section', {}, [p, other])]);
  dom.body.appendChild(main);
  SS.gloss.scan(main);
  const m = markers(main)[0];
  const tip = tipOf(dom, m);
  m.rect = { left: 600, top: 300, width: 24, height: 24 };
  tip.rect = { left: 0, top: 0, width: 320, height: 120 };
  return { dom, SS, m, tip, other };
}
const isShown = (tip) => tip.getAttribute('hidden') === null;
function runTimers(dom) { for (let i = 0; i < dom.timers.length; i++) { const fn = dom.timers[i]; dom.timers[i] = () => {}; fn(); } }

test('keyboard: focus opens the tip, Escape closes it, blur closes it', () => {
  const { dom, SS, m, tip } = oneMarker();
  m.fire('focus');
  assert.ok(isShown(tip));
  assert.strictEqual(SS.gloss.openMarker(), m);
  assert.strictEqual(tip.parentNode, dom.body, 'while open the tip sits under body, fixed to the window');
  assert.strictEqual(m.getAttribute('data-open'), '');
  const esc = dom.document.fireDoc('keydown', { key: 'Escape' });
  assert.ok(!isShown(tip));
  assert.ok(esc.prevented);
  assert.strictEqual(m.getAttribute('data-open'), null);
  assert.strictEqual(tip.parentNode, m.parentNode, 'closed, the tip goes back beside its marker');
  m.fire('focus');
  assert.ok(isShown(tip));
  m.fire('blur');
  assert.ok(!isShown(tip));
});

test('keyboard: Enter and Space toggle the span marker inside a label', () => {
  const dom = makeDom();
  const { el, SS } = dom;
  const label = el('label', {}, [el('input', { type: 'radio' }), 'Placebo']);
  const main = el('main', {}, [el('form', {}, [label])]);
  dom.body.appendChild(main);
  SS.gloss.scan(main);
  const m = markers(main)[0];
  const tip = tipOf(dom, m);
  m.rect = { left: 100, top: 100, width: 24, height: 24 };
  label.rect = { left: 40, top: 90, width: 200, height: 44 };
  tip.rect = { left: 0, top: 0, width: 300, height: 80 };
  const e = m.fire('keydown', { key: 'Enter' });
  assert.ok(e.prevented, 'Enter does not also pick the option');
  assert.ok(isShown(tip));
  assert.ok(Number.parseInt(tip.style.top, 10) >= 134, 'the tip sits below the whole label, never over the option');
  m.fire('keydown', { key: ' ' });
  assert.ok(!isShown(tip));
  m.fire('keydown', { key: 'a' });
  assert.ok(!isShown(tip), 'other keys do nothing');
});

test('tap and click: toggle the tip; a tap elsewhere closes it; touch never counts as hover', () => {
  const { dom, m, tip, other } = oneMarker();
  m.fire('pointerenter', { pointerType: 'touch' });
  assert.ok(!isShown(tip), 'a touch does not open on hover');
  m.fire('click');
  assert.ok(isShown(tip), 'tap opens');
  m.fire('pointerleave', { pointerType: 'touch' });
  assert.ok(isShown(tip), 'lifting the finger keeps it open');
  dom.document.fireDoc('pointerdown', { target: tip });
  assert.ok(isShown(tip), 'a tap on the tip keeps it open');
  dom.document.fireDoc('pointerdown', { target: other });
  assert.ok(!isShown(tip), 'a tap elsewhere closes');
  m.fire('click');
  assert.ok(isShown(tip));
  m.fire('click');
  assert.ok(!isShown(tip), 'a second tap closes');
});

test('mouse: hover opens, leaving closes after a short grace, and the tip itself keeps it open', () => {
  const { dom, m, tip } = oneMarker();
  m.fire('pointerenter', { pointerType: 'mouse' });
  assert.ok(isShown(tip));
  m.fire('pointerleave', { pointerType: 'mouse' });
  tip.fire('pointerenter', { pointerType: 'mouse' });
  runTimers(dom);
  assert.ok(isShown(tip), 'moving onto the tip keeps it open');
  tip.fire('pointerleave', { pointerType: 'mouse' });
  runTimers(dom);
  assert.ok(!isShown(tip));
  // a click pins it: leaving does not close a pinned tip
  m.fire('click');
  m.fire('pointerleave', { pointerType: 'mouse' });
  runTimers(dom);
  assert.ok(isShown(tip), 'a clicked tip stays until closed');
});

test('two markers: opening one closes the other', () => {
  const dom = makeDom();
  const { el, SS } = dom;
  const main = el('main', {}, [el('section', {}, [el('p', {}, ['The judge and the reconstructor are two models of different families in this study.'])])]);
  dom.body.appendChild(main);
  SS.gloss.scan(main);
  const [a, b] = markers(main);
  for (const x of [a, b]) { x.rect = { left: 100, top: 100, width: 24, height: 24 }; tipOf(dom, x).rect = { left: 0, top: 0, width: 200, height: 60 }; }
  a.fire('click');
  assert.ok(isShown(tipOf(dom, a)));
  b.fire('click');
  assert.ok(!isShown(tipOf(dom, a)));
  assert.ok(isShown(tipOf(dom, b)));
  assert.strictEqual(SS.gloss.openMarker(), b);
});

// ------------------------------------------------------------------------------------------- position

function covers(p, size, a) {
  return p.left < a.left + a.width && a.left < p.left + size.width && p.top < a.top + a.height && a.top < p.top + size.height;
}

test('position: inside the window with a 16px gutter and never over the marker, at 390 and 1440', () => {
  const { SS } = makeDom();
  const size390 = { width: 358, height: 140 };
  const size1440 = { width: 320, height: 140 };
  for (const [view, size] of [[{ width: 390, height: 844 }, size390], [{ width: 1440, height: 900 }, size1440]]) {
    for (const left of [0, 8, 180, view.width - 30, view.width - 24]) {
      for (const top of [0, 60, 400, view.height - 140, view.height - 24]) {
        const a = { left, top, width: 24, height: 24, right: left + 24, bottom: top + 24 };
        const p = SS.gloss.position(a, size, view);
        assert.ok(p.left >= 16 && p.left + size.width <= view.width - 16, JSON.stringify([view, a, p]));
        assert.ok(!covers(p, size, a), 'over the marker: ' + JSON.stringify([view, a, p]));
        assert.ok(p.side === 'below' ? p.top >= a.bottom : p.top + size.height <= a.top);
      }
    }
  }
});

test('position: below when there is room, above when there is not', () => {
  const { SS } = makeDom();
  const view = { width: 390, height: 844 };
  const size = { width: 300, height: 120 };
  assert.strictEqual(SS.gloss.position({ left: 100, top: 100, width: 24, height: 24, right: 124, bottom: 124 }, size, view).side, 'below');
  assert.strictEqual(SS.gloss.position({ left: 100, top: 780, width: 24, height: 24, right: 124, bottom: 804 }, size, view).side, 'above');
});

test('layout: the open tip gets left, top and side from position()', () => {
  const { m, tip } = oneMarker({ width: 390, height: 844 });
  m.rect = { left: 350, top: 300, width: 24, height: 24 };
  tip.rect = { left: 0, top: 0, width: 358, height: 100 };
  m.fire('click');
  assert.strictEqual(tip.style.left, '16px');
  assert.strictEqual(tip.style.top, '332px');
  assert.strictEqual(tip.getAttribute('data-side'), 'below');
});

// ------------------------------------------------------------------------------------------- Part 6b: spacing
// The marker sits against the end of its term with no space in the text (the gap is CSS margin); the punctuation that
// follows stays right after it; a parenthesis that closes around the term goes before the marker; and the term's last
// word, the marker and that punctuation share one no-wrap span, so the marker never starts a line.

// The paragraph as a string: text as is, a marker as "[i]", a tip as nothing, the no-wrap span as "{...}".
function shape(n) {
  if (n.nodeType === 3) return n.nodeValue;
  const cls = (n.getAttribute('class') || '').split(/\s+/);
  if (cls.includes('ss-gloss-tip')) return '';
  if (cls.includes('ss-gloss')) return '[i]';
  const inner = n.childNodes.map(shape).join('');
  return cls.includes('ss-gloss-term') ? '{' + inner + '}' : inner;
}
function marked(sentence) {
  const dom = makeDom();
  const p = dom.el('p', {}, [sentence]);
  const main = dom.el('main', {}, [p]);
  dom.body.appendChild(main);
  dom.SS.gloss.scan(main);
  return { dom, p, main, shape: shape(p) };
}

test('spacing: a comma after the term sits right after the marker, with no space in between', () => {
  const r = marked('Of the 6 services that participants marked as wished for, the AI drew none as received.');
  assert.strictEqual(r.shape, 'Of the 6 services that participants marked as {wished for[i],} the AI drew none as received.');
  assert.strictEqual(keysOf(r.p)[0], 'wished_for');
});

test('spacing: a full stop, a semicolon and a colon after the term stay against the marker too', () => {
  assert.strictEqual(marked('Read the judge; it labels quotes.').shape, 'Read the {judge[i];} it labels quotes.');
  assert.strictEqual(marked('Read the judge: it labels quotes.').shape, 'Read the {judge[i]:} it labels quotes.');
  assert.strictEqual(marked('It never came up as the ideal.').shape, 'It {never came[i]} up as the ideal.', 'only the first term of a short text');
  assert.strictEqual(marked('This run shows no flattened ideal.').shape, 'This run shows no flattened {ideal[i].}');
  assert.strictEqual(marked('Who reads the maps? The judge.').shape, 'Who reads the maps? The {judge[i].}');
});

test('spacing: a closing parenthesis around the term goes before the marker, then the marker, then what follows', () => {
  assert.strictEqual(marked('The study\'s instructions (rung 2)').shape, 'The study\'s instructions ({rung 2)[i]}');
  assert.strictEqual(marked('The study\'s instructions (rung 2), run once.').shape, 'The study\'s instructions ({rung 2)[i],} run once.');
  assert.strictEqual(marked('We ran the prompts (rung 2).').shape, 'We ran the prompts ({rung 2)[i].}');
  // the visible text keeps its parenthesis, comma and order: the marker adds no character to it
  const r = marked('The study\'s instructions (rung 2), run once.');
  const plain = all(r.p, (n) => n.nodeType === 3).filter((t) => !/ss-gloss-tip/.test(t.parentNode.getAttribute('class') || '')).map((t) => t.nodeValue).join('');
  assert.strictEqual(plain, 'The study\'s instructions (rung 2), run once.');
});

test('spacing: a closing parenthesis with no opening one in the same text is plain punctuation after the marker', () => {
  assert.strictEqual(marked('Rung 2) is the main run.').shape, '{Rung 2[i])} is the main run.');
});

test('spacing: no text space is added before the marker; a space already in the source is left alone', () => {
  assert.strictEqual(marked('Ask the judge , please.').shape, 'Ask the {judge[i]} , please.');
  const t = marked('The judge reads quotes.');
  assert.strictEqual(t.shape, 'The {judge[i]} reads quotes.');
  const m = markers(t.p)[0];
  assert.strictEqual(m.previousSibling.nodeValue, 'judge', 'the marker follows the term text directly');
});

test('spacing: the no-wrap span holds the last word, the marker and the punctuation (a long term keeps only its last word)', () => {
  const r = marked('It was drawn from a strict matching rule, as set.');
  assert.strictEqual(r.shape, 'It was drawn from a strict matching {rule[i],} as set.');
  const wrap = all(r.p, (n) => n.nodeType === 1 && n.getAttribute('class') === 'ss-gloss-term')[0];
  assert.ok(wrap, 'a no-wrap span');
  assert.strictEqual(wrap.childNodes[0].nodeValue, 'rule', 'only the last word of a long term travels with the marker');
  assert.strictEqual(wrap.childNodes[1].getAttribute('class'), 'ss-gloss');
  assert.strictEqual(wrap.childNodes[wrap.childNodes.length - 1].nodeValue, ',', 'the comma is inside the span, after the tip');
  // the span is the unit the CSS keeps on one line
  const css = fs.readFileSync(path.join(ROOT, 'second_scribe', 'app', 'static', 'css', 'glossary.css'), 'utf8');
  assert.ok(/\.ss-gloss-term\s*\{[^}]*white-space:\s*nowrap/.test(css), '.ss-gloss-term does not wrap');
});

test('spacing: the CSS gap is a margin on the marker, not a space (left gap and right gap are small and set)', () => {
  const css = fs.readFileSync(path.join(ROOT, 'second_scribe', 'app', 'static', 'css', 'glossary.css'), 'utf8');
  const m = /\n\.ss-gloss\s*\{[^}]*?margin:\s*(-?[\d.]+)px\s+(-?[\d.]+)px\s+(-?[\d.]+)px\s+(-?[\d.]+)px/.exec(css) ||
    /\.ss-gloss\s*\{[\s\S]*?margin:\s*(-?[\d.]+)px\s+(-?[\d.]+)px\s+(-?[\d.]+)px\s+(-?[\d.]+)px/.exec(css);
  assert.ok(m, 'four-value margin on .ss-gloss');
  const left = 4.5 + Number(m[4]);    // air inside the 24px box (4.5px a side) plus the margin
  const right = 4.5 + Number(m[2]);
  assert.ok(left >= 2 && left <= 4, 'a thin gap after the term: ' + left);
  assert.ok(right >= 1 && right <= 3, 'a thin gap before the punctuation: ' + right);
});

test('spacing: a second marker in a long text finds its term after punctuation was moved into the first span', () => {
  const r = marked('The judge, which reads every quote in the transcript, then labels each box that the AI map, as drawn, holds.');
  assert.strictEqual(r.shape, 'The {judge[i],} which reads every quote in the transcript, then labels each box that the {AI map[i],} as drawn, holds.');
});

test('glossary: "member check" gets a marker (council 30, seat 02 F5)', () => {
  const { SS } = makeDom();
  const hit = SS.gloss.TERMS.filter((t) => t[0] === 'member_check');
  assert.strictEqual(hit.length, 1);
  assert.ok(hit[0][1].test('A later member check can take the AI boxes.'));
  // on The study, "What a team does with it": the paragraph holds two markers, and member check is one of them
  const dom = makeDom();
  const main = dom.el('main', {}, [dom.el('h2', {}, ['What a team does with it']), dom.el('p', {}, [EN['study.todo.body']])]);
  dom.body.appendChild(main);
  dom.SS.gloss.scan(main);
  assert.ok(keysOf(main).indexOf('member_check') >= 0, keysOf(main).join(','));
  assert.ok(EN['glossary.member_check'].startsWith('A member check asks participants'));
});
