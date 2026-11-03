/* core/store.js — 会话与本地草稿（localStorage 安全读写，禁用时自动降级内存） */
(function () {
  'use strict';
  const App = (window.App = window.App || {});
  const mem = Object.create(null);
  const PREFIX = 'missai.app.';

  function ls() {
    try {
      const t = '__probe__';
      window.localStorage.setItem(t, '1');
      window.localStorage.removeItem(t);
      return window.localStorage;
    } catch (e) { return null; }
  }
  const store = ls();

  function get(key, fallback) {
    try {
      const raw = store ? store.getItem(PREFIX + key) : mem[key];
      if (raw === null || raw === undefined) return fallback;
      return JSON.parse(raw);
    } catch (e) { return fallback; }
  }
  function set(key, value) {
    const raw = JSON.stringify(value);
    try {
      if (store) store.setItem(PREFIX + key, raw); else mem[key] = raw;
    } catch (e) { mem[key] = raw; }
    return value;
  }
  function remove(key) {
    try { if (store) store.removeItem(PREFIX + key); delete mem[key]; } catch (e) { /* noop */ }
  }

  let sessionPromise = null;
  function session(refresh) {
    if (sessionPromise && !refresh) return sessionPromise;
    sessionPromise = App.api.get('/api/auth/session', null, { timeout: 6000 })
      .then((data) => {
        const d = data || {};
        return {
          authenticated: !!d.authenticated,
          user: d.user || null,
        };
      })
      .catch(() => ({ authenticated: false, user: null }));
    return sessionPromise;
  }

  App.store = {
    get, set, remove,
    session,
    resetSession() { sessionPromise = null; },
    lastWork() { return get('lastWork', ''); },
    setLastWork(id) { set('lastWork', id || ''); },
  };
})();
