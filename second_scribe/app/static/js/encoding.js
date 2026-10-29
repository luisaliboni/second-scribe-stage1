/* SS.encoding: which shape an item gets in which lane. Shape carries the meaning; colour only repeats it.
     normal  = drawn as happened
     dashed  = drawn as missing (reference: red; AI: blue)
     leaked  = a missing step the AI drew as happened: solid red + the up-triangle
     never   = the AI never drew it (dotted grey); with the tag "Not attested by its own quote" (SPEC v5 wording) when only unlocated quotes matched
     script  = grey fill: the lane of the same model without the interview (code name kept: script)
     unknown = no recorded fate for this rung: "not computed", never a normal box
   Classic script; attaches to window.SS (after core.js). */
(function () {
  'use strict';
  var root = typeof window !== 'undefined' ? window : globalThis;
  var SS = root.SS = root.SS || {};
  var hasOwn = Object.prototype.hasOwnProperty;

  var UP = '▲';
  var DOWN = '▼';
  var SHAPES = ['normal', 'dashed', 'leaked', 'never', 'script', 'unknown'];

  var AI_BY_FATE = {
    kept: { shape: 'dashed', glyph: '' },
    leaked: { shape: 'leaked', glyph: UP },
    recovered: { shape: 'normal', glyph: '' },
    flipped: { shape: 'dashed', glyph: DOWN },
    missed: { shape: 'never', glyph: '' },
    unattested: { shape: 'never', glyph: '' }
  };

  function fateAt(item, rung) {
    var f = item && item.fate;
    if (f === null || typeof f !== 'object' || !hasOwn.call(f, rung)) return undefined;
    return f[rung];
  }

  // SS.encoding.forItem(lane, item, rung) -> {shape, glyph[, tag]} | null
  //   lane: 'reference' | 'ai' | 'script'. null = nothing is drawn in that lane for this item
  //   (the no-interview lane only has the steps that map drew).
  function forItem(lane, item, rung) {
    if (lane === 'script') {
      return item && item.script_has === true ? { shape: 'script', glyph: '' } : null;
    }
    if (lane === 'reference') {
      var st = item && item.reference_status;
      if (st === 'happened') return { shape: 'normal', glyph: '' };
      if (st === 'missing') return { shape: 'dashed', glyph: '' };
      return { shape: 'unknown', glyph: '' };
    }
    var fate = fateAt(item, rung);
    if (typeof fate !== 'string' || !hasOwn.call(AI_BY_FATE, fate)) return { shape: 'unknown', glyph: '' };
    var e = { shape: AI_BY_FATE[fate].shape, glyph: AI_BY_FATE[fate].glyph };
    if (fate === 'unattested') e.tag = SS.t('lane.quote_not_found');
    return e;
  }

  // One CSS class per shape (viewer.css).
  function shapeClass(shape) {
    return 'ss-shape-' + (SHAPES.indexOf(shape) >= 0 ? shape : 'unknown');
  }

  SS.encoding = { SHAPES: SHAPES, forItem: forItem, shapeClass: shapeClass, fateAt: fateAt };
})();
