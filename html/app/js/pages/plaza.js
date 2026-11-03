/* pages/plaza.js — 创意广场：分区 / 排序 / 搜索 / 卡片网格 / 详情弹层
   接口：GET /api/gva/creativePlaza/items，详情 /items/{id}，试用 /trial，购买 /purchase，源码 /prompt */
(function () {
  'use strict';
  const A = App.api;
  const U = App.ui;
  const T = App.util;
  const $ = (id) => document.getElementById(id);
  const esc = U.esc;
  const BASE = '/api/gva/creativePlaza';

  const TYPE_LABEL = {
    speaking_style: '写作风格',
    break_armor: '破甲词',
    ui_theme: '界面换肤',
    skin_css: '皮肤 CSS',
    reply_example: '回复示例',
    custom_config: '自定义配置',
  };

  const state = {
    page: 1,
    pageSize: 24,
    itemType: '',
    sortBy: 'latest',
    keyword: '',
    items: [],
    total: 0,
    hasMore: false,
    loading: false,
  };

  function plainText(v) {
    return String(v || '').replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ').trim();
  }

  function typeLabel(it) {
    const t = T.str(T.pick(it, ['itemType', 'item_type']), '');
    return TYPE_LABEL[t] || t || '创意';
  }

  function priceText(it) {
    const p = T.num(T.pick(it, ['price'], 0));
    return p > 0 ? p + ' 积分' : '免费';
  }

  /* ---------------------------------------------------------- 列表 */
  function load(reset) {
    if (state.loading) return;
    state.loading = true;
    if (reset) {
      state.page = 1;
      state.items = [];
      $('grid').innerHTML = '<div class="skeleton" style="height:180px"></div>' +
        '<div class="skeleton" style="height:180px"></div>' +
        '<div class="skeleton" style="height:180px"></div>';
      $('btn-more').hidden = true;
    } else {
      $('btn-more').disabled = true;
    }
    A.get(BASE + '/items', {
      page: state.page,
      pageSize: state.pageSize,
      itemType: state.itemType,
      keyword: state.keyword,
      sortBy: state.sortBy,
    }, { timeout: 10000 })
      .then((data) => {
        const list = T.asList(data, ['items', 'list']).filter((x) => T.isObj(x));
        state.total = T.num(T.pick(data, ['total'], list.length));
        state.hasMore = Boolean(T.pick(data, ['hasMore'], false));
        state.items = reset ? list : state.items.concat(list);
        render();
        if (state.hasMore) state.page += 1;
      })
      .catch((err) => {
        if (reset) $('grid').innerHTML = U.emptyHtml('创意广场暂时打不开', err.message || '请稍后重试');
        U.toast(err.message || '加载失败', false);
      })
      .then(() => {
        state.loading = false;
        $('btn-more').disabled = false;
      });
  }

  function render() {
    const grid = $('grid');
    const count = $('count');
    count.textContent = state.total
      ? '共 ' + state.total + ' 个创意，当前显示 ' + state.items.length + ' 个'
      : '暂无创意';
    if (!state.items.length) {
      grid.innerHTML = U.emptyHtml(
        '暂无创意商品',
        '当前分区还没有内容，试试换个分区或关键词；本地库未同步时也会是空列表');
      $('btn-more').hidden = true;
      return;
    }
    grid.innerHTML = state.items.map((it, i) => cardHtml(it, i)).join('');
    grid.querySelectorAll('img[data-fallback]').forEach((img, i) => {
      U.safeImg(img, img.getAttribute('data-fallback') || '创', i + 1);
    });
    $('btn-more').hidden = !state.hasMore;
  }

  function cardHtml(it, idx) {
    const id = T.pick(it, ['id', 'ID'], '');
    const title = T.str(T.pick(it, ['title', 'name']), '未命名创意');
    const desc = plainText(T.pick(it, ['description', 'summary', 'intro'])) || '暂无简介';
    const tags = Array.isArray(it.tags) ? it.tags.slice(0, 3) : [];
    const sales = T.num(T.pick(it, ['salesCount', 'sales_count'], 0));
    const rate = T.num(T.pick(it, ['approvalRate'], 0));
    const author = T.str(T.pick(it, ['authorName', 'author_name']), '匿名');
    const avatar = T.str(T.pick(it, ['authorAvatar', 'coverURL', 'coverUrl'], ''));
    const cover = avatar || U.placeholder(title.slice(0, 1), idx + 1);
    const badges = [];
    if (T.pick(it, ['isOpenSource'], false)) badges.push('<span class="badge ok">开源</span>');
    if (T.pick(it, ['allowTrial'], false)) badges.push('<span class="badge warn">可试用</span>');
    return '<article class="card" data-id="' + esc(id) + '" style="cursor:pointer">' +
      '<img class="cover" alt="" src="' + esc(cover) + '" data-fallback="' + esc(title.slice(0, 1)) + '">' +
      '<div class="body">' +
      '<b title="' + esc(title) + '">' + esc(title) + '</b>' +
      '<div class="row" style="gap:4px;margin-top:5px">' +
      '<span class="tag plain">' + esc(typeLabel(it)) + '</span>' + badges.join('') + '</div>' +
      '<p>' + esc(desc) + '</p>' +
      '<div class="row" style="gap:4px">' +
      tags.map((t) => '<span class="tag">' + esc(t) + '</span>').join('') +
      '</div>' +
      '<div class="foot"><span>' + esc(author) + ' · 销量 ' + sales +
      (rate ? ' · 好评 ' + Math.round(rate * 100) / 100 : '') + '</span>' +
      '<span>' + esc(priceText(it)) + '</span></div>' +
      '</div></article>';
  }

  /* ---------------------------------------------------------- 详情 */
  function openDetail(id) {
    const m = U.openModal({
      title: '创意详情',
      html: '<div class="loading-line">加载中…</div>',
      footer: '<button class="btn" data-close>关闭</button>',
    });
    if (!m) return;
    const body = m.querySelector('.modal-body');
    A.get(BASE + '/items/' + encodeURIComponent(id), null, { timeout: 10000 })
      .then((data) => {
        const it = T.isObj(data) && T.isObj(data.item) ? data.item : (T.isObj(data) ? data : {});
        if (!T.pick(it, ['id', 'ID'])) {
          body.innerHTML = U.emptyHtml('商品不存在', '可能已下架或本地库未同步该商品');
          return;
        }
        const title = T.str(T.pick(it, ['title', 'name']), '未命名创意');
        const desc = String(T.pick(it, ['description', 'summary'], '')).trim();
        const tags = Array.isArray(it.tags) ? it.tags : [];
        const sales = T.num(T.pick(it, ['salesCount', 'sales_count'], 0));
        const like = T.num(T.pick(it, ['likeCount'], 0));
        const rate = T.num(T.pick(it, ['approvalRate'], 0));
        const author = T.str(T.pick(it, ['authorName', 'author_name']), '匿名');
        const bound = T.str(T.pick(it, ['boundWorkName', 'boundWorkId'], ''));
        body.innerHTML =
          '<div class="row between" style="align-items:flex-start">' +
          '<div class="grow"><b style="font-size:15px">' + esc(title) + '</b>' +
          '<div class="row" style="gap:4px;margin-top:6px">' +
          '<span class="tag plain">' + esc(typeLabel(it)) + '</span>' +
          (T.pick(it, ['isOpenSource'], false) ? '<span class="badge ok">开源</span>' : '') +
          (T.pick(it, ['allowTrial'], false) ? '<span class="badge warn">可试用</span>' : '') +
          '</div></div>' +
          '<span class="badge">' + esc(priceText(it)) + '</span></div>' +
          '<p class="small muted" style="margin:10px 0;white-space:pre-wrap">' +
          esc(plainText(desc) || '暂无简介') + '</p>' +
          '<div class="row" style="gap:12px;font-size:12px;color:var(--muted)">' +
          '<span>作者：' + esc(author) + '</span>' +
          '<span>销量：' + sales + '</span>' +
          '<span>点赞：' + like + '</span>' +
          '<span>好评：' + rate + '</span>' +
          (bound ? '<span>绑定作品：' + esc(bound) + '</span>' : '') +
          '</div>' +
          (tags.length ? '<div class="row" style="gap:4px;margin-top:10px">' +
            tags.map((t) => '<span class="tag">' + esc(t) + '</span>').join('') + '</div>' : '') +
          '<div class="row" style="gap:8px;margin-top:16px">' +
          (T.pick(it, ['allowTrial'], false) ? '<button class="btn sm" id="dt-trial">试用</button>' : '') +
          (T.num(T.pick(it, ['price'], 0)) > 0 ? '<button class="btn sm primary" id="dt-buy">购买</button>' : '') +
          '<button class="btn sm ghost" id="dt-source">查看源码</button>' +
          '</div>';
        wireDetail(body, it, id, title);
      })
      .catch((err) => {
        body.innerHTML = U.emptyHtml('详情加载失败', err.message || '请稍后重试');
      });
  }

  function wireDetail(body, it, id, title) {
    const trial = body.querySelector('#dt-trial');
    if (trial) trial.addEventListener('click', () => {
      U.requireLogin().then((ok) => {
        if (!ok) return;
        A.post(BASE + '/items/' + id + '/trial', {}).then(() => {
          U.toast('已开始试用「' + title + '」');
        }).catch((err) => U.toast(err.message || '试用失败', false));
      });
    });

    const buy = body.querySelector('#dt-buy');
    if (buy) buy.addEventListener('click', () => {
      U.requireLogin().then((ok) => {
        if (!ok) return;
        U.confirmDialog('购买创意',
          '将花费 ' + T.num(T.pick(it, ['price'], 0)) + ' 积分购买「' + title + '」，确定？',
          () => {
            A.post(BASE + '/items/' + id + '/purchase', {
              idempotencyKey: 'app-' + Date.now() + '-' + Math.random().toString(36).slice(2, 8),
            }).then(() => {
              U.toast('购买成功');
              load(true);
            }).catch((err) => U.toast(err.message || '购买失败', false));
          });
      });
    });

    const src = body.querySelector('#dt-source');
    if (src) src.addEventListener('click', () => {
      A.get(BASE + '/items/' + id + '/prompt', null, { timeout: 10000 })
        .then((data) => {
          const text = T.str(T.pick(data, ['promptContent', 'content', 'prompt'], ''));
          U.openModal({
            title: '源码 · ' + title,
            html: '<pre style="margin:0;white-space:pre-wrap;word-break:break-word;font-size:12px;line-height:1.7">' +
              esc(text || '该商品没有可查看的源码内容') + '</pre>',
          });
        })
        .catch((err) => U.toast(err.message || '源码不可用', false));
    });
  }

  /* ---------------------------------------------------------- 事件 */
  $('zones').addEventListener('click', (e) => {
    const btn = e.target.closest('button[data-type]');
    if (!btn) return;
    const type = btn.getAttribute('data-type') || '';
    if (type === state.itemType) return;
    state.itemType = type;
    $('zones').querySelectorAll('button').forEach((b) => b.classList.remove('active'));
    btn.classList.add('active');
    load(true);
  });

  $('sort').addEventListener('change', (e) => {
    state.sortBy = e.target.value || 'latest';
    load(true);
  });

  $('btn-search').addEventListener('click', () => {
    state.keyword = ($('kw').value || '').trim();
    load(true);
  });

  $('kw').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      state.keyword = (e.target.value || '').trim();
      load(true);
    }
  });

  $('btn-more').addEventListener('click', () => load(false));

  $('grid').addEventListener('click', (e) => {
    const card = e.target.closest('.card');
    if (!card) return;
    const id = card.getAttribute('data-id');
    if (id) openDetail(id);
  });

  U.mountTopbar('plaza');
  load(true);
})();
