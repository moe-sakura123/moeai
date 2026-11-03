/* core/guard.js — 全局兜底：未捕获异常只显示顶部错误条，绝不让页面白屏 */
(function () {
  'use strict';
  const App = (window.App = window.App || {});

  let bar = null;
  let lastAt = 0;
  const shown = Object.create(null);

  function ensureBar() {
    if (bar && document.body.contains(bar)) return bar;
    bar = document.createElement('div');
    bar.className = 'guard-bar';
    bar.hidden = true;
    document.body.appendChild(bar);
    return bar;
  }

  function report(message, source) {
    const msg = String(message || '页面脚本出错').slice(0, 180);
    const now = Date.now();
    // 同一条错误 5 秒内只报一次，避免刷屏
    if (shown[msg] && now - shown[msg] < 5000) return;
    shown[msg] = now;
    if (now - lastAt > 5000) lastAt = now;

    const el = ensureBar();
    el.textContent = '页面遇到问题：' + msg + (source ? '（' + source + '）' : '') + ' — 其它功能仍可使用';
    el.hidden = false;
    clearTimeout(el._t);
    el._t = setTimeout(() => { el.hidden = true; }, 8000);
    if (window.App && App.toast) App.toast('页面遇到问题：' + msg, false);
  }

  window.addEventListener('error', function (e) {
    if (e && e.target && e.target.tagName) return;   // 资源加载错误不刷屏
    report((e && e.message) || '脚本异常');
  });

  window.addEventListener('unhandledrejection', function (e) {
    const r = e && e.reason;
    const msg = (r && (r.message || r.msg)) || (typeof r === 'string' ? r : '异步操作失败');
    report(msg, 'async');
  });

  App.guard = { report };
})();
