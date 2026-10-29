/* Site shell (Task 28): the light/dark toggle on every site page. Loaded in <head>, so a remembered choice is set
   before the page paints. Without a choice the page is dark, the default of tokens.css; the button is pressed when the
   page shows dark. A choice is kept in localStorage ("ss-theme"); storage that is missing
   or throws (a private window, a locked-down browser) is ignored and the toggle still works for this page.
   #theme=dark or #theme=light in the address forces a theme without remembering it (screenshots, tools/shot.sh).
   Classic script; attaches SS.siteShell to window.SS. */
(function () {
  'use strict';
  var root = typeof window !== 'undefined' ? window : globalThis;
  var SS = root.SS = root.SS || {};
  var KEY = 'ss-theme';

  function valid(v) { return v === 'light' || v === 'dark' ? v : null; }

  function stored() {
    try { return valid(root.localStorage ? root.localStorage.getItem(KEY) : null); } catch (e) { return null; }
  }
  function remember(theme) {
    try { if (root.localStorage) root.localStorage.setItem(KEY, theme); } catch (e) { /* not kept; still applied */ }
  }
  function fromHash() {
    var h = root.location && typeof root.location.hash === 'string' ? root.location.hash : '';
    var m = /(?:^#|&)theme=(light|dark)(?:&|$)/.exec(h);
    return m ? m[1] : null;
  }
  function forced() {
    var de = root.document && root.document.documentElement;
    return de ? valid(de.getAttribute('data-theme')) : null;
  }
  function effective() { return forced() || 'dark'; }   // dark is the default (tokens.css)

  function apply(theme) {
    var de = root.document && root.document.documentElement;
    if (de && theme) de.setAttribute('data-theme', theme);
  }
  function sync(button) {
    if (button) button.setAttribute('aria-pressed', effective() === 'dark' ? 'true' : 'false');
  }

  function bind() {
    var button = root.document && root.document.getElementById ? root.document.getElementById('site-theme') : null;
    if (!button) return;
    sync(button);
    button.addEventListener('click', function () {
      var next = effective() === 'dark' ? 'light' : 'dark';
      apply(next);
      remember(next);
      sync(button);
    });
  }

  var initial = fromHash() || stored();
  if (initial) apply(initial);

  SS.siteShell = { effective: effective, bind: bind };
  if (root.document) {
    if (root.document.readyState === 'loading' && root.document.addEventListener) {
      root.document.addEventListener('DOMContentLoaded', bind);
    } else {
      bind();
    }
  }
})();
