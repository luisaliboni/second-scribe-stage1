/* SS.shell: the local app's frame (Task 31). Classic script; needs core.js and api.js.
   Screens (Tasks 32-42) live in their own static/js/app-<name>.js; the server adds every such file after this one,
   and each registers itself:
     SS.shell.register('setup', { order: 10, titleKey: 'shell.nav.setup', render: function (main, sub) { ... } });
   The hash is #<screen>/<sub> (app.html#setup/2). On load the shell checks the session (GET /api/session); without
   one it shows the "open the link" text. ?theme=dark or ?theme=light forces a theme (for screenshots). */
(function () {
  'use strict';
  var root = typeof window !== 'undefined' ? window : globalThis;
  var SS = root.SS = root.SS || {};
  var hasOwn = Object.prototype.hasOwnProperty;
  var screens = {};
  var ID = /^[a-z][a-z0-9-]*$/;

  function el(doc, tag, cls, text) {
    var n = doc.createElement(tag);
    if (cls) n.setAttribute('class', cls);
    if (text !== undefined) n.textContent = text;
    return n;
  }

  function clear(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
  }

  SS.shell = {
    register: function register(id, def) {
      if (typeof id !== 'string' || !ID.test(id)) throw new Error('SS.shell.register: bad screen id ' + String(id));
      if (hasOwn.call(screens, id)) throw new Error('SS.shell.register: screen ' + id + ' is already registered');
      if (!def || typeof def.render !== 'function' || typeof def.titleKey !== 'string') {
        throw new Error('SS.shell.register: screen ' + id + ' needs titleKey and render');
      }
      screens[id] = { id: id, order: typeof def.order === 'number' ? def.order : 100, titleKey: def.titleKey,
        render: def.render };
    },
    // Screen ids by order, then id.
    list: function list() {
      return Object.keys(screens).sort(function (a, b) {
        return (screens[a].order - screens[b].order) || (a < b ? -1 : a > b ? 1 : 0);
      });
    },
    // '#setup/2' -> {screen: 'setup', sub: '2'}; '' -> {screen: '', sub: ''}
    parseHash: function parseHash(hash) {
      var h = typeof hash === 'string' ? hash.replace(/^#/, '') : '';
      var i = h.indexOf('/');
      var screen = i < 0 ? h : h.slice(0, i);
      return { screen: ID.test(screen) ? screen : '', sub: i < 0 ? '' : h.slice(i + 1) };
    },
    // The screen to show for a hash: the named one when registered, else the first; '' when none is registered.
    pick: function pick(hash) {
      var want = SS.shell.parseHash(hash).screen;
      if (want && hasOwn.call(screens, want)) return want;
      var ids = SS.shell.list();
      return ids.length ? ids[0] : '';
    },
    // Builds the frame inside `mount` and shows the screen for the current hash.
    start: function start(doc, mount) {
      var header = el(doc, 'header', 'ss-shell-header');
      var name = el(doc, 'span', 'ss-shell-name');
      var mark = el(doc, 'span', 'ss-mark');                 // the three-circle brand mark, decorative
      mark.setAttribute('aria-hidden', 'true');
      for (var i = 0; i < 3; i++) mark.appendChild(el(doc, 'i'));
      name.appendChild(mark);
      name.appendChild(doc.createTextNode(SS.t('app.name')));
      header.appendChild(name);
      var nav = el(doc, 'nav', 'ss-shell-nav');
      nav.setAttribute('aria-label', SS.t('shell.nav_label'));
      header.appendChild(nav);
      var main = el(doc, 'main', 'ss-shell-main');
      main.setAttribute('id', 'ss-main');
      main.setAttribute('tabindex', '-1');
      clear(mount);
      mount.appendChild(header);
      mount.appendChild(main);

      function drawNav(current) {
        clear(nav);
        SS.shell.list().forEach(function (id) {
          var a = el(doc, 'a', 'ss-shell-link', SS.t(screens[id].titleKey));
          a.setAttribute('href', '#' + id);
          if (id === current) a.setAttribute('aria-current', 'page');
          nav.appendChild(a);
        });
      }

      function show() {
        var id = SS.shell.pick(root.location ? root.location.hash : '');
        drawNav(id);
        clear(main);
        if (!id) {
          main.appendChild(el(doc, 'p', 'ss-shell-note', SS.t('shell.empty')));
          return;
        }
        try {
          screens[id].render(main, SS.shell.parseHash(root.location.hash).sub);
        } catch (e) {
          if (typeof console !== 'undefined' && console.error) console.error(e);
          clear(main);
          main.appendChild(el(doc, 'p', 'ss-shell-note', SS.t('shell.screen_failed')));
        }
      }

      main.appendChild(el(doc, 'p', 'ss-shell-note', SS.t('shell.loading')));
      return SS.api.session().then(function () {
        if (root.addEventListener) root.addEventListener('hashchange', show);
        show();
      }, function () {
        clear(nav);
        clear(main);
        main.appendChild(el(doc, 'p', 'ss-shell-note', SS.t('server.open_link')));
      });
    }
  };

  function theme(doc) {
    var m = /(?:^|[?&])theme=(dark|light)(?:&|$)/.exec(root.location ? root.location.search || '' : '');
    if (m) doc.documentElement.setAttribute('data-theme', m[1]);
  }

  if (typeof document !== 'undefined' && document.addEventListener) {
    document.addEventListener('DOMContentLoaded', function () {
      var mount = document.getElementById ? document.getElementById('ss-app') : null;
      if (!mount) return;
      theme(document);
      SS.shell.start(document, mount);
    });
  }
})();
