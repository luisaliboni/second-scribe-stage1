'use strict';
// Review R13: a panel that scrolls sideways says so. SS.scrollCue(scroll, host, hint) (a11y.js) sets
// host data-scroll = "" | "right" | "left" | "both" and shows the hint line only while the content is wider than the panel.
// Hidden scrollbars are the reason: the cue is drawn by the page (an edge fade and a line of text), not by the browser.
const test = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const { load, JS_DIR, ROOT } = require('./harness');

const FILES = ['core.js', 'layout.js', 'encoding.js', 'a11y.js'];
const EN = JSON.parse(fs.readFileSync(path.join(ROOT, 'second_scribe', 'app', 'strings', 'en.json'), 'utf8'));

function fake(clientWidth, scrollWidth, scrollLeft) {
  const listeners = {};
  const attrs = {};
  return {
    clientWidth, scrollWidth, scrollLeft: scrollLeft || 0, listeners, attrs, hidden: true, isConnected: true,
    addEventListener(t, fn) { (listeners[t] = listeners[t] || []).push(fn); },
    removeEventListener(t, fn) { const l = listeners[t] || []; const i = l.indexOf(fn); if (i >= 0) l.splice(i, 1); },
    setAttribute(k, v) { attrs[k] = String(v); },
    getAttribute(k) { return Object.prototype.hasOwnProperty.call(attrs, k) ? attrs[k] : null; },
    removeAttribute(k) { delete attrs[k]; },
  };
}

test('R13: the hint shows only when the content is wider than the panel', () => {
  const { SS, errors } = load({ files: FILES });
  const wide = { scroll: fake(1000, 1700), host: fake(0, 0), hint: fake(0, 0) };
  const cue = SS.scrollCue(wide.scroll, wide.host, wide.hint);
  assert.strictEqual(wide.hint.hidden, false, 'overflowing: hint shown');
  assert.strictEqual(wide.host.getAttribute('data-scroll'), 'right');
  assert.strictEqual(cue.update(), 'right');
  const fits = { scroll: fake(1000, 1000), host: fake(0, 0), hint: fake(0, 0) };
  SS.scrollCue(fits.scroll, fits.host, fits.hint);
  assert.strictEqual(fits.hint.hidden, true, 'fits: hint stays hidden');
  assert.strictEqual(fits.host.getAttribute('data-scroll'), '');
  const off = { scroll: fake(1000, 1001), host: fake(0, 0), hint: fake(0, 0) };
  SS.scrollCue(off.scroll, off.host, off.hint);
  assert.strictEqual(off.hint.hidden, true, 'one pixel of rounding is not overflow');
  assert.deepStrictEqual(errors, []);
});

test('R13: the fade follows the scroll position and the hint stays while there is more to see', () => {
  const { SS } = load({ files: FILES });
  const scroll = fake(1000, 1700);
  const host = fake(0, 0);
  const hint = fake(0, 0);
  SS.scrollCue(scroll, host, hint);
  assert.strictEqual(host.getAttribute('data-scroll'), 'right');
  scroll.scrollLeft = 300;
  scroll.listeners.scroll.forEach((fn) => fn());
  assert.strictEqual(host.getAttribute('data-scroll'), 'both');
  scroll.scrollLeft = 700;
  scroll.listeners.scroll.forEach((fn) => fn());
  assert.strictEqual(host.getAttribute('data-scroll'), 'left', 'at the right end only the left edge fades');
  assert.strictEqual(hint.hidden, false, 'the content still overflows');
});

test('R13: a resize that makes the content fit hides the hint and the fade again', () => {
  const { SS, window } = load({ files: FILES });
  let observed = null;
  window.ResizeObserver = function (fn) { observed = fn; this.observe = function () {}; this.disconnect = function () {}; };
  const scroll = fake(500, 1700);
  const host = fake(0, 0);
  const hint = fake(0, 0);
  SS.scrollCue(scroll, host, hint);
  assert.strictEqual(hint.hidden, false);
  assert.ok(observed, 'watches the panel size');
  scroll.clientWidth = 1800;
  observed();
  assert.strictEqual(hint.hidden, true);
  assert.strictEqual(host.getAttribute('data-scroll'), '');
});

test('R13: a panel that has no layout yet (not attached) shows nothing, then the first resize decides', () => {
  const { SS, window } = load({ files: FILES });
  let observed = null;
  window.ResizeObserver = function (fn) { observed = fn; this.observe = function () {}; this.disconnect = function () {}; };
  const scroll = fake(0, 0);
  const host = fake(0, 0);
  const hint = fake(0, 0);
  SS.scrollCue(scroll, host, hint);
  assert.strictEqual(hint.hidden, true);
  scroll.clientWidth = 390; scroll.scrollWidth = 1700;
  observed();
  assert.strictEqual(hint.hidden, false);
});

test('R13: lanes, the other two map views and the absent-steps table all carry the cue', () => {
  for (const f of ['lanes.js', 'views.js']) {
    const src = fs.readFileSync(path.join(JS_DIR, f), 'utf8');
    const n = (src.match(/SS\.scrollCue\(/g) || []).length;
    assert.ok(n >= (f === 'views.js' ? 2 : 1), f + ' calls SS.scrollCue ' + n + ' times');
  }
  assert.ok(/SS\.scrollCue\(/.test(fs.readFileSync(path.join(JS_DIR, 'ledger.js'), 'utf8')), 'ledger.js');
  assert.strictEqual(EN['scroll.hint.phases'], 'Scroll sideways to see all phases.');
  assert.strictEqual(EN['scroll.hint.columns'], 'Scroll sideways to see all columns.');
});
