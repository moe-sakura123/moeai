/* pages/account.js — 账号管理中心：个人资料 / 头像 / 修改密码 / 登录信息
   接口：GET /api/auth/session、GET /api/gva/user/getUserInfo、
         PUT /api/gva/user/setSelfInfo、POST /api/gva/user/changeMyPassword */
(function () {
  'use strict';
  const A = App.api;
  const U = App.ui;
  const T = App.util;
  const esc = U.esc;
  const $ = (id) => document.getElementById(id);

  const state = { user: null, points: null, works: null };

  const SEX_OPTIONS = [['', '未设置'], ['male', '男'], ['female', '女']];

  function nameOf(u) {
    return T.str(T.pick(u || {}, ['nickName', 'nick_name', 'userName', 'username']), '用户');
  }
  function avatarOf(u) {
    return T.str(T.pick(u || {}, ['headerImg', 'avatar']));
  }
  function fmtDate(v) {
    if (!v) return '—';
    const d = new Date(v);
    if (Number.isNaN(d.getTime())) return String(v).slice(0, 10) || '—';
    const p = (n) => String(n).padStart(2, '0');
    return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate());
  }
  function statHtml(value, label) {
    const text = value === null || value === undefined ? '—' : value;
    return '<div class="ac-stat"><b>' + esc(text) + '</b><span>' + esc(label) + '</span></div>';
  }

  function avatarHtml(u) {
    const name = nameOf(u);
    const src = avatarOf(u);
    return '<div class="ac-avatar" id="ac-avatar" title="点击更换头像">' +
      '<span class="ac-fallback">' + esc(name.slice(0, 1)) + '</span>' +
      (src ? '<img id="ac-avatar-img" src="' + esc(src) + '" alt="' + esc(name) + '">' : '') +
      '<input type="file" id="ac-avatar-file" accept="image/png,image/jpeg,image/webp" hidden>' +
      '<button class="ac-upload" type="button" id="ac-avatar-btn">更换头像</button>' +
      '</div>';
  }

  function growthOf(u) {
    const src = u || {};
    const level = T.num(T.pick(src, ['level'], 1), 1);
    const experience = T.num(T.pick(src, ['experience'], 0), 0);
    const nextLevelExp = T.num(T.pick(src, ['nextLevelExp'], 0), 0);
    const progress = T.num(T.pick(src, ['levelProgress'], 0), 0);
    return {
      level, experience, nextLevelExp, progress,
      levelName: T.str(T.pick(src, ['levelName'], ''), ''),
      isMaxLevel: !!T.pick(src, ['isMaxLevel'], false),
      canPublish: level >= 2,
    };
  }

  function summaryHtml() {
    const u = state.user || {};
    const username = T.str(T.pick(u, ['userName', 'username']), '');
    const sig = T.str(T.pick(u, ['signature']));
    const g = growthOf(u);
    return '<div class="panel"><div class="panel-body account-profile">' +
      avatarHtml(u) +
      '<div class="ac-profile-name">' + esc(nameOf(u)) + '</div>' +
      '<div class="ac-profile-user">@' + esc(username || 'local') + '</div>' +
      '<div class="ac-level"><span class="ac-level-badge">Lv.' + g.level + '</span>' +
      '<span class="tiny muted">' + esc(g.levelName || '成长中') + '</span></div>' +
      '<p class="ac-profile-sig">' + esc(sig || '还没有填写个性签名') + '</p>' +
      '<div class="account-stats">' +
      statHtml(state.points, '积分') + statHtml(state.works, '作品') +
      statHtml('Lv.' + g.level, '等级') +
      '</div>' +
      '<div class="ac-actions">' +
      '<a class="btn sm ghost" href="/app/works">我的作品</a>' +
      '<button class="btn sm ghost" id="ac-logout" type="button">退出登录</button>' +
      '</div></div></div>';
  }

  function sexOptions(current) {
    const raw = T.str(current);
    const opts = SEX_OPTIONS.slice();
    if (raw && !opts.some((o) => o[0] === raw)) opts.push([raw, raw]);
    return opts.map((o) => '<option value="' + esc(o[0]) + '"' +
      (o[0] === raw ? ' selected' : '') + '>' + esc(o[1]) + '</option>').join('');
  }

  function profilePanel() {
    const u = state.user || {};
    return '<div class="panel"><div class="panel-head"><b>个人资料</b>' +
      '<span class="tiny muted">昵称与头像会展示在作品、评论等位置</span></div>' +
      '<div class="panel-body">' +
      '<div class="ac-form-grid">' +
      '<div class="field"><label>昵称 <span class="req">*</span></label>' +
      '<input class="input" id="ac-nick" maxlength="32" value="' + esc(nameOf(u)) + '"></div>' +
      '<div class="field"><label>性别</label>' +
      '<select class="select" id="ac-sex">' + sexOptions(T.pick(u, ['gender', 'sex'])) + '</select></div>' +
      '</div>' +
      '<div class="field"><label>个性签名</label>' +
      '<textarea class="textarea" id="ac-signature" maxlength="100" placeholder="写一句话介绍自己">' +
      esc(T.str(T.pick(u, ['signature']))) + '</textarea>' +
      '<span class="hint">最多 100 个字符</span></div>' +
      '<div class="row"><button class="btn primary" id="ac-save-profile" type="button">保存资料</button>' +
      '<span class="tiny muted" id="ac-profile-state"></span></div>' +
      '</div></div>';
  }

  function growthPanel() {
    const g = growthOf(state.user);
    const pct = g.isMaxLevel ? 100 : Math.max(0, Math.min(100, g.progress));
    const nextText = g.isMaxLevel
      ? '已达到最高等级'
      : '距离 Lv.' + (g.level + 1) + ' 还需 '
        + Math.max(0, g.nextLevelExp - g.experience) + ' 经验';
    return '<div class="panel"><div class="panel-head"><b>成长等级</b>' +
      '<span class="tiny muted">发布作品需 Lv.2</span></div>' +
      '<div class="panel-body">' +
      '<div class="row between"><div>' +
      '<b class="ac-growth-title">Lv.' + g.level + ' · ' + esc(g.levelName || '成长中') + '</b>' +
      '<div class="tiny muted">经验 ' + g.experience + ' / ' + g.nextLevelExp + '</div></div>' +
      '<span class="badge ' + (g.canPublish ? 'ok' : '') + '">' +
      (g.canPublish ? '可发布作品' : '未达发布等级') + '</span></div>' +
      '<div class="ac-progress"><i style="width:' + pct + '%"></i></div>' +
      '<div class="tiny muted" style="margin-top:8px">' + nextText +
      (g.canPublish ? '；注册、签到与首次发布都会增加经验。'
        : '；先完成签到积累经验即可解锁发布。') +
      '</div></div></div>';
  }

  function securityPanel() {
    return '<div class="panel"><div class="panel-head"><b>账号安全</b>' +
      '<span class="tiny muted">密码至少 8 个字符</span></div>' +
      '<div class="panel-body">' +
      '<div class="ac-form-grid">' +
      '<div class="field"><label>当前密码 <span class="req">*</span></label>' +
      '<input class="input" type="password" id="ac-old-pass" autocomplete="current-password" placeholder="当前密码"></div>' +
      '<div class="field"><label>新密码 <span class="req">*</span></label>' +
      '<input class="input" type="password" id="ac-new-pass" autocomplete="new-password" placeholder="至少 8 位"></div>' +
      '<div class="field"><label>确认新密码 <span class="req">*</span></label>' +
      '<input class="input" type="password" id="ac-new-pass2" autocomplete="new-password" placeholder="再次输入新密码"></div>' +
      '</div>' +
      '<div class="row"><button class="btn primary" id="ac-save-pass" type="button">修改密码</button>' +
      '<span class="tiny muted" id="ac-pass-state"></span></div>' +
      '</div></div>';
  }

  function infoBodyHtml() {
    const u = state.user || {};
    const username = T.str(T.pick(u, ['userName', 'username']), '—');
    const uid = T.pick(u, ['id', 'userId']);
    const email = T.str(T.pick(u, ['email']));
    return '<div class="panel-body account-rows">' +
      '<div class="ac-row"><span>登录账号</span><span>' + esc(username) + '</span></div>' +
      '<div class="ac-row"><span>用户 ID</span><span>' + esc(uid === undefined || uid === null ? '—' : uid) + '</span></div>' +
      '<div class="ac-row"><span>绑定邮箱</span><span>' + esc(email || '未绑定') + '</span></div>' +
      '<div class="ac-row"><span>注册时间</span><span>' + esc(fmtDate(T.pick(u, ['createdAt', 'created_at']))) + '</span></div>' +
      '<div class="ac-row"><span>登录状态</span><span class="badge ok">已登录</span></div>' +
      '</div>';
  }

  function infoPanel() {
    return '<div class="panel"><div class="panel-head"><b>登录信息</b>' +
      '<span class="tiny muted">当前设备已登录</span></div><div id="ac-info">' +
      infoBodyHtml() + '</div></div>';
  }

  function guestHtml() {
    return '<div class="panel login-box"><div class="panel-body" style="text-align:center">' +
      '<h2 style="margin:0 0 6px;font-size:18px">登录后使用账号中心</h2>' +
      '<p class="muted small" style="margin:0 0 16px">管理个人资料、修改密码，并查看账号信息。</p>' +
      '<div class="row" style="justify-content:center">' +
      '<button class="btn primary" id="ac-login" type="button">登录</button>' +
      '<button class="btn" id="ac-register" type="button">注册账号</button>' +
      '</div></div></div>';
  }

  function render() {
    const root = $('account-root');
    if (!state.user) {
      root.innerHTML = guestHtml();
      const loginBtn = $('ac-login');
      if (loginBtn) loginBtn.addEventListener('click', () => U.loginDialog(() => load()));
      const regBtn = $('ac-register');
      if (regBtn) regBtn.addEventListener('click', () => U.loginDialog(() => load(), 'register'));
      return;
    }
    root.innerHTML = '<div class="account-grid">' +
      '<div id="ac-summary">' + summaryHtml() + '</div>' +
      '<div class="stack">' + profilePanel() + growthPanel() + securityPanel() + infoPanel() + '</div></div>';
    bindSummary();
    bindForm();
  }

  function refreshSummary() {
    const host = $('ac-summary');
    if (!host) { render(); return; }
    host.innerHTML = summaryHtml();
    bindSummary();
  }

  function refreshInfo() {
    const host = $('ac-info');
    if (host) host.innerHTML = infoBodyHtml();
  }

  function applyUser(u) {
    if (u && typeof u === 'object') state.user = u;
    App.store.resetSession();
    U.renderUser();
  }

  function logoutAccount() {
    A.post('/api/gva/base/logout', {}, { timeout: 6000 })
      .catch(() => {})
      .then(() => {
        App.store.resetSession();
        state.user = null;
        U.toast('已退出登录');
        render();
        U.renderUser();
      });
  }

  function bindSummary() {
    const logoutBtn = $('ac-logout');
    if (logoutBtn) {
      logoutBtn.addEventListener('click', () => {
        U.confirmDialog('退出登录', '退出后需要重新登录才能管理作品，确定退出吗？', logoutAccount);
      });
    }

    const avatar = $('ac-avatar');
    const file = $('ac-avatar-file');
    const btn = $('ac-avatar-btn');
    if (!avatar || !file) return;
    const pick = () => file.click();
    if (btn) btn.addEventListener('click', (e) => { e.stopPropagation(); pick(); });
    avatar.addEventListener('click', (e) => { if (e.target !== file) pick(); });
    file.addEventListener('change', uploadAvatar);
  }

  function uploadAvatar() {
    const file = $('ac-avatar-file');
    const f = file && file.files && file.files[0];
    if (!f) return;
    if (!/^image\//.test(f.type)) {
      U.toast('请选择图片文件', false);
      file.value = '';
      return;
    }
    if (f.size > 5 * 1024 * 1024) {
      U.toast('头像大小不能超过 5MB', false);
      file.value = '';
      return;
    }
    U.toast('头像上传中…');
    A.upload('/api/gva/fileUploadAndDownload/upload', f)
      .then((d) => {
        const url = (d && d.file && d.file.url) || (d && d.url) || '';
        if (!url) throw new A.ApiError('上传未返回图片地址');
        return A.put('/api/gva/user/setSelfInfo', { headerImg: url });
      })
      .then((u) => {
        applyUser(u);
        refreshSummary();
        U.toast('头像已更新');
      })
      .catch((err) => U.toast(err.message || '头像上传失败', false))
      .then(() => { file.value = ''; });
  }

  function bindForm() {
    const saveProfile = $('ac-save-profile');
    if (saveProfile) {
      saveProfile.addEventListener('click', () => {
        const nick = ($('ac-nick').value || '').trim();
        if (!nick) { U.toast('昵称不能为空', false); return; }
        saveProfile.disabled = true;
        A.put('/api/gva/user/setSelfInfo', {
          nickName: nick,
          signature: ($('ac-signature').value || '').trim(),
          sex: $('ac-sex').value,
        }).then((u) => {
          applyUser(u);
          refreshSummary();
          refreshInfo();
          U.toast('资料已保存');
        }).catch((err) => U.toast(err.message || '保存失败', false))
          .then(() => { saveProfile.disabled = false; });
      });
    }

    const savePass = $('ac-save-pass');
    if (savePass) {
      savePass.addEventListener('click', () => {
        const oldPass = $('ac-old-pass').value || '';
        const newPass = $('ac-new-pass').value || '';
        const newPass2 = $('ac-new-pass2').value || '';
        if (!oldPass || !newPass) { U.toast('请填写当前密码和新密码', false); return; }
        if (newPass.length < 8) { U.toast('新密码至少 8 个字符', false); return; }
        if (newPass !== newPass2) { U.toast('两次输入的新密码不一致', false); return; }
        savePass.disabled = true;
        A.post('/api/gva/user/changeMyPassword', { password: oldPass, newPassword: newPass })
          .then((d) => {
            const u = d && (d.user || d.userInfo);
            if (u) applyUser(u);
            $('ac-old-pass').value = '';
            $('ac-new-pass').value = '';
            $('ac-new-pass2').value = '';
            U.toast('密码已修改');
          })
          .catch((err) => U.toast(err.message || '修改密码失败', false))
          .then(() => { savePass.disabled = false; });
      });
    }
  }

  function loadExtras() {
    A.get('/api/gva/user/getUserInfo').then((d) => {
      const u = d && (d.userInfo || d.user);
      if (u && typeof u === 'object') {
        state.user = u;
        refreshSummary();
        refreshInfo();
      }
    }).catch(() => {});

    A.get('/api/gva/pointsAcc/getUserPointsAccount').then((d) => {
      state.points = T.num(T.pick(d || {}, ['balance', 'points', 'combinedBalance'], 0));
      refreshSummary();
    }).catch(() => {});

    A.get('/api/gva/works/my', { page: 1, pageSize: 1 }).then((d) => {
      state.works = T.num(T.pick(d || {}, ['total'], 0));
      refreshSummary();
    }).catch(() => {});
  }

  function load() {
    const root = $('account-root');
    root.innerHTML = '<div class="account-grid">' +
      '<div class="skeleton" style="height:280px"></div>' +
      '<div class="stack"><div class="skeleton" style="height:220px"></div>' +
      '<div class="skeleton" style="height:170px"></div></div></div>';
    App.store.session(true).then((s) => {
      if (!s.authenticated) {
        state.user = null;
        render();
        return;
      }
      state.user = s.user || {};
      render();
      loadExtras();
    });
  }

  function bind() {
    U.mountTopbar('account');
    load();
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', bind);
  else bind();
})();
