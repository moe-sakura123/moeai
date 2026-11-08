/* pages/draw.js — AI 绘图：模型配方 / 提示词构建器 / 生成 / 我的图库
   接口：GET|POST /api/local/gallery/*（works / generate / like / delete） */
(function () {
  'use strict';
  const A = App.api;
  const U = App.ui;
  const T = App.util;
  const $ = (id) => document.getElementById(id);
  const esc = U.esc;
  const G = '/api/local/gallery';

  // 尺寸不再本地硬编码：比例 / 档位 / 精确像素全部由 /gallery/models 的
  // sizeOptions（数据库 image_sizes 表）驱动。
  const FALLBACK_MODEL = {
    ID: 2, modelId: 'local-placeholder', modelName: '本地占位绘图',
    providerName: '本地', providerKind: 'local', isDefault: true,
    capabilities: { ratio: true, tier: true, n: true, seed: true, negative: false },
  };
  const BUILDER = [
    { key: '画质', items: ['杰作', '超清', '8K', '细节丰富'] },
    { key: '风格', items: ['写实', '动漫', '赛博朋克', '水墨', '油画'] },
    { key: '光照', items: ['霓虹灯光', '柔光', '逆光', '体积光'] },
    { key: '镜头', items: ['电影感', '广角', '特写', '景深'] },
  ];
  const picked = new Set();
  const CATALOG = '/api/gva/aiMedia/creator/catalog';

  const state = {
    models: [],
    sizeOptions: [],
    charge: { pointsPerImage: 0, freeLocal: true },
    templates: [],
    videoModels: [],
    videoProviders: [],
    selectedVideoId: 0,
    selectedVideo: null,
    recipe: null,
    fields: {},
    autoNegative: '',
    me: {},
    points: null,
    providers: 0,
    rateId: 0,
    scope: 'new',
    works: [],
    busy: false,
  };

  /* ---------------------------------------------------------- 侧栏 */
  function modelList() {
    return state.models.length ? state.models : [FALLBACK_MODEL];
  }

  function currentModel() {
    const list = modelList();
    return list.find((m) => Number(m.ID) === Number(state.rateId))
      || list.find((m) => m.isDefault) || list[0];
  }

  function renderMe() {
    const box = $('me-box');
    const me = state.me || {};
    const name = me.name || (me.loggedIn ? '本地用户' : '未登录');
    const pts = state.points === null || state.points === undefined ? '—' : state.points;
    box.innerHTML =
      '<div class="row between">' +
      '<b style="font-size:13px">' + esc(name) + '</b>' +
      '<span class="badge">积分 ' + esc(pts) + '</span></div>' +
      '<p class="tiny muted" style="margin:8px 0 0">' +
      (me.loggedIn ? '生成记录保存在本地图库' : '登录后才能生成、点赞和删除') + '</p>' +
      (me.loggedIn ? '' : '<button class="btn sm primary" id="me-login" style="margin-top:10px">登录</button>');
    const btn = $('me-login');
    if (btn) btn.addEventListener('click', () => {
      U.loginDialog(() => loadGallery());
    });
  }

  function renderModels() {
    const wrap = $('model-list');
    const list = modelList();
    if (state.selectedVideo) {
      state.rateId = 0;                       // 视频模式下不默认勾选绘图模型
    } else if (!list.some((m) => Number(m.ID) === Number(state.rateId))) {
      const def = list.find((m) => m.isDefault) || list[0];
      state.rateId = Number(def.ID);
    }
    wrap.innerHTML = list.map((m) => {
      const on = !state.selectedVideo && Number(m.ID) === Number(state.rateId);
      const local = m.providerKind === 'local';
      const name = local ? '本地占位绘图' : (m.modelName || m.modelId || '未命名模型');
      const desc = local ? '不联网，按提示词生成占位图' : (m.providerName || '接入') + ' · 按接入计费';
      return '<button type="button" class="model-card' + (on ? ' on' : '') +
        '" data-rate="' + esc(m.ID) + '">' +
        '<span class="dot"></span><span class="grow"><b>' + esc(name) + '</b>' +
        '<small>' + esc(desc) + '</small></span>' +
        '<span class="badge' + (local ? ' ok' : '') + '">' + (local ? '免费' : '积分') + '</span></button>';
    }).join('');
    syncSizeControls();
    applyCapabilities();
    updatePoints();
    const hint = $('model-hint');
    if (hint) {
      hint.textContent = state.providers
        ? '已接入 ' + state.providers + ' 个绘图模型'
        : '未接入真实模型：本地占位绘图';
    }
  }

  // ------------------------------------------------ 尺寸（数据库驱动）
  const CUSTOM_RATIO = 'custom';
  const CUSTOM_MIN = 64, CUSTOM_MAX = 8192, CUSTOM_MAX_PIXELS = 20000000;

  function enabledSizes() {
    return (state.sizeOptions || []).filter((o) => o && o.enabled);
  }

  function isCustomSize() {
    const el = $('ratio');
    return !!el && el.value === CUSTOM_RATIO;
  }

  // 读取自定义宽高并校验：整数、64–8192 px、总量 ≤2000 万像素
  function customSize() {
    const wEl = $('width'), hEl = $('height');
    if (!wEl || !hEl) return { error: '自定义宽高输入框未就绪' };
    const w = Number(wEl.value), h = Number(hEl.value);
    const notInt = (v) => !Number.isFinite(v) || Math.floor(v) !== v;
    if (notInt(w) || notInt(h)) return { error: '宽度和高度必须是整数 px' };
    if (w < CUSTOM_MIN || w > CUSTOM_MAX || h < CUSTOM_MIN || h > CUSTOM_MAX) {
      return { error: '宽高需在 ' + CUSTOM_MIN + '–' + CUSTOM_MAX + ' px 之间（当前 ' + w + '×' + h + '）' };
    }
    if (w * h > CUSTOM_MAX_PIXELS) {
      return { error: '像素总量过大（上限 2000 万，当前 ' + w + '×' + h + '）' };
    }
    return { width: w, height: h };
  }

  // 本地估算最接近的官方组合（后端以 image_sizes 表为准，这里只做预览提示）
  function nearestOfficial(w, h) {
    const opts = enabledSizes();
    if (!opts.length) return null;
    const target = w / h;
    const ratios = [];
    opts.forEach((o) => { if (ratios.indexOf(o.ratio) < 0) ratios.push(o.ratio); });
    let bestRatio = '', bestGap = Infinity;
    ratios.forEach((r) => {
      const parts = String(r).split(':');
      const rv = Number(parts[0]) / Number(parts[1]);
      if (!Number.isFinite(rv) || rv <= 0) return;
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

  // 自定义模式下隐藏档位下拉，换成宽高输入框
  function toggleCustomInputs() {
    const custom = isCustomSize();
    const tierField = $('tier-field');
    const wf = $('width-field'), hf = $('height-field');
    if (tierField) tierField.hidden = custom;
    if (wf) wf.hidden = !custom;
    if (hf) hf.hidden = !custom;
  }

  function currentSize() {
    const ratio = $('ratio').value;
    const tier = $('tier').value;
    return enabledSizes().find((o) => o.ratio === ratio && o.tier === tier) || null;
  }

  function sizePoints(opt) {
    const model = currentModel() || {};
    if (model.providerKind === 'local' && state.charge.freeLocal) return 0;
    if (opt && Number(opt.points) > 0) return Number(opt.points);
    return Number(state.charge.pointsPerImage) || 0;
  }

  function fillTiers(ratio, keepTier) {
    const opts = enabledSizes().filter((o) => o.ratio === ratio);
    const tierSel = $('tier');
    tierSel.innerHTML = opts.map((o) =>
      '<option value="' + esc(o.tier) + '">' + esc(o.tier) + '</option>').join('');
    if (opts.some((o) => o.tier === keepTier)) tierSel.value = keepTier;
    else if (opts.length) tierSel.value = opts[0].tier;
    updateSizePixels();
  }

  function updateSizePixels() {
    const px = $('size-pixels');
    if (!px) return;
    px.style.color = '';
    if (isCustomSize()) {
      const c = customSize();
      if (c.error) {
        px.style.color = '#fecaca';
        px.textContent = c.error;
        return;
      }
      const near = nearestOfficial(c.width, c.height);
      const per = sizePoints(near ? near.opt : null);
      let text = '自定义 ' + c.width + ' × ' + c.height +
        '（单张 ' + (per > 0 ? per + ' 积分' : '免费') + '）';
      if (near) {
        const exact = near.opt.width === c.width && near.opt.height === c.height;
        text += exact
          ? '；精确命中官方 ' + near.opt.ratio + ' ' + near.opt.tier + ' → ' + near.opt.width + ' × ' + near.opt.height
          : '；上游将按官方 ' + near.opt.ratio + ' ' + near.opt.tier + ' → ' +
            near.opt.width + ' × ' + near.opt.height + ' 输出';
        if (!exact && near.gap > Math.log(1.15)) {
          text += '（宽高比与 8 种官方比例差别较大，可能被进一步归一化）';
        }
      }
      px.textContent = text;
      return;
    }
    const opt = currentSize();
    if (!opt) {
      px.textContent = '该比例下没有可用档位，请换一个比例';
      return;
    }
    const per = sizePoints(opt);
    px.textContent = opt.ratio + ' · ' + opt.tier + ' → ' + opt.width + ' × ' + opt.height +
      '（单张 ' + (per > 0 ? per + ' 积分' : '免费') + '）';
  }

  function syncSizeControls() {
    const ratioSel = $('ratio');
    const tierSel = $('tier');
    if (!ratioSel || !tierSel) return;
    const opts = enabledSizes();
    if (!opts.length) {
      ratioSel.innerHTML = '<option value="">尺寸未加载</option>' +
        '<option value="' + CUSTOM_RATIO + '">自定义</option>';
      tierSel.innerHTML = '<option value="">—</option>';
      const px = $('size-pixels');
      if (px) px.textContent = '尺寸选项来自 /api/local/gallery/models；加载失败时请点刷新重试';
      toggleCustomInputs();
      return;
    }
    const ratios = [];
    opts.forEach((o) => { if (ratios.indexOf(o.ratio) < 0) ratios.push(o.ratio); });
    const keepR = ratioSel.value;
    ratioSel.innerHTML = ratios.map((r) =>
      '<option value="' + esc(r) + '">' + esc(r) + '</option>').join('') +
      '<option value="' + CUSTOM_RATIO + '">自定义</option>';
    const wantR = keepR === CUSTOM_RATIO ? CUSTOM_RATIO
      : (ratios.indexOf(keepR) >= 0 ? keepR
        : (ratios.indexOf('1:1') >= 0 ? '1:1' : ratios[0]));
    ratioSel.value = wantR;
    toggleCustomInputs();
    if (isCustomSize()) updateSizePixels();
    else fillTiers(wantR, tierSel.value);
    const btn = $('btn-generate');
    const model = currentModel() || {};
    if (btn && model.providerKind === 'local') btn.textContent = '开始生成 · 本地免费';
    else if (btn) btn.textContent = '开始生成 · 按接入计费';
  }

  // 模型实际能力：种子上游不支持就禁用，避免留一个不生效的控件
  function applyCapabilities() {
    const model = currentModel() || {};
    const cap = model.capabilities || {};
    const seedEl = $('seed');
    const seedField = $('seed-field');
    const noSeed = cap.seed === false;
    if (seedEl) {
      seedEl.disabled = noSeed;
      if (noSeed) seedEl.value = '0';
    }
    if (seedField) {
      const label = seedField.querySelector('label');
      if (label) label.innerHTML = '种子' + (noSeed
        ? ' <span class="hint">当前模型不支持</span>' : '');
      seedField.title = noSeed
        ? 'Agnes 官方上游不接受 seed；只有本地占位绘图可按种子复现' : '';
    }
    const negEl = $('negative');
    const noNeg = cap.negative === false;
    if (negEl) {
      negEl.disabled = noNeg;
      if (noNeg) negEl.value = '';
    }
    const negHint = $('negative-hint');
    if (negHint) {
      negHint.textContent = noNeg
        ? '当前模型不支持反向提示词'
        : (cap.negativeViaPrompt ? '排除语义会并入提示词下发' : '直接作为负向词下发');
    }
    const negField = $('negative-field');
    if (negField) negField.title = noNeg ? '本地占位绘图不使用 negative' : '';
  }

  function updatePoints() {
    const n = Number($('num').value) || 1;
    let per;
    if (isCustomSize()) {
      const c = customSize();
      const near = c.error ? null : nearestOfficial(c.width, c.height);
      per = sizePoints(near ? near.opt : null);
    } else {
      per = sizePoints(currentSize());
    }
    const total = per * n;
    const bal = state.points;
    const head = '本次 ' + (total > 0 ? total + ' 积分' : '免费') +
      '（' + (per > 0 ? per : 0) + ' × ' + n + ' 张）';
    $('points').textContent = head +
      (bal === null || bal === undefined ? '' : ' · 余额 ' + bal);
  }

  /* ---------------------------------------------------------- 构建器 */
  function renderBuilder() {
    $('builder').innerHTML = BUILDER.map((g) =>
      '<div class="row" style="gap:6px"><span class="tiny muted" style="width:28px">' + esc(g.key) + '</span>' +
      '<div class="chips">' + g.items.map((t) =>
        '<button type="button" data-chip="' + esc(t) + '"' +
        (picked.has(t) ? ' class="on"' : '') + '>' + esc(t) + '</button>').join('') +
      '</div></div>').join('');
    updatePreview();
  }

  // 按 BUILDER 分组顺序输出（画质 → 风格 → 光照 → 镜头），与点选先后无关
  function orderedPicked() {
    const out = [];
    BUILDER.forEach((g) => g.items.forEach((t) => {
      if (picked.has(t)) out.push(t);
    }));
    return out;
  }

  function updatePreview() {
    $('builder-preview').textContent = picked.size ? orderedPicked().join('，') : '（未选择）';
    $('builder-apply').disabled = !picked.size;
  }

  function applyBuilder() {
    if (!picked.size) return;
    const input = $('prompt');
    const add = orderedPicked().join('，');
    const text = (input.value || '').trim();
    input.value = text ? (text.replace(/[，,]\s*$/, '') + '，' + add) : add;
    picked.clear();
    renderBuilder();
    input.focus();
    if (state.recipe) refreshPreview();
    U.toast('已追加 ' + add.split('，').length + ' 个风格词');
  }

  /* ---------------------------------------------------------- 模板 */
  function renderVideoModels() {
    const wrap = $('video-model-list');
    if (!wrap) return;
    const cards = [];
    if (state.videoModels.length) {
      cards.push(state.videoModels.map((r) => {
        const on = !!state.recipe && state.recipe.recipeKey === r.recipeKey;
        const points = Number((r.price || {}).points) || 0;
        return '<button type="button" class="model-card video' + (on ? ' on' : '') +
          '" data-video="' + esc(r.recipeKey) + '">' +
          '<span class="dot"></span><span class="grow"><b>' + esc(T.str(r.name, '未命名视频模型')) + '</b>' +
          '<small>' + esc(T.str(r.description, '文生视频')) + '</small></span>' +
          '<span class="badge">' + (points > 0 ? points + ' 积分' : '积分') + '</span></button>';
      }).join(''));
    }
    if (state.videoProviders.length) {
      cards.push('<p class="tiny muted" style="margin:6px 0;line-height:1.6">' +
        '控制台已接入（选中后点「生成」即创建视频任务）</p>');
      cards.push(state.videoProviders.map((m) => {
        const id = Number(m.id) || 0;
        const sv = state.selectedVideo;
        const on = !!sv && String(sv.modelID) === String(m.modelID)
          && Number(sv.providerId) === Number(m.providerId);
        return '<button type="button" class="model-card video' + (on ? ' on' : '') +
          '" data-video-provider="' + id + '" data-model="' + esc(m.modelID) + '">' +
          '<span class="dot"></span><span class="grow"><b>' + esc(T.str(m.modelID, '视频模型')) + '</b>' +
          '<small>' + esc(T.str(m.providerName, '视频接入') +
            (m.isDefault ? ' · 默认' : '')) + '</small></span>' +
          '<span class="badge">视频</span></button>';
      }).join(''));
    }
    if (!cards.length) {
      wrap.innerHTML = '<p class="tiny muted" style="margin:0;line-height:1.6">' +
        '暂无视频模型 · 在控制台「模型接入 → 视频接入」检测模型，或导入 video 模板</p>';
      return;
    }
    wrap.innerHTML = cards.join('');
  }


  function loadTemplates() {
    A.get(CATALOG, {}, { timeout: 8000 })
      .then((data) => {
        const cats = T.asList(data, ['categories']).filter((c) => T.isObj(c));
        state.templates = [];
        state.videoModels = [];
        const sel = $('template');
        let html = '<option value="">自定义（不使用模板）</option>';
        cats.forEach((cat) => {
          const recipes = T.asList(cat, ['recipes']).filter((r) => T.isObj(r) && T.str(r.recipeKey));
          if (!recipes.length) return;
          html += '<optgroup label="' + esc(T.str(cat.name)) + '">' +
            recipes.map((r) => {
              state.templates.push(r);
              if (T.str(r.outputMediaType) === 'video') state.videoModels.push(r);
              return '<option value="' + esc(r.recipeKey) + '">' + esc(T.str(r.name)) + '</option>';
            }).join('') + '</optgroup>';
        });
        if (sel) sel.innerHTML = html;
        renderVideoModels();
      })
      .catch(() => renderVideoModels());
  }

  // 控制台「视频接入」里已检测的模型（仅展示，生成接口待接入）
  function loadVideoProviders() {
    A.get('/api/local/gallery/video-models', {}, { timeout: 8000 })
      .then((data) => {
        state.videoProviders = T.asList(data, ['models'])
          .filter((m) => T.isObj(m) && T.str(m.modelID));
        renderVideoModels();
      })
      .catch(() => {});
  }

  function selectVideoModel(key) {
    const recipe = state.videoModels.find((r) => r.recipeKey === key);
    if (!recipe) return;
    state.selectedVideoId = 0;
    state.selectedVideo = null;
    applyTemplate(key);
    U.toast('已选择视频模板：' + T.str(recipe.name, '视频模型'));
  }

  function selectVideoProvider(id, modelID) {
    const m = state.videoProviders.find((x) => Number(x.id) === Number(id)
      && (!modelID || String(x.modelID) === String(modelID)));
    if (!m) return;
    state.selectedVideo = m;
    state.selectedVideoId = Number(id) || 0;
    state.rateId = 0;                         // 取消绘图模型的选中态
    applyTemplate('');                        // 选视频模型时退出图片模板，避免表单参数互相干扰
    renderModels();
    renderVideoModels();
    U.toast('已选择视频模型：' + T.str(m.modelID, '视频模型') + '，点「生成」开始出视频');
  }

  function applyTemplate(key) {
    const recipe = state.templates.find((r) => r.recipeKey === key) || null;
    const prevAuto = state.autoNegative;
    state.recipe = recipe;
    state.fields = recipe ? templateDefaults(recipe) : {};
    if (recipe) state.selectedVideo = null;   // 选图片模板即退出视频模式

    const sel = $('template');
    const want = recipe ? recipe.recipeKey : '';
    if (sel && sel.value !== want) sel.value = want;

    const neg = $('negative');
    const label = $('prompt-label');
    const promptEl = $('prompt');
    const fieldsBox = $('template-fields');
    const preview = $('template-preview');
    const sizeField = $('size-field');

    if (!recipe) {
      // 切回自定义：恢复默认表单，清掉模板自动填的反向词
      if (neg && prevAuto && (neg.value || '') === prevAuto) neg.value = '';
      state.autoNegative = '';
      label.innerHTML = '提示词 <span class="req">*</span>';
      promptEl.placeholder = '描述画面：人物、场景、光线、构图…';
      fieldsBox.hidden = true;
      fieldsBox.innerHTML = '';
      preview.hidden = true;
      preview.textContent = '';
      sizeField.hidden = false;
      $('size-pixels').hidden = false;
      syncSizeControls();
      $('template-hint').textContent = '';
      renderVideoModels();
      return;
    }

    const promptField = (recipe.schema.fields || []).find((f) => f.key === 'prompt') || {};
    label.innerHTML = esc(T.str(promptField.label, '画面描述')) +
      (promptField.required ? ' <span class="req">*</span>' : '');
    promptEl.placeholder = T.str(promptField.placeholder, '描述画面…');
    sizeField.hidden = true;
    $('size-pixels').hidden = true;          // 模板自带比例，尺寸由模板解析
    preview.hidden = false;
    $('template-hint').textContent = T.str(recipe.description, '');
    renderTemplateFields(recipe);
    // 反向词只在空着或仍是上一个模板自动填的值时才跟随切换
    if (neg && (!(neg.value || '').trim() || (neg.value || '') === prevAuto)) {
      neg.value = T.str((recipe.local || {}).negative, '');
      state.autoNegative = neg.value;
    } else {
      state.autoNegative = prevAuto;
    }
    renderVideoModels();
    refreshPreview();
  }

  function templateDefaults(recipe) {
    const out = {};
    (recipe.schema.fields || []).forEach((f) => {
      if (f.type === 'select') {
        const opts = f.options || [];
        out[f.key] = T.str(f.defaultValue, opts.length ? T.str(opts[0].value) : '');
      } else {
        out[f.key] = T.str(f.defaultValue, '');
      }
    });
    return out;
  }

  function renderTemplateFields(recipe) {
    const box = $('template-fields');
    const list = (recipe.schema.fields || []).filter((f) => f.key !== 'prompt');
    if (!list.length) {
      box.hidden = true;
      box.innerHTML = '';
      return;
    }
    box.innerHTML = list.map((f) => {
      const val = T.str(state.fields[f.key]);
      let control;
      if (f.type === 'select') {
        control = '<select class="select" data-tf="' + esc(f.key) + '">' +
          (f.options || []).map((o) => '<option value="' + esc(T.str(o.value)) + '"' +
            (T.str(o.value) === val ? ' selected' : '') + '>' +
            esc(T.str(o.label, o.value)) + '</option>').join('') + '</select>';
      } else if (f.type === 'textarea') {
        control = '<textarea class="textarea" rows="2" data-tf="' + esc(f.key) +
          '" placeholder="' + esc(T.str(f.placeholder)) + '">' + esc(val) + '</textarea>';
      } else {
        control = '<input class="input" data-tf="' + esc(f.key) +
          '" value="' + esc(val) + '" placeholder="' + esc(T.str(f.placeholder)) + '">';
      }
      return '<div class="field" style="margin:0"><label>' + esc(T.str(f.label, f.key)) +
        (f.required ? ' <span class="req">*</span>' : '') + '</label>' + control + '</div>';
    }).join('');
    box.hidden = false;
    box.querySelectorAll('[data-tf]').forEach((el) => {
      const key = el.getAttribute('data-tf');
      const ev = el.tagName === 'SELECT' ? 'change' : 'input';
      el.addEventListener(ev, () => {
        state.fields[key] = el.value;
        refreshPreview();
      });
    });
  }

  let previewTimer = 0;
  function refreshPreview() {
    if (!state.recipe) return;
    clearTimeout(previewTimer);
    previewTimer = setTimeout(() => {
      if (!state.recipe) return;
      const fields = collectFields();
      A.post(G + '/template-prompt', {
        recipeKey: state.recipe.recipeKey, fields,
      }).then((data) => {
        if (!state.recipe) return;
        const p = T.str(T.pick(data, ['prompt'], ''));
        const size = T.str(T.pick(data, ['size'], ''));
        const negText = T.str(T.pick(data, ['negative'], ''));
        $('template-preview').textContent = p
          ? ('最终提示词：' + p + (size ? '　尺寸：' + size.replace('x', ' × ') : ''))
          : '';
        const neg = $('negative');
        if (neg && (!(neg.value || '').trim() || (neg.value || '') === state.autoNegative)) {
          neg.value = negText;
          state.autoNegative = negText;
        }
      }).catch(() => {});
    }, 250);
  }

  function collectFields() {
    const out = Object.assign({}, state.fields);
    const pf = state.recipe
      ? (state.recipe.schema.fields || []).find((f) => f.key === 'prompt') : null;
    if (pf) out.prompt = ($('prompt').value || '').trim();
    return out;
  }

  /* ---------------------------------------------------------- 图库 */
  function loadGallery() {
    A.get(G + '/works', {
      limit: 60,
      sort: state.scope === 'hot' ? 'hot' : 'new',
      mine: state.scope === 'mine' ? 1 : '',
    }, { timeout: 10000 })
      .then((data) => {
        state.works = T.asList(data, ['works']).filter((x) => T.isObj(x));
        state.models = T.asList(data, ['models']);
        state.sizeOptions = T.asList(data, ['sizeOptions']).filter((o) => T.isObj(o));
        state.charge = T.pick(data, ['charge'], state.charge) || state.charge;
        state.me = T.pick(data, ['me'], {});
        state.points = T.pick(data, ['points'], null);
        state.providers = T.num(T.pick(data, ['providers'], 0));
        renderMe();
        renderModels();
        renderGallery();
      })
      .catch((err) => {
        $('gallery').innerHTML = U.emptyHtml('图库加载失败', err.message || '请稍后重试');
        U.toast(err.message || '加载失败', false);
      });
  }

  function renderGallery() {
    syncScopes();
    const grid = $('gallery');
    if (!state.works.length) {
      grid.innerHTML = U.emptyHtml(
        state.scope === 'mine' ? '你还没有生成作品' : '图库还是空的',
        state.scope === 'mine' ? '写一句提示词，生成第一张图吧' : '生成后作品会自动出现在这里');
      return;
    }
    grid.innerHTML = '<div class="gallery-grid">' + state.works.map((w) => {
      const id = T.pick(w, ['id'], '');
      const url = T.str(T.pick(w, ['url_key', 'url', 'path'], ''));
      const prompt = T.str(T.pick(w, ['prompt'], '')) || '（无提示词）';
      const author = T.str(T.pick(w, ['author'], '匿名'));
      const likes = T.num(T.pick(w, ['likes'], 0));
      const when = U.fmtTime(T.pick(w, ['created_at'], ''));
      const isVideo = T.str(T.pick(w, ['kind'], '')) === 'video';
      return '<div class="gallery-item">' +
        (isVideo
          ? '<video src="' + esc(url) + '" muted playsinline preload="metadata" controls' +
            ' data-id="' + esc(id) + '"></video>'
          : '<img alt="" src="' + esc(url) + '" data-id="' + esc(id) + '" data-fallback="' +
            esc(prompt.slice(0, 1) || '图') + '">') +
        '<div class="g-body"><p title="' + esc(prompt) + '">' + esc(prompt) + '</p>' +
        '<div class="g-foot"><span class="grow" title="' + esc(author) + '">' + esc(author) +
        (when ? ' · ' + esc(when) : '') + '</span>' +
        '<button class="btn sm ghost" data-like="' + esc(id) + '">♥ ' + likes + '</button>' +
        '<button class="btn sm ghost" data-del="' + esc(id) + '">删除</button>' +
        '</div></div></div>';
    }).join('') + '</div>';
    grid.querySelectorAll('img[data-fallback]').forEach((img, i) => {
      U.safeImg(img, img.getAttribute('data-fallback') || '图', i + 1);
    });
    grid.querySelectorAll('img[data-id], video[data-id]').forEach((el) => {
      el.addEventListener('click', () => openImage(el.getAttribute('data-id')));
    });
    grid.querySelectorAll('[data-like]').forEach((btn) => {
      btn.addEventListener('click', () => likeWork(btn.getAttribute('data-like')));
    });
    grid.querySelectorAll('[data-del]').forEach((btn) => {
      btn.addEventListener('click', () => deleteWork(btn.getAttribute('data-del')));
    });
  }

  function syncScopes() {
    const map = { new: 'scope-new', hot: 'scope-hot', mine: 'scope-mine' };
    Object.keys(map).forEach((k) => {
      const b = $(map[k]);
      if (b) b.classList.toggle('primary', state.scope === k);
    });
  }

  function openImage(id) {
    const w = state.works.find((x) => String(x.id) === String(id)) || {};
    const url = T.str(T.pick(w, ['url_key', 'url', 'path'], ''));
    const isVideo = T.str(T.pick(w, ['kind'], '')) === 'video';
    U.openModal({
      title: isVideo ? '查看视频' : '查看图片',
      html: (isVideo
        ? '<video controls autoplay playsinline src="' + esc(url) +
          '" style="max-width:100%;border-radius:10px;background:#000"></video>'
        : '<img alt="" src="' + esc(url) + '" style="max-width:100%;border-radius:10px">') +
        '<p class="small muted" style="margin:10px 0 0;white-space:pre-wrap">' +
        esc(T.str(T.pick(w, ['prompt'], ''))) + '</p>',
    });
  }

  function likeWork(id) {
    A.post(G + '/like', { id: Number(id) })
      .then((data) => {
        const likes = T.num(T.pick(data, ['likes'], 0));
        const w = state.works.find((x) => String(x.id) === String(id));
        if (w) w.likes = likes;
        renderGallery();
        U.toast('已点赞');
      })
      .catch((err) => U.toast(err.message || '点赞失败', false));
  }

  function deleteWork(id) {
    U.confirmDialog('删除图片', '删除后无法恢复，确定删除这张图吗？', () => {
      A.post(G + '/delete', { id: Number(id) })
        .then(() => {
          state.works = state.works.filter((x) => String(x.id) !== String(id));
          renderGallery();
          U.toast('已删除');
        })
        .catch((err) => U.toast(err.message || '删除失败', false));
    });
  }

  /* ---------------------------------------------------------- 生成 */
  function generate(e) {
    e.preventDefault();
    if (state.busy) return;
    // 构建器里已选但没点「追加」的词，生成时自动补进提示词，避免白选
    if (picked.size) applyBuilder();
    const prompt = ($('prompt').value || '').trim();
    const hint = $('hint');
    hint.className = 'small';
    hint.style.color = '';
    if (state.recipe) {
      const fields = collectFields();
      if (!fields.prompt) {
        hint.textContent = '先写一句画面描述吧';
        hint.style.color = '#fecaca';
        $('prompt').focus();
        return;
      }
      const payload = { recipeKey: state.recipe.recipeKey, fields };
      U.requireLogin().then((ok) => {
        if (!ok) return;
        runGenerate(prompt, payload);
      });
      return;
    }
    if (!prompt) {
      hint.textContent = '先写一句提示词吧';
      hint.style.color = '#fecaca';
      $('prompt').focus();
      return;
    }
    if (state.selectedVideo) {
      U.requireLogin().then((ok) => {
        if (!ok) return;
        runGenerateVideo(prompt);
      });
      return;
    }
    U.requireLogin().then((ok) => {
      if (!ok) return;
      runGenerate(prompt);
    });
  }

  function runGenerate(prompt, tpl) {
    const extra = tpl || {};
    const btn = $('btn-generate');
    const progress = $('progress');
    const hint = $('hint');
    const seedEl = $('seed');
    const negEl = $('negative');
    const opt = currentSize();
    const seed = seedEl.disabled ? 0 : Math.max(0, Math.min(2147483647, Number(seedEl.value) || 0));
    const n = Number($('num').value) || 1;
    let custom = null;
    if (!extra.recipeKey && isCustomSize()) {
      const c = customSize();
      if (c.error) {
        hint.textContent = c.error;
        hint.style.color = '#fecaca';
        return;
      }
      custom = c;
    }
    const payload = extra.recipeKey
      // 模板：比例由模板字段决定，后端按 pick_size 反查数据库尺寸表
      ? {
        prompt,
        negative: negEl.disabled ? '' : (negEl.value || '').trim(),
        rateId: Number(state.rateId),
        n,
        seed,
        recipeKey: extra.recipeKey,
        fields: extra.fields || {},
      }
      // 自定义宽高：ratio="custom" + width/height；后端先精确匹配官方表，
      // 匹配不到再归一化为最接近的官方比例 + 档位，并回传 requested/actual
      : custom
        ? {
          prompt,
          negative: negEl.disabled ? '' : (negEl.value || '').trim(),
          rateId: Number(state.rateId),
          ratio: CUSTOM_RATIO,
          width: custom.width,
          height: custom.height,
          n,
          seed,
        }
        // 官方语义 size=档位、ratio=比例；查询不到后端会明确报错
        : {
          prompt,
          negative: negEl.disabled ? '' : (negEl.value || '').trim(),
          rateId: Number(state.rateId),
          size: opt ? opt.tier : '1K',
          ratio: opt ? opt.ratio : '1:1',
          n,
          seed,
        };
    state.busy = true;
    btn.disabled = true;
    btn.textContent = '生成中…';
    progress.hidden = false;
    $('progress-text').textContent = '正在生成，请稍候…（真实模型通常 5～40 秒）';
    hint.textContent = '';
    hint.style.color = '';

    A.post(G + '/generate', payload, { timeout: 120000 })
      .then((data) => {
        const works = T.asList(data, ['works']);
        const used = T.num(T.pick(data, ['pointsDeducted'], 0), 0);
        const px = T.str(T.pick(data, ['pixels'], ''));
        const ratio = T.str(T.pick(data, ['ratio'], ''));
        const tier = T.str(T.pick(data, ['tier'], ''));
        const reqPx = T.str(T.pick(data, ['requestedPixels'], ''));
        const actPx = T.str(T.pick(data, ['actualPixels'], px));
        const normalized = !!T.pick(data, ['normalized'], false);
        const aspectWarning = !!T.pick(data, ['aspectWarning'], false);
        U.toast('已生成 ' + works.length + ' 张' +
          (used > 0 ? '，扣除 ' + used + ' 积分' : ''));
        let sizeText = '';
        if (px) {
          sizeText = normalized && reqPx
            ? '（请求 ' + reqPx + ' → 实际 ' + actPx +
              '，已归一化为官方 ' + (ratio ? ratio + ' ' : '') + (tier || '') + '）'
            : '（' + (ratio ? ratio + ' ' : '') + (tier ? tier + ' ' : '') + px + '）';
        }
        if (normalized && reqPx) {
          sizeText += ' 上游不支持精确自定义尺寸，已按最接近的官方组合输出' +
            (aspectWarning ? '；宽高比偏差较大，建议改用预设比例' : '') + '。';
        }
        if (T.pick(data, ['local'], false)) {
          hint.textContent = '这次用的是本地占位绘图（未调用真实 API），换个种子可得到不同配色。' + sizeText;
        } else {
          hint.textContent = works.length + ' 张已存入「我的图库」。' + sizeText;
        }
        loadGallery();
      })
      .catch((err) => {
        if (err && err.status === 401) {
          U.loginDialog(() => loadGallery());
          hint.textContent = '登录后才能生成';
          return;
        }
        hint.textContent = '生成失败：' + (err.message || '未知错误');
        hint.style.color = '#fecaca';
        U.toast(err.message || '生成失败', false);
      })
      .then(() => {
        state.busy = false;
        btn.disabled = false;
        progress.hidden = true;
        syncSizeControls();
        applyCapabilities();
        updatePoints();
      });
  }

  /* ---------------------------------------------------------- 视频生成 */
  function resetBusy() {
    state.busy = false;
    const btn = $('btn-generate');
    btn.disabled = false;
    btn.textContent = '开始生成';
    $('progress').hidden = true;
    syncSizeControls();
    updatePoints();
  }

  function runGenerateVideo(prompt) {
    const m = state.selectedVideo || {};
    const btn = $('btn-generate');
    const progress = $('progress');
    const hint = $('hint');
    state.busy = true;
    btn.disabled = true;
    btn.textContent = '生成中…';
    progress.hidden = false;
    $('progress-text').textContent = '正在提交视频任务…';
    hint.textContent = '';
    hint.style.color = '';

    A.post(G + '/generate-video', {
      prompt,
      modelID: T.str(m.modelID, ''),
      providerId: Number(m.providerId) || 0,
      seed: Math.max(0, Math.min(2147483647, Number($('seed').value) || 0)),
      seconds: '5',
      aspectRatio: '16:9',
    }, { timeout: 300000 })
      .then((data) => {
        const vid = T.str(T.pick(data, ['videoId'], ''));
        if (!vid) throw new Error('上游没有返回任务 ID');
        const used = T.num(T.pick(data, ['pointsDeducted'], 0), 0);
        $('progress-text').textContent = '视频任务已提交，正在生成…（通常 30～120 秒）';
        pollVideo(vid, m, prompt, used, 0);
      })
      .catch((err) => {
        hint.textContent = '视频生成失败：' + (err.message || '未知错误');
        hint.style.color = '#fecaca';
        U.toast(err.message || '视频生成失败', false);
        resetBusy();
      });
  }

  function pollVideo(videoId, model, prompt, used, tries) {
    const hint = $('hint');
    A.get(G + '/video-status', {
      videoId,
      modelID: T.str(model.modelID, ''),
      providerId: Number(model.providerId) || 0,
    }, { timeout: 70000 })
      .then((data) => {
        const status = T.str(T.pick(data, ['status'], ''));
        const pct = T.num(T.pick(data, ['progress'], 0), 0);
        const url = T.str(T.pick(data, ['url'], ''));
        if (status === 'completed' && url) {
          resetBusy();
          hint.textContent = '视频已生成并存入「我的图库」' +
            (used > 0 ? '，扣除 ' + used + ' 积分' : '');
          U.toast('视频生成完成' + (used > 0 ? '，扣除 ' + used + ' 积分' : ''));
          U.openModal({
            title: '视频生成完成',
            html: '<video controls autoplay playsinline src="' + esc(url) +
              '" style="width:100%;border-radius:10px;background:#000"></video>' +
              '<p class="small muted" style="margin:10px 0 0;white-space:pre-wrap">' +
              esc(prompt) + '</p>',
          });
          loadGallery();
          return;
        }
        if (status === 'failed') {
          resetBusy();
          hint.textContent = '视频生成失败：' +
            (T.str(T.pick(data, ['failedReason'], '')) || '上游返回失败');
          hint.style.color = '#fecaca';
          U.toast('视频生成失败', false);
          return;
        }
        if (tries >= 150) {
          resetBusy();
          hint.textContent = '视频生成超时，请稍后到「我的图库」查看结果';
          hint.style.color = '#fecaca';
          return;
        }
        $('progress-text').textContent = '视频生成中…' + (pct ? pct + '%' : '');
        setTimeout(() => pollVideo(videoId, model, prompt, used, tries + 1), 2000);
      })
      .catch((err) => {
        resetBusy();
        hint.textContent = '视频状态查询失败：' + (err.message || '未知错误');
        hint.style.color = '#fecaca';
        U.toast(err.message || '视频状态查询失败', false);
      });
  }

  /* ---------------------------------------------------------- 事件 */
  $('draw-form').addEventListener('submit', generate);
  $('prompt').addEventListener('keydown', (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
      e.preventDefault();
      $('draw-form').requestSubmit();
    }
  });
  $('prompt').addEventListener('input', () => {
    if (state.recipe) refreshPreview();
  });

  $('model-list').addEventListener('click', (e) => {
    const card = e.target.closest('[data-rate]');
    if (!card) return;
    state.rateId = Number(card.getAttribute('data-rate'));
    state.selectedVideo = null;              // 选绘图模型时取消视频模型的选中态
    state.selectedVideoId = 0;
    renderModels();
    renderVideoModels();
  });

  $('builder').addEventListener('click', (e) => {
    const chip = e.target.closest('[data-chip]');
    if (!chip) return;
    const t = chip.getAttribute('data-chip');
    if (picked.has(t)) picked.delete(t); else picked.add(t);
    chip.classList.toggle('on', picked.has(t));
    updatePreview();
  });
  $('builder-apply').addEventListener('click', applyBuilder);
  $('builder-reset').addEventListener('click', () => {
    picked.clear();
    renderBuilder();
  });
  $('template').addEventListener('change', () => applyTemplate($('template').value));

  // 比例变了要重灌档位；切到「自定义」时换成宽高输入框
  $('ratio').addEventListener('change', () => {
    toggleCustomInputs();
    if (isCustomSize()) updateSizePixels();
    else fillTiers($('ratio').value, $('tier').value);
    updatePoints();
  });
  $('tier').addEventListener('change', () => {
    updateSizePixels();
    updatePoints();
  });
  ['width', 'height'].forEach((id) => {
    $(id).addEventListener('input', () => {
      updateSizePixels();
      updatePoints();
    });
    $(id).addEventListener('change', () => {
      updateSizePixels();
      updatePoints();
    });
  });
  $('num').addEventListener('change', () => updatePoints());

  $('video-model-list').addEventListener('click', (e) => {
    const prov = e.target.closest('[data-video-provider]');
    if (prov) {
      selectVideoProvider(Number(prov.getAttribute('data-video-provider')),
        prov.getAttribute('data-model') || '');
      return;
    }
    const card = e.target.closest('[data-video]');
    if (!card) return;
    selectVideoModel(card.getAttribute('data-video'));
  });

  $('btn-refresh').addEventListener('click', () => loadGallery());

  $('scope-new').addEventListener('click', () => { state.scope = 'new'; loadGallery(); });
  $('scope-hot').addEventListener('click', () => { state.scope = 'hot'; loadGallery(); });
  $('scope-mine').addEventListener('click', () => {
    U.requireLogin().then((ok) => {
      if (!ok) return;
      state.scope = 'mine';
      loadGallery();
    });
  });

  U.mountTopbar('draw');
  renderBuilder();
  loadTemplates();
  loadVideoProviders();
  loadGallery();
})();
