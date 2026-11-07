/* pages/works.js — 我的作品：列表 / 状态筛选 / 搜索 / 右上角 ⋮ 菜单
   菜单：继续编辑 / 发布(直接·定时) / 复制 / 删除
   数据：GET /api/gva/works/my（需登录） */
(function () {
  'use strict';
  const A = App.api;
  const U = App.ui;
  const T = App.util;
  const $ = (id) => document.getElementById(id);
  const esc = U.esc;

  const state = { works: [], status: '', kw: '', openMenu: null };

  const STATUS = {
    draft: { label: '未发布', cls: 'draft' },
    published: { label: '已发布', cls: 'published' },
    scheduled: { label: '定时发布', cls: 'scheduled' },
  };

  function cidOf(w) {
    return T.str(T.pick(w, ['characterId', 'ID', 'id', 'workID']));
  }

  function statusOf(w) {
    const s = T.str(T.pick(w, ['publishStatus', 'status']), 'draft');
    return s === 'published' || s === 'scheduled' ? s : 'draft';
  }

  function plain(v) {
    return String(v || '').replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ').trim();
  }

  function fmtTime(v) {
    const s = String(v || '');
    const m = s.match(/\d{4}-(\d{2}-\d{2})T(\d{2}:\d{2})/);
    return m ? m[1] + ' ' + m[2] : (s.slice(0, 16) || '—');
  }

  function visible() {
    const kw = state.kw.toLowerCase();
    return state.works.filter((w) => {
      if (state.status && statusOf(w) !== state.status) return false;
      if (!kw) return true;
      const tags = Array.isArray(w.tags) ? w.tags.join(' ') : '';
      return (String(w.name || '') + ' ' + String(w.summary || '') + ' ' + tags)
        .toLowerCase().indexOf(kw) >= 0;
    });
  }

  /* 菜单项图标（文本符号，零依赖） */
  const ICO = { edit: '✎', publish: '➤', copy: '⧉', del: '🗑' };

  function menuHtml(st, cid) {
    // 未发布 → 发布；已发布 → 下架；定时发布 → 取消定时
    const mid = st === 'published'
      ? '<button data-act="unpublish"><span class="ico">↓</span>下架</button>'
      : (st === 'scheduled'
        ? '<button data-act="cancel"><span class="ico">✕</span>取消定时</button>'
        : '<button data-act="publish" class="publish"><span class="ico">' + ICO.publish + '</span>发布</button>');
    return '<button class="wc-more" data-more="' + esc(cid) + '" aria-label="更多操作">⋮</button>' +
      '<div class="wc-menu" data-menu="' + esc(cid) + '" hidden>' +
      '<button data-act="edit"><span class="ico">' + ICO.edit + '</span>继续编辑</button>' +
      mid +
      '<button data-act="copy"><span class="ico">' + ICO.copy + '</span>复制</button>' +
      '<button data-act="del" class="danger"><span class="ico">' + ICO.del + '</span>删除</button>' +
      '</div>';
  }

  function render() {
    const box = $('grid');
    const list = visible();
    $('count').textContent = '共 ' + state.works.length + ' 个作品 · 当前 ' + list.length + ' 个';
    if (!list.length) {
      box.innerHTML = U.emptyHtml(state.works.length ? '没有符合条件的作品' : '还没有作品',
        state.works.length ? '换个筛选条件或关键词试试' : '点右上角「新建作品」开始创作');
      return;
    }
    box.innerHTML = list.map((w, i) => {
      const cid = cidOf(w);
      const name = T.str(T.pick(w, ['name', 'title']), '未命名');
      const cover = T.str(T.pick(w, ['avatarURL', 'coverURL'])) ||
        U.placeholder(name.slice(0, 1), i);
      const summary = plain(T.pick(w, ['summary'])) || '还没有写简介。';
      const tags = (Array.isArray(w.tags) ? w.tags : []).filter(Boolean).slice(0, 3);
      const st = STATUS[statusOf(w)] || STATUS.draft;
      const chatUrl = cid ? '/app/chat?id=' + encodeURIComponent(cid) : '/app/chat';
      return '<div class="work-card">' +
        '<a class="wc-cover" href="' + esc(chatUrl) + '">' +
        '<img src="' + esc(cover) + '" alt="' + esc(name) + '">' +
        '<span class="wc-status ' + st.cls + '">' + st.label + '</span></a>' +
        menuHtml(st.cls, cid) +
        '<div class="wc-body"><b class="wc-name">' + esc(name) + '</b>' +
        '<p class="wc-sum">' + esc(summary) + '</p>' +
        (tags.length ? '<div class="wc-tags">' + tags.map((t) =>
          '<span>' + esc(t) + '</span>').join('') + '</div>' : '') +
        '<div class="wc-time">更新于 ' +
        esc(fmtTime(T.pick(w, ['updatedAt', 'updated_at']))) + '</div>' +
        '</div></div>';
    }).join('');
    Array.prototype.forEach.call(box.querySelectorAll('.wc-cover img'), (img, i) => {
      U.safeImg(img, T.str(T.pick(list[i] || {}, ['name']), '?'), i);
    });
    bindMenus(box);
  }

  /* ---------------------------------------------------------- ⋮ 菜单交互 */
  function closeMenus() {
    Array.prototype.forEach.call(document.querySelectorAll('.wc-menu'), (m) => { m.hidden = true; });
    Array.prototype.forEach.call(document.querySelectorAll('.wc-more.active'),
      (b) => b.classList.remove('active'));
    state.openMenu = null;
  }

  function bindMenus(box) {
    Array.prototype.forEach.call(box.querySelectorAll('.wc-more'), (btn) => {
      btn.addEventListener('click', (e) => {
        e.preventDefault();
        e.stopPropagation();
        const cid = btn.getAttribute('data-more');
        const menu = box.querySelector('.wc-menu[data-menu="' + cid + '"]');
        const isOpen = menu && !menu.hidden;
        closeMenus();
        if (menu && !isOpen) {
          menu.hidden = false;
          btn.classList.add('active');
          state.openMenu = cid;
        }
      });
    });
    Array.prototype.forEach.call(box.querySelectorAll('.wc-menu button'), (item) => {
      item.addEventListener('click', (e) => {
        e.preventDefault();
        e.stopPropagation();
        const menu = item.closest('.wc-menu');
        const cid = menu && menu.getAttribute('data-menu');
        const act = item.getAttribute('data-act');
        closeMenus();
        handleAction(act, cid);
      });
    });
  }

  // 点击卡片其它区域 / 菜单外 → 关闭菜单
  document.addEventListener('click', closeMenus);

  function findWork(cid) {
    return state.works.find((w) => cidOf(w) === cid) || {};
  }

  function handleAction(act, cid) {
    if (act === 'edit') {                       // 继续编辑 → 跳创作页
      location.href = '/app/creation?id=' + encodeURIComponent(cid);
      return;
    }
    if (act === 'publish') return openPublish(cid);
    if (act === 'unpublish') return setStatus(cid, 'unpublish', '已下架');
    if (act === 'cancel') return setStatus(cid, 'cancel', '已取消定时');
    if (act === 'copy') return copyWork(cid);
    if (act === 'del') return deleteWork(cid);
  }

  /* ---------------------------------------------------------- 状态操作 */
  function setStatus(cid, kind, okMsg) {
    const url = { unpublish: '/api/gva/work/unpublish', cancel: '/api/gva/work/schedule/cancel' }[kind];
    A.post(url, { characterId: cid })
      .then(() => { U.toast(okMsg); load(); })
      .catch((err) => U.toast('操作失败：' + err.message, false));
  }

  function deleteWork(cid) {
    const w = findWork(cid);
    const name = T.str(T.pick(w, ['name', 'title']), '该作品');
    U.confirmDialog('删除作品', '确定删除「' + name + '」吗？删除后不可恢复。', () => {
      A.del('/api/gva/work', { characterId: cid })
        .then(() => { U.toast('已删除「' + name + '」'); load(); })
        .catch((err) => U.toast('删除失败：' + err.message, false));
    });
  }

  function copyWork(cid) {
    A.get('/api/gva/work/my', { characterId: cid })
      .then((data) => {
        const w = (data && typeof data === 'object') ? Object.assign({}, data) : {};
        // 去掉身份字段，让服务端生成新的 characterId
        ['characterId', 'ID', 'id', 'workID', 'publishStatus',
          'status', 'scheduledPublishAt', 'updatedAt', 'createdAt'].forEach((k) => delete w[k]);
        w.name = T.str(T.pick(data, ['name', 'title']), '未命名') + ' 副本';
        return A.post('/api/gva/work', w);
      })
      .then(() => { U.toast('已复制为新作品'); load(); })
      .catch((err) => U.toast('复制失败：' + err.message, false));
  }

  /* ---------------------------------------------------------- 发布（直接 / 定时） */
  function openPublish(cid) {
    const w = findWork(cid);
    const name = T.str(T.pick(w, ['name', 'title']), '该作品');
    const html =
      '<div class="field"><label>发布方式</label>' +
      '<div class="row">' +
      '<label class="check"><input type="radio" name="pub-mode" value="now" checked> 立即发布</label>' +
      '<label class="check"><input type="radio" name="pub-mode" value="later"> 定时发布</label>' +
      '</div></div>' +
      '<div class="field" id="pub-when" hidden><label>定时发布时间</label>' +
      '<input class="input" type="datetime-local" id="pub-time">' +
      '<span class="hint">到达设定时间后自动发布</span></div>' +
      '<p class="muted small" style="margin:0">《' + esc(name) + '》将被发布到公开作品库。</p>';
    const m = U.openModal({
      title: '发布作品',
      html,
      footer: '<button class="btn" data-close>取消</button>' +
        '<button class="btn primary" id="pub-ok">确认发布</button>',
      onMount(root) {
        const when = root.querySelector('#pub-when');
        Array.prototype.forEach.call(root.querySelectorAll('input[name="pub-mode"]'), (r) => {
          r.addEventListener('change', () => {
            when.hidden = root.querySelector('input[name="pub-mode"]:checked').value !== 'later';
          });
        });
        root.querySelector('#pub-ok').addEventListener('click', () => {
          const mode = root.querySelector('input[name="pub-mode"]:checked').value;
          if (mode === 'later') {
            const val = root.querySelector('#pub-time').value;
            if (!val) { U.toast('请选择定时发布时间', false); return; }
            U.closeModal();
            A.post('/api/gva/work/schedule',
              { characterId: cid, scheduledTime: new Date(val).toISOString() })
              .then(() => { U.toast('已设置定时发布'); load(); })
              .catch((err) => U.toast('定时发布失败：' + err.message, false));
            return;
          }
          U.closeModal();
          A.post('/api/gva/work/publish', { characterId: cid })
            .then((data) => {
              const gain = data && data.experienceGained;
              U.toast(gain ? '已发布，+' + gain + ' 经验' : '已发布');
              load();
            })
            .catch((err) => U.toast('发布失败：' + err.message, false));
        });
      },
    });
    return m;
  }

  /* ---------------------------------------------------------- 数据加载 */
  function load() {
    $('grid').innerHTML = new Array(3).fill('<div class="skeleton" style="height:240px"></div>').join('');
    A.get('/api/gva/works/my', { page: 1, pageSize: 100 })
      .then((data) => {
        state.works = T.asList(data, ['list', 'records']);
        render();
      })
      .catch((err) => {
        state.works = [];
        const box = $('grid');
        if (err.status === 401) {
          box.innerHTML = '<div class="empty"><b>尚未登录</b>登录后可以管理你的作品<br><br>' +
            '<button class="btn sm primary" id="login-btn">登录</button></div>';
          $('login-btn').addEventListener('click', () => U.loginDialog(() => load()));
          return;
        }
        box.innerHTML = U.emptyHtml('作品加载失败', err.message || '');
      });
  }

  function bind() {
    U.mountTopbar('works');
    $('kw').addEventListener('input', (e) => { state.kw = e.target.value.trim(); render(); });
    $('filters').addEventListener('click', (e) => {
      const btn = e.target.closest('[data-status]');
      if (!btn) return;
      state.status = btn.getAttribute('data-status');
      Array.prototype.forEach.call($('filters').querySelectorAll('button'),
        (x) => x.classList.toggle('active', x === btn));
      render();
    });
    load();
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', bind);
  else bind();
})();
