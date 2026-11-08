/* pages/creation.js — 创作中心：作品列表 / 角色全字段 / 封面 / CG / 发布
   所有请求走 App.api；任何渲染都做字段兜底，页面不因单条数据异常而崩。 */
(function () {
  'use strict';
  const A = App.api;
  const U = App.ui;
  const S = App.store;
  const T = App.util;
  const $ = (id) => document.getElementById(id);
  const esc = U.esc;

  const EP = '/api/gva/narratiumWorldbookEntry/';
  const GALLERY = '/api/local/gallery';
  // 尺寸全部由 /api/local/gallery/models 的 sizeOptions（数据库 image_sizes）驱动
  let galleryModelCache = null;

  function loadGalleryModel() {
    if (galleryModelCache) return Promise.resolve(galleryModelCache);
    return A.get(GALLERY + '/models', undefined, { timeout: 8000 })
      .then((data) => {
        const list = T.asList(data, ['models']).filter((m) => T.isObj(m));
        galleryModelCache = {
          model: list.find((m) => m.isDefault) || list[0] || null,
          sizeOptions: T.asList(data, ['sizeOptions']).filter((o) => T.isObj(o)),
          charge: T.pick(data, ['charge'], {}),
        };
        return galleryModelCache;
      })
      .catch(() => {
        galleryModelCache = { model: null, sizeOptions: [], charge: {} };
        return galleryModelCache;
      });
  }

  // 按原图宽高吸附到最接近的官方比例，再在同比例里选像素最接近的档位
  function snapCgSize(opts, w, h) {
    const target = w / h;
    const ratios = [];
    opts.forEach((o) => { if (ratios.indexOf(o.ratio) < 0) ratios.push(o.ratio); });
    let bestRatio = '', bestGap = Infinity;
    ratios.forEach((r) => {
      const p = String(r).split(':');
      const rv = Number(p[0]) / Number(p[1]);
      if (!Number.isFinite(rv) || rv <= 0) return;
      // 对数距离：接近 3:4 的图不会被 1:1 抢走
      const gap = Math.abs(Math.log(target / rv));
      if (gap < bestGap) { bestGap = gap; bestRatio = r; }
    });
    const tp = Math.log(w * h);
    let best = null, bestPixelGap = Infinity;
    opts.forEach((o) => {
      if (o.ratio !== bestRatio) return;
      const g = Math.abs(Math.log(o.width * o.height) - tp);
      if (g < bestPixelGap) { bestPixelGap = g; best = o; }
    });
    return best ? { opt: best, gap: bestGap } : null;
  }

  // 与后端 _cg_ai_compose 同序：备注最前（核心约束），角色→场景→情绪→动作补在后
  const CG_AI_SEGMENTS = [
    ['character', '角色', '角色标签'],
    ['scene', '场景', '场景标签'],
    ['emotion', '情绪', '情绪标签'],
    ['action', '动作/状态', '动作标签'],
  ];

  function composeCgPrompt(desc, tags) {
    const segs = [];
    if (desc) segs.push({ key: 'note', label: '备注（核心约束）', source: '手动填写', text: desc });
    CG_AI_SEGMENTS.forEach(([k, label, source]) => {
      const vals = (tags[k] || []).filter(Boolean);
      if (vals.length) {
        segs.push({ key: k, label: label, source: source, text: vals.join('、') });
      }
    });
    const prompt = segs.map((s) => (s.key === 'note' ? s.text : s.label + '：' + s.text))
      .join('\n').trim();
    return { segs, prompt };
  }

  const state = {
    works: [],
    cid: '',
    work: {},
    saved: false,
    cg: [],
    // 后端下发的「平台默认规则」说明文案（hintRule 接口返回）
    cgRule: '',
    dirty: false,
    worldbook: [],
    // 导入角色卡时先把 world_book 存在这里，保存拿到 characterId 后再写进库
    pendingWorldBook: null,
    // 分类/性向/标签选择器的当前值（对应 JSON: genderOrientation / extensions / tags）
    category: { orientation: '', type: '', tags: [], personality: '', source: '' },
  };

  // 源站 creation 的 DEFAULT_FILTER_OPTIONS（pages chunk 模块 76861）
  const CAT_OPTIONS = {
    orientation: ['男性向', '女性向', '双性恋', '男同性', '女同性'],
    type: ['都市', '历史', '科幻', '恐怖', '悬疑', '玄幻', '修真', '同人', '军事', '魔法', '后宫', '侦探', '纯聊'],
    tags: ['R18', 'RPG', '大世界', '纯爱', '低消耗', '自定义角色', '单人卡', '萝莉', '妹妹', '乱伦', '出轨',
      '贞操', '绿帽', '催眠', '偷窥', '性虐待', '绑架', '角色扮演', '女仆', '怀孕', '怪物'],
    personality: ['支配', '顺从', '熟女', '恶霸', '女装男孩', '假小子', '反派', '英雄', '傲娇', '病娇', '冷娇', '娇羞', '呆萌'],
    source: ['原创角色', '游戏', '电影', '虚拟主播', '书籍', '民间传说'],
  };
  const CAT_MAX_TAGS = 5;

  function blankPrompt() {
    return {
      breakArmorWord: '', aiIdentityStylePrompt: '', worldViewPrompt: '',
      systemPrompt: '', writingStylePrompt: '', postPrompt: '',
    };
  }

  function blankWork() {
    return {
      characterId: '', name: '', avatarURL: '', coverURL: '', summary: '',
      tags: [], genderOrientation: '', extensions: { orientation: '', type: '', source: '', personality: '' },
      personality: '', description: '', scenario: '',
      promptData: blankPrompt(), customCSS: '', blockedWords: [],
      systemPrompt: '', postHistoryInstructions: '', firstMes: '', alternateGreetings: [],
      quickCommands: { commands: [] },
      mesExample: '', creatorComment: '', publishStatus: 'draft',
    };
  }

  function splitList(v) {
    if (Array.isArray(v)) return v.map((x) => String(x).trim()).filter(Boolean);
    return String(v === undefined || v === null ? '' : v)
      .split(/[,，\n]/).map((s) => s.trim()).filter(Boolean);
  }

  function wbKeysOf(e) {
    if (!e) return [];
    if (Array.isArray(e.keys)) return e.keys.filter(Boolean);
    if (Array.isArray(e.key)) return e.key.filter(Boolean);
    if (T.isObj(e.keys) && Array.isArray(e.keys.keys)) return e.keys.keys.filter(Boolean);
    if (typeof e.keys === 'string') return splitList(e.keys);
    if (typeof e.key === 'string') return splitList(e.key);
    if (Array.isArray(e.keywords)) return e.keywords;
    return [];
  }

  function cidOf(w) {
    return T.str(T.pick(w, ['characterId', 'ID', 'id', 'workID']));
  }
  function sameCid(w) {
    return !!state.cid && cidOf(w) === state.cid;
  }

  /* ---------------------------------------------------------- 作品列表 */
  function loadWorks(selectCid) {
    A.get('/api/gva/works/my', { page: 1, pageSize: 50 })
      .then((data) => {
        state.works = T.asList(data, ['list', 'records']);
        renderWorks();
        // 只有明确指定作品 id（如「我的作品」点编辑 / 聊天页带参数）才载入已有作品；
        // 直接点顶栏「创作」进入默认就是新建空白作品
        const want = selectCid || new URLSearchParams(location.search).get('id');
        let target = null;
        if (want) target = state.works.find((w) => cidOf(w) === want) || null;
        if (target) selectWork(cidOf(target));
        else newWork(true);
      })
      .catch((err) => {
        const box = $('work-list');
        if (box && err.status === 401) {
          box.innerHTML = '<div class="empty"><b>尚未登录</b>登录后可以创建和保存作品<br><br>' +
            '<button class="btn sm primary" id="list-login">登录</button></div>';
          const btn = $('list-login');
          if (btn) btn.addEventListener('click', () => U.loginDialog(() => loadWorks()));
        } else if (box) {
          box.innerHTML = U.emptyHtml('作品列表加载失败', err.message || '');
        }
        newWork(true);
      });
  }

  /* 只刷新作品列表，不重新选中作品（保存后用，避免把编辑器内容重渲染覆盖） */
  function refreshList() {
    A.get('/api/gva/works/my', { page: 1, pageSize: 50 })
      .then((data) => {
        state.works = T.asList(data, ['list', 'records']);
        renderWorks();
      })
      .catch(() => {});
  }

  function renderWorks() {
    const box = $('work-list');
    if (!box) return;
    if (!state.works.length) {
      box.innerHTML = U.emptyHtml('还没有作品', '点右上角「新建」开始');
      return;
    }
    box.innerHTML = state.works.map((w, i) => {
      const cid = cidOf(w);
      const name = T.str(T.pick(w, ['name', 'title']), '未命名');
      const cover = T.str(T.pick(w, ['avatarURL', 'coverURL', 'cover_url']));
      const status = T.str(T.pick(w, ['publishStatus', 'status']), 'draft');
      const badge = status === 'published' ? '已发布' : (status === 'scheduled' ? '定时' : '草稿');
      return '<div class="work-item' + (cid === state.cid ? ' active' : '') + '" data-cid="' + esc(cid) + '" data-i="' + i + '">' +
        (cover ? '<img src="' + esc(cover) + '" alt="">' : '<div class="thumb">' + esc(name.slice(0, 1)) + '</div>') +
        '<div class="meta"><b>' + esc(name) + '</b><small>' + esc(badge) + '</small></div></div>';
    }).join('');
    Array.prototype.forEach.call(box.querySelectorAll('.work-item'), (item) => {
      item.addEventListener('click', () => selectWork(item.getAttribute('data-cid')));
    });
  }

  function selectWork(cid) {
    if (!cid) return;
    state.cid = cid;
    S.setLastWork(cid);
    Array.prototype.forEach.call(document.querySelectorAll('.work-item'), (el) => {
      el.classList.toggle('active', el.getAttribute('data-cid') === cid);
    });
    state.pendingWorldBook = null;
    A.get('/api/gva/work/my', { characterId: cid })
      .then((data) => {
        const w = T.isObj(data) ? data : null;
        if (!w) { U.toast('作品读取为空，可能已被删除', false); state.work = blankWork(); renderForm(); return; }
        state.work = Object.assign(blankWork(), w);
        state.saved = true;
        state.dirty = false;
        renderForm();
        loadCG();
        loadWorldbook();
        setSaveState('已保存');
      })
      .catch((err) => {
        U.toast('读取作品失败：' + err.message, false);
        state.work = blankWork();
        renderForm();
      });
    ensureListSelection(cid);
  }

  function ensureListSelection(cid) {
    const hit = state.works.find((w) => cidOf(w) === cid);
    if (hit) renderWorks();
    else {
      // 新建但还没保存：保留列表，取消高亮
      Array.prototype.forEach.call(document.querySelectorAll('.work-item'), (el) => el.classList.remove('active'));
    }
  }

  function newWork(silent) {
    state.cid = '';
    state.work = blankWork();
    state.saved = false;
    state.cg = [];
    state.dirty = false;
    state.worldbook = [];
    state.pendingWorldBook = null;
    renderForm();
    renderCG();
    loadWorldbook();
    setSaveState('未保存');
    ensureListSelection('');
    if (!silent) U.toast('已新建空白作品');
  }

  /* ---------------------------------------------------------- 表单渲染 / 收集 */
  const TEXT_FIELDS = {
    name: 'f-name', avatar: 'f-avatar', summary: 'f-summary',
    description: 'f-description',
    comment: 'f-comment',
    customCSS: 'f-customcss',
  };
  const WORK_KEYS = {
    name: 'name', avatar: 'avatarURL', summary: 'summary',
    description: 'description', example: 'mesExample',
    comment: 'creatorComment', customCSS: 'customCSS',
  };
  // 提示词面板 → promptData 的六个字段（与角色卡 JSON 一一对应）
  // 提示词富文本编辑器（AI 身份 / 世界观 / 主提示词 / 写作风格）
  const RICH_PROMPT = {
    aiIdentity: { box: 'rich-ai-identity', ph: '你是谁、以什么身份与用户对话…' },
    worldview: { box: 'rich-worldview', ph: '故事发生在什么世界、什么地点…' },
    system: { box: 'rich-system', ph: '角色扮演规则、禁止事项…' },
    writing: { box: 'rich-writing', ph: '叙事风格、节奏、描写重点…' },
  };
  // 回复样式富文本编辑器（数据仍写 mesExample）
  const RICH_EXTRA = {
    example: { box: 'rich-example', ph: '用户：…\n角色：…' },
  };
  const PROMPT_KEYS = {
    break: 'breakArmorWord', aiIdentity: 'aiIdentityStylePrompt', worldview: 'worldViewPrompt',
    system: 'systemPrompt', writing: 'writingStylePrompt', post: 'postPrompt',
  };

  function promptOf(w) {
    const pd = T.isObj(w.promptData) ? w.promptData : {};
    return {
      breakArmorWord: T.str(pd.breakArmorWord),
      aiIdentityStylePrompt: T.str(pd.aiIdentityStylePrompt),
      worldViewPrompt: T.str(pd.worldViewPrompt),
      systemPrompt: T.str(pd.systemPrompt) || T.str(w.systemPrompt),
      writingStylePrompt: T.str(pd.writingStylePrompt),
      postPrompt: T.str(pd.postPrompt) || T.str(w.postHistoryInstructions),
    };
  }

  function extensionsOf(w) {
    const ext = T.isObj(w.extensions) ? w.extensions : {};
    return {
      orientation: T.str(ext.orientation) || T.str(w.genderOrientation),
      type: T.str(ext.type),
      source: T.str(ext.source),
      personality: T.str(ext.personality),
    };
  }

  /* 分类/性向/标签：从作品数据还原选择器状态，并更新那一行的摘要 */
  function setCategoryFromWork(w) {
    const ext = extensionsOf(w);
    const tags = splitList(w.tags);
    // 想保留导入卡里那些不在预设里的标签，也展示成可取消的胶囊
    const options = CAT_OPTIONS.tags.concat(tags.filter((t) => CAT_OPTIONS.tags.indexOf(t) < 0));
    state.category = {
      orientation: ext.orientation,
      type: ext.type,
      source: ext.source,
      personality: ext.personality,
      tags,
    };
    state.category.tags = state.category.tags.slice(0, CAT_MAX_TAGS);
    renderCatSummary();
    return options;
  }

  function renderCatSummary() {
    const el = $('cat-summary');
    if (!el) return;
    const c = state.category;
    const parts = [c.orientation, c.type, c.tags.length ? c.tags.length + '个标签' : '']
      .filter(Boolean);
    const extra = [c.personality, c.source].filter(Boolean);
    const text = parts.concat(extra).join(' / ');
    el.textContent = text || '点击选择分类、性向和标签';
    el.className = text ? 'grow' : 'grow muted';
  }

  function catOptionsOf(group, draft) {
    const base = CAT_OPTIONS[group] || [];
    if (group !== 'tags') return base;
    return base.concat(draft.tags.filter((t) => base.indexOf(t) < 0));
  }

  function catGroupHtml(draft, group, title, hint) {
    const opts = catOptionsOf(group, draft);
    const multi = group === 'tags';
    const chips = opts.map((v) => {
      const on = multi ? draft.tags.indexOf(v) >= 0 : draft[group] === v;
      const extra = multi && CAT_OPTIONS.tags.indexOf(v) < 0;
      const full = multi && !on && draft.tags.length >= CAT_MAX_TAGS;
      return '<button type="button" class="chip' + (on ? ' on' : '') + (extra ? ' extra' : '') +
        '" data-group="' + group + '" data-val="' + esc(v) + '"' + (full ? ' disabled' : '') + '>' +
        esc(v) + '</button>';
    }).join('');
    return '<div class="cat-group" data-group="' + group + '">' +
      '<div class="cat-head"><span class="dot"></span><b>' + esc(title) + '</b>' +
      '<span class="' + (hint.req ? 'req' : 'opt') + '">' + esc(hint.text) + '</span></div>' +
      '<div class="chip-group">' + chips + '</div></div>';
  }

  function catBodyHtml(draft) {
    return catGroupHtml(draft, 'orientation', '性向', { req: true, text: '必选' }) +
      catGroupHtml(draft, 'type', '分类', { req: true, text: '必选' }) +
      catGroupHtml(draft, 'tags', '标签', { text: '可选，最多 ' + CAT_MAX_TAGS + ' 个（已选 ' + draft.tags.length + '）' }) +
      catGroupHtml(draft, 'personality', '性格', { text: '可选，单选' }) +
      catGroupHtml(draft, 'source', '来源', { text: '可选，单选' }) +
      '<p class="small muted" id="cat-msg" style="min-height:18px;margin:8px 0 0"></p>';
  }

  function openCategoryPicker() {
    const draft = {
      orientation: state.category.orientation,
      type: state.category.type,
      source: state.category.source,
      personality: state.category.personality,
      tags: state.category.tags.slice(0, CAT_MAX_TAGS),
    };
    const m = U.openModal({
      title: '分类 / 性向 / 标签',
      html: catBodyHtml(draft),
      footer: '<button class="btn ghost" id="cat-reset" style="margin-right:auto">重置</button>' +
        '<button class="btn" data-close>取消</button>' +
        '<button class="btn primary" id="cat-ok">确定</button>',
      onMount(root) {
        const body = root.querySelector('.modal-body');
        const msg = () => root.querySelector('#cat-msg');
        const redraw = () => { body.innerHTML = catBodyHtml(draft); };
        body.addEventListener('click', (e) => {
          const chip = e.target.closest && e.target.closest('.chip');
          if (!chip || chip.disabled) return;
          const g = chip.getAttribute('data-group');
          const v = chip.getAttribute('data-val');
          if (g === 'tags') {
            const i = draft.tags.indexOf(v);
            if (i >= 0) draft.tags.splice(i, 1);
            else if (draft.tags.length < CAT_MAX_TAGS) draft.tags.push(v);
          } else {
            draft[g] = draft[g] === v ? '' : v;
          }
          msg().textContent = '';
          redraw();
        });
        root.querySelector('#cat-reset').addEventListener('click', () => {
          draft.orientation = '';
          draft.type = '';
          draft.source = '';
          draft.personality = '';
          draft.tags = [];
          msg().textContent = '';
          redraw();
        });
        root.querySelector('#cat-ok').addEventListener('click', () => {
          if (!draft.orientation || !draft.type) {
            msg().textContent = '请选择性向和分类（必选）';
            return;
          }
          state.category = draft;
          renderCatSummary();
          markDirty();
          U.closeModal();
        });
      },
    });
    return m;
  }

  function renderForm() {
    const w = state.work || {};
    Object.keys(TEXT_FIELDS).forEach((k) => {
      const el = $(TEXT_FIELDS[k]);
      if (el) el.value = T.str(w[WORK_KEYS[k]]);
    });
    const pd = promptOf(w);
    Object.keys(RICH_PROMPT).forEach((k) => {
      const cfg = RICH_PROMPT[k];
      const box = $(cfg.box);
      // 保存走 innerText → 加载按纯文本往返，避免字面标签被当 HTML 吃掉
      if (box) box.innerHTML = richEditorHtml(cfg.ph, pd[PROMPT_KEYS[k]], true);
    });
    Object.keys(RICH_EXTRA).forEach((k) => {
      const cfg = RICH_EXTRA[k];
      const box = $(cfg.box);
      // 回复样式：入库是带 <START>/<{{char}}> 前缀的 mesExample，
      // 编辑器只显示正文（与原站一致），保存时再自动拼回前缀
      if (box) box.innerHTML = richEditorHtml(cfg.ph,
        k === 'example' ? mesExampleToReply(T.str(w[WORK_KEYS[k]])) : T.str(w[WORK_KEYS[k]]), true);
    });
    setCategoryFromWork(w);

    renderGreetings(T.str(w.firstMes), Array.isArray(w.alternateGreetings) ? w.alternateGreetings : []);
    renderQuickCommands(w.quickCommands);
    renderRichToggle('break', pd.breakArmorWord);
    renderRichToggle('next', pd.postPrompt);
    renderPublish(T.str(w.publishStatus, 'draft'));
    updateSide();
    updateCounters();
    renderDescHL();
    renderCssHL();
    renderLive();
  }

  /* ---------- 开场白：卡片式富文本编辑器（工具栏 / 全屏，参照源站） ---------- */
  const GREET_PH = '输入开场白内容，用户点击后将作为首条消息发送。';
  const ALIGN_ICON = {
    left: '<svg viewBox="0 0 16 16" width="13" height="13" fill="none" stroke="currentColor" stroke-width="1.4"><path d="M2 3h12M2 7h8M2 11h12M2 15h7"/></svg>',
    center: '<svg viewBox="0 0 16 16" width="13" height="13" fill="none" stroke="currentColor" stroke-width="1.4"><path d="M2 3h12M4 7h8M2 11h12M5 15h6"/></svg>',
    right: '<svg viewBox="0 0 16 16" width="13" height="13" fill="none" stroke="currentColor" stroke-width="1.4"><path d="M2 3h12M6 7h8M2 11h12M7 15h7"/></svg>',
  };
  const TRASH_ICON = '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.6"><path d="M4 7h16M9 7V4h6v3M7 7l1 13h8l1-13"/></svg>';

  function greetToolbarHtml() {
    return '<div class="rich-bar">' +
      '<button type="button" data-cmd="bold" title="加粗"><b>B</b></button>' +
      '<button type="button" data-cmd="italic" title="斜体"><i>I</i></button>' +
      '<button type="button" data-cmd="underline" title="下划线"><u>U</u></button>' +
      '<button type="button" data-cmd="strike" title="删除线"><s>S</s></button>' +
      '<button type="button" data-cmd="highlight" title="高亮"><span class="tb-hl">A</span></button>' +
      '<button type="button" data-cmd="code" title="行内代码">&lt;&gt;</button>' +
      '<button type="button" data-cmd="clear" title="清除格式">✎</button>' +
      '<span class="sep"></span>' +
      '<button type="button" data-cmd="ul" title="无序列表">•≡</button>' +
      '<button type="button" data-cmd="ol" title="有序列表">1≡</button>' +
      '<button type="button" data-cmd="quote" title="引用">❝</button>' +
      '<span class="sep"></span>' +
      '<button type="button" data-cmd="left" title="左对齐">' + ALIGN_ICON.left + '</button>' +
      '<button type="button" data-cmd="center" title="居中">' + ALIGN_ICON.center + '</button>' +
      '<button type="button" data-cmd="right" title="右对齐">' + ALIGN_ICON.right + '</button>' +
      '<span class="sep"></span>' +
      '<button type="button" data-cmd="link" title="插入链接"><svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="1.6"><path d="M10 14a4 4 0 0 0 6 0l3-3a4 4 0 0 0-6-6l-2 2"/><path d="M14 10a4 4 0 0 0-6 0l-3 3a4 4 0 0 0 6 6l2-2"/></svg></button>' +
      '<button type="button" data-cmd="image" title="插入图片"><svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="1.6"><rect x="3" y="4" width="18" height="16" rx="2"/><circle cx="8.5" cy="9" r="1.5"/><path d="M3 17l5-5 4 4 3-2 6 5"/></svg></button>' +
      '<span class="sep"></span>' +
      '<button type="button" data-cmd="undo" title="撤销">↶</button>' +
      '<button type="button" data-cmd="redo" title="重做">↷</button>' +
      '<button type="button" class="fs-btn" data-gfs><span>全屏</span></button>' +
      '</div>';
  }

  function richBody(html) {
    const KNOWN = /<(b|strong|i|em|u|s|mark|br|p|div|span|ul|ol|li|blockquote|code|a|img)\b/i;
    return KNOWN.test(html || '') ? html : esc(html || '').replace(/\n/g, '<br>');
  }

  /* 纯文本字段（保存时用 innerText 读回）：一律转义、保留真实换行，
     绝不按 HTML 解析、不转 <br>。否则角色卡里 <say>/<book_status>/<START> 等字面标签
     会被浏览器当元素吃掉；且 <br> 在 contenteditable 的 innerText 里会被吞掉，
     换行无法往返。配合 .rich-area 的 white-space:pre-wrap 显示换行。 */
  function richBodyText(text) {
    return esc(String(text == null ? '' : text).replace(/\r\n?/g, '\n'));
  }

  /* 单个富文本编辑器（无卡片外壳），供破甲词等字段复用。
     asText=true 表示该字段保存走 innerText（纯文本往返），加载时不做 HTML 判定。 */
  function richEditorHtml(ph, html, asText) {
    return '<div class="rich-box">' + greetToolbarHtml() +
      '<div class="rich-area" data-garea contenteditable="true" spellcheck="false" data-ph="' + esc(ph) + '">' +
      (asText ? richBodyText(html) : richBody(html)) + '</div></div>';
  }

  /* ---------- 回复样式 ↔ mesExample（与原站 creation 逻辑一致） ----------
     原站编辑器只放回复正文：保存时自动拼 "<START>\n{{user}}: \n{{char}}: " 前缀
     存入 mesExample（modules 87936 tj/l7）；加载时按 <START> 拆块、
     用 {{user}}/{{char}} 正则抽取对话对还原正文（bc），多组对话用 --- 分隔。 */
  const REPLY_PREFIX = '<START>\n{{user}}: \n{{char}}: ';

  function replyToMesExample(text) {
    const t = String(text == null ? '' : text).replace(/\r\n?/g, '\n').trim();
    return t ? REPLY_PREFIX + t : '';
  }

  function mesExampleToReply(src) {
    const s = String(src == null ? '' : src).trim();
    if (!s) return '';
    if (s.indexOf('<START>') < 0) return s;
    const pairs = [];
    s.split('<START>').forEach((block) => {
      if (!block.trim()) return;
      const u = block.match(/\{\{user\}\}:\s*([\s\S]*?)(?=\{\{char\}\}:|$)/i);
      const c = block.match(/\{\{char\}\}:\s*([\s\S]*?)$/i);
      if (!u && !c) return;
      const q = u ? u[1].trim() : '';
      const a = c ? c[1].trim() : '';
      pairs.push(q && a ? '{{user}}: ' + q + '\n{{char}}: ' + a : (a || q));
    });
    if (!pairs.length) return s;
    return pairs.filter(Boolean).join('\n\n---\n\n');
  }

  function greetCardHtml(html) {
    const body = richBody(html);
    return '<div class="greet-card">' +
      '<div class="greet-card-top"><b class="greet-no"></b>' +
      '<button type="button" class="greet-del" data-gdel title="删除这条开场白">' + TRASH_ICON + '</button></div>' +
      '<div class="rich-box">' + greetToolbarHtml() +
      '<div class="rich-area" data-garea contenteditable="true" spellcheck="false" data-ph="' + GREET_PH + '">' + body + '</div>' +
      '</div></div>';
  }

  function renumberGreets() {
    const box = $('greet-list');
    if (!box) return;
    Array.prototype.forEach.call(box.querySelectorAll('.greet-no'), (el, i) => {
      el.textContent = '开场' + (i + 1);
    });
  }

  function renderGreetings(first, alts) {
    const box = $('greet-list');
    if (!box) return;
    const items = [T.str(first)].concat((alts || []).map((x) => T.str(x)));
    if (!items.length) items.push('');
    box.innerHTML = items.map(greetCardHtml).join('');
    renumberGreets();
  }

  function addGreetCard(focus) {
    const box = $('greet-list');
    if (!box) return;
    const card = document.createElement('div');
    card.innerHTML = greetCardHtml('');
    const el = card.firstChild;
    box.appendChild(el);
    renumberGreets();
    markDirty();
    if (focus !== false) {
      const area = el.querySelector('[data-garea]');
      if (area) area.focus();
    }
  }

  function collectGreetCards() {
    const box = $('greet-list');
    if (!box) return { first: '', alts: [] };
    const vals = Array.prototype.map.call(box.querySelectorAll('[data-garea]'), (el) =>
      (el.innerText || '').trim() ? el.innerHTML.trim() : '');
    return { first: vals[0] || '', alts: vals.slice(1).filter(Boolean) };
  }

  /* 对齐：直接给块级子元素加 text-align，避免 execCommand 反复插入空块导致行距变宽 */
  function setGreetAlign(area, cmd) {
    const val = cmd === 'center' ? 'center' : (cmd === 'right' ? 'right' : 'left');
    const sel = window.getSelection();
    let nodes = [];
    if (sel && sel.rangeCount && area.contains(sel.anchorNode)) {
      const range = sel.getRangeAt(0);
      nodes = Array.prototype.filter.call(area.children, (ch) => range.intersectsNode(ch));
    }
    if (!nodes.length) nodes = Array.prototype.slice.call(area.children);
    if (!nodes.length) {
      area.style.textAlign = val;
    } else {
      nodes.forEach((n) => { n.style.textAlign = val; });
    }
    markDirty();
  }

  /* ---------- 创意广场引用（参照源站：列表 → 取源码 → 回填） ---------- */
  const PLAZA_BASE = '/api/gva/creativePlaza';

  function openPlazaPicker(itemType, onPick) {
    U.openModal({
      title: '从创意广场引用',
      html: '<div class="plaza-pick" id="plaza-pick"><div class="loading-line">加载中…</div></div>',
      onMount(root) {
        const box = root.querySelector('#plaza-pick');
        A.get(PLAZA_BASE + '/items', {
          page: 1, pageSize: 30, itemType: itemType, sortBy: 'latest',
        }, { timeout: 10000 })
          .then((data) => {
            const list = T.asList(data, ['items', 'list']).filter((x) => T.isObj(x));
            if (!list.length) {
              box.innerHTML = U.emptyHtml('暂无可引用的创意', '换个分类或稍后再试');
              return;
            }
            box.innerHTML = list.map((it, i) =>
              '<button type="button" class="plaza-row" data-i="' + i + '">' +
              '<b>' + esc(T.str(T.pick(it, ['title', 'name']), '未命名创意')) + '</b>' +
              '<small>' + esc(T.str(T.pick(it, ['description', 'summary']), '')) + '</small>' +
              '</button>').join('');
            box.addEventListener('click', (e) => {
              const row = e.target.closest('[data-i]');
              if (!row) return;
              const it = list[Number(row.getAttribute('data-i'))];
              const id = T.str(T.pick(it, ['id', 'itemID', 'itemId', 'item_id']));
              if (!id) { U.toast('该创意缺少 ID', false); return; }
              row.disabled = true;
              A.get(PLAZA_BASE + '/items/' + encodeURIComponent(id) + '/prompt')
                .then((src) => {
                  const text = T.str(T.pick(src, ['promptContent', 'content', 'prompt']));
                  if (!text) { U.toast('该创意没有可引用的内容', false); row.disabled = false; return; }
                  U.closeModal();
                  onPick(text, it);
                })
                .catch((err) => {
                  row.disabled = false;
                  U.toast(err.message || '引用失败', false);
                });
            });
          })
          .catch((err) => {
            box.innerHTML = U.emptyHtml('创意广场加载失败', err.message || '请稍后重试');
          });
      },
    });
  }

  /* 给富文本容器绑定工具栏 / 全屏 / 输入事件（开场白外的通用编辑器） */
  function bindRichEditor(container) {
    container.addEventListener('mousedown', (e) => {
      if (e.target.closest('[data-cmd]')) e.preventDefault();
    });
    container.addEventListener('click', (e) => {
      const fs = e.target.closest('[data-gfs]');
      if (fs) {
        const rich = fs.closest('.rich-box');
        if (!rich) return;
        rich.classList.toggle('fs-open');
        const label = fs.querySelector('span');
        if (label) label.textContent = rich.classList.contains('fs-open') ? '退出全屏' : '全屏';
        return;
      }
      const cmd = e.target.closest('[data-cmd]');
      if (cmd) greetCmd(cmd.getAttribute('data-cmd'), cmd.closest('.rich-box'));
    });
    container.addEventListener('input', markDirty);
  }

  /* ---------- 开关 + 富文本编辑器（破甲词 / 剧情选项共用） ---------- */
  const RICH_TOGGLES = {
    break: { wrap: 'break-editor', sw: 'sw-break', info: 'break-default', ph: '输入自定义破甲词…' },
    next: { wrap: 'next-editor', sw: 'sw-next', info: 'next-default', hint: 'next-hint', ph: '输入自定义剧情选项提示词…' },
  };

  function richToggleValue(key) {
    const cfg = RICH_TOGGLES[key];
    const wrap = $(cfg.wrap);
    const area = wrap && wrap.querySelector('[data-garea]');
    if (!area) return '';
    return (area.innerText || '').trim() ? area.innerHTML.trim() : '';
  }

  function richToggleOn(key) {
    const sw = $(RICH_TOGGLES[key].sw);
    return !!(sw && sw.checked);
  }

  function applyToggleState(key, on) {
    const cfg = RICH_TOGGLES[key];
    const sw = $(cfg.sw);
    if (sw) sw.checked = !!on;
    const info = $(cfg.info);
    if (info) info.hidden = !!on;
    const wrap = $(cfg.wrap);
    if (wrap) wrap.hidden = !on;
    if (cfg.hint) {
      const hint = $(cfg.hint);
      if (hint) hint.hidden = !on;
    }
  }

  function renderRichToggle(key, value) {
    const cfg = RICH_TOGGLES[key];
    const wrap = $(cfg.wrap);
    if (!wrap) return;
    wrap.innerHTML = richEditorHtml(cfg.ph, value);
    const on = !!T.str(value).replace(/<[^>]*>/g, '').trim();
    applyToggleState(key, on);
  }

  /* ---------- 快捷指令（最多 10 个：指令标题 + 指令内容） ---------- */
  const QC_MAX = 10;

  function quickListOf(qc) {
    const cmds = T.isObj(qc) ? T.asList(qc, ['commands']) : [];
    return cmds.map((c) => ({
      title: T.str(T.pick(c, ['title', 'name'])),
      content: T.str(T.pick(c, ['content', 'text'])),
    }));
  }

  function qcCardHtml(item) {
    return '<div class="qc-card">' +
      '<div class="qc-card-top"><b class="qc-no"></b>' +
      '<button type="button" class="greet-del" data-qdel title="删除该指令">' + TRASH_ICON + '</button></div>' +
      '<input class="input" data-qtitle maxlength="40" placeholder="指令标题" value="' + esc(item.title || '') + '">' +
      '<textarea class="textarea" data-qcontent rows="2" placeholder="指令内容">' + esc(item.content || '') + '</textarea>' +
      '</div>';
  }

  function renumberQc() {
    const box = $('qc-list');
    if (!box) return;
    Array.prototype.forEach.call(box.querySelectorAll('.qc-no'), (el, i) => {
      el.textContent = '指令' + (i + 1);
    });
    const n = box.querySelectorAll('.qc-card').length;
    const badge = $('qc-count');
    if (badge) badge.textContent = n;
    const add = $('btn-add-qc');
    if (add) add.disabled = n >= QC_MAX;
  }

  function renderQuickCommands(qc) {
    const box = $('qc-list');
    if (!box) return;
    const list = quickListOf(qc);
    if (!list.length) list.push({ title: '', content: '' });
    box.innerHTML = list.slice(0, QC_MAX).map(qcCardHtml).join('');
    renumberQc();
  }

  function addQuickCard() {
    const box = $('qc-list');
    if (!box) return;
    if (box.querySelectorAll('.qc-card').length >= QC_MAX) {
      U.toast('最多添加 ' + QC_MAX + ' 个快捷指令', false);
      return;
    }
    box.insertAdjacentHTML('beforeend', qcCardHtml({ title: '', content: '' }));
    renumberQc();
    markDirty();
    const input = box.querySelector('.qc-card:last-child [data-qtitle]');
    if (input) input.focus();
  }

  function collectQuickCommands() {
    const box = $('qc-list');
    if (!box) return [];
    return Array.prototype.map.call(box.querySelectorAll('.qc-card'), (card) => ({
      title: T.str((card.querySelector('[data-qtitle]') || {}).value).trim(),
      content: T.str((card.querySelector('[data-qcontent]') || {}).value).trim(),
    })).filter((c) => c.title || c.content);
  }

  /* 恢复编辑器里的选区（弹窗输入后插入内容用） */
  function restoreGreetRange(area, range) {
    area.focus();
    const s = window.getSelection();
    if (!s) return;
    s.removeAllRanges();
    if (range) { try { s.addRange(range); } catch (e) { /* 选区已失效则插入到末尾 */ } }
  }

  /* 通用地址输入弹窗（替代被环境禁用的 window.prompt） */
  function askInsertUrl(title, ph, hint, onOk) {
    U.openModal({
      title: title,
      html: '<div class="field"><input class="input" id="ask-url" placeholder="' + esc(ph) + '"></div>' +
        '<div class="tiny muted" style="margin-top:-4px">' + esc(hint) + '</div>',
      footer: '<button class="btn" data-close>取消</button><button class="btn primary" id="ask-ok">确定</button>',
      onMount(root) {
        const inp = root.querySelector('#ask-url');
        const ok = root.querySelector('#ask-ok');
        inp.focus();
        ok.addEventListener('click', () => {
          const v = inp.value.trim();
          if (!v) { U.toast('请输入地址', false); return; }
          U.closeModal();
          onOk(v);
        });
        inp.addEventListener('keydown', (e) => {
          if (e.key === 'Enter') { e.preventDefault(); ok.click(); }
        });
      },
    });
  }

  /* 插入图片：图片地址或上传本地图片 */
  function askInsertImage(onOk) {
    U.openModal({
      title: '插入图片',
      html: '<div class="field"><input class="input" id="img-url" placeholder="图片地址 https://…"></div>' +
        '<div class="row" style="margin-top:8px"><button type="button" class="btn sm" id="img-pick">上传本地图片</button>' +
        '<input type="file" id="img-file" accept="image/*" hidden>' +
        '<span class="tiny muted">可粘贴地址或上传</span></div>',
      footer: '<button class="btn" data-close>取消</button><button class="btn primary" id="img-ok">插入</button>',
      onMount(root) {
        const url = root.querySelector('#img-url');
        const file = root.querySelector('#img-file');
        root.querySelector('#img-pick').addEventListener('click', () => file.click());
        file.addEventListener('change', () => {
          const f = file.files && file.files[0];
          if (!f) return;
          U.toast('上传中…');
          A.upload('/api/gva/fileUploadAndDownload/upload', f, {})
            .then((data) => {
              const u = T.str(T.pick(data, ['file.url', 'url']));
              if (!u) throw new Error('接口未返回图片地址');
              url.value = u;
              U.toast('上传成功');
            })
            .catch((err) => U.toast('上传失败：' + err.message, false));
        });
        root.querySelector('#img-ok').addEventListener('click', () => {
          const v = url.value.trim();
          if (!v) { U.toast('请填写图片地址或上传图片', false); return; }
          U.closeModal();
          onOk(v);
        });
        url.focus();
      },
    });
  }

  function greetCmd(cmd, box) {
    const area = box.querySelector('[data-garea]');
    if (!area) return;
    area.focus();
    switch (cmd) {
      case 'bold': case 'italic': case 'underline':
        document.execCommand(cmd, false, null); break;
      case 'strike': document.execCommand('strikeThrough', false, null); break;
      case 'ul': document.execCommand('insertUnorderedList', false, null); break;
      case 'ol': document.execCommand('insertOrderedList', false, null); break;
      case 'left': case 'center': case 'right': setGreetAlign(area, cmd); return;
      case 'undo': document.execCommand('undo', false, null); break;
      case 'redo': document.execCommand('redo', false, null); break;
      case 'clear': document.execCommand('removeFormat', false, null); break;
      case 'highlight': {
        const s = window.getSelection();
        if (!s || !s.rangeCount || !String(s).trim()) break;
        const mark = document.createElement('mark');
        try {
          s.getRangeAt(0).surroundContents(mark);
        } catch (err) {
          document.execCommand('insertHTML', false, '<mark>' + esc(String(s)) + '</mark>');
        }
        break;
      }
      case 'code': {
        const sel = window.getSelection() ? String(window.getSelection()) : '';
        document.execCommand('insertHTML', false, '<code>' + esc(sel) + '</code>');
        break;
      }
      case 'quote': {
        const q = document.queryCommandValue('formatBlock');
        document.execCommand('formatBlock', false, q === 'blockquote' ? '<p>' : '<blockquote>');
        break;
      }
      case 'link': {
        const s = window.getSelection();
        if (!s || !String(s).trim()) { U.toast('请先选中要加链接的文字', false); break; }
        const range = s.rangeCount ? s.getRangeAt(0).cloneRange() : null;
        askInsertUrl('插入链接', 'https://…', '请输入链接地址', (url) => {
          restoreGreetRange(area, range);
          document.execCommand('createLink', false, url);
          markDirty();
        });
        return;
      }
      case 'image': {
        const s = window.getSelection();
        const range = s && s.rangeCount ? s.getRangeAt(0).cloneRange() : null;
        askInsertImage((url) => {
          restoreGreetRange(area, range);
          document.execCommand('insertImage', false, url);
          markDirty();
        });
        return;
      }
      default: break;
    }
    markDirty();
  }

  function collect() {
    const w = Object.assign({}, state.work);
    Object.keys(WORK_KEYS).forEach((k) => {
      const id = TEXT_FIELDS[k];
      if (!id) return;
      const el = $(id);
      w[WORK_KEYS[k]] = el ? (el.value || '').trim() : '';
    });
    // 回复样式走富文本编辑器，取纯文本后自动拼 <START> 前缀存 mesExample（与原站一致）
    Object.keys(RICH_EXTRA).forEach((k) => {
      const area = $(RICH_EXTRA[k].box) && $(RICH_EXTRA[k].box).querySelector('[data-garea]');
      const text = area ? (area.innerText || '').trim() : '';
      w[WORK_KEYS[k]] = k === 'example' ? replyToMesExample(text) : text;
    });
    // 提示词面板 → promptData（同时同步旧的扁平字段，聊天链路两个都认）
    const pd = Object.assign({}, blankPrompt(),
      T.isObj(state.work.promptData) ? state.work.promptData : {});
    Object.keys(RICH_PROMPT).forEach((k) => {
      const area = $(RICH_PROMPT[k].box) && $(RICH_PROMPT[k].box).querySelector('[data-garea]');
      pd[PROMPT_KEYS[k]] = area ? (area.innerText || '').trim() : '';
    });
    // 破甲词 / 剧情选项：开关关闭 → 留空（使用系统默认）；打开 → 取自定义编辑器内容
    pd[PROMPT_KEYS.break] = richToggleOn('break') ? richToggleValue('break') : '';
    pd[PROMPT_KEYS.post] = richToggleOn('next') ? richToggleValue('next') : '';
    w.promptData = pd;
    w.systemPrompt = pd.systemPrompt;
    w.postHistoryInstructions = pd.postPrompt;

    w.tags = state.category.tags.slice(0, CAT_MAX_TAGS);

    const cat = state.category;
    w.genderOrientation = cat.orientation;
    w.extensions = Object.assign({}, T.isObj(w.extensions) ? w.extensions : {},
      { orientation: cat.orientation, type: cat.type, source: cat.source, personality: cat.personality });
    const greet = collectGreetCards();
    w.firstMes = greet.first;
    w.alternateGreetings = greet.alts;
    w.quickCommands = { commands: collectQuickCommands() };
    if (state.cid) w.characterId = state.cid;
    else delete w.characterId;
    return w;
  }

  /* ---------------------------------------------------------- 世界书（world_book） */
  function updateWbManageHref() {
    const a = $('btn-wb-manage');
    if (!a) return;
    a.href = '/app/worldbook' + (state.cid ? ('?cid=' + encodeURIComponent(state.cid)) : '');
  }

  function wbRows(list) {
    return list.map((e) => {
      const keys = wbKeysOf(e);
      const title = T.str(T.pick(e, ['comment', 'title', 'name'])) || keys[0] || '未命名条目';
      const off = (e.enabled === false) || !!e.disable;
      return '<div class="work-item" style="cursor:default">' +
        '<div class="meta"><b>' + esc(title) +
        (e.constant ? ' <span class="badge ok">常驻</span>' : '') +
        (off ? ' <span class="badge">停用</span>' : '') + '</b>' +
        '<small>' + esc(keys.length ? keys.join(' / ') : '无关键词') + '</small></div></div>';
    }).join('');
  }

  function loadWorldbook() {
    const box = $('wb-list-mini');
    if (!box) return;
    updateWbManageHref();
    if (!state.cid) {
      const pend = Array.isArray(state.pendingWorldBook) ? state.pendingWorldBook : [];
      state.worldbook = [];
      box.innerHTML = pend.length
        ? '<div class="tiny muted" style="margin-bottom:6px">以下 ' + pend.length +
          ' 条来自导入的角色卡，保存后写入本地库。</div>' + wbRows(pend)
        : U.emptyHtml('还没有世界书条目', '保存作品后到这里点「管理世界书」添加');
      return;
    }
    box.innerHTML = '<div class="loading-line">加载中…</div>';
    A.get(EP + 'getEntriesByCharacterID', { characterId: state.cid })
      .then((data) => {
        state.worldbook = Array.isArray(data) ? data : T.asList(data, ['list', 'records']);
        box.innerHTML = state.worldbook.length
          ? wbRows(state.worldbook)
          : U.emptyHtml('还没有世界书条目', '点右上角「管理世界书」添加');
      })
      .catch((err) => {
        state.worldbook = [];
        box.innerHTML = U.emptyHtml('世界书加载失败', err.message);
      });
  }

  /* 本地条目 → 角色卡 JSON 的 world_book 条目（原样带回卡片里的字段） */
  function entryToCard(e) {
    const keys = wbKeysOf(e);
    return {
      key: Array.isArray(e.key) && e.key.length ? e.key : keys,
      secondary_keys: Array.isArray(e.secondary_keys) ? e.secondary_keys
        : (Array.isArray(e.secondaryKeys) ? e.secondaryKeys : []),
      value: T.str(e.value !== undefined ? e.value : T.pick(e, ['content', 'value'])),
      and: !!e.and,
      enabled: e.enabled === undefined ? !e.disable : !!e.enabled,
      order: T.num(e.order, 0),
      group: T.num(e.group, 0),
      constant: !!e.constant,
      comment: T.str(e.comment),
      useRegex: !!e.useRegex,
      key_region: T.num(e.key_region, 6),
    };
  }

  /* 角色卡 JSON 的 world_book 条目 → 本地世界书接口的 payload */
  function cardEntryToPayload(entry, cid) {
    const e = T.isObj(entry) ? entry : {};
    const keys = wbKeysOf(e);
    const canonical = {
      characterId: cid,
      comment: T.str(e.comment) || keys[0] || '导入条目',
      keys: keys,
      content: T.str(e.value !== undefined ? e.value : T.pick(e, ['content', 'value'])),
      constant: !!e.constant,
      disable: e.enabled === undefined ? !!e.disable : !e.enabled,
      position: 0,
      depth: 4,
      probability: 100,
      order: T.num(e.order, 0),
      useProbability: true,
      selective: false,
      caseSensitive: false,
    };
    // 卡片里的其它字段原样保留，导出时还能还原
    return Object.assign({}, e, canonical);
  }

  function flushPendingWorldBook(cid) {
    const list = Array.isArray(state.pendingWorldBook) ? state.pendingWorldBook.slice() : [];
    state.pendingWorldBook = null;
    if (!cid || !list.length) return Promise.resolve(0);
    let done = 0;
    return list.reduce((chain, entry) => chain.then(() =>
      A.post(EP + 'createNarratiumWorldbookEntry', cardEntryToPayload(entry, cid))
        .then(() => { done += 1; })
        .catch(() => {})), Promise.resolve()).then(() => done);
  }

  /* ---------------------------------------------------------- 导入 / 导出 JSON */
  function downloadJson(obj, filename) {
    const blob = new Blob([JSON.stringify(obj, null, 2)], { type: 'application/json;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  function exportJson() {
    const w = collect();
    const build = (entries) => {
      const ext = extensionsOf(w);
      const pd = promptOf(w);
      // 字段与 json/樱花书屋的兼职少女.json 一一对应（顺序也一致）
      const card = {
        format: 'missai',
        version: '1.0',
        exportTime: new Date().toISOString(),
        characterId: state.cid || '',
        name: w.name,
        summary: w.summary,
        description: w.description,
        tags: w.tags,
        genderOrientation: w.genderOrientation,
        extensions: { orientation: ext.orientation, type: ext.type, source: ext.source, personality: ext.personality },
        promptData: {
          breakArmorWord: pd.breakArmorWord,
          aiIdentityStylePrompt: pd.aiIdentityStylePrompt,
          worldViewPrompt: pd.worldViewPrompt,
          systemPrompt: pd.systemPrompt,
          postPrompt: pd.postPrompt,
          writingStylePrompt: pd.writingStylePrompt,
        },
        customCSS: w.customCSS,
        blockedWords: w.blockedWords,
        mesExample: w.mesExample,
        world_book: entries.map(entryToCard),
        // 下面这些样本卡里没写，但创作页有对应输入项，一并带上避免丢数据
        avatarURL: w.avatarURL,
        personality: w.personality,
        scenario: w.scenario,
        firstMes: w.firstMes,
        alternateGreetings: w.alternateGreetings,
        quickCommands: w.quickCommands,
        creatorComment: w.creatorComment,
      };
      const name = String(card.name || 'character').replace(/[\\/:*?"<>|\s]+/g, '_').slice(0, 60);
      downloadJson(card, (name || 'character') + '.json');
      U.toast('已导出角色卡 JSON');
    };

    if (!state.cid) { build(Array.isArray(state.pendingWorldBook) ? state.pendingWorldBook : []); return; }
    A.get(EP + 'getEntriesByCharacterID', { characterId: state.cid })
      .then((data) => {
        const entries = Array.isArray(data) ? data : T.asList(data, ['list', 'records']);
        build(entries.length ? entries : state.worldbook);
      })
      .catch(() => build(state.worldbook));
  }

  function applyCard(obj) {
    const pd = T.isObj(obj.promptData) ? obj.promptData : {};
    const ext = T.isObj(obj.extensions) ? obj.extensions : {};
    const entries = Array.isArray(obj.world_book) ? obj.world_book
      : (Array.isArray(obj.worldBook) ? obj.worldBook : []);
    const gender = T.str(obj.genderOrientation) || T.str(ext.orientation);
    const work = Object.assign(blankWork(), {
      name: T.str(obj.name),
      summary: T.str(obj.summary),
      description: T.str(obj.description),
      tags: splitList(obj.tags),
      genderOrientation: gender,
      extensions: {
        orientation: gender,
        type: T.str(ext.type),
        source: T.str(ext.source),
        personality: T.str(ext.personality),
      },
      personality: T.str(obj.personality),
      scenario: T.str(obj.scenario),
      avatarURL: T.str(T.pick(obj, ['avatarURL', 'coverURL'])),
      firstMes: T.str(T.pick(obj, ['firstMes', 'first_mes'])),
      alternateGreetings: Array.isArray(obj.alternateGreetings) ? obj.alternateGreetings : [],
      quickCommands: { commands: quickListOf(obj.quickCommands) },
      mesExample: T.str(obj.mesExample),
      creatorComment: T.str(obj.creatorComment),
      customCSS: T.str(obj.customCSS),
      blockedWords: splitList(obj.blockedWords),
      promptData: {
        breakArmorWord: T.str(pd.breakArmorWord),
        aiIdentityStylePrompt: T.str(pd.aiIdentityStylePrompt),
        worldViewPrompt: T.str(pd.worldViewPrompt),
        systemPrompt: T.str(pd.systemPrompt) || T.str(obj.systemPrompt),
        writingStylePrompt: T.str(pd.writingStylePrompt),
        postPrompt: T.str(pd.postPrompt) || T.str(obj.postHistoryInstructions),
      },
      publishStatus: 'draft',
      // 导入一律当新作品：清掉原 characterId，保存时由服务端生成新的
      characterId: '',
    });
    work.systemPrompt = work.promptData.systemPrompt;
    work.postHistoryInstructions = work.promptData.postPrompt;

    state.cid = '';
    state.work = work;
    state.saved = false;
    state.dirty = true;
    state.cg = [];
    state.worldbook = [];
    state.pendingWorldBook = entries;
    renderForm();
    renderCG();
    updateWbManageHref();
    loadWorldbook();
    setSaveState('导入完成，未保存', true);
    switchTab('base');
    U.toast('已导入角色卡：' + (work.name || '未命名') +
      (entries.length ? '（含 ' + entries.length + ' 条世界书，保存后写入）' : ''));
  }

  function importJson(file) {
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => {
      let obj = null;
      try { obj = JSON.parse(String(reader.result || '')); } catch (e) {
        U.toast('JSON 解析失败：' + (e && e.message ? e.message : '格式不正确'), false);
        return;
      }
      if (!T.isObj(obj)) { U.toast('角色卡 JSON 必须是对象', false); return; }
      applyCard(obj);
    };
    reader.onerror = () => U.toast('文件读取失败', false);
    reader.readAsText(file, 'utf-8');
  }


  // 旧版创作页的详情描述就是 HTML：原样保留、原样渲染，不做拦截。
  // 预览放在 sandbox="allow-scripts" 的 iframe 里：脚本可以执行（与旧版一致），
  // 但没有 allow-same-origin，拿不到本站 Cookie / 父页面 DOM。
  function descDoc(html) {
    return '<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">' +
      '<style>html,body{margin:0}body{padding:14px;font:14px/1.7 system-ui,-apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;' +
      'color:#222;background:#fff;word-break:break-word}img{max-width:100%;height:auto}*{box-sizing:border-box}' +
      'a{color:#7c5cfc}table{border-collapse:collapse;max-width:100%}pre{white-space:pre-wrap}</style>' +
      '</head><body>' + String(html || '') + '</body></html>';
  }

  /* ---------- 代码高亮：HTML 标签 / CSS 属性着色（配色取自源站 Prism 深色主题） ---------- */
  function hlEsc(s) {
    return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }

  /* 给单个已转义的标签片段上色（标签名 / 属性名 / 属性值 / 标点） */
  function paintTag(tag) {
    const m = /^(&lt;)(\/?)([a-zA-Z][\w:-]*)([^]*?)(\/?&gt;)$/.exec(tag);
    if (!m) return tag;
    const attrs = m[4].replace(/(\s+)([^\s=]+)(=)?("[^"]*"|'[^']*')?/g, (mm, sp, an, eq, av) =>
      sp + '<span class="tk-a">' + an + '</span>' +
      (eq ? '<span class="tk-p">=</span>' : '') +
      (av ? '<span class="tk-sv">' + av + '</span>' : ''));
    return '<span class="tk-p">' + m[1] + m[2] + '</span>' +
      '<span class="tk-tag">' + m[3] + '</span>' + attrs +
      '<span class="tk-p">' + m[5] + '</span>';
  }

  function highlightCode(src) {
    const stash = [];
    const hold = (html) => {
      stash.push(html);
      return '\u0000' + String.fromCharCode(0xE000 + stash.length - 1) + '\u0000';
    };
    let s = hlEsc(String(src || ''));
    // <style> 整段保持默认白色（CSS 不着色），只给 style 标签本身着色
    s = s.replace(/(&lt;style[^]*?&gt;)([^]*?)(&lt;\/style&gt;)/gi,
      (m, open, body, close) => hold(paintTag(open) + body + paintTag(close)));
    // 注释（HTML 注释与 JS 注释）
    s = s.replace(/&lt;!--[\s\S]*?--&gt;|\/\*[\s\S]*?\*\//g, (m) => hold('<span class="tk-c">' + m + '</span>'));
    // 标签：<tag attr="v"> / </tag> / 自闭合
    s = s.replace(/(&lt;)(\/?)([a-zA-Z][\w:-]*)([^]*?)(\/?&gt;)/g, (m) => hold(paintTag(m)));
    // JS 字符串
    s = s.replace(/('[^'\n]*'|"[^"\n]*")/g, (m) => hold('<span class="tk-str">' + m + '</span>'));
    // JS 关键字
    s = s.replace(/\b(var|let|const|function|return|if|else|for|while|do|switch|case|break|continue|try|catch|finally|throw|new|this|typeof|instanceof|delete|void|yield|async|await|class|extends|super|true|false|null|undefined)\b/g,
      (m) => hold('<span class="tk-kw">' + m + '</span>'));
    // 回升占位
    let guard = 0;
    while (s.indexOf('\u0000') >= 0 && guard < 200) {
      s = s.replace(/\u0000([\uE000-\uF8FF])\u0000/g, (m, ch) => stash[ch.charCodeAt(0) - 0xE000] || '');
      guard += 1;
    }
    return s;
  }

  function renderDescHL() {
    const ta = $('f-description');
    const pre = $('desc-pre');
    if (!ta || !pre) return;
    pre.innerHTML = highlightCode(ta.value) + '\n';
  }

  function syncDescScroll() {
    const ta = $('f-description');
    const hl = $('desc-hl');
    if (!ta || !hl) return;
    hl.scrollTop = ta.scrollTop;
    hl.scrollLeft = ta.scrollLeft;
  }

  /* ---------- 自定义 CSS：透明 textarea + 高亮层（与详细介绍同一套结构） ---------- */
  function highlightCss(src) {
    const stash = [];
    const hold = (html) => {
      stash.push(html);
      return '\u0000' + String.fromCharCode(0xE000 + stash.length - 1) + '\u0000';
    };
    let s = hlEsc(String(src || ''));
    // 注释
    s = s.replace(/\/\*[\s\S]*?\*\//g, (m) => hold('<span class="tk-c">' + m + '</span>'));
    // 字符串
    s = s.replace(/"[^"\n]*"|'[^'\n]*'/g, (m) => hold('<span class="tk-str">' + m + '</span>'));
    // @规则关键字
    s = s.replace(/@[\w-]+/g, (m) => hold('<span class="tk-kw">' + m + '</span>'));
    // 选择器（{ 之前的整段）
    s = s.replace(/[^{};]+(?=\s*\{)/g,
      (m) => (m.trim() ? hold('<span class="tk-a">' + m + '</span>') : m));
    // 声明：属性名 : 值 ;  —— 属性名着色，值保持默认白色（同源站 Prism 行为）
    s = s.replace(/([-\w]+)(\s*:\s*)([^;{}]+)(;?)/g, (m, prop, colon, val, semi) =>
      '<span class="tk-tag">' + prop + '</span>' +
      '<span class="tk-p">' + colon + '</span>' +
      val +
      (semi ? '<span class="tk-p">;</span>' : ''));
    // 其余标点（大括号 / 圆括号）
    s = s.replace(/[{}()]/g, (m) => hold('<span class="tk-p">' + m + '</span>'));
    // 回升占位
    let guard = 0;
    while (s.indexOf('\u0000') >= 0 && guard < 200) {
      s = s.replace(/\u0000([\uE000-\uF8FF])\u0000/g, (m, ch) => stash[ch.charCodeAt(0) - 0xE000] || '');
      guard += 1;
    }
    return s;
  }

  function renderCssHL() {
    const ta = $('f-customcss');
    const pre = $('css-pre');
    if (!ta || !pre) return;
    pre.innerHTML = highlightCss(ta.value) + '\n';
    renderCssWarn();
  }

  function syncCssScroll() {
    const ta = $('f-customcss');
    const hl = $('css-hl');
    if (!ta || !hl) return;
    hl.scrollTop = ta.scrollTop;
    hl.scrollLeft = ta.scrollLeft;
  }

  /* ---------- 自定义 CSS 可读性检测：文字与背景对比度 < 4.5 时给出黄色提醒 ---------- */
  const CSS_COLOR_NAMES = {
    black: '#000000', white: '#ffffff', red: '#ff0000', blue: '#0000ff',
    green: '#008000', gray: '#808080', grey: '#808080',
  };

  function cssParseColor(raw) {
    const t = String(raw || '').replace(/!important/gi, '').trim().toLowerCase();
    const hex = CSS_COLOR_NAMES[t] || t;
    let m = /^#([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/.exec(hex);
    if (m) return { r: parseInt(m[1], 16), g: parseInt(m[2], 16), b: parseInt(m[3], 16) };
    m = /^#([0-9a-f])([0-9a-f])([0-9a-f])$/.exec(hex);
    if (m) return { r: parseInt(m[1] + m[1], 16), g: parseInt(m[2] + m[2], 16), b: parseInt(m[3] + m[3], 16) };
    m = /^rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)/.exec(hex);
    if (m) return { r: Number(m[1]), g: Number(m[2]), b: Number(m[3]) };
    return null;
  }

  function cssLum(c) {
    const ch = (v) => {
      const x = v / 255;
      return x <= 0.03928 ? x / 12.92 : Math.pow((x + 0.055) / 1.055, 2.4);
    };
    return 0.2126 * ch(c.r) + 0.7152 * ch(c.g) + 0.0722 * ch(c.b);
  }

  function analyzeCssReadability(src, threshold) {
    const limit = threshold || 4.5;
    if (!String(src || '').trim()) return { warnings: [] };
    const s = String(src).replace(/\/\*[\s\S]*?\*\//g, '');
    const warnings = [];
    const ruleRe = /([^{}@]+)\{([^{}]+)\}/g;
    let m;
    while ((m = ruleRe.exec(s))) {
      const selector = m[1].trim();
      const body = m[2];
      const fgM = /(?:^|[;\s])color\s*:\s*([^;]+)/i.exec(body);
      const bgM = /background(?:-color)?\s*:\s*([^;]+)/i.exec(body);
      if (!selector || !fgM || !bgM) continue;
      const fg = fgM[1].replace(/!important/gi, '').trim();
      const bg = bgM[1].replace(/!important/gi, '').trim();
      const cf = cssParseColor(fg);
      const cb = cssParseColor(bg);
      if (!cf || !cb) continue;
      const ratio = (Math.max(cssLum(cf), cssLum(cb)) + 0.05) / (Math.min(cssLum(cf), cssLum(cb)) + 0.05);
      if (ratio < limit) {
        warnings.push({
          selector, foreground: fg, background: bg,
          contrastRatio: Number(ratio.toFixed(2)),
        });
      }
    }
    return { warnings };
  }

  function renderCssWarn() {
    const ta = $('f-customcss');
    const box = $('css-warn');
    if (!ta || !box) return;
    const ws = analyzeCssReadability(ta.value).warnings;
    if (!ws.length) {
      box.hidden = true;
      box.textContent = '';
      return;
    }
    const w = ws[0];
    box.hidden = false;
    box.textContent = 'CSS 可读性提醒：' + w.selector + ' 的文字 ' + w.foreground +
      ' 与背景 ' + w.background + ' 对比度仅 ' + w.contrastRatio +
      ':1，建议提高到 4.5:1 以上。';
  }

  /* 详细介绍编辑框：右上角「全屏」→ 浏览器内整页编辑（来源站） */
  function updateFsBtn() {
    const box = $('desc-box');
    const btn = $('desc-full');
    if (!box || !btn) return;
    const on = box.classList.contains('fs-open');
    const label = btn.querySelector('span');
    if (label) label.textContent = on ? '退出全屏' : '全屏';
  }

  function toggleDescFull() {
    const box = $('desc-box');
    if (!box) return;
    box.classList.toggle('fs-open');
    updateFsBtn();
    syncDescScroll();
    const ta = $('f-description');
    if (ta) ta.focus();
  }

  function updateSide() {
    const w = state.work || {};
    const name = T.str(w.name) || '未命名作品';
    const cover = T.str(w.avatarURL || w.coverURL);
    const p = $('cover-preview');
    const drop = $('btn-upload');
    const rm = $('btn-remove-cover');
    if (!p) return;
    if (rm) rm.hidden = !cover;
    if (cover) {
      p.hidden = false;
      p.src = cover;
      U.safeImg(p, name, 2);
      if (drop) drop.classList.add('has-img');
    } else {
      p.hidden = true;
      p.removeAttribute('src');
      if (drop) drop.classList.remove('has-img');
    }
  }

  /* 字数统计（简介 / 创作者备注） */
  function updateCounters() {
    Array.prototype.forEach.call(document.querySelectorAll('.char-count'), (el) => {
      const ta = $(el.getAttribute('data-for'));
      if (!ta) return;
      el.textContent = (ta.value || '').length + '/' + (ta.getAttribute('maxlength') || '500');
    });
  }

  /* ---------------------------------------------------------- 封面裁剪（3:4，拖动 + 缩放） */
  function cropOpen(file) {
    if (!file) return;
    if (!/image\//.test(file.type)) { U.toast('请选择图片文件', false); return; }
    if (file.size > 10 * 1024 * 1024) { U.toast('图片不能超过 10MB', false); return; }
    const reader = new FileReader();
    reader.onload = () => {
      const img = new Image();
      img.onload = () => openCropDialog(img, String(reader.result));
      img.onerror = () => U.toast('图片读取失败，换一张试试', false);
      img.src = String(reader.result);
    };
    reader.onerror = () => U.toast('图片读取失败，换一张试试', false);
    reader.readAsDataURL(file);
  }

  function openCropDialog(img, src) {
    const m = U.openModal({
      title: '裁剪封面图片',
      html:
        '<div class="crop-body">' +
        '<div class="crop-stage" id="crop-stage"><img id="crop-img" alt=""></div>' +
        '<div class="crop-zoom">' +
        '<button type="button" id="crop-out" title="缩小">−</button>' +
        '<input type="range" id="crop-zoom" min="100" max="300" step="1" value="100">' +
        '<button type="button" id="crop-in" title="放大">+</button>' +
        '</div>' +
        '<p class="crop-hint">拖动图片调整位置，滑动缩放大小</p>' +
        '</div>',
      footer: '<button class="btn" data-close>取消</button>' +
        '<button class="btn primary" id="crop-ok">确认裁剪</button>',
      onMount(root) {
        const stage = root.querySelector('#crop-stage');
        const el = root.querySelector('#crop-img');
        const range = root.querySelector('#crop-zoom');
        el.src = src;
        let fw = stage.clientWidth || 230;
        let fh = stage.clientHeight || 306;
        const coverScale = Math.max(fw / img.naturalWidth, fh / img.naturalHeight);
        let zoom = 1, x = 0, y = 0;
        const dw = () => img.naturalWidth * coverScale * zoom;
        const dh = () => img.naturalHeight * coverScale * zoom;
        function render() {
          const w = dw(), h = dh();
          const mx = Math.max(0, (w - fw) / 2);
          const my = Math.max(0, (h - fh) / 2);
          x = Math.max(-mx, Math.min(mx, x));
          y = Math.max(-my, Math.min(my, y));
          el.style.width = w + 'px';
          el.style.height = h + 'px';
          el.style.left = ((fw - w) / 2 + x) + 'px';
          el.style.top = ((fh - h) / 2 + y) + 'px';
        }
        let drag = null;
        stage.addEventListener('pointerdown', (e) => {
          drag = { px: e.clientX, py: e.clientY, x, y };
          stage.setPointerCapture(e.pointerId);
        });
        stage.addEventListener('pointermove', (e) => {
          if (!drag) return;
          x = drag.x + (e.clientX - drag.px);
          y = drag.y + (e.clientY - drag.py);
          render();
        });
        ['pointerup', 'pointercancel'].forEach((ev) => {
          stage.addEventListener(ev, () => { drag = null; });
        });
        range.addEventListener('input', () => { zoom = Number(range.value) / 100; render(); });
        root.querySelector('#crop-in').addEventListener('click', () => {
          range.value = String(Math.min(300, Number(range.value) + 10));
          zoom = Number(range.value) / 100; render();
        });
        root.querySelector('#crop-out').addEventListener('click', () => {
          range.value = String(Math.max(100, Number(range.value) - 10));
          zoom = Number(range.value) / 100; render();
        });
        root.querySelector('#crop-ok').addEventListener('click', () => {
          const w = dw(), h = dh();
          const sx = (-((fw - w) / 2 + x)) / w * img.naturalWidth;
          const sy = (-((fh - h) / 2 + y)) / h * img.naturalHeight;
          const sw = fw / w * img.naturalWidth;
          const sh = fh / h * img.naturalHeight;
          const canvas = document.createElement('canvas');
          canvas.width = 600;
          canvas.height = 800;
          const ctx = canvas.getContext('2d');
          ctx.fillStyle = '#ffffff';
          ctx.fillRect(0, 0, canvas.width, canvas.height);
          ctx.drawImage(img, sx, sy, sw, sh, 0, 0, canvas.width, canvas.height);
          U.closeModal();
          canvas.toBlob((blob) => {
            if (!blob) { U.toast('裁剪失败，请重试', false); return; }
            uploadCover(new File([blob], 'cover.jpg', { type: 'image/jpeg' }));
          }, 'image/jpeg', 0.92);
        });
        render();
      },
    });
    const modal = m.querySelector('.modal');
    if (modal) modal.classList.add('crop-modal');
    const foot = m.querySelector('.modal-foot');
    if (foot) foot.classList.add('crop-foot');
  }

  function uploadCover(file) {
    U.toast('上传中…');
    A.upload('/api/gva/fileUploadAndDownload/upload', file, {})
      .then((data) => {
        let url = T.str(T.pick(data, ['file.url', 'url']));
        if (!url && T.isObj(data) && T.isObj(data.file)) url = T.str(data.file.url);
        if (!url) throw new Error('接口未返回图片地址');
        const el = $('f-avatar');
        if (el) el.value = url;
        state.work.avatarURL = url;
        updateSide();
        markDirty();
        U.toast('封面已更新');
      })
      .catch((err) => U.toast('上传失败：' + err.message, false));
  }

  function setSaveState(text, isDirty) {
    const el = $('save-state');
    if (!el) return;
    el.textContent = text;
    el.className = 'tiny ' + (isDirty ? 'badge warn' : 'muted');
  }
  function markDirty() {
    state.dirty = true;
    setSaveState('未保存的修改', true);
    scheduleLive();
  }

  /* ---------------------------------------------------------- 保存 / 发布 / 删除 */
  function save() {
    const btn = $('btn-save');
    const w = collect();
    if (!w.name) { U.toast('请先填写角色名', false); switchTab('base'); $('f-name').focus(); return; }
    btn.disabled = true;
    const path = '/api/gva/work';
    const req = state.cid ? A.put(path, w) : A.post(path, w);
    req.then((data) => {
      const saved = T.isObj(data) ? data : null;
      if (!saved) throw new A.ApiError('保存失败：接口未返回作品数据');
      state.work = Object.assign(blankWork(), saved, w);
      state.cid = cidOf(saved) || state.cid;
      state.saved = true;
      state.work.characterId = state.cid;
      S.setLastWork(state.cid);
      renderPublish(T.str(saved.publishStatus, 'draft'));
      updateSide();
      setSaveState('已保存');
      state.dirty = false;
      U.toast('已保存');
      refreshList();
      loadCG();
      flushPendingWorldBook(state.cid).then((n) => {
        if (n) U.toast('已保存，并写入 ' + n + ' 条世界书');
        loadWorldbook();
      });
    }).catch((err) => {
      if (err.status === 401) U.loginDialog(() => save());
      else U.toast('保存失败：' + err.message, false);
    }).then(() => { btn.disabled = false; });
  }

  function publish() {
    if (!state.cid) { U.toast('请先保存作品', false); return; }
    A.post('/api/gva/work/publish', { characterId: state.cid })
      .then((data) => {
        const st = T.str(T.pick(data, ['publishStatus', 'status']), 'published');
        state.work.publishStatus = st;
        renderPublish(st);
        const gained = T.num(T.pick(data, ['experienceGained'], 0), 0);
        U.toast(gained ? '已发布，+' + gained + ' 经验' : '已发布');
        loadWorks(state.cid);
        loadPublishPolicy();
      })
      .catch((err) => U.toast('发布失败：' + err.message, false));
  }

  function unpublish() {
    if (!state.cid) return;
    A.post('/api/gva/work/unpublish', { characterId: state.cid })
      .then(() => {
        state.work.publishStatus = 'draft';
        renderPublish('draft');
        U.toast('已下架');
        loadWorks(state.cid);
      })
      .catch((err) => U.toast('下架失败：' + err.message, false));
  }

  function removeWork() {
    if (!state.cid) { U.toast('当前还没有保存过的作品', false); return; }
    U.confirmDialog('删除作品', '删除后角色和它的世界书都会一并删掉，且不可恢复。', () => {
      A.del('/api/gva/work', { characterId: state.cid })
        .then(() => {
          U.toast('已删除');
          state.cid = '';
          S.setLastWork('');
          loadWorks();
        })
        .catch((err) => U.toast('删除失败：' + err.message, false));
    });
  }

  function renderPublish(status) {
    const b = $('pub-badge');
    if (!b) return;
    const map = { published: ['已发布', 'ok'], scheduled: ['定时发布', 'warn'], draft: ['草稿', ''] };
    const hit = map[status] || map.draft;
    b.textContent = hit[0];
    b.className = 'badge ' + hit[1];
  }

  // 发布资格：等级不足时禁用发布并提示，后端仍会二次校验
  function loadPublishPolicy() {
    const hint = $('pub-gate-hint');
    const btn = $('btn-publish');
    if (!hint || !btn) return;
    A.get('/api/gva/narratium/work/publish-policy')
      .then((data) => {
        const min = T.num(T.pick(data, ['minimumLevel'], 2), 2);
        const lv = T.num(T.pick(data, ['currentLevel'], min), min);
        const exp = T.num(T.pick(data, ['currentExp'], 0), 0);
        const next = T.num(T.pick(data, ['nextLevelExp'], 0), 0);
        const gain = T.num(T.pick(data, ['rewardExperience'], 20), 20);
        if (lv < min) {
          btn.disabled = true;
          hint.textContent = '发布需要 Lv.' + min + '，当前 Lv.' + lv
            + '（经验 ' + exp + '/' + next + '）。可在账号中心查看成长进度，签到也能获得经验。';
        } else {
          btn.disabled = false;
          hint.textContent = '发布后可获得 ' + gain + ' 经验，并出现在探索页。';
        }
      })
      .catch(() => { /* 资格接口失败时不阻塞本地保存，发布由后端校验 */ });
  }

  /* ---------------------------------------------------------- CG */
  let cgFilter = 'all';   // all / active / inactive / no-tag

  // CG 规则区：选「自定义提示词」显示输入框，选「平台默认规则」显示后端下发的规则说明
  function syncCgRuleUI() {
    const mode = $('cg-mode').value;
    $('cg-hint-wrap').hidden = mode !== 'custom';
    const note = $('cg-default-rule');
    if (!note) return;
    const show = mode === 'platform_default' && !!state.cgRule;
    note.hidden = !show;
    if (show) note.textContent = state.cgRule;
  }

  /* CG 导入 / 导出：一个 JSON 装下该角色全部 CG（图片链接、备注、标签、关键词、触发设置） */
  function openCgIoPanel() {
    if (!state.cid) { U.toast('请先保存作品', false); return; }
    const name = T.str(T.pick(state.work, ['name', 'title']), 'cg');
    U.openModal({
      title: 'CG 导入 / 导出',
      html: '<p class="tiny muted" style="margin:0 0 12px;line-height:1.6">' +
        'JSON 数据直接取自数据库，包含该角色全部 CG：图片链接、备注、标签、关键词、触发设置，' +
        '以及全局权重（备注 / 标签 / 关键词）和平台规则选择（默认规则 / 自定义提示词 / 关闭 CG）。' +
        '导入会用文件内容覆盖当前角色的全部 CG、权重与规则。</p>' +
        '<div class="row" style="gap:10px">' +
        '<button class="btn primary" id="cg-json-export">导出 JSON</button>' +
        '<button class="btn" id="cg-json-import">导入 JSON</button>' +
        '</div>',
      onMount(mod) {
        mod.querySelector('#cg-json-export').addEventListener('click', () => {
          A.get('/api/gva/narratium/cg/export', { characterId: state.cid })
            .then((data) => {
              const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
              const href = URL.createObjectURL(blob);
              const a = document.createElement('a');
              a.href = href;
              a.download = name.replace(/[\\/:*?"<>|]/g, '_') + '-CG.json';
              a.style.display = 'none';
              document.body.appendChild(a);
              a.click();
              // 立刻 revoke 会让部分浏览器在下载真正开始前丢掉 Blob，延后释放
              setTimeout(() => {
                if (a.parentNode) a.parentNode.removeChild(a);
                URL.revokeObjectURL(href);
              }, 1500);
              U.toast('已导出 ' + T.num(T.pick(data, ['count'], 0), 0) + ' 张 CG');
            })
            .catch((err) => U.toast('导出失败：' + err.message, false));
        });
        mod.querySelector('#cg-json-import').addEventListener('click', () => {
          const inp = document.createElement('input');
          inp.type = 'file';
          inp.accept = 'application/json,.json';
          inp.onchange = () => {
            const f = inp.files && inp.files[0];
            if (!f) return;
            const rd = new FileReader();
            rd.onload = () => {
              let data = null;
              try { data = JSON.parse(String(rd.result || '')); }
              catch (e) { U.toast('JSON 解析失败', false); return; }
              const items = Array.isArray(data) ? data
                : (data && Array.isArray(data.images) ? data.images : null);
              if (!items) { U.toast('格式不正确：缺少 images 数组', false); return; }
              U.confirmDialog('导入 CG',
                '会用该文件的 ' + items.length + ' 张 CG 覆盖当前角色的全部 CG、权重与平台规则，确认吗？', () => {
                  const rule = data && T.isObj(data.hintRule) ? data.hintRule : null;
                  const weights = data && T.isObj(data.weights) ? data.weights : null;
                  A.post('/api/gva/narratium/cg/import',
                    { characterId: state.cid, images: items,
                      hintRule: rule || undefined, weights: weights || undefined })
                    .then((res) => {
                      U.toast('已导入 ' + T.num(T.pick(res, ['imported'], items.length),
                        items.length) + ' 张 CG');
                      loadCG();
                    })
                    .catch((err) => U.toast('导入失败：' + err.message, false));
                });
            };
            rd.onerror = () => U.toast('文件读取失败', false);
            rd.readAsText(f, 'utf-8');
          };
          inp.click();
        });
      },
    });
  }

  function loadCG() {
    if (!state.cid) { renderCG(); return; }
    A.get('/api/gva/narratium/cg/list', { characterId: state.cid })
      .then((data) => {
        state.cg = Array.isArray(data) ? data : T.asList(data, ['list', 'images']);
        renderCG();
      })
      .catch(() => { state.cg = []; renderCG(); });

    A.get('/api/gva/narratium/cg/hintRule', { characterId: state.cid })
      .then((data) => {
        const d = T.isObj(data) ? data : {};
        const mode = T.str(d.mode, 'platform_default');
        $('cg-mode').value = mode;
        $('cg-hint').value = T.str(d.customHintPrompt);
        state.cgRule = T.str(d.platformRule);
        syncCgRuleUI();
      })
      .catch(() => { /* 规则读取失败不阻塞 */ });

    A.get('/api/gva/narratium/cg/my-images-stats', { characterId: state.cid })
      .then((data) => renderCgStats(data))
      .catch(() => {});
  }

  function cgTagsOf(x) {
    const rows = [
      ['char', T.asList(x, ['characterTags'])],
      ['scene', T.asList(x, ['sceneTags'])],
      ['emo',  T.asList(x, ['emotionTags'])],
      ['act',  T.asList(x, ['actionTags'])],
    ];
    return rows;
  }
  function cgHasCoreTags(x) {
    // 缺标签判定：角色/场景/情绪/动作 至少各有 1 个（对齐源站"缺核心标签"提示）
    return ['characterTags', 'sceneTags', 'emotionTags', 'actionTags']
      .every((k) => T.asList(x, [k]).length > 0);
  }

  function renderCgStats(data) {
    const box = $('cg-stats');
    if (!box) return;
    const s = (data && data.summary) || {};
    const cards = [
      ['图库容量', (s.totalImages || 0) + '/1000'],
      ['已启用', s.activeImages || 0],
      ['缺标签', s.missingTagImages || 0],
      ['我的已曝光', s.exposedImages || 0],
      ['我的总曝光', s.totalExposures || 0],
    ];
    box.innerHTML = cards.map(([k, v]) =>
      '<div style="background:rgba(255,255,255,.04);padding:8px 14px;border-radius:8px;min-width:90px">' +
      '<div class="tiny muted">' + esc(k) + '</div>' +
      '<div style="font-size:18px;font-weight:600">' + esc(String(v)) + '</div></div>'
    ).join('');
  }

  function renderCgFilterBar() {
    const box = $('cg-filter-bar');
    if (!box) return;
    const tabs = [
      ['all', '全部'],
      ['active', '已启用'],
      ['inactive', '已禁用'],
      ['no-tag', '缺标签'],
    ];
    box.innerHTML = tabs.map(([k, label]) => {
      const n = k === 'all' ? state.cg.length
        : k === 'active' ? state.cg.filter((x) => x.isActive).length
        : k === 'inactive' ? state.cg.filter((x) => !x.isActive).length
        : state.cg.filter((x) => !cgHasCoreTags(x)).length;
      return '<button class="btn sm ' + (cgFilter === k ? 'primary' : 'ghost') +
        '" data-cg-filter="' + k + '">' + esc(label) + ' ' + n + '</button>';
    }).join('');
    Array.prototype.forEach.call(box.querySelectorAll('[data-cg-filter]'), (b) => {
      b.addEventListener('click', () => {
        cgFilter = b.getAttribute('data-cg-filter');
        renderCG();
      });
    });
  }

  function filteredCg() {
    if (cgFilter === 'active') return state.cg.filter((x) => x.isActive);
    if (cgFilter === 'inactive') return state.cg.filter((x) => !x.isActive);
    if (cgFilter === 'no-tag') return state.cg.filter((x) => !cgHasCoreTags(x));
    return state.cg;
  }

  function renderCG() {
    const box = $('cg-list');
    if (!box) return;
    if (!state.cid) { box.innerHTML = U.emptyHtml('保存作品后才能上传 CG', '先保存一次作品'); return; }
    renderCgFilterBar();
    const list = filteredCg();
    if (!list.length) {
      box.innerHTML = U.emptyHtml(state.cg.length ? '当前筛选下无 CG 图' : '还没有 CG 图',
        state.cg.length ? '切换筛选 tab 试试' : '点右上角「上传 CG」');
      return;
    }
    box.className = '';   // 不用 card-grid 栅格，改表格行
    box.style.cssText = 'display:flex;flex-direction:column;gap:8px';
    box.innerHTML = list.map((x) => {
      const id = T.str(T.pick(x, ['id', 'imageId']), '');
      const url = T.str(T.pick(x, ['url', 'imageURL', 'imageUrl']));
      const name = T.str(T.pick(x, ['imageName']), 'CG');
      const size = Number(x.imageSize || 0);
      const sizeKb = size > 0 ? Math.round(size / 1024) + ' KB' : '';
      const active = !!x.isActive;
      const tags = cgTagsOf(x).map(([kind, arr]) =>
        arr.map((t) =>
          '<span class="tag cg-tag-' + kind + '">' + esc(String(t)) + '</span>'
        ).join('')).join('');
      return '<div class="card" data-cg="' + esc(id) + '" style="display:flex;align-items:center;' +
        'gap:14px;padding:10px 12px;cursor:default">' +
        '<label class="switch"><input type="checkbox" data-cg-toggle="' + esc(id) + '"' +
        (active ? ' checked' : '') + '><i></i></label>' +
        (url ? '<img src="' + esc(url) + '" alt="" style="width:64px;height:84px;object-fit:cover;' +
              'border-radius:6px;background:#222">' : '<div style="width:64px;height:84px;background:#222;border-radius:6px"></div>') +
        '<div style="flex:1;min-width:0">' +
          '<div style="font-weight:600">' + esc(name) + '</div>' +
          (sizeKb ? '<div class="tiny muted">' + esc(sizeKb) + '</div>' : '') +
          '<div style="margin-top:6px;display:flex;flex-wrap:wrap;gap:4px">' + (tags || '<span class="tiny muted">未打标签</span>') + '</div>' +
        '</div>' +
        '<div class="tiny muted" style="min-width:60px;text-align:center">优先级<br><b style="color:#fff">' +
          esc(String(x.priority || 100)) + '</b></div>' +
        '<div class="row" style="gap:6px">' +
          '<button class="btn sm ghost" data-cg-copy="' + esc(id) + '" title="复制图片链接">复制</button>' +
          '<button class="btn sm" data-cg-edit="' + esc(id) + '">编辑</button>' +
          '<button class="btn sm danger" data-cg-del="' + esc(id) + '">删除</button>' +
        '</div></div>';
    }).join('');

    Array.prototype.forEach.call(box.querySelectorAll('img'), (img) => U.safeImg(img, 'CG', 3));
    Array.prototype.forEach.call(box.querySelectorAll('[data-cg-toggle]'), (cb) => {
      cb.addEventListener('change', () => {
        const id = cb.getAttribute('data-cg-toggle');
        const next = cb.checked;
        A.put('/api/gva/narratium/cg/toggleActive', { id, isActive: next })
          .then(() => {
            const hit = state.cg.find((x) => T.str(T.pick(x, ['id', 'imageId'])) === id);
            if (hit) hit.isActive = next;
            renderCG();
          })
          .catch((err) => U.toast('操作失败：' + err.message, false));
      });
    });
    Array.prototype.forEach.call(box.querySelectorAll('[data-cg-copy]'), (btn) => {
      btn.addEventListener('click', () => {
        const id = btn.getAttribute('data-cg-copy');
        const hit = state.cg.find((x) => T.str(T.pick(x, ['id', 'imageId'])) === id);
        const url = hit && T.str(T.pick(hit, ['url', 'imageURL', 'imageUrl']));
        if (!url) return U.toast('无图片链接', false);
        navigator.clipboard.writeText(url).then(() => U.toast('链接已复制'))
          .catch(() => U.toast('复制失败', false));
      });
    });
    Array.prototype.forEach.call(box.querySelectorAll('[data-cg-edit]'), (btn) => {
      btn.addEventListener('click', () => openCgEditor(btn.getAttribute('data-cg-edit')));
    });
    Array.prototype.forEach.call(box.querySelectorAll('[data-cg-del]'), (btn) => {
      btn.addEventListener('click', () => {
        const id = btn.getAttribute('data-cg-del');
        U.confirmDialog('删除 CG', '确认删除这张 CG 图？', () => {
          A.del('/api/gva/narratium/cg/delete', { id })
            .then(() => {
              state.cg = state.cg.filter((x) => T.str(T.pick(x, ['id', 'imageId'])) !== id);
              renderCG();
              U.toast('已删除');
            })
            .catch((err) => U.toast('删除失败：' + err.message, false));
        });
      });
    });
  }

  /* 编辑弹窗：标签（角色/场景/情绪/动作）+ 备注 + 高级设置 */
  function openCgEditor(id) {
    const x = state.cg.find((c) => T.str(T.pick(c, ['id', 'imageId'])) === id);
    if (!x) return;
    const url = T.str(T.pick(x, ['url', 'imageURL', 'imageUrl']));
    const tags = {
      character: T.asList(x, ['characterTags']).slice(),
      scene:     T.asList(x, ['sceneTags']).slice(),
      emotion:   T.asList(x, ['emotionTags']).slice(),
      action:    T.asList(x, ['actionTags']).slice(),
      keyword:   T.asList(x, ['keywords']).slice(),
    };
    const posOptions = [['center', '居中'], ['left', '靠左'], ['right', '靠右']];
    const modeOptions = [['inline', '文内插图'], ['card', '独立卡片']];

    function tagEditorRow(label, key, hint) {
      const chips = tags[key].map((t, i) =>
        '<span class="chip cg-chip cg-chip-' + key + '">' + esc(String(t)) + '</span>').join('');
      return '<div class="cg-edit-row">' +
        '<div class="cg-edit-head">' +
          '<div class="cg-edit-head-left"><label class="cg-edit-label">' + esc(label) + '</label>' +
          '<span class="cg-chips" data-chips="' + key + '">' + chips + '</span></div>' +
          '<span class="tiny muted cg-count">' + tags[key].length + '/10</span>' +
        '</div>' +
        '<div class="cg-edit-input-row">' +
          '<input class="input cg-tag-input" data-newtag="' + key + '" placeholder="">' +
          '<button class="cg-add-btn" type="button" data-addtag="' + key + '" title="添加">+</button>' +
        '</div>' +
        '<div class="tiny muted" style="margin-top:6px">' + esc(hint || ('每行只输入一个' + label + '。')) + '</div>' +
      '</div>';
    }

    const html =
      // AI 生成前先回显完整提示词：分段来源 + 可编辑全文 + 费用，确认后才发。
      // 放在弹窗最上方，点按钮立刻出现在可视区，不会“点了没反应”。
      '<div id="cg-ai-panel" hidden style="margin-bottom:16px;border:1px solid rgba(255,255,255,.16);' +
        'border-radius:12px;padding:12px;background:rgba(255,255,255,.03)">' +
        '<div class="tiny muted">最终提示词预览（备注固定第一段·核心约束；可直接修改后再生成）</div>' +
        '<div id="cg-ai-segs" class="tiny" style="margin-top:6px"></div>' +
        '<textarea id="cg-ai-prompt" class="textarea" rows="6" ' +
          'style="width:100%;margin-top:8px" placeholder="提示词"></textarea>' +
        '<div style="display:flex;gap:10px;margin-top:8px;flex-wrap:wrap">' +
          '<div style="min-width:170px"><div class="tiny muted">尺寸</div>' +
            '<select class="select" id="cg-ai-size"></select>' +
            '<div class="tiny" id="cg-ai-size-note" style="margin-top:4px;line-height:1.5"></div></div>' +
          '<div style="flex:1;min-width:180px"><div class="tiny muted">负向提示词（可留空）</div>' +
            '<input class="input" id="cg-ai-negative" placeholder="不希望出现的元素"></div>' +
        '</div>' +
        '<div class="tiny" id="cg-ai-cost" style="margin-top:6px"></div>' +
        '<div style="display:flex;gap:8px;margin-top:8px">' +
          '<button class="btn" type="button" id="cg-ai-cancel">取消</button>' +
          '<button class="btn primary" type="button" id="cg-ai-go">确认生成</button>' +
        '</div>' +
      '</div>' +
      '<div style="display:grid;grid-template-columns:260px 1fr;gap:18px">' +
        '<div><div class="tiny muted">图片预览</div>' +
          '<img id="cg-editor-preview" src="' + esc(url) + '" style="width:100%;border-radius:8px;display:block">' +
          '<div class="tiny muted" id="cg-editor-name" style="margin-top:6px">' + esc(T.str(x.imageName, '')) + '</div>' +
          '<button class="btn sm" type="button" id="cg-editor-replace" style="margin-top:8px;width:100%">替换 CG 图片</button>' +
          '<button class="btn sm" type="button" id="cg-editor-ai" style="margin-top:8px;width:100%">AI 生成 CG</button>' +
          '<div class="tiny muted" id="cg-editor-ai-hint" style="margin-top:6px">' +
            '按备注（核心约束）+ 角色/场景/情绪/动作标签拼提示词，先预览可改再生成，' +
            '生成后原地替换这张图</div>' +
          '<input type="file" id="cg-editor-replace-file" accept="image/*" hidden>' +
        '</div>' +
        '<div>' +
          tagEditorRow('角色', 'character') +
          tagEditorRow('场景', 'scene') +
          tagEditorRow('情绪', 'emotion') +
          tagEditorRow('动作/状态', 'action') +
          '<div class="field"><label>备注</label>' +
          '<textarea class="textarea" data-f="description" rows="3" maxlength="500" ' +
            'placeholder="写画面里有什么…">' +
            esc(T.str(x.description)) + '</textarea>' +
          '<div class="cg-counter" data-count-for="description"></div>' +
          '<div class="cg-desc-hint">参与匹配权重最高——写画面里有什么（人物、动作、环境、镜头），' +
            '不要写这张图什么时候用。</div></div>' +
          '<details open style="margin-top:10px"><summary>高级设置</summary>' +
            tagEditorRow('触发关键词', 'keyword', '关键词不会直接触发图片，只在 AI 已标记插图后帮助系统选择更合适的图。') +
            '<div class="cg-field-label">触发来源</div>' +
            '<div class="cg-duo">' +
              '<label class="cg-check-card"><input type="checkbox" data-f="srcAI"' +
                (x.triggerSource !== 'user' ? ' checked' : '') + '><span class="cg-check-box"></span>' +
                '<b>AI 回复</b></label>' +
              '<label class="cg-check-card"><input type="checkbox" data-f="srcUser"' +
                (Array.isArray(x.triggerSource) && x.triggerSource.indexOf('user') >= 0 ? ' checked' : '') +
                '><span class="cg-check-box"></span><b>用户回复</b></label>' +
            '</div>' +
            '<div class="tiny muted" style="margin:4px 0 12px">至少保留一个来源；默认 AI 回复即可。</div>' +
            '<div class="cg-duo">' +
              '<div><div class="cg-field-label">扫描深度</div>' +
                '<input class="input cg-num" type="number" min="1" max="20" data-f="scanDepth" value="' +
                  (Number(x.scanDepth) || 1) + '">' +
                '<div class="tiny muted" style="margin-top:4px">范围 1 - 20</div></div>' +
              '<div><div class="cg-field-label">展示时长</div>' +
                '<input class="input cg-num" type="number" min="0" max="3600" data-f="displayDuration" value="' +
                  (Number(x.displayDuration) || 0) + '">' +
                '<div class="tiny muted" style="margin-top:4px">范围 0 - 3600</div></div>' +
            '</div>' +
            '<div class="cg-duo" style="margin-top:12px">' +
              '<div><div class="cg-field-label">展示位置</div>' +
                '<select class="select cg-num" data-f="position">' +
                posOptions.map(([v, l]) => '<option value="' + v + '"' + (x.position === v ? ' selected' : '') + '>' + l + '</option>').join('') +
                '</select></div>' +
              '<div><div class="cg-field-label">展示方式</div>' +
                '<select class="select cg-num" data-f="triggerMode">' +
                modeOptions.map(([v, l]) => '<option value="' + v + '"' + ((x.triggerMode || 'inline') === v ? ' selected' : '') + '>' + l + '</option>').join('') +
                '</select></div>' +
            '</div>' +
            '<div style="margin-top:12px"><div class="cg-field-label">优先级</div>' +
              '<input class="input cg-num" type="number" min="1" max="1000" data-f="priority" value="' +
                (Number(x.priority) || 100) + '">' +
              '<div class="tiny muted" style="margin-top:4px">数字越小优先级越高</div></div>' +
            '<div style="margin-top:12px"><div class="cg-field-label">排序值</div>' +
              '<input class="input cg-num" type="number" min="0" max="9999" data-f="sortOrder" value="' +
                (Number(x.sortOrder) || 0) + '">' +
              '<div class="tiny muted" style="margin-top:4px">同优先级下按排序值从小到大显示</div></div>' +
            '<label class="cg-check-card cg-check-block" style="margin-top:14px">' +
              '<input type="checkbox" data-f="triggerOnce"' + (x.triggerOnce ? ' checked' : '') + '>' +
              '<span class="cg-check-box"></span><div><b>仅触发一次</b>' +
              '<div class="tiny muted">每个会话只展示一次</div></div></label>' +
            '<label class="cg-check-card cg-check-block" style="margin-top:10px">' +
              '<input type="checkbox" data-f="allowMulti"' + (x.allowMulti !== false ? ' checked' : '') + '>' +
              '<span class="cg-check-box"></span><div><b>允许多图共存</b>' +
              '<div class="tiny muted">同一位置可展示多张 CG</div></div></label>' +
            '<div style="margin-top:12px"><div class="cg-field-label">管理备注</div>' +
              '<input class="input cg-num" type="text" data-f="managementNote" placeholder="仅供作者管理用" ' +
              'value="' + esc(T.str(x.managementNote)) + '"></div>' +
          '</details>' +
        '</div>' +
      '</div>';

    const m = U.openModal({
      title: '编辑 CG 图片',
      html,
      footer: '<button class="btn" data-close>取消</button>' +
        '<span class="grow"></span>' +
        '<button class="btn primary" id="cg-editor-save">保存</button>',
      onMount(mod) {
        // 备注字数：输入即统计（上限 500）
        const descEl = mod.querySelector('[data-f="description"]');
        const descCount = mod.querySelector('[data-count-for="description"]');
        const syncDescCount = () => {
          descCount.textContent = (descEl.value || '').length + '/500';
        };
        descEl.addEventListener('input', syncDescCount);
        syncDescCount();
        // 重新渲染某一类 chips
        function rerenderChips(key) {
          const host = mod.querySelector('.cg-chips[data-chips="' + key + '"]')
                    || mod.querySelector('[data-chips="' + key + '"]');
          if (!host) return;
          host.innerHTML = tags[key].map((t) =>
            '<span class="chip cg-chip cg-chip-' + key + '">' + esc(String(t)) + '</span>').join('');
          bindChips(host, key);
          // 同步计数器
          const count = host.closest('.cg-edit-row').querySelector('.cg-count');
          if (count) count.textContent = tags[key].length + '/10';
        }
        function bindChips(host, key) {
          Array.prototype.forEach.call(host.querySelectorAll('.cg-chip'), (el) => {
            el.style.cursor = 'pointer';
            el.title = '点击删除';
            el.addEventListener('click', () => {
              const v = el.textContent;
              const i = tags[key].indexOf(v);
              if (i >= 0) tags[key].splice(i, 1);
              rerenderChips(key);
            });
          });
        }
        // 初次绑定已有 chips
        ['character', 'scene', 'emotion', 'action', 'keyword'].forEach((k) => {
          const host = mod.querySelector('[data-chips="' + k + '"]');
          if (host) bindChips(host, k);
        });
        // 添加 tag
        Array.prototype.forEach.call(mod.querySelectorAll('[data-addtag]'), (btn) => {
          btn.addEventListener('click', () => {
            const key = btn.getAttribute('data-addtag');
            const inp = mod.querySelector('[data-newtag="' + key + '"]');
            const v = (inp.value || '').trim();
            if (!v) return;
            if (tags[key].indexOf(v) >= 0) { inp.value = ''; return; }
            if (tags[key].length >= 10) { U.toast('最多 10 个', false); return; }
            tags[key].push(v);
            inp.value = '';
            rerenderChips(key);
          });
        });
        // 替换图片：上传新图后原地替换这条 CG，保留标签与触发配置
        const repBtn = mod.querySelector('#cg-editor-replace');
        const repFile = mod.querySelector('#cg-editor-replace-file');
        repBtn.addEventListener('click', () => repFile.click());
        repFile.addEventListener('change', () => {
          const file = repFile.files && repFile.files[0];
          repFile.value = '';
          if (!file) return;
          if (!state.cid) { U.toast('请先保存作品', false); return; }
          repBtn.disabled = true;
          U.toast('上传中…');
          A.upload('/api/gva/narratium/cg/upload', file,
            { characterId: state.cid, replaceId: id })
            .then((data) => {
              if (data && data.ok === false) throw new Error(data.error || '替换失败');
              const next = data || {};
              const newUrl = T.str(T.pick(next, ['url', 'imageURL', 'imageUrl']), '');
              if (newUrl) {
                mod.querySelector('#cg-editor-preview').src = newUrl;
                x.url = newUrl;
                x.imageURL = newUrl;
              }
              const newName = T.str(next.imageName, file.name);
              mod.querySelector('#cg-editor-name').textContent = newName;
              x.imageName = newName;
              const i = state.cg.findIndex((c) => T.str(T.pick(c, ['id', 'imageId'])) === id);
              if (i >= 0) Object.assign(state.cg[i], x);
              renderCG();
              U.toast('CG 图片已替换');
            })
            .catch((err) => U.toast('替换失败：' + err.message, false))
            .finally(() => { repBtn.disabled = false; });
        });
        // AI 生成：本地拼装提示词（备注核心约束在最前）→ 用绘图广场同一接口预览费用 →
        // 确认后同一接口出图，再把结果接现有 /cg/upload 落库/替换（不另起一套）。
        const aiBtn = mod.querySelector('#cg-editor-ai');
        const aiPanel = mod.querySelector('#cg-ai-panel');
        const aiSegs = mod.querySelector('#cg-ai-segs');
        const aiPrompt = mod.querySelector('#cg-ai-prompt');
        const aiCost = mod.querySelector('#cg-ai-cost');
        const aiGo = mod.querySelector('#cg-ai-go');
        const aiCancel = mod.querySelector('#cg-ai-cancel');
        const aiSize = mod.querySelector('#cg-ai-size');
        const aiNegative = mod.querySelector('#cg-ai-negative');
        const aiDesc = mod.querySelector('[data-f="description"]');
        const aiSizeNote = mod.querySelector('#cg-ai-size-note');
        let aiBusy = false;
        let aiModel = null;
        let aiCharged = false;
        let aiSizeOpts = [];
        let aiSizeTouched = false;
        // 被编辑 CG 的真实宽高：优先读图片本身的 natural 像素，记录里的宽高先兜底
        let cgSourceSize = null;
        const recW = Number(T.pick(x, ['imageWidth', 'width'], 0)) || 0;
        const recH = Number(T.pick(x, ['imageHeight', 'height'], 0)) || 0;
        if (recW > 0 && recH > 0) {
          cgSourceSize = { width: recW, height: recH, from: 'record' };
        }

        function setSizeNote(text, kind) {
          if (!aiSizeNote) return;
          aiSizeNote.textContent = text;
          aiSizeNote.style.color = kind === 'fallback' ? '#fbbf24'
            : kind === 'manual' ? 'rgba(255,255,255,.85)' : 'rgba(255,255,255,.72)';
        }

        // 默认值：按原图吸附到最接近的官方比例 + 最接近的档位；拿不到原图尺寸回退 3:4 + 1K
        function applyDefaultSize() {
          const opts = aiSizeOpts.filter((o) => o && o.enabled !== false);
          if (!opts.length) return;
          let pick = null;
          if (cgSourceSize) {
            const snap = snapCgSize(opts, cgSourceSize.width, cgSourceSize.height);
            pick = snap && snap.opt;
          }
          if (pick) {
            aiSize.value = pick.ratio + '|' + pick.tier;
            setSizeNote('最佳比例 · 跟随原图 ' + pick.ratio + ' · 清晰度 ' + pick.tier +
              '（贴近原图 ' + cgSourceSize.width + '×' + cgSourceSize.height +
              '，输出 ' + pick.width + '×' + pick.height + '）', 'auto');
            return;
          }
          pick = opts.find((o) => o.ratio === '3:4' && o.tier === '1K')
            || opts.find((o) => o.tier === '1K') || opts[0];
          aiSize.value = pick.ratio + '|' + pick.tier;
          setSizeNote('回退值（原图尺寸读取失败）· ' + pick.ratio + ' · ' + pick.tier +
            '（' + pick.width + '×' + pick.height + '）', 'fallback');
        }

        const fillSizes = (cache) => {
          aiSizeOpts = ((cache && cache.sizeOptions) || []).filter((o) => o && o.enabled);
          aiSize.innerHTML = aiSizeOpts.map((o) =>
            '<option value="' + esc(o.ratio + '|' + o.tier) + '">' +
            esc(o.ratio + ' · ' + o.tier + ' · ' + o.width + '×' + o.height +
              (Number(o.points) > 0 ? '（' + o.points + ' 积分）' : '')) +
            '</option>').join('');
          if (!aiSizeOpts.length) {
            aiSize.innerHTML = '<option value="">尺寸未加载</option>';
            setSizeNote('尺寸选项未加载，请刷新页面重试', 'fallback');
            return;
          }
          if (!aiSizeTouched) applyDefaultSize();
        };
        loadGalleryModel().then((cache) => { aiModel = cache.model; fillSizes(cache); });

        // 图片加载完成后用真实像素覆盖记录值，并刷新默认吸附（用户手动改过就不再改）
        const previewImg = mod.querySelector('#cg-editor-preview');
        const readImageSize = () => {
          if (!previewImg || !previewImg.naturalWidth || !previewImg.naturalHeight) return;
          cgSourceSize = { width: previewImg.naturalWidth,
                           height: previewImg.naturalHeight, from: 'image' };
          if (!aiSizeTouched) applyDefaultSize();
        };
        if (previewImg) {
          if (previewImg.complete) readImageSize();
          previewImg.addEventListener('load', readImageSize);
          previewImg.addEventListener('error', () => {
            if (!cgSourceSize) { aiSizeTouched = false; applyDefaultSize(); }
          });
        }

        aiSize.addEventListener('change', () => {
          aiSizeTouched = true;
          const hit = aiSizeOpts.find((o) => o.ratio + '|' + o.tier === aiSize.value);
          if (hit) {
            setSizeNote('手动选择 · ' + hit.ratio + ' · ' + hit.tier +
              '（' + hit.width + '×' + hit.height + '）', 'manual');
          } else {
            setSizeNote('手动选择', 'manual');
          }
        });

        const aiFields = () => ({
          characterId: state.cid,
          replaceId: id,
          description: aiDesc ? String(aiDesc.value || '').trim() : '',
          characterTags: tags.character.slice(),
          sceneTags: tags.scene.slice(),
          emotionTags: tags.emotion.slice(),
          actionTags: tags.action.slice(),
        });

        // 与绘图广场 /api/local/gallery/generate 完全同一套字段（比例 + 档位）
        const aiParams = (promptText, dryRun) => {
          const token = String((aiSize && aiSize.value) || '');
          const sep = token.indexOf('|');
          const ratio = sep >= 0 ? token.slice(0, sep) : '';
          const tier = sep >= 0 ? token.slice(sep + 1) : '';
          return {
            prompt: promptText,
            negative: String((aiNegative && aiNegative.value) || '').trim(),
            rateId: Number((aiModel && aiModel.ID) || 0),
            size: tier,
            ratio,
            n: 1,
            seed: 0,
            dryRun: !!dryRun,
          };
        };

        const showPreview = (segs, draft, data) => {
          aiPanel.hidden = false;
          aiSegs.innerHTML = segs.map((s) =>
            '<div style="margin-bottom:4px">' +
              '<span style="opacity:.65">' + esc(s.label) + ' · 来源：' + esc(s.source) + '</span>' +
              '<div style="white-space:pre-wrap">' + esc(s.text) + '</div>' +
            '</div>').join('');
          aiPrompt.value = T.str(T.pick(data || {}, ['prompt'], draft));
          const cost = Number(data && data.cost) || 0;
          const bal = Number(data && data.balance) || 0;
          if (data && data.canAfford === false) {
            aiCost.innerHTML = '<span style="color:#f87171">积分不足：本次需要 ' + cost +
              ' 积分，当前余额 ' + bal + '</span>';
            aiGo.disabled = true;
            aiGo.textContent = '积分不足';
            U.toast('积分不足：本次需要 ' + cost + ' 积分，当前余额 ' + bal, false);
          } else {
            aiCost.innerHTML = '本次消耗 <b>' + cost + '</b> 积分 · 当前余额 <b>' + bal +
              '</b>' + (data && data.local ? ' · 本地占位绘图（占位图，非真实画面）'
                                           : ' · ' + esc(T.str(data && data.providerName)));
            aiGo.disabled = !String(aiPrompt.value || '').trim();
            aiGo.textContent = '确认生成';
          }
          aiGo.setAttribute('data-afford', data && data.canAfford === false ? '0' : '1');
          // 弹窗内容可能被滚动过：把预览面板滚进可视区，确保点完立刻看到结果
          if (aiPanel.scrollIntoView) {
            aiPanel.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
          }
        };

        aiBtn.addEventListener('click', () => {
          if (aiBusy) return;
          if (!state.cid) { U.toast('请先保存作品', false); return; }
          const f = aiFields();
          if (!f.description && !f.characterTags.length && !f.sceneTags.length &&
              !f.emotionTags.length && !f.actionTags.length) {
            U.toast('备注或标签至少填一项', false);
            return;
          }
          const composed = composeCgPrompt(f.description, tags);
          if (!composed.prompt) { U.toast('备注和标签都为空，先填一项再生成', false); return; }
          aiBtn.disabled = true;
          const oldText = aiBtn.textContent;
          aiBtn.textContent = '拼装中…';
          loadGalleryModel().then((cache) => {
            aiModel = cache.model;
            if (!aiSize.options.length || !String(aiSize.value || '')) fillSizes(cache);
            if (!String(aiSize.value || '')) {
              throw new Error('尺寸选项未加载，请刷新页面后重试');
            }
            return A.post(GALLERY + '/generate', aiParams(composed.prompt, true),
              { timeout: 15000 });
          })
            .then((data) => showPreview(composed.segs, composed.prompt, data))
            .catch((err) => U.toast(err.message, false))
            .finally(() => {
              aiBtn.disabled = false;
              aiBtn.textContent = oldText;
            });
        });

        aiPrompt.addEventListener('input', () => {
          if (aiBusy) return;
          aiGo.disabled = aiGo.getAttribute('data-afford') === '0'
            || !String(aiPrompt.value || '').trim();
        });

        aiCancel.addEventListener('click', () => {
          if (aiBusy) return;
          aiPanel.hidden = true;
        });

        aiGo.addEventListener('click', () => {
          if (aiBusy) return;
          if (aiGo.getAttribute('data-afford') === '0') {
            U.toast('积分不足，无法生成', false);
            return;
          }
          const promptText = String(aiPrompt.value || '').trim();
          if (!promptText) { U.toast('提示词不能为空', false); return; }
          if (!state.cid) { U.toast('请先保存作品', false); return; }
          aiBusy = true;
          aiCharged = false;
          aiGo.disabled = true;
          aiCancel.disabled = true;
          aiBtn.disabled = true;
          const oldText = aiBtn.textContent;
          aiBtn.textContent = '生成中…';
          aiGo.textContent = '生成中…';
          aiCost.innerHTML = '生成中，请稍候…（失败不会扣积分）';
          A.post(GALLERY + '/generate', aiParams(promptText, false), { timeout: 180000 })
            .then((data) => {
              const works = T.asList(data, ['works']);
              const work = works[0] || {};
              const urlKey = T.str(T.pick(work, ['url_key', 'url', 'path'], ''));
              if (!urlKey) throw new Error('生成成功但没有返回图片地址');
              aiCharged = true;
              return fetch(urlKey, { credentials: 'same-origin', cache: 'no-store' })
                .then((res) => {
                  if (!res.ok) throw new Error('图片读取失败（HTTP ' + res.status + '）');
                  return res.blob();
                })
                .then((blob) => {
                  const isJpg = String(blob.type || '').indexOf('jpeg') >= 0;
                  const file = new File([blob], 'ai_cg_' + Date.now() + (isJpg ? '.jpg' : '.png'),
                    { type: blob.type || 'image/png' });
                  return A.upload('/api/gva/narratium/cg/upload', file,
                    { characterId: state.cid, replaceId: id })
                    .then((up) => ({ up: up || {}, gen: data || {} }));
                });
            })
            .then((res) => {
              const up = res.up || {};
              const gen = res.gen || {};
              if (up.ok === false) throw new Error(up.error || 'CG 落库失败');
              const newUrl = T.str(T.pick(up, ['url', 'imageURL', 'imageUrl']), '');
              if (newUrl) {
                mod.querySelector('#cg-editor-preview').src = newUrl;
                x.url = newUrl;
                x.imageURL = newUrl;
              }
              const newName = T.str(up.imageName, '');
              if (newName) {
                mod.querySelector('#cg-editor-name').textContent = newName;
                x.imageName = newName;
              }
              const i = state.cg.findIndex((c) => T.str(T.pick(c, ['id', 'imageId'])) === id);
              if (i >= 0) Object.assign(state.cg[i], x);
              renderCG();
              aiPanel.hidden = true;
              const used = Number(gen.pointsDeducted) || 0;
              const outPx = T.str(T.pick(gen, ['actualPixels', 'pixels'], ''));
              const reqPx = T.str(T.pick(gen, ['requestedPixels'], ''));
              const norm = !!T.pick(gen, ['normalized'], false);
              const tip = (used
                ? '已生成并替换（消耗 ' + used + ' 积分，余额 ' + gen.balance + '）'
                : '已生成并替换') +
                (outPx ? ' · 输出 ' + outPx : '') +
                (norm && reqPx ? '（请求 ' + reqPx + ' 已归一化）' : '');
              U.toast(tip + (gen.local ? '（占位图，非真实画面）' : ''));
            })
            .catch((err) => {
              aiCost.innerHTML = '<span style="color:#f87171">' + esc(err.message) +
                (aiCharged ? '（图片已生成，积分已按绘图接口扣除）'
                           : '（本次未扣积分）') + '</span>';
              U.toast(err.message, false);
            })
            .finally(() => {
              aiBusy = false;
              aiBtn.disabled = false;
              aiBtn.textContent = oldText;
              aiCancel.disabled = false;
              aiGo.textContent = '确认生成';
              aiGo.disabled = aiGo.getAttribute('data-afford') === '0'
                || !String(aiPrompt.value || '').trim();
            });
        });
        mod.querySelector('#cg-editor-save').addEventListener('click', () => {
          const src = [];
          if (mod.querySelector('[data-f="srcAI"]').checked) src.push('ai');
          if (mod.querySelector('[data-f="srcUser"]').checked) src.push('user');
          const payload = {
            id,
            characterId: state.cid,
            characterTags: tags.character,
            sceneTags: tags.scene,
            emotionTags: tags.emotion,
            actionTags: tags.action,
            keywords: tags.keyword,
            description: mod.querySelector('[data-f="description"]').value || '',
            scanDepth: clampInt(mod.querySelector('[data-f="scanDepth"]').value, 1, 20, 1),
            displayDuration: clampInt(mod.querySelector('[data-f="displayDuration"]').value, 0, 3600, 0),
            position: mod.querySelector('[data-f="position"]').value,
            triggerMode: mod.querySelector('[data-f="triggerMode"]').value,
            priority: clampInt(mod.querySelector('[data-f="priority"]').value, 1, 1000, 100),
            sortOrder: clampInt(mod.querySelector('[data-f="sortOrder"]').value, 0, 9999, 0),
            triggerOnce: mod.querySelector('[data-f="triggerOnce"]').checked,
            allowMulti: mod.querySelector('[data-f="allowMulti"]').checked,
            managementNote: mod.querySelector('[data-f="managementNote"]').value || '',
            triggerSource: src,
          };
          A.put('/api/gva/narratium/cg/update', payload)
            .then((updated) => {
              const i = state.cg.findIndex((c) => T.str(T.pick(c, ['id', 'imageId'])) === id);
              if (i >= 0 && updated && typeof updated === 'object') state.cg[i] = updated;
              renderCG();
              U.toast('CG 已保存');
              U.closeModal && U.closeModal();
            })
            .catch((e) => U.toast('保存失败：' + e.message, false));
        });
      },
    });
    return m;
  }
  function clampInt(v, lo, hi, dft) {
    const n = parseInt(v, 10);
    if (!isFinite(n)) return dft;
    return Math.min(hi, Math.max(lo, n));
  }

  function uploadCG(file) {
    if (!state.cid) { U.toast('请先保存作品', false); return; }
    if (!file) return;
    U.toast('上传中…');
    A.upload('/api/gva/narratium/cg/upload', file, { characterId: state.cid })
      .then((data) => {
        if (data && data.ok === false) throw new Error(data.error || '上传失败');
        U.toast('CG 已上传');
        loadCG();
      })
      .catch((err) => U.toast('上传失败：' + err.message, false));
  }

  /* ---------------------------------------------------------- 顶栏 / 交互绑定 */
  function switchTab(name) {
    Array.prototype.forEach.call(document.querySelectorAll('#tabs button'), (b) => {
      b.classList.toggle('active', b.getAttribute('data-tab') === name);
    });
    Array.prototype.forEach.call(document.querySelectorAll('[data-panel]'), (s) => {
      s.hidden = s.getAttribute('data-panel') !== name;
    });
    const hints = {
      base: '必填：角色名。封面、简介、标签、性别取向、类型会展示在角色卡上。',
      setting: '描述越具体，角色扮演越稳定。',
      prompt: '对应角色卡 JSON 的 promptData。',
      greet: '开场白决定第一次对话的语气。',
      quick: '设置快捷指令，用户在聊天时可以快速选择发送，最多 10 个。',
      example: '可留空；有样式样本时模型会模仿回复格式。下方自定义 CSS 只影响聊天气泡。',
      worldbook: '对应角色卡 JSON 的 world_book，导入导出会一并带上。',
      cg: 'CG 图按关键词插入聊天，可随时停用。',
      publish: '发布后出现在探索页，随时可下架。',
    };
    $('tab-hint').textContent = hints[name] || '';
    if (name === 'worldbook') loadWorldbook();
  }

  function bind() {
    Array.prototype.forEach.call(document.querySelectorAll('#tabs button'), (b) => {
      b.addEventListener('click', () => switchTab(b.getAttribute('data-tab')));
    });

    Object.keys(TEXT_FIELDS).forEach((k) => {
      const el = $(TEXT_FIELDS[k]);
      if (el) el.addEventListener('input', () => {
        markDirty();
        if (k === 'name' || k === 'avatar') updateSide();
        if (k === 'summary' || k === 'comment') updateCounters();
      });
    });
    // 三个预览统一刷新：编辑区任何输入（普通输入框 / 代码框 / 富文本）都触发实时预览
    document.addEventListener('input', () => scheduleLive(), true);
    $('btn-category').addEventListener('click', openCategoryPicker);

    ['break-editor', 'next-editor', 'rich-ai-identity', 'rich-worldview', 'rich-system', 'rich-writing',
      'rich-example']
      .forEach((id) => {
        const box = $(id);
        if (box) bindRichEditor(box);
      });

    // 角色卡 JSON 导入 / 导出
    $('btn-import').addEventListener('click', () => $('card-file').click());
    $('card-file').addEventListener('change', (e) => {
      const file = e.target.files && e.target.files[0];
      e.target.value = '';
      if (!file) return;
      if (state.dirty && !window.confirm('当前作品有未保存的修改，导入会覆盖表单，仍要继续吗？')) return;
      importJson(file);
    });
    $('btn-export').addEventListener('click', exportJson);
    $('btn-wb-refresh').addEventListener('click', loadWorldbook);

    // 详细介绍：语法高亮 + 右上角全屏（浏览器内）
    $('desc-full').addEventListener('click', toggleDescFull);
    // 自定义 CSS：右上角全屏
    const cssFull = $('css-full');
    if (cssFull) {
      cssFull.addEventListener('click', () => {
        const box = $('css-box');
        if (!box) return;
        box.classList.toggle('fs-open');
        const label = cssFull.querySelector('span');
        if (label) label.textContent = box.classList.contains('fs-open') ? '退出全屏' : '全屏';
        renderCssHL();
        syncCssScroll();
        const ta = $('f-customcss');
        if (ta) ta.focus();
      });
    }
    $('f-description').addEventListener('input', renderDescHL);
    $('f-description').addEventListener('scroll', syncDescScroll);
    // 自定义 CSS：输入实时高亮、滚动同步、Tab 插入两个空格（同源站 CodeEditor）
    const cssArea = $('f-customcss');
    if (cssArea) {
      cssArea.addEventListener('input', renderCssHL);
      cssArea.addEventListener('scroll', syncCssScroll);
      cssArea.addEventListener('keydown', (e) => {
        if (e.key !== 'Tab' || e.shiftKey || e.altKey || e.ctrlKey || e.metaKey) return;
        e.preventDefault();
        const ok = document.execCommand && document.execCommand('insertText', false, '  ');
        if (!ok) {
          const start = cssArea.selectionStart;
          const end = cssArea.selectionEnd;
          cssArea.setRangeText('  ', start, end, 'end');
          renderCssHL();
        }
      });
    }
    document.addEventListener('keydown', (e) => {
      if (e.key !== 'Escape') return;
      const box = $('desc-box');
      if (box && box.classList.contains('fs-open')) {
        box.classList.remove('fs-open');
        updateFsBtn();
      }
      const cssBox = $('css-box');
      if (cssBox && cssBox.classList.contains('fs-open')) {
        cssBox.classList.remove('fs-open');
        const cl = $('css-full') && $('css-full').querySelector('span');
        if (cl) cl.textContent = '全屏';
      }
      const rich = document.querySelector('.rich-box.fs-open');
      if (rich) {
        rich.classList.remove('fs-open');
        const label = rich.querySelector('[data-gfs] span');
        if (label) label.textContent = '全屏';
      }
    });

    const btnNew = $('btn-new');
    if (btnNew) btnNew.addEventListener('click', () => {
      if (state.dirty && !window.confirm('当前作品有未保存的修改，仍要新建吗？')) return;
      newWork();
      switchTab('base');
    });
    $('btn-save').addEventListener('click', save);
    $('btn-publish').addEventListener('click', publish);
    $('btn-unpublish').addEventListener('click', unpublish);
    const btnDelete = $('btn-delete');
    if (btnDelete) btnDelete.addEventListener('click', removeWork);

    // 开场白：添加 / 删除 / 工具栏 / 全屏（事件委托）
    $('btn-add-greet').addEventListener('click', () => addGreetCard(true));
    const greetList = $('greet-list');
    greetList.addEventListener('mousedown', (e) => {
      if (e.target.closest('[data-cmd]')) e.preventDefault();
    });
    greetList.addEventListener('click', (e) => {
      const del = e.target.closest('[data-gdel]');
      if (del) {
        const card = del.closest('.greet-card');
        const box = $('greet-list');
        if (card) card.parentNode.removeChild(card);
        if (box && !box.querySelector('.greet-card')) addGreetCard(false);
        renumberGreets();
        markDirty();
        return;
      }
      const fs = e.target.closest('[data-gfs]');
      if (fs) {
        const rich = fs.closest('.rich-box');
        rich.classList.toggle('fs-open');
        const label = fs.querySelector('span');
        if (label) label.textContent = rich.classList.contains('fs-open') ? '退出全屏' : '全屏';
        return;
      }
      const cmd = e.target.closest('[data-cmd]');
      if (cmd) {
        greetCmd(cmd.getAttribute('data-cmd'), cmd.closest('.rich-box'));
      }
    });
    greetList.addEventListener('input', markDirty);
    // 破甲词 / 剧情选项：自定义开关
    ['break', 'next'].forEach((key) => {
      $(RICH_TOGGLES[key].sw).addEventListener('change', (e) => {
        applyToggleState(key, e.target.checked);
        markDirty();
        if (e.target.checked) {
          const wrap = $(RICH_TOGGLES[key].wrap);
          const area = wrap && wrap.querySelector('[data-garea]');
          if (area) area.focus();
        }
      });
    });
    // 从创意广场引用写作风格：直接回填写作风格编辑器（保存走 innerText → 纯文本回填）
    $('btn-writing-plaza').addEventListener('click', () => {
      openPlazaPicker('speaking_style', (text) => {
        const area = $('rich-writing') && $('rich-writing').querySelector('[data-garea]');
        if (area) area.innerHTML = richBodyText(text);
        markDirty();
        U.toast('已引用创意广场的写作风格');
      });
    });
    // 从创意广场引用破甲词：选中后自动打开自定义开关并回填
    $('btn-break-plaza').addEventListener('click', () => {
      openPlazaPicker('break_armor', (text) => {
        applyToggleState('break', true);
        const area = $('break-editor') && $('break-editor').querySelector('[data-garea]');
        if (area) area.innerHTML = richBody(text);
        markDirty();
        U.toast('已引用创意广场的破甲词');
      });
    });
    // 快捷指令：添加 / 删除 / 输入（事件委托）
    $('btn-add-qc').addEventListener('click', addQuickCard);
    const qcList = $('qc-list');
    qcList.addEventListener('input', markDirty);
    qcList.addEventListener('click', (e) => {
      const del = e.target.closest('[data-qdel]');
      if (!del) return;
      const card = del.closest('.qc-card');
      if (card) card.parentNode.removeChild(card);
      if (!qcList.querySelector('.qc-card')) {
        qcList.insertAdjacentHTML('beforeend', qcCardHtml({ title: '', content: '' }));
      }
      renumberQc();
      markDirty();
    });

    // 封面上传
    $('btn-upload').addEventListener('click', () => $('cover-file').click());
    $('cover-file').addEventListener('change', (e) => {
      const file = e.target.files && e.target.files[0];
      e.target.value = '';
      cropOpen(file);
    });
    $('btn-remove-cover').addEventListener('click', () => {
      state.work.avatarURL = '';
      state.work.coverURL = '';
      const el = $('f-avatar');
      if (el) el.value = '';
      updateSide();
      markDirty();
      U.toast('封面已移除');
    });

    // CG
    $('btn-cg-io').addEventListener('click', openCgIoPanel);
    $('btn-cg-upload').addEventListener('click', () => $('cg-file').click());
    $('cg-file').addEventListener('change', (e) => {
      const file = e.target.files && e.target.files[0];
      e.target.value = '';
      uploadCG(file);
    });
    $('btn-cg-refresh').addEventListener('click', loadCG);
    $('cg-mode').addEventListener('change', syncCgRuleUI);
    $('btn-cg-hint-save').addEventListener('click', () => {
      if (!state.cid) { U.toast('请先保存作品', false); return; }
      A.put('/api/gva/narratium/cg/hintRule', {
        characterId: state.cid,
        mode: $('cg-mode').value,
        customHintPrompt: $('cg-hint').value || '',
      }).then(() => U.toast('CG 规则已保存'))
        .catch((err) => U.toast('保存失败：' + err.message, false));
    });

    // 跳转
    $('btn-chat').addEventListener('click', () => {
      if (!state.cid) { U.toast('请先保存作品', false); return; }
      location.href = '/app/chat?id=' + encodeURIComponent(state.cid);
    });
    $('btn-worldbook').addEventListener('click', () => {
      if (!state.cid) { U.toast('请先保存作品', false); return; }
      location.href = '/app/worldbook?cid=' + encodeURIComponent(state.cid);
    });
    $('btn-preview').addEventListener('click', previewWork);

    // 右侧预览：详细介绍 / 提示词
    Array.prototype.forEach.call($('pv-tabs').querySelectorAll('button'), (b) => {
      b.addEventListener('click', () => switchPv(b.getAttribute('data-pv')));
    });
  }

  /* ---------------------------------------------------------- 右侧实时预览 */
  let pvMode = 'desc';
  let pvTimer = 0;

  /* 完整提示词预览：只取「提示词」页签的字段，段落之间横线分隔 */
  function pvPromptHtml(w) {
    // 只读取 promptData 里的字段（与角色卡 JSON 一一对应）
    const pd0 = T.isObj(w.promptData) ? w.promptData : {};
    const pd = {
      breakArmorWord: T.str(pd0.breakArmorWord),
      aiIdentityStylePrompt: T.str(pd0.aiIdentityStylePrompt),
      worldViewPrompt: T.str(pd0.worldViewPrompt),
      systemPrompt: T.str(pd0.systemPrompt),
      writingStylePrompt: T.str(pd0.writingStylePrompt),
      postPrompt: T.str(pd0.postPrompt),
    };
    const blocks = [
      ['破甲词', pd.breakArmorWord],
      ['AI 身份设定', pd.aiIdentityStylePrompt],
      ['世界观设定', pd.worldViewPrompt],
      ['主提示词', pd.systemPrompt],
      ['写作风格', pd.writingStylePrompt],
      ['下一步剧情选项', pd.postPrompt],
    ].filter((b) => T.str(b[1]).trim());
    if (!blocks.length) return '<div class="pv-empty">（还没有可预览的提示词）</div>';
    // 保留 <next_prompt> 这类提示词标记，只去掉普通 HTML 标签
    const plain = (s) => T.str(s).replace(/<(?!\/?next_prompt>)[^>]*>/gi, '');
    return '<div class="pv-doc"><h3 class="pv-title">完整提示词</h3><div class="pv-box">' +
      blocks.map((b, i) =>
        (i ? '<hr class="pv-sep">' : '') +
        '<div class="pv-sec"><h4>' + esc(b[0]) + '</h4><p>' +
        esc(plain(b[1])) + '</p></div>').join('') + '</div></div>';
  }

  /* 回复样式预览：白卡片渲染 + say/book_status/item 标签解析（与聊天页一致） */
  const PV_RICH_TAGS = /^(b|strong|i|em|u|s|br|p|span|say|book_status|item)$/i;

  function pvRenderRich(src) {
    let s = String(src || '');
    if (s.indexOf('<') < 0) return esc(s);

    // book_status 块重组（防御 LLM 未闭合 item）
    s = s.replace(/<book_status[^>]*>([\s\S]*?)(?=<\/?book_status[^>]*>|$)/gi, (m, inner) => {
      let body = inner.replace(/<\/?book_status[^>]*>/gi, '');
      const parts = body.split(/<item[^>]*>/i)
        .map(t => t.replace(/<\/item>/gi, '').trim())
        .filter(Boolean)
        .filter(t => /[\u4e00-\u9fa5a-zA-Z0-9]/.test(t));
      if (!parts.length) return '<book_status></book_status>';
      const items = parts.map((p) => {
        let fixed = p;
        fixed = fixed.replace(/<b>([^<]*?)<b>/gi, '<b>$1</b>');
        fixed = fixed.replace(/<b>([^<]*?)$/i, '<b>$1</b>');
        return '<item>' + fixed + '</item>';
      }).join('');
      return '<book_status>' + items + '</book_status>';
    });

    // 标签白名单过滤
    s = s.replace(/<\/?([a-zA-Z][\w-]*)([^>]*)>/g, (m, tag) => {
      if (!PV_RICH_TAGS.test(tag)) return '';
      const lower = tag.toLowerCase();
      if (m.startsWith('</')) return '</' + lower + '>';
      return '<' + lower + '>';
    });
    return s;
  }

  function pvReplyHtml(w) {
    const raw = mesExampleToReply(T.str(w.mesExample)).trim();
    if (!raw) return '<div class="pv-empty">（还没有可预览的回复样式）</div>';

    // 按空行拆成消息段
    const bubbles = raw.split(/\n\s*\n/).map((s) => s.trim()).filter(Boolean);
    const customCss = T.str(w.customCSS);

    const cards = bubbles.map((b) => {
      const mine = /^(?:<)?(?:用户|User|user|我)(?:>)?\s*[：:]/i.test(b);
      // 用户消息：直接转义
      if (mine) {
        return '<div class="pv-msg pv-user"><div class="pv-bubble-user">' + esc(b) + '</div></div>';
      }
      // AI 消息：白卡片 + 富文本解析
      const parsed = pvRenderRich(b);
      return '<div class="pv-msg pv-ai">' +
        '<div class="pv-card">' +
          '<div class="pv-card-body content-renderer-v3-container theme-light markdown-body" data-pv-bubble>' +
            parsed +
          '</div>' +
        '</div>' +
      '</div>';
    }).join('');

    return '<div class="pv-doc"><h3 class="pv-title">回复样式</h3>' +
      (customCss ? '<style class="pv-custom-css">' + customCss + '</style>' : '') +
      '<div class="pv-box"><div class="pv-chat pv-chat-white">' + cards + '</div></div></div>';
  }

  function renderLive() {
    const w = collect();
    const name = T.str(w.name) || '未命名作品';
    const cover = T.str(w.avatarURL);
    $('pv-name').textContent = name;
    const img = $('pv-cover');
    img.src = cover || U.placeholder(name.slice(0, 1), 1);
    U.safeImg(img, name, 1);
    const ext = extensionsOf(w);
    const sub = [ext.orientation, ext.type, ext.source, ext.personality].filter(Boolean).join(' / ');
    $('pv-sub').textContent = sub || T.str(w.summary, '暂无简介');
    $('pv-chips').innerHTML = (w.tags || [])
      .map((t) => '<span class="tag">' + esc(t) + '</span>').join('');

    const live = $('pv-live');
    const prompt = $('pv-prompt');
    const reply = $('pv-reply');
    live.hidden = pvMode !== 'desc';
    prompt.hidden = pvMode !== 'prompt';
    reply.hidden = pvMode !== 'reply';
    if (pvMode === 'prompt') { prompt.innerHTML = pvPromptHtml(w); return; }
    if (pvMode === 'reply') { reply.innerHTML = pvReplyHtml(w); return; }
    const desc = T.str(w.description);
    if (!desc) {
      live.innerHTML = '<div class="pv-empty">详细介绍的内容将在这里显示</div>';
      return;
    }
    let f = live.querySelector('iframe');
    if (!f) {
      live.innerHTML = '<iframe sandbox="allow-scripts" title="详细介绍预览"></iframe>';
      f = live.querySelector('iframe');
    }
    f.srcdoc = descDoc(desc);
  }

  function scheduleLive() {
    if (pvTimer) clearTimeout(pvTimer);
    pvTimer = setTimeout(() => { pvTimer = 0; renderLive(); }, 400);
  }

  function switchPv(mode) {
    pvMode = (mode === 'prompt' || mode === 'reply') ? mode : 'desc';
    Array.prototype.forEach.call($('pv-tabs').querySelectorAll('button'), (b) => {
      b.classList.toggle('active', b.getAttribute('data-pv') === pvMode);
    });
    $('pv-hint').textContent = pvMode === 'desc' ? '实时' : '随输入更新';
    renderLive();
  }

  function previewWork() {
    const w = collect();
    const cover = T.str(w.avatarURL);
    const desc = T.str(w.description);
    const descBlock = !desc
      ? '<div class="tiny muted">（还没有详情描述）</div>'
      : '<iframe class="desc-frame" sandbox="allow-scripts" title="详情描述" id="pv-desc"></iframe>';
    U.openModal({
      title: '角色卡预览',
      html: '<div class="row" style="align-items:flex-start;gap:14px">' +
        '<img src="' + esc(cover || U.placeholder(T.str(w.name).slice(0, 1), 1)) + '" alt="" ' +
        'style="width:120px;height:120px;border-radius:14px;object-fit:cover">' +
        '<div class="grow"><h2 style="margin:0 0 4px">' + esc(T.str(w.name, '未命名作品')) + '</h2>' +
        '<p class="muted" style="margin:0 0 8px">' + esc(T.str(w.summary, '暂无简介')) + '</p>' +
        '<div class="row">' + (w.tags || []).map((t) => '<span class="tag">' + esc(t) + '</span>').join('') + '</div>' +
        '</div></div>' +
        '<hr style="border:0;border-top:1px solid var(--line);margin:14px 0">' +
        '<div class="small muted" style="white-space:pre-wrap;margin-bottom:12px">' +
        esc(T.str(w.firstMes, '（还没有开场白）')) + '</div>' +
        '<div class="tiny muted" style="margin-bottom:6px">详情描述</div>' + descBlock,
      onMount(m) {
        const f = m.querySelector('#pv-desc');
        if (f) f.srcdoc = descDoc(desc);
      },
    });
  }

  /* ---------------------------------------------------------- 启动 */
  function init() {
    U.mountTopbar('creation');
    bind();
    switchTab('base');
    loadWorks();
    loadPublishPolicy();
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
