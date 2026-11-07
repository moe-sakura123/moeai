/* pages/worldbook.js — 世界书条目管理（角色条目 / 自定义条目） */
(function () {
  'use strict';
  const A = App.api;
  const U = App.ui;
  const S = App.store;
  const T = App.util;
  const $ = (id) => document.getElementById(id);
  const esc = U.esc;

  const state = {
    works: [],
    cid: '',
    kind: 'char',
    entries: [],
    current: null,
  };

  const EP = '/api/gva/narratiumWorldbookEntry/';

  function cidOf(w) {
    return T.str(T.pick(w, ['characterId', 'ID', 'id', 'workID']));
  }
  function entryId(e) {
    if (!e) return '';
    return T.str(T.pick(e, ['worldbookID', 'ID', 'id']));
  }
  function entryTitle(e) {
    return T.str(T.pick(e, ['comment', 'title', 'name']), '未命名条目');
  }
  function keysOf(e) {
    if (!e) return [];
    if (Array.isArray(e.keys)) return e.keys.filter(Boolean);
    if (T.isObj(e.keys) && Array.isArray(e.keys.keys)) return e.keys.keys.filter(Boolean);
    if (typeof e.keys === 'string') return e.keys.split(/[,，]/).map((s) => s.trim()).filter(Boolean);
    if (Array.isArray(e.keywords)) return e.keywords;
    return [];
  }

  /* ---------------------------------------------------------- 作品 */
  function loadWorks() {
    A.get('/api/gva/works/my', { page: 1, pageSize: 100 })
      .then((data) => {
        state.works = T.asList(data, ['list', 'records']);
        const sel = $('wb-work');
        if (!state.works.length) {
          sel.innerHTML = '<option value="">还没有作品，先去创作页新建</option>';
          sel.disabled = true;
          $('wb-list').innerHTML = U.emptyHtml('还没有作品', '先到创作页保存一个角色，再来写世界书');
          state.cid = '';
          loadRules();
          return;
        }
        sel.disabled = false;
        sel.innerHTML = state.works.map((w) => {
          const cid = cidOf(w);
          return '<option value="' + esc(cid) + '">' + esc(T.str(T.pick(w, ['name', 'title']), '未命名')) + '</option>';
        }).join('');
        const want = new URLSearchParams(location.search).get('cid') || S.lastWork();
        state.cid = (want && state.works.some((w) => cidOf(w) === want)) ? want : cidOf(state.works[0]);
        sel.value = state.cid;
        $('to-creation').href = '/app/creation' + (state.cid ? ('?id=' + encodeURIComponent(state.cid)) : '');
        loadEntries();
        loadRules();
      })
      .catch((err) => {
        $('wb-list').innerHTML = err.status === 401
          ? '<div class="empty"><b>尚未登录</b>登录后才能管理世界书<br><br><button class="btn sm primary" id="wb-login">登录</button></div>'
          : U.emptyHtml('作品列表加载失败', err.message);
        const btn = $('wb-login');
        if (btn) btn.addEventListener('click', () => U.loginDialog(() => loadWorks()));
      });
  }

  /* ---------------------------------------------------------- 条目列表 */
  function loadEntries(selectId) {
    if (!state.cid) { state.entries = []; renderList(); showEditor(null); return; }
    const custom = state.kind === 'custom';
    const path = EP + (custom ? 'getUserCustomEntries' : 'getEntriesByCharacterID');
    $('wb-list').innerHTML = '<div class="loading-line">加载中…</div>';
    A.get(path, { characterId: state.cid })
      .then((data) => {
        state.entries = Array.isArray(data) ? data : T.asList(data, ['list', 'records']);
        renderList();
        const want = selectId || entryId(state.current);
        const hit = want ? state.entries.find((e) => entryId(e) === want) : null;
        if (hit) showEditor(hit);
        else if (state.entries.length) showEditor(state.entries[0]);
        else showEditor(null);
      })
      .catch((err) => {
        state.entries = [];
        renderList();
        showEditor(null);
        U.toast('条目加载失败：' + err.message, false);
      });
  }

  function renderList() {
    const box = $('wb-list');
    $('wb-count').textContent = state.entries.length ? state.entries.length + ' 条' : '';
    if (!state.entries.length) {
      box.innerHTML = U.emptyHtml('还没有条目', '点右上角「新建条目」');
      return;
    }
    box.innerHTML = state.entries.map((e, i) => {
      const id = entryId(e);
      const on = !e.disable;
      const keys = keysOf(e).slice(0, 3);
      const cur = state.current && entryId(state.current) === id;
      return '<div class="work-item' + (cur ? ' active' : '') + '" data-id="' + esc(id) + '" data-i="' + i + '">' +
        '<div class="meta"><b>' + esc(entryTitle(e)) +
        (e.constant ? ' <span class="badge ok">常驻</span>' : '') +
        (on ? '' : ' <span class="badge">停用</span>') + '</b>' +
        '<small>' + esc(keys.length ? keys.join(' / ') : '无关键词') + '</small></div></div>';
    }).join('');
    Array.prototype.forEach.call(box.querySelectorAll('.work-item'), (item) => {
      item.addEventListener('click', () => {
        const i = Number(item.getAttribute('data-i'));
        const hit = state.entries[i];
        if (hit) showEditor(hit);
      });
    });
  }

  /* ---------------------------------------------------------- 编辑器 */
  function showEditor(entry) {
    state.current = entry || null;
    const has = !!entry;
    $('editor-empty').hidden = has;
    $('editor-form').hidden = !has;
    if (!has) { $('editor-title').textContent = '条目编辑'; return; }
    $('editor-title').textContent = state.kind === 'custom' ? '编辑自定义条目' : '编辑角色条目';
    $('e-comment').value = T.str(T.pick(entry, ['comment', 'title']));
    $('e-keys').value = keysOf(entry).join(', ');
    $('e-content').value = T.str(entry.content);
    $('e-constant').checked = !!entry.constant;
    $('e-enabled').checked = !entry.disable;
    const pos = entry.position !== undefined ? Number(entry.position) : 0;
    $('e-position').value = Number.isFinite(pos) && pos === 1 ? '1' : '0';
    $('e-depth').value = String(T.num(entry.depth, 4));
    $('e-probability').value = String(T.num(entry.probability, 100));
    $('e-order').value = String(T.num(entry.order, 0));
    $('editor-state').textContent = '已保存';
    $('editor-state').className = 'tiny muted';
    renderList();
  }

  function newEntry() {
    if (!state.cid) { U.toast('请先选择作品', false); return; }
    state.current = null;
    $('editor-empty').hidden = true;
    $('editor-form').hidden = false;
    $('editor-title').textContent = state.kind === 'custom' ? '新建自定义条目' : '新建角色条目';
    $('e-comment').value = '';
    $('e-keys').value = '';
    $('e-content').value = '';
    $('e-constant').checked = false;
    $('e-enabled').checked = true;
    $('e-position').value = '0';
    $('e-depth').value = '4';
    $('e-probability').value = '100';
    $('e-order').value = '0';
    $('editor-state').textContent = '新条目（未保存）';
    $('editor-state').className = 'tiny badge warn';
    Array.prototype.forEach.call($('wb-list').querySelectorAll('.work-item'), (el) => el.classList.remove('active'));
    $('e-comment').focus();
  }

  function collect() {
    return {
      characterId: state.cid,
      comment: ($('e-comment').value || '').trim(),
      keys: ($('e-keys').value || '').split(/[,，]/).map((s) => s.trim()).filter(Boolean),
      content: ($('e-content').value || '').trim(),
      constant: $('e-constant').checked,
      disable: !$('e-enabled').checked,
      position: Number($('e-position').value) === 1 ? 1 : 0,
      depth: T.num($('e-depth').value, 4),
      probability: Math.max(0, Math.min(100, T.num($('e-probability').value, 100))),
      order: T.num($('e-order').value, 0),
      useProbability: true,
      selective: false,
      caseSensitive: false,
    };
  }

  function save() {
    if (!state.cid) { U.toast('请先选择作品', false); return; }
    const body = collect();
    if (!body.comment) { U.toast('请先填写条目标题', false); $('e-comment').focus(); return; }
    const custom = state.kind === 'custom';
    const id = entryId(state.current);
    const btn = $('btn-save');
    btn.disabled = true;
    const req = custom
      ? A.post(EP + (id ? 'updateUserCustomEntry' : 'createUserCustomEntry'),
        id ? Object.assign({ id }, body) : body)
      : A.post(EP + (id ? 'updateNarratiumWorldbookEntry' : 'createNarratiumWorldbookEntry'),
        id ? Object.assign({ worldbookID: id }, body) : body);
    req.then((data) => {
      const saved = T.isObj(data) ? Object.assign({}, state.current || {}, data) : Object.assign({}, state.current || {}, body);
      state.current = saved;
      U.toast('条目已保存');
      loadEntries(entryId(saved) || id);
    }).catch((err) => {
      if (err.status === 401) U.loginDialog(() => save());
      else U.toast('保存失败：' + err.message, false);
    }).then(() => { btn.disabled = false; });
  }

  function remove() {
    const id = entryId(state.current);
    if (!id) { U.toast('这条还没有保存', false); return; }
    U.confirmDialog('删除条目', '删除后无法恢复，确认删除「' + entryTitle(state.current) + '」？', () => {
      const custom = state.kind === 'custom';
      A.post(EP + (custom ? 'deleteUserCustomEntry' : 'deleteNarratiumWorldbookEntry'),
        custom ? { id } : { worldbookID: id })
        .then(() => {
          U.toast('已删除');
          state.current = null;
          loadEntries();
        })
        .catch((err) => U.toast('删除失败：' + err.message, false));
    });
  }

  function markDirty() {
    $('editor-state').textContent = '未保存的修改';
    $('editor-state').className = 'tiny badge warn';
  }

  /* ---------------------------------------------------------- 绑定 / 启动 */
  function bind() {
    $('wb-work').addEventListener('change', () => {
      state.cid = $('wb-work').value;
      S.setLastWork(state.cid);
      $('to-creation').href = '/app/creation?id=' + encodeURIComponent(state.cid);
      state.current = null;
      loadEntries();
      loadRules();
    });
    Array.prototype.forEach.call($('wb-tabs').querySelectorAll('button'), (b) => {
      b.addEventListener('click', () => {
        Array.prototype.forEach.call($('wb-tabs').querySelectorAll('button'), (x) => x.classList.remove('active'));
        b.classList.add('active');
        state.kind = b.getAttribute('data-kind');
        $('wb-tip').textContent = state.kind === 'custom' ? '仅自己可见的补充设定' : '关键词命中才触发';
        state.current = null;
        loadEntries();
      });
    });
    $('btn-new').addEventListener('click', newEntry);
    $('wb-rule-tabs').addEventListener('click', (e) => {
      const btn = e.target.closest('button[data-stat]');
      if (!btn) return;
      ruleState.stat = btn.getAttribute('data-stat');
      ruleState.sel = '';
      Array.prototype.forEach.call($('wb-rule-tabs').querySelectorAll('button'),
        (x) => x.classList.toggle('active', x === btn));
      renderRules();
    });
    $('wb-rule-list').addEventListener('click', (e) => {
      const row = e.target.closest('.rule-row');
      if (row) selectRule(row.getAttribute('data-range'));
    });
    $('wb-rule-new').addEventListener('click', () => openRuleDialog('new'));
    $('wb-rule-edit').addEventListener('click', () => openRuleDialog('edit'));
    $('wb-rule-del').addEventListener('click', removeRule);
    $('wb-rule-export').addEventListener('click', exportRules);
    $('wb-rule-meta-save').addEventListener('click', saveRuleMeta);
    $('wb-rule-scope').addEventListener('change', () => {
      const want = $('wb-rule-scope').value;
      if (want === ruleState.scope) return;
      setRuleScope(want);
    });
    $('btn-save').addEventListener('click', save);
    $('btn-delete').addEventListener('click', remove);
    $('btn-clear').addEventListener('click', () => {
      $('e-comment').value = '';
      $('e-keys').value = '';
      $('e-content').value = '';
      markDirty();
    });
    ['e-comment', 'e-keys', 'e-content', 'e-constant', 'e-enabled', 'e-position', 'e-depth', 'e-probability', 'e-order']
      .forEach((id) => {
        const el = $(id);
        if (!el) return;
        el.addEventListener('input', markDirty);
        el.addEventListener('change', markDirty);
      });
  }

  /* ---------------------------------------------------------- 平台数值规则 */
  const RULE_EP = '/api/gva/narratiumStatRules/';
  const ruleState = {
    stats: {}, keywords: {}, meta: {}, stat: '', sel: '',
    extraRanges: {}, scope: 'global', cid: '',
  };

  function loadRules() {
    ruleState.cid = state.cid || '';
    A.get(RULE_EP, { characterId: ruleState.cid })
      .then((data) => {
        ruleState.sel = '';
        applyRuleData(data);
        renderRuleTabs();
        renderRules();
      })
      .catch((err) => {
        $('wb-rule-list').innerHTML = U.emptyHtml('规则加载失败', err.message);
      });
  }

  function applyRuleData(data) {
    const d = data || {};
    ruleState.stats = d.stats || {};
    ruleState.keywords = d.keywords || {};
    ruleState.meta = d.meta || {};
    ruleState.scope = d.scope === 'custom' ? 'custom' : 'global';
    const scopeSel = $('wb-rule-scope');
    if (scopeSel) {
      scopeSel.value = ruleState.scope;
      scopeSel.disabled = !state.cid;
    }
    const tip = $('wb-rule-scope-tip');
    if (tip) {
      tip.textContent = ruleState.scope === 'custom'
        ? '本角色专属 · 可编辑'
        : (state.cid ? '全局默认 · 只读预览' : '未选角色 · 全局规则');
    }
    if (!ruleState.stats[ruleState.stat]) {
      ruleState.stat = Object.keys(ruleState.stats)[0] || '';
    }
    applyRuleMode();
  }

  /* 全局模式只读；自定义模式才允许新建 / 编辑 / 删除 / 改属性设置 */
  function applyRuleMode() {
    const editable = ruleState.scope === 'custom';
    ['wb-rule-new', 'wb-rule-edit', 'wb-rule-del'].forEach((id) => {
      const el = $(id);
      if (el) el.hidden = !editable;
    });
    const metaBtn = $('wb-rule-meta-save');
    if (metaBtn) metaBtn.hidden = !editable;
  }

  function setRuleScope(scope) {
    if (!state.cid) { U.toast('请先选择作品', false); return; }
    const apply = () => {
      A.post(RULE_EP + 'setStatRuleScope',
        { characterId: state.cid, scope })
        .then((data) => {
          applyRuleData(data);
          renderRuleTabs();
          renderRules();
          U.toast(scope === 'custom' ? '已切换为角色专属规则' : '已恢复全局默认');
        })
        .catch((err) => {
          $('wb-rule-scope').value = ruleState.scope;
          if (err.status === 401) U.loginDialog(() => setRuleScope(scope));
          else U.toast('切换失败：' + err.message, false);
        });
    };
    if (scope === 'global') {
      U.confirmDialog('恢复全局默认', '当前角色的专属规则会被删除（全局规则不受影响），确认继续？', apply);
    } else {
      apply();
    }
  }

  function renderRuleTabs() {
    const names = Object.keys(ruleState.stats);
    $('wb-rule-tabs').innerHTML = names.map((n) =>
      '<button data-stat="' + esc(n) + '"' + (n === ruleState.stat ? ' class="active"' : '')
      + '>' + esc(n) + '</button>').join('');
  }

  function renderRules() {
    const rows = ruleState.stats[ruleState.stat] || [];
    renderRuleKeywords();
    renderRuleMeta();
    if (!rows.length) {
      ruleState.sel = '';
      $('wb-rule-list').innerHTML = U.emptyHtml('暂无规则', '点「新建」添加第一档，或换个属性');
      syncRuleButtons();
      return;
    }
    if (!rows.some((r) => r.range === ruleState.sel)) ruleState.sel = rows[0].range;
    $('wb-rule-list').innerHTML = rows.map((r) =>
      '<div class="rule-row' + (r.range === ruleState.sel ? ' active' : '')
      + '" data-range="' + esc(r.range || '') + '">'
      + '<code class="rule-range">' + esc(r.range || '') + '</code>'
      + '<div class="rule-body"><b>' + esc(r.emotion || '') + '</b>'
      + '<div class="tiny muted">' + esc(r.behavior || '') + '</div></div></div>').join('');
    syncRuleButtons();
  }

  /* 触发关键词：正文命中才注入该属性档位，未配置则常驻 */
  function renderRuleKeywords() {
    const kws = ruleState.keywords[ruleState.stat] || [];
    const box = $('wb-rule-kw');
    if (!box) return;
    box.innerHTML = kws.length
      ? '触发关键词：' + kws.map((k) => '<span class="kw">' + esc(k) + '</span>').join('')
      : '<span class="muted">未配置关键词 → 常驻注入</span>';
  }

  /* 属性级设置：初始值 / 最小值 / 最大值 */
  function renderRuleMeta() {
    const stat = ruleState.stat;
    const meta = ruleState.meta[stat] || {};
    const editable = ruleState.scope === 'custom' && !!stat;
    const ids = ['rule-initial', 'rule-min', 'rule-max'];
    ids.forEach((id, i) => {
      const el = $(id);
      if (!el) return;
      el.disabled = !editable;
      const key = ['initial_value', 'min_value', 'max_value'][i];
      el.value = meta[key] !== undefined ? String(meta[key]) : '';
    });
    $('wb-rule-meta-save').disabled = !editable;
  }

  function saveRuleMeta() {
    const stat = ruleState.stat;
    if (!stat) { U.toast('请先选择属性', false); return; }
    const read = (id) => ($(id).value || '').trim();
    const num = (id) => (/^-?\d+$/.test(read(id)) ? Number(read(id)) : null);
    const iv = num('rule-initial');
    const lo = num('rule-min');
    const hi = num('rule-max');
    if (iv === null || lo === null || hi === null) {
      U.toast('初始值 / 最小值 / 最大值都必须是整数', false);
      return;
    }
    if (lo > hi) { U.toast('最小值不能大于最大值', false); return; }
    if (iv < lo || iv > hi) { U.toast('初始值必须落在最小值与最大值之间', false); return; }
    const btn = $('wb-rule-meta-save');
    btn.disabled = true;
    A.post(RULE_EP + 'saveStatMeta', {
      stat, initial_value: iv, min_value: lo, max_value: hi,
      characterId: state.cid,
    }).then((data) => {
      applyRuleData(data);
      renderRuleTabs();
      renderRules();
      U.toast('属性设置已保存');
    }).catch((err) => {
      if (err.status === 401) U.loginDialog(() => saveRuleMeta());
      else U.toast('保存失败：' + err.message, false);
    }).then(() => { btn.disabled = false; });
  }

  function selectRule(range) {
    ruleState.sel = range || '';
    Array.prototype.forEach.call($('wb-rule-list').querySelectorAll('.rule-row'), (el) => {
      el.classList.toggle('active', el.getAttribute('data-range') === ruleState.sel);
    });
    syncRuleButtons();
  }

  function syncRuleButtons() {
    const rows = ruleState.stats[ruleState.stat] || [];
    const has = !!(ruleState.sel && rows.some((r) => r.range === ruleState.sel));
    const editable = ruleState.scope === 'custom';
    $('wb-rule-edit').disabled = !editable || !has;
    $('wb-rule-del').disabled = !editable || !has;
    $('wb-rule-export').disabled = !rows.length;
  }

  /* 已保存的区间 + 本次手动加入的区间，供区间输入框的下拉候选 */
  function rangeOptions(stat) {
    const saved = (ruleState.stats[stat] || []).map((r) => r.range).filter(Boolean);
    const extra = ruleState.extraRanges[stat] || [];
    return saved.concat(extra.filter((x) => saved.indexOf(x) < 0));
  }

  function renderRangeOptions(mask) {
    const stat = ((mask.querySelector('#rule-stat') || {}).value || '').trim();
    const list = mask.querySelector('#rule-range-opts');
    if (!list) return;
    list.innerHTML = rangeOptions(stat).map((v) =>
      '<option value="' + esc(v) + '"></option>').join('');
  }

  /* 区间小按钮：把填好的区间收进下拉框，方便下次直接选 */
  function addRangeOption(mask) {
    const stat = ((mask.querySelector('#rule-stat') || {}).value || '').trim();
    const input = mask.querySelector('#rule-range');
    const range = (input.value || '').replace(/～/g, '~').replace(/\s*~\s*/, ' ~ ').trim();
    if (!stat) { U.toast('请先填写属性名', false); return; }
    if (!/^-?\d+ ~ -?\d+$/.test(range)) {
      U.toast('数值区间格式应为「最小 ~ 最大」，例如 -100 ~ -80', false);
      return;
    }
    const opts = rangeOptions(stat);
    if (opts.indexOf(range) < 0) {
      ruleState.extraRanges[stat] = (ruleState.extraRanges[stat] || []).concat([range]);
    }
    input.value = range;
    renderRangeOptions(mask);
    U.toast('区间已加入下拉框');
    const emo = mask.querySelector('#rule-emotion');
    if (emo) emo.focus();
  }

  /* 导出当前选中属性（含关键词与全部分档） */
  function exportRules() {
    const stat = ruleState.stat;
    const rows = ruleState.stats[stat] || [];
    if (!stat || !rows.length) { U.toast('请先选择要导出的数值规则', false); return; }
    const payload = {
      stat,
      characterId: ruleState.scope === 'custom' ? state.cid : 'global',
      initial_value: (ruleState.meta[stat] || {}).initial_value,
      min_value: (ruleState.meta[stat] || {}).min_value,
      max_value: (ruleState.meta[stat] || {}).max_value,
      keywords: ruleState.keywords[stat] || [],
      rules: rows.map((r) => ({
        range: r.range || '', emotion: r.emotion || '', behavior: r.behavior || '',
      })),
    };
    const blob = new Blob([JSON.stringify(payload, null, 2)],
      { type: 'application/json;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = (stat.replace(/[\\/:*?"<>|]/g, '_') || 'stat') + '-数值规则.json';
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    U.toast('已导出「' + stat + '」');
  }

  function openRuleDialog(mode) {
    const stat = ruleState.stat;
    const rows = ruleState.stats[stat] || [];
    const row = mode === 'edit' ? rows.find((r) => r.range === ruleState.sel) : null;
    if (mode === 'edit' && !row) { U.toast('请先选择一档规则', false); return; }
    const kws = (ruleState.keywords[stat] || []).join(', ');
    const html =
      '<div class="field"><label>属性名 <span class="req">*</span></label>' +
      '<input class="input" id="rule-stat" placeholder="例如：好感度 / SAN值" value="' + esc(stat) + '"></div>' +
      '<div class="field"><label>数值区间 <span class="req">*</span></label>' +
      '<div class="rule-range-row">' +
      '<input class="input" id="rule-range" list="rule-range-opts" autocomplete="off"' +
      ' placeholder="例如：-100 ~ -80" value="' + esc(row ? row.range : '') + '">' +
      '<button class="btn sm" type="button" id="rule-range-add">保存</button>' +
      '</div>' +
      '<datalist id="rule-range-opts"></datalist></div>' +
      '<div class="field"><label>情绪 / 状态</label>' +
      '<input class="input" id="rule-emotion" placeholder="这一档对应的情绪描述" value="' + esc(row ? row.emotion : '') + '"></div>' +
      '<div class="field"><label>行为指引</label>' +
      '<textarea class="textarea" id="rule-behavior" rows="3" placeholder="模型在这一档应该表现出的行为">' + esc(row ? row.behavior : '') + '</textarea></div>' +
      '<div class="field"><label>触发关键词</label>' +
      '<input class="input" id="rule-kw" placeholder="逗号分隔；留空 = 常驻注入" value="' + esc(kws) + '"></div>';
    const m = U.openModal({
      title: mode === 'edit' ? '编辑数值规则' : '新建数值规则',
      html,
      footer: '<button class="btn" data-close>取消</button>' +
        '<button class="btn primary" id="rule-save">保存</button>',
      onMount(mask) {
        renderRangeOptions(mask);
        mask.querySelector('#rule-stat').addEventListener('input', () => renderRangeOptions(mask));
        mask.querySelector('#rule-range-add').addEventListener('click', () => addRangeOption(mask));
        const rangeEl = mask.querySelector('#rule-range');
        if (rangeEl) rangeEl.focus();
        mask.querySelector('#rule-save').addEventListener('click', () => {
          saveRule(mode, stat, row, mask);
        });
      },
    });
    return m;
  }

  function saveRule(mode, oldStat, oldRow, mask) {
    const read = (id) => ((mask.querySelector('#' + id) || {}).value || '').trim();
    const stat = read('rule-stat');
    const range = read('rule-range').replace(/～/g, '~');
    if (!stat) { U.toast('请填写属性名', false); return; }
    if (!/^-?\d+\s*~\s*-?\d+$/.test(range)) {
      U.toast('数值区间格式应为「最小 ~ 最大」，例如 -100 ~ -80', false);
      return;
    }
    const body = {
      stat,
      range,
      emotion: read('rule-emotion'),
      behavior: read('rule-behavior'),
      keywords: read('rule-kw').split(/[,，、]/).map((s) => s.trim()).filter(Boolean),
      characterId: state.cid,
    };
    if (mode === 'edit' && oldRow) {
      body.oldStat = oldStat;
      body.oldRange = oldRow.range;
    }
    const btn = mask.querySelector('#rule-save');
    btn.disabled = true;
    A.post(RULE_EP + 'saveStatRule', body)
      .then((data) => {
        const saved = (data && data.saved) || {};
        applyRuleData(data);
        ruleState.stat = saved.stat || stat;
        ruleState.sel = saved.range || range;
        renderRuleTabs();
        renderRules();
        U.closeModal();
        U.toast('规则已保存');
      })
      .catch((err) => {
        btn.disabled = false;
        if (err.status === 401) U.loginDialog(() => saveRule(mode, oldStat, oldRow, mask));
        else U.toast('保存失败：' + err.message, false);
      });
  }

  function removeRule() {
    const stat = ruleState.stat;
    const range = ruleState.sel;
    if (!stat || !range) { U.toast('请先选择一档规则', false); return; }
    U.confirmDialog('删除数值规则', '确认删除「' + stat + ' ' + range + '」这一档？', () => {
      A.post(RULE_EP + 'deleteStatRule',
        { stat, range, characterId: state.cid })
        .then((data) => {
          ruleState.sel = '';
          applyRuleData(data);
          renderRuleTabs();
          renderRules();
          U.toast('已删除');
        })
        .catch((err) => {
          if (err.status === 401) U.loginDialog(() => removeRule());
          else U.toast('删除失败：' + err.message, false);
        });
    });
  }

  function init() {
    U.mountTopbar('worldbook');
    bind();
    loadWorks();
    loadRules();
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
