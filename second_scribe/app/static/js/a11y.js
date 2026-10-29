/* SS.a11y: screen-reader labels for lane items, built only from string keys a11y.item.* and a11y.at.*.
   SS.scrollCue: the visible cue that a panel scrolls sideways (review R13), for browsers that hide scrollbars.
   Classic script; attaches to window.SS (after core.js and encoding.js). */
(function () {
  'use strict';
  var root = typeof window !== 'undefined' ? window : globalThis;
  var SS = root.SS = root.SS || {};
  var hasOwn = Object.prototype.hasOwnProperty;

  var KNOWN_RUNGS = { rung1: 1, rung2: 1, placebo: 1, rung3: 1 };

  function atRung(rung) {
    if (hasOwn.call(KNOWN_RUNGS, rung)) return SS.t('a11y.at.' + rung);
    return SS.t('a11y.at.other', { rung: String(rung) });
  }

  // In mode B the bottom lane is the researcher's reading, not the participant's.
  function referenceClause(item, refType) {
    var suffix = refType === 'B' ? '_b' : '';
    var status = item && item.reference_status === 'missing' ? 'missing' : 'happened';
    return SS.t('a11y.item.ref_' + status + suffix);
  }

  function aiClause(item, rung) {
    var at = atRung(rung);
    var fate = SS.encoding.fateAt(item, rung);
    switch (fate) {
      case 'kept':
      case 'flipped':
        return SS.t('a11y.item.ai_as_missing', { at: at });
      case 'leaked':
      case 'recovered':
        return SS.t('a11y.item.ai_as_happened', { at: at });
      case 'missed':
        return SS.t('a11y.item.ai_never', { at: at });
      case 'unattested':
        return SS.t('a11y.item.ai_unattested', { at: at });
      default:
        return SS.t('a11y.item.ai_not_computed', { at: at });
    }
  }

  // "Pre-application meeting: participant marked it as wished for; AI drew it as received at rung 2"
  function itemLabel(item, rung, refType) {
    return SS.t('a11y.item.sentence', {
      label: item && item.label !== undefined && item.label !== null ? String(item.label) : '',
      reference: referenceClause(item, refType),
      ai: aiClause(item, rung)
    });
  }

  // The label a button carries in a given lane. The AI lane uses itemLabel; the other two lanes say only
  // what their own reading holds, so the no-interview lane never claims a rung.
  function laneLabel(lane, item, rung, refType) {
    var label = item && item.label !== undefined && item.label !== null ? String(item.label) : '';
    if (lane === 'script') return SS.t('a11y.item.script_drew', { label: label });
    if (lane === 'reference') return SS.t('a11y.item.reference_only', { label: label, reference: referenceClause(item, refType) });
    return itemLabel(item, rung, refType);
  }

  SS.a11y = { itemLabel: itemLabel, laneLabel: laneLabel };

  // SS.scrollCue(scroll, host, hint) -> {update, destroy}
  //   scroll: the element with overflow-x: auto; host: the positioned frame around it, which gets
  //   data-scroll = "" | "right" | "left" | "both" (the CSS draws an edge fade on the sides that have more to see);
  //   hint: the line of text, shown (hidden = false) only while the content is wider than the panel.
  // It runs at once, on scroll, and on every size change of the panel (a panel that is not laid out yet reads as
  // fitting; the first resize decides). One pixel of difference is rounding, not overflow.
  function scrollCue(scroll, host, hint) {
    function state() {
      var w = scroll.clientWidth || 0;
      var total = scroll.scrollWidth || 0;
      if (total <= w + 1) return '';
      var left = (scroll.scrollLeft || 0) > 1;
      var right = (scroll.scrollLeft || 0) + w < total - 1;
      if (left && right) return 'both';
      return left ? 'left' : (right ? 'right' : '');
    }
    function update() {
      var st = state();
      var over = (scroll.scrollWidth || 0) > (scroll.clientWidth || 0) + 1;
      host.setAttribute('data-scroll', st);
      if (hint) hint.hidden = !over;
      return st;
    }
    scroll.addEventListener('scroll', update);
    var watcher = null;
    if (typeof root.ResizeObserver === 'function') {
      watcher = new root.ResizeObserver(update);
      watcher.observe(scroll);
    } else if (typeof root.addEventListener === 'function') {
      var onResize = function () {
        if (scroll.isConnected === false) root.removeEventListener('resize', onResize);
        else update();
      };
      root.addEventListener('resize', onResize);
      if (typeof root.setTimeout === 'function') root.setTimeout(update, 0);
    }
    update();
    return {
      update: update,
      destroy: function destroy() {
        if (watcher && typeof watcher.disconnect === 'function') watcher.disconnect();
        scroll.removeEventListener('scroll', update);
      }
    };
  }
  SS.scrollCue = scrollCue;
})();
