/* SS.install: the site's Install page (plan Task 30, spec section 6.4). Classic script; needs core.js only.
   Tabs for macOS, Windows and Linux (an ARIA tablist: arrow keys, Home and End move between tabs), each with
   Python 3.10 or newer, getting the code, two local models of different families (examples, not endorsements),
   OLLAMA_NO_CLOUD=1, the start command and links to INSTALL.md and DATAFLOW.md. Every command has a Copy button
   (navigator.clipboard.writeText, falling back to selecting the text).

   SS.install.model()                    -> [{id, label_key, steps: [{id, title_key, body_key, commands, links}]}]
   SS.install.render(root, {tab})        draws the page; tab defaults to #os=<id>, then the platform's guess.
   SS.install.nextIndex(i, key, count)   -> the tab index a key moves to, or -1.
   SS.install.guessTab(platform)         -> "macos" | "windows" | "linux" ("macos" when unknown).
   Every word comes from en.json (install.*); commands are install.cmd.* keys, printed exactly.
   Data reaches the DOM through textContent and attributes only. No outside links: sites are named, not linked. */
(function () {
  'use strict';
  var root = typeof window !== 'undefined' ? window : globalThis;
  var SS = root.SS = root.SS || {};
  var TABS = ['macos', 'windows', 'linux'];
  var DOCS = [['install.link.install', '../INSTALL.md'], ['install.link.dataflow', '../DATAFLOW.md']];

  function steps(os) {
    var win = os === 'windows';
    return [
      { id: 'python', title_key: 'install.step.python.title', body_key: 'install.step.python.body.' + os,
        commands: win ? ['install.cmd.python_version_windows'] :
          os === 'macos' ? ['install.cmd.python_version', 'install.cmd.brew_python'] : ['install.cmd.python_version'] },
      { id: 'get', title_key: 'install.step.get.title', body_key: win ? 'install.step.get.body.windows' : 'install.step.get.body',
        commands: [] },
      { id: 'models', title_key: 'install.step.models.title', body_key: 'install.step.models.body',
        more_keys: ['install.step.models.where', 'install.step.models.reference', 'install.step.models.local'],
        commands: ['install.cmd.pull_gemma', 'install.cmd.pull_qwen'],
        after_keys: ['install.step.models.one_computer', 'install.step.models.hosted'] },
      { id: 'no_cloud', title_key: 'install.step.no_cloud.title', body_key: 'install.step.no_cloud.body.' + os,
        commands: os === 'macos' ? ['install.cmd.no_cloud_macos_app', 'install.cmd.no_cloud_serve'] :
          win ? ['install.cmd.no_cloud_windows'] :
            ['install.cmd.systemd_edit', 'install.cmd.systemd_restart', 'install.cmd.no_cloud_serve'] },
      // item 21: say where to run the start command (the kit folder), show the cd line, and what "No module named
      // second_scribe" means; the start command itself is unchanged
      { id: 'start', title_key: 'install.step.start.title', body_key: win ? 'install.step.start.where.windows' : 'install.step.start.where',
        commands: win ? ['install.cmd.cd_windows', 'install.cmd.start_windows'] : ['install.cmd.cd', 'install.cmd.start'],
        after_keys: ['install.step.start.body', 'install.step.start.no_module'] },
      { id: 'docs', title_key: 'install.step.docs.title', body_key: 'install.step.docs.body', commands: [], links: DOCS }
    ];
  }

  function model() {
    return TABS.map(function (id) { return { id: id, label_key: 'install.os.' + id, steps: steps(id) }; });
  }

  function nextIndex(i, key, n) {
    if (key === 'ArrowRight' || key === 'ArrowDown') return (i + 1) % n;
    if (key === 'ArrowLeft' || key === 'ArrowUp') return (i - 1 + n) % n;
    if (key === 'Home') return 0;
    if (key === 'End') return n - 1;
    return -1;
  }

  function guessTab(platform) {
    var p = String(platform || '');
    if (/mac|iphone|ipad|darwin/i.test(p)) return 'macos';
    if (/win/i.test(p)) return 'windows';
    if (/linux|x11|cros|bsd/i.test(p)) return 'linux';
    return 'macos';
  }

  function platform() {
    var nav = root.navigator;
    if (!nav) return '';
    if (nav.userAgentData && typeof nav.userAgentData.platform === 'string' && nav.userAgentData.platform) {
      return nav.userAgentData.platform;
    }
    return typeof nav.platform === 'string' ? nav.platform : '';
  }

  function hashTab() {
    var m = /(?:^#|&)os=([a-z]+)/.exec(String(root.location && root.location.hash || ''));
    return m && TABS.indexOf(m[1]) >= 0 ? m[1] : null;
  }

  // ------------------------------------------------------------------------------------------- DOM
  function el(tag, cls, text, parent) {
    var n = root.document.createElement(tag);
    if (cls) n.setAttribute('class', cls);
    if (text !== undefined && text !== null) n.textContent = String(text);
    if (parent) parent.appendChild(n);
    return n;
  }
  function clear(node) {
    if (typeof node.replaceChildren === 'function') { node.replaceChildren(); return; }
    while (node.firstChild) node.removeChild(node.firstChild);
  }

  function selectText(node) {
    try {
      var sel = root.getSelection && root.getSelection();
      var range = root.document.createRange && root.document.createRange();
      if (!sel || !range) return false;
      range.selectNodeContents(node);
      sel.removeAllRanges();
      sel.addRange(range);
      return true;
    } catch (e) {
      return false;
    }
  }

  function copy(text, codeNode, statusNode) {
    var say = function (key) {
      statusNode.textContent = SS.t(key);
      if (typeof root.setTimeout === 'function') root.setTimeout(function () { statusNode.textContent = ''; }, 4000);
    };
    var fallback = function () { selectText(codeNode); say('install.copy.selected'); };
    try {
      var cb = root.navigator && root.navigator.clipboard;
      if (cb && typeof cb.writeText === 'function') {
        cb.writeText(text).then(function () { say('install.copy.done'); }, fallback);
        return;
      }
    } catch (e) { /* fall through to selecting the text */ }
    fallback();
  }

  function command(key, parent) {
    var text = SS.t(key);
    var box = el('div', 'install-cmd', undefined, parent);
    var code = el('code', 'mono', text, el('pre', 'install-cmd-text', undefined, box));
    code.setAttribute('data-command', key);
    var btn = el('button', 'install-copy', SS.t('install.copy'), box);
    btn.setAttribute('type', 'button');
    btn.setAttribute('data-copy', key);
    btn.setAttribute('aria-label', SS.t('install.copy.label', { command: text }));
    var st = el('span', 'install-copy-status', '', box);
    st.setAttribute('role', 'status');
    btn.addEventListener('click', function () { copy(text, code, st); });
    return box;
  }

  function panel(tab, parent, selected) {
    var p = el('div', 'install-panel', undefined, parent);
    p.setAttribute('role', 'tabpanel');
    p.setAttribute('id', 'install-panel-' + tab.id);
    p.setAttribute('aria-labelledby', 'install-tab-' + tab.id);
    p.setAttribute('tabindex', '0');
    p.hidden = !selected;
    var ol = el('ol', 'install-steps', undefined, p);
    tab.steps.forEach(function (s) {
      var li = el('li', 'install-step install-step-' + s.id, undefined, ol);
      el('h3', '', SS.t(s.title_key), li);
      el('p', '', SS.t(s.body_key), li);
      (s.more_keys || []).forEach(function (k) { el('p', '', SS.t(k), li); });   // INSTALL.md's paragraphs, same order
      (s.commands || []).forEach(function (k) { command(k, li); });
      (s.after_keys || []).forEach(function (k) { el('p', '', SS.t(k), li); });
      if (s.links && s.links.length) {
        var ul = el('ul', 'install-links', undefined, li);
        s.links.forEach(function (l) { el('a', '', SS.t(l[0]), el('li', '', undefined, ul)).setAttribute('href', l[1]); });
      }
    });
    return p;
  }

  function render(rootNode, opts) {
    opts = opts || {};
    clear(rootNode);
    var tabs = model();
    var current = opts.tab || hashTab() || guessTab(platform());
    if (TABS.indexOf(current) < 0) current = 'macos';
    var page = el('div', 'install-wrap', undefined, rootNode);
    el('h1', '', SS.t('install.title'), page);
    el('p', 'install-intro', SS.t('install.intro'), page);
    var list = el('div', 'install-tablist', undefined, page);
    list.setAttribute('role', 'tablist');
    list.setAttribute('aria-label', SS.t('install.tabs.label'));
    var buttons = [];
    var panels = [];
    function select(i, focus) {
      buttons.forEach(function (b, j) {
        b.setAttribute('aria-selected', j === i ? 'true' : 'false');
        b.setAttribute('tabindex', j === i ? '0' : '-1');
        panels[j].hidden = j !== i;
      });
      if (focus && typeof buttons[i].focus === 'function') buttons[i].focus();
    }
    tabs.forEach(function (t, i) {
      var b = el('button', 'install-tab', SS.t(t.label_key), list);
      b.setAttribute('type', 'button');
      b.setAttribute('role', 'tab');
      b.setAttribute('id', 'install-tab-' + t.id);
      b.setAttribute('aria-controls', 'install-panel-' + t.id);
      b.setAttribute('aria-selected', t.id === current ? 'true' : 'false');
      b.setAttribute('tabindex', t.id === current ? '0' : '-1');
      b.addEventListener('click', function () { select(i, false); });
      b.addEventListener('keydown', function (ev) {
        var j = nextIndex(i, ev.key, tabs.length);
        if (j < 0) return;
        if (typeof ev.preventDefault === 'function') ev.preventDefault();
        select(j, true);
      });
      buttons.push(b);
    });
    tabs.forEach(function (t) { panels.push(panel(t, page, t.id === current)); });
    return rootNode;
  }

  SS.install = { model: model, render: render, nextIndex: nextIndex, guessTab: guessTab };

  function mount() {
    var node = root.document.getElementById('install-root');
    if (node) render(node);
  }
  if (root.document && typeof root.document.getElementById === 'function') {
    if (root.document.readyState === 'loading') root.document.addEventListener('DOMContentLoaded', mount);
    else mount();
  }
})();
