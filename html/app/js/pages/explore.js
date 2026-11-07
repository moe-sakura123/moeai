/* pages/explore.js — 探索页：本地已发布角色卡片（搜索 / 排序 / 标签 / 分页）
   数据：GET /api/app/explore（公开，读本地内容库 works 表） */
(function () {
  'use strict';
  const A = App.api;
  const U = App.ui;
  const T = App.util;
  const $ = (id) => document.getElementById(id);
  const esc = U.esc;

  const state = {
    page: 0, pageSize: 24, sort: 'latest', q: '', tag: '',
    items: [], total: 0, tags: [], loading: false,
  };

  function plain(v) {
    return String(v || '').replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ').trim();
  }

  function tagName(t) {
    return T.isObj(t) ? T.str(T.pick(t, ['tag', 'name'])) : T.str(t);
  }

  /* ---------------------------------------------------------- 标签 */
  function renderTags() {
    const box = $('tags');
    if (!box) return;
    let list = state.tags.map(tagName).filter(Boolean);
    if (!list.length) {
      // 标签库为空时（作品刚发布还没同步标签），从当前卡片里汇总
      const seen = [];
      state.items.forEach((it) => {
        (Array.isArray(it.tags) ? it.tags : []).forEach((t) => {
          if (t && seen.indexOf(t) < 0) seen.push(t);
        });
      });
      list = seen.slice(0, 24);
    }
    box.innerHTML = list.map((t) =>
      '<button type="button" class="explore-tag' + (t === state.tag ? ' on' : '') +
      '" data-tag="' + esc(t) + '">' + esc(t) + '</button>').join('');
  }

  /* ---------------------------------------------------------- 卡片 */
  function renderGrid() {
    const box = $('grid');
    if (!box) return;
    if (!state.items.length) {
      box.innerHTML = U.emptyHtml('还没有已发布的角色', '到创作页保存并发布作品后，会出现在这里');
      return;
    }
    box.innerHTML = state.items.map((it, i) => {
      const cid = T.str(T.pick(it, ['characterId', 'id']));
      const name = T.str(T.pick(it, ['name', 'title']), '未命名');
      const summary = plain(T.pick(it, ['summary', 'description'])) || '这个角色还没有写简介。';
      const cover = T.str(T.pick(it, ['cover', 'coverURL', 'avatarURL'])) ||
        U.placeholder(name.slice(0, 1), i);
      const author = T.str(T.pick(it, ['author', 'authorName']), '匿名');
      const rate = Number(T.pick(it, ['rate'])) || 0;
      const votes = Number(T.pick(it, ['votes'])) || 0;
      const tags = (Array.isArray(it.tags) ? it.tags : []).filter(Boolean).slice(0, 3);
      return '<a class="explore-card" href="' +
        (cid ? '/app/chat?id=' + encodeURIComponent(cid) + '&new=1' : '#') + '">' +
        '<div class="ec-cover"><img src="' + esc(cover) + '" alt="' + esc(name) + '">' +
        '<span class="ec-badge">已发布</span></div>' +
        '<div class="ec-body"><b class="ec-name">' + esc(name) + '</b>' +
        '<p class="ec-sum">' + esc(summary) + '</p>' +
        (tags.length ? '<div class="ec-tags">' + tags.map((t) =>
          '<span>' + esc(t) + '</span>').join('') + '</div>' : '') +
        '<div class="ec-meta"><span>' + esc(author) + '</span>' +
        '<span>★ ' + (rate ? rate.toFixed(1) : '—') + '</span>' +
        '<span>♥ ' + votes + '</span></div></div></a>';
    }).join('');
    Array.prototype.forEach.call(box.querySelectorAll('.ec-cover img'), (img, i) => {
      U.safeImg(img, T.str(T.pick(state.items[i] || {}, ['name']), '?'), i);
    });
  }

  /* ---------------------------------------------------------- 列表 */
  function load(reset) {
    if (state.loading) return;
    state.loading = true;
    const grid = $('grid');
    if (reset) {
      state.page = 0;
      state.items = [];
      $('btn-more').hidden = true;
      if (grid) {
        grid.innerHTML = new Array(6).fill('<div class="skeleton" style="height:260px"></div>').join('');
      }
    }
    $('btn-more').disabled = true;
    A.get('/api/app/explore', {
      limit: state.pageSize,
      offset: state.page * state.pageSize,
      sort: state.sort,
      q: state.q,
      tag: state.tag,
    })
      .then((data) => {
        const list = T.asList(data, ['list']);
        state.items = reset ? list : state.items.concat(list);
        state.page += 1;
        const total = Number(T.pick(data, ['total'])) || state.items.length;
        state.total = total;
        const tags = T.asList(data, ['tags']);
        if (tags.length) state.tags = tags;
        renderTags();
        renderGrid();
        $('count').textContent = state.total + ' 个角色';
        $('btn-more').hidden = state.items.length >= state.total || !list.length;
      })
      .catch((err) => {
        if (reset && grid) grid.innerHTML = U.emptyHtml('加载失败', err.message || '');
      })
      .then(() => {
        state.loading = false;
        $('btn-more').disabled = false;
      });
  }

  function bind() {
    U.mountTopbar('home');
    $('btn-search').addEventListener('click', () => {
      state.q = $('q').value.trim();
      state.tag = '';
      load(true);
    });
    $('q').addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        state.q = e.target.value.trim();
        state.tag = '';
        load(true);
      }
    });
    $('sorts').addEventListener('click', (e) => {
      const btn = e.target.closest('[data-sort]');
      if (!btn) return;
      state.sort = btn.getAttribute('data-sort');
      Array.prototype.forEach.call($('sorts').querySelectorAll('button'),
        (x) => x.classList.toggle('active', x === btn));
      load(true);
    });
    $('tags').addEventListener('click', (e) => {
      const btn = e.target.closest('[data-tag]');
      if (!btn) return;
      const t = btn.getAttribute('data-tag');
      state.tag = state.tag === t ? '' : t;
      load(true);
    });
    $('btn-more').addEventListener('click', () => load(false));
    load(true);
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', bind);
  else bind();
})();
