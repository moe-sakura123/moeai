/* core/ui.js — 顶栏 / toast / 弹层 / 空态 / 登录框 / 图片兜底
   页面只调用 App.ui.*，不直接操作这些结构。 */
(function () {
  'use strict';
  const App = (window.App = window.App || {});

  const NAV = [
    { key: 'home', label: '探索', href: '/app/explore' },
    { key: 'creation', label: '创作', href: '/app/creation' },
    { key: 'worldbook', label: '世界书', href: '/app/worldbook' },
    { key: 'chat', label: '聊天', href: '/app/chat' },
    { key: 'plaza', label: '创意广场', href: '/app/plaza' },
    { key: 'draw', label: 'AI 绘图', href: '/app/draw' },
    { key: 'console', label: '控制台', href: '/app/console' },
    { key: 'works', label: '我的作品', href: '/app/works' },
    { key: 'account', label: '账号管理', href: '/app/account' },
  ];

  function esc(v) {
    return String(v === undefined || v === null ? '' : v)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  const PALETTE = [
    ['#5b21b6', '#9d174d'], ['#1e3a8a', '#6d28d9'], ['#7c2d12', '#be185d'],
    ['#134e4a', '#4c1d95'], ['#3f1d5c', '#7c2d12'],
  ];
  function placeholder(text, seed) {
    const p = PALETTE[Math.abs(Number(seed || 0)) % PALETTE.length];
    const label = esc(String(text || '?').slice(0, 1));
    const svg = '<svg xmlns="http://www.w3.org/2000/svg" width="120" height="120">' +
      '<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">' +
      '<stop offset="0" stop-color="' + p[0] + '"/><stop offset="1" stop-color="' + p[1] + '"/>' +
      '</linearGradient></defs><rect width="120" height="120" fill="url(#g)"/>' +
      '<text x="60" y="73" font-size="40" fill="rgba(255,255,255,.9)" text-anchor="middle" ' +
      'font-family="system-ui,sans-serif">' + label + '</text></svg>';
    return 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(svg);
  }

  function safeImg(img, text, seed) {
    if (!img) return;
    img.addEventListener('error', function onerr() {
      img.removeEventListener('error', onerr);
      img.src = placeholder(text, seed);
    });
  }

  /* ---------------------------------------------------------- 顶栏 */
  function mountTopbar(active) {
    const host = document.getElementById('topbar');
    if (!host) return;
    host.className = 'app-topbar';
    host.innerHTML =
      '<a class="app-brand" href="/app/explore"><span class="app-brand-mark">蜜</span>' +
      '<span class="app-brand-text"><b>蜜丝 AI</b><small>LOCAL</small></span></a>' +
      '<nav class="app-nav">' + NAV.map((n) =>
        '<a href="' + n.href + '"' + (n.key === active ? ' class="active"' : '') + '>' +
        esc(n.label) + '</a>').join('') + '</nav>' +
      '<div class="app-topbar-right" id="topbar-user"></div>';
    renderUser();
  }

  function renderUser() {
    const host = document.getElementById('topbar-user');
    if (!host) return;
    App.store.session().then((s) => {
      if (!document.body.contains(host)) return;
      if (s.authenticated && s.user) {
        const u = s.user;
        const name = u.nickName || u.nick_name || u.userName || u.user_name || '用户';
        host.innerHTML = '<a class="tag plain" href="/app/account" title="账号管理">' +
          esc(name) + '</a>' +
          '<button class="btn sm ghost" id="topbar-logout">退出</button>';
        const btn = document.getElementById('topbar-logout');
        if (btn) btn.addEventListener('click', logout);
      } else {
        host.innerHTML = '<button class="btn sm ghost" id="topbar-register">注册</button>' +
          '<button class="btn sm primary" id="topbar-login">登录</button>';
        const loginBtn = document.getElementById('topbar-login');
        if (loginBtn) loginBtn.addEventListener('click', () => loginDialog());
        const regBtn = document.getElementById('topbar-register');
        if (regBtn) regBtn.addEventListener('click', () => loginDialog(null, 'register'));
      }
    });
  }

  function logout() {
    App.api.post('/api/gva/base/logout', {}, { timeout: 6000 })
      .catch(() => {})
      .then(() => {
        App.store.resetSession();
        location.reload();
      });
  }

  /* ---------------------------------------------------------- toast */
  let toastEl = null;
  let toastTimer = 0;
  function toast(text, ok) {
    if (!toastEl) {
      toastEl = document.createElement('div');
      toastEl.className = 'app-toast';
      toastEl.hidden = true;
      document.body.appendChild(toastEl);
    }
    toastEl.textContent = String(text || '');
    toastEl.className = 'app-toast' + (ok === false ? ' err' : '');
    toastEl.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { toastEl.hidden = true; }, 3600);
  }

  /* ---------------------------------------------------------- 弹层 */
  let mask = null;
  let onCloseHook = null;
  function closeModal() {
    if (mask && mask.parentNode) mask.parentNode.removeChild(mask);
    mask = null;
    document.body.classList.remove('modal-open');
    const cb = onCloseHook;
    onCloseHook = null;
    if (cb) {
      try { cb(); } catch (e) { App.guard && App.guard.report(e.message); }
    }
  }
  function openModal(opts) {
    const o = opts || {};
    closeModal();
    onCloseHook = typeof o.onClose === 'function' ? o.onClose : null;
    mask = document.createElement('div');
    mask.className = 'modal-mask';
    mask.innerHTML =
      '<div class="modal" role="dialog" aria-modal="true">' +
      '<div class="modal-head"><b>' + esc(o.title || '') + '</b>' +
      '<button class="btn sm ghost" data-close>关闭</button></div>' +
      '<div class="modal-body">' + (o.html || '') + '</div>' +
      (o.footer === false ? '' :
        '<div class="modal-foot">' + (o.footer || '<button class="btn" data-close>关闭</button>') + '</div>') +
      '</div>';
    document.body.appendChild(mask);
    document.body.classList.add('modal-open');
    mask.addEventListener('click', (e) => {
      if (e.target === mask || (e.target.hasAttribute && e.target.hasAttribute('data-close'))) closeModal();
    });
    if (typeof o.onMount === 'function') {
      try { o.onMount(mask); } catch (e) { App.guard && App.guard.report(e.message); }
    }
    return mask;
  }

  function confirmDialog(title, text, onOk) {
    const html = '<p class="muted" style="margin:0">' + esc(text || '') + '</p>';
    const m = openModal({
      title: title || '确认',
      html,
      footer: '<button class="btn" data-close>取消</button>' +
        '<button class="btn danger" id="confirm-ok">确定</button>',
    });
    const ok = m.querySelector('#confirm-ok');
    if (ok) ok.addEventListener('click', () => { closeModal(); if (onOk) onOk(); });
  }

  /* ---------------------------------------------------------- 空态 / loading */
  function emptyHtml(title, desc) {
    return '<div class="empty"><b>' + esc(title || '暂无内容') + '</b>' +
      (desc ? esc(desc) : '') + '</div>';
  }
  function skeleton(count) {
    let out = '';
    for (let i = 0; i < (count || 3); i += 1) {
      out += '<div class="skeleton sk-line"></div>';
    }
    return out;
  }

  /* ---------------------------------------------------------- 登录 / 注册 */
  let loginShown = false;
  function loginDialog(onDone, mode) {
    if (loginShown) return;
    loginShown = true;
    const start = mode === 'register' ? 'register' : 'login';
    openModal({
      title: start === 'register' ? '注册本地账号' : '登录本地账号',
      html:
        '<div class="tabs auth-tabs" id="auth-tabs">' +
        '<button type="button" data-mode="login">登录</button>' +
        '<button type="button" data-mode="register">注册</button></div>' +
        '<div class="auth-pane" data-pane="login">' +
        '<div class="field"><label>账号 / 邮箱</label>' +
        '<input class="input" id="lg-user" autocomplete="username" placeholder="用户名或邮箱"></div>' +
        '<div class="field"><label>密码</label>' +
        '<input class="input" type="password" id="lg-pass" autocomplete="current-password" placeholder="密码"></div>' +
        '</div>' +
        '<div class="auth-pane" data-pane="register" hidden>' +
        '<div class="auth-row">' +
        '<div class="field"><label>用户名 <span class="req">*</span></label>' +
        '<input class="input" id="rg-user" autocomplete="username" placeholder="登录用户名"></div>' +
        '<div class="field"><label>昵称</label>' +
        '<input class="input" id="rg-nick" placeholder="默认与用户名相同"></div></div>' +
        '<div class="field"><label>邮箱（选填）</label>' +
        '<input class="input" type="email" id="rg-email" autocomplete="email" placeholder="绑定后可用邮箱登录"></div>' +
        '<div class="auth-row">' +
        '<div class="field"><label>密码 <span class="req">*</span></label>' +
        '<input class="input" type="password" id="rg-pass" autocomplete="new-password" placeholder="至少 8 位"></div>' +
        '<div class="field"><label>确认密码 <span class="req">*</span></label>' +
        '<input class="input" type="password" id="rg-pass2" autocomplete="new-password" placeholder="再次输入密码"></div>' +
        '</div></div>' +
        '<p class="small muted auth-msg" id="lg-msg"></p>',
      footer: '<button class="btn" data-close>取消</button>' +
        '<button class="btn primary" id="lg-submit">登录</button>',
      onClose() { loginShown = false; },
      onMount(m) {
        const msg = m.querySelector('#lg-msg');
        const submitBtn = m.querySelector('#lg-submit');
        const titleEl = m.querySelector('.modal-head b');
        let current = start;

        const done = (text) => {
          App.store.resetSession();
          closeModal();
          toast(text);
          renderUser();
          if (onDone) onDone(true);
        };

        const submit = () => {
          if (submitBtn.disabled) return;
          if (current === 'login') {
            const user = (m.querySelector('#lg-user').value || '').trim();
            const pass = m.querySelector('#lg-pass').value || '';
            if (!user || !pass) { msg.textContent = '请填写账号和密码'; return; }
            msg.textContent = '登录中…';
            submitBtn.disabled = true;
            App.api.post('/api/gva/base/login', { userName: user, passWord: pass })
              .then(() => done('登录成功'))
              .catch((err) => {
                msg.textContent = err.message || '登录失败';
                submitBtn.disabled = false;
              });
            return;
          }

          const user = (m.querySelector('#rg-user').value || '').trim();
          const nick = (m.querySelector('#rg-nick').value || '').trim();
          const email = (m.querySelector('#rg-email').value || '').trim();
          const pass = m.querySelector('#rg-pass').value || '';
          const pass2 = m.querySelector('#rg-pass2').value || '';
          if (user.length < 2) { msg.textContent = '用户名至少 2 个字符'; return; }
          if (email && !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) {
            msg.textContent = '邮箱格式不正确'; return;
          }
          if (pass.length < 8) { msg.textContent = '密码至少 8 个字符'; return; }
          if (pass !== pass2) { msg.textContent = '两次输入的密码不一致'; return; }
          msg.textContent = '注册中…';
          submitBtn.disabled = true;
          App.api.post('/api/gva/user/register_v2',
            { userName: user, nickName: nick || user, passWord: pass, email: email })
            .then(() => done('注册成功，已自动登录'))
            .catch((err) => {
              msg.textContent = err.message || '注册失败';
              submitBtn.disabled = false;
            });
        };

        const setMode = (next) => {
          current = next === 'register' ? 'register' : 'login';
          Array.prototype.forEach.call(m.querySelectorAll('#auth-tabs button'), (b) => {
            b.classList.toggle('active', b.getAttribute('data-mode') === current);
          });
          Array.prototype.forEach.call(m.querySelectorAll('.auth-pane'), (p) => {
            p.hidden = p.getAttribute('data-pane') !== current;
          });
          submitBtn.textContent = current === 'register' ? '注册并登录' : '登录';
          if (titleEl) {
            titleEl.textContent = current === 'register' ? '注册本地账号' : '登录本地账号';
          }
          msg.textContent = '';
          submitBtn.disabled = false;
          setTimeout(() => {
            const input = m.querySelector(current === 'register' ? '#rg-user' : '#lg-user');
            if (input) input.focus();
          }, 30);
        };

        Array.prototype.forEach.call(m.querySelectorAll('#auth-tabs button'), (b) => {
          b.addEventListener('click', () => setMode(b.getAttribute('data-mode')));
        });
        submitBtn.addEventListener('click', submit);
        Array.prototype.forEach.call(m.querySelectorAll('.auth-pane input'), (i) => {
          i.addEventListener('keydown', (e) => { if (e.key === 'Enter') submit(); });
        });
        setMode(start);
      },
    });
  }

  /* 需要登录的操作前调用：已登录直接放行，否则弹框并返回 false */
  function requireLogin(onDone) {
    return App.store.session().then((s) => {
      if (s.authenticated) return true;
      loginDialog(onDone);
      return false;
    });
  }

  function fmtTime(iso) {
    if (!iso) return '';
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return '';
    const p = (n) => String(n).padStart(2, '0');
    return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate()) +
      ' ' + p(d.getHours()) + ':' + p(d.getMinutes());
  }

  App.ui = {
    esc, placeholder, safeImg, mountTopbar, renderUser,
    toast, openModal, closeModal, confirmDialog,
    emptyHtml, skeleton, loginDialog, requireLogin, fmtTime,
  };
})();
