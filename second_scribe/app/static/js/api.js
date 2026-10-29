/* SS.api: the browser side of the local app's JSON API (Task 31). Classic script; needs core.js.
   SS.api.get(path, query?) and SS.api.post(path, body?) return Promises of the reply object. They send the session
   cookie (credentials: "same-origin") and, for POST, the CSRF header from GET /api/session (fetched once).
   A refusal rejects with an Error carrying status, code and message from the server's {error, message}; a 401
   also emits "api:no_session" on SS.bus, and a POST the server accepted emits "api:posted" with {path} (Part 5c: the
   Setup step list reads its marks again after every action). Paths must start with /api/. */
(function () {
  'use strict';
  var root = typeof window !== 'undefined' ? window : globalThis;
  var SS = root.SS = root.SS || {};
  var CSRF_HEADER = 'X-SS-CSRF';
  var csrf = null;          // Promise of the token, or null

  function failure(status, code, message) {
    var err = new Error(message || code);
    err.status = status;
    err.code = code;
    return err;
  }

  function emit(name, payload) {
    if (SS.bus && typeof SS.bus.emit === 'function') SS.bus.emit(name, payload);
  }

  function isPlainObject(v) {
    return v !== null && typeof v === 'object' && !Array.isArray(v);
  }

  function readReply(resp) {
    return resp.text().then(function (text) {
      var doc = null;
      try {
        doc = text ? JSON.parse(text) : null;
      } catch (e) {
        doc = null;
      }
      if (resp.ok && isPlainObject(doc)) return doc;
      var code = isPlainObject(doc) && typeof doc.error === 'string' ? doc.error
        : (resp.ok ? 'bad_reply' : 'http_' + resp.status);
      var message = isPlainObject(doc) && typeof doc.message === 'string' ? doc.message : SS.t('api.failed');
      var err = failure(resp.status, code, message);
      if (resp.status === 401) {
        csrf = null;
        emit('api:no_session', err);
      }
      throw err;
    });
  }

  function request(method, path, body, headers) {
    if (typeof path !== 'string' || path.indexOf('/api/') !== 0) {
      return Promise.reject(failure(0, 'bad_path', 'SS.api: path must start with /api/: ' + String(path)));
    }
    var init = { method: method, credentials: 'same-origin', cache: 'no-store', redirect: 'error',
      headers: headers || {} };
    if (body !== undefined) init.body = body;
    var sent;
    try {
      sent = root.fetch(path, init);
    } catch (e) {
      return Promise.reject(failure(0, 'network', SS.t('api.unreachable')));
    }
    return Promise.resolve(sent).then(readReply, function () {
      throw failure(0, 'network', SS.t('api.unreachable'));
    });
  }

  function withQuery(path, query) {
    if (!isPlainObject(query)) return path;
    var parts = [];
    Object.keys(query).sort().forEach(function (k) {
      var v = query[k];
      if (v === undefined || v === null) return;
      parts.push(encodeURIComponent(k) + '=' + encodeURIComponent(String(v)));
    });
    return parts.length ? path + '?' + parts.join('&') : path;
  }

  SS.api = {
    CSRF_HEADER: CSRF_HEADER,
    // The CSRF token (Promise). Fetched once per page; fetched again after a failure or a 401.
    session: function session() {
      if (!csrf) {
        csrf = request('GET', '/api/session').then(function (doc) {
          if (typeof doc.csrf !== 'string' || !doc.csrf) throw failure(0, 'bad_reply', SS.t('api.failed'));
          return doc.csrf;
        });
        csrf.then(null, function () { csrf = null; });
      }
      return csrf;
    },
    get: function get(path, query) {
      return request('GET', withQuery(path, query));
    },
    post: function post(path, body) {
      var payload;
      try {
        payload = JSON.stringify(body === undefined ? {} : body);
      } catch (e) {
        return Promise.reject(failure(0, 'bad_body', 'SS.api: body is not JSON'));
      }
      return SS.api.session().then(function (token) {
        var headers = { 'Content-Type': 'application/json' };
        headers[CSRF_HEADER] = token;
        return request('POST', path, payload, headers);
      }).then(function (doc) {
        emit('api:posted', { path: path });     // a screen that shows a summary of the project reads it again
        return doc;
      });
    }
  };
})();
