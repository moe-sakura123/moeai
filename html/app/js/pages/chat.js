/* pages/chat.js — 独立聊天页：角色卡 / 会话列表 / 流式对话 / 选项按钮
   协议：POST /api/gva/narratium/chatSession/*，流式走 SSE（App.sse）。 */
(function () {
  'use strict';
  const A = App.api;
  const U = App.ui;
  const S = App.store;
  const T = App.util;
  const $ = (id) => document.getElementById(id);
  const esc = U.esc;
  const CHAT = '/api/gva/narratium/chatSession/';

  const state = {
    charId: '',
    sessionId: '',
    char: {},
    sessions: [],
    messages: [],
    profile: null,
    stream: null,
    // 介绍网页 postMessage 捕获到的文字（用于右上角提示与「发送并开始游戏」）
    introText: '',
    // 模型切换：列表来自 /local/api/llm，选择存 localStorage，随 chatStream 上传
    models: [],
    modelRateId: S.get('chat.modelRateId', 0),
    // 输入栏工具行：流式开关（state.streaming 已被「正在生成」占用，这里另起名）
    streamOn: S.get('chat.streaming', 1) !== 0,
    // 最近对话多选模式
    multiSelect: false,
    selectedSessions: new Set(),
    // 生成中是否跟随滚到底部：用户往上翻看时不再强制拉回
    autoFollow: true,
  };

  /* ---------------------------------------------------------- 自定设定存储 */
  const CUSTOM_KEY = 'chat.custom.';

  function customList() {
    if (!state.charId) return [];
    const v = S.get(CUSTOM_KEY + state.charId, []);
    return Array.isArray(v) ? v : [];
  }

  function saveCustom(list) {
    if (state.charId) S.set(CUSTOM_KEY + state.charId, list);
  }

  /* 「自定」面板：参照原站「自定义设定」结构——左侧时间线按注入位置分三段
     （主提示词前 / 主提示词后 / 用户回复后），段内卡片列出条目 + 添加设定，
     顶部导出/导入。点条目展开编辑（启用/常量/关键词/内容），改完即存 */
  function openCustomPanel() {
    if (!state.charId) { U.toast('请先打开一个角色', false); return; }
    let list = customList().slice();
    let editing = '';            // 当前展开编辑的条目 key（pos:index）
    // 三段分组：与原站一致——破甲词/定义/世界观在提示词前，作者主提示词在中，用户输入在后
    const GROUPS = [
      { pos: 'beforeMain', dot: 'gray', label: '破甲词 · <b>AI定义</b> · <b>世界观</b>（作者）',
        hint: '在角色设定之前注入' },
      { pos: 'afterMain', dot: 'purple', label: '<b>主提示词</b>（作者）',
        hint: '在角色设定之后注入' },
      { pos: 'afterUser', dot: 'green', label: '<b>用户输入</b>（你）',
        hint: 'AI 最后读取，注意力最高' },
    ];
    const charName = T.str(T.pick(state.char, ['name', 'title']), '当前角色');

    function entryHtml(it, i) {
      const key = it.position + ':' + i;
      const name = String(it.content || '').split('\n')[0].slice(0, 24) || '(空)';
      if (editing !== key) {
        // 列表态：一行条目卡（原站是 📄 图标 + 名称）
        return '<div class="cc-row' + (it.enabled === false ? ' off' : '') +
          '" data-edit="' + esc(key) + '">' +
          '<span class="cc-doc">📄</span><span class="cc-name">' + esc(name) + '</span>' +
          (it.constant ? '<span class="cc-tag">常量</span>' : '') +
          (it.keywords && it.keywords.length ?
            '<span class="cc-kw">' + esc(it.keywords.join('，')) + '</span>' : '') +
          '</div>';
      }
      // 编辑态：展开表单
      return '<div class="cc-item" data-i="' + i + '">' +
        '<div class="cc-head">' +
        '<label class="cc-on"><input type="checkbox" data-f="enabled"' +
        (it.enabled === false ? '' : ' checked') + '>启用</label>' +
        '<label class="cc-on"><input type="checkbox" data-f="constant"' +
        (it.constant ? ' checked' : '') + '>常量（始终生效）</label>' +
        '<span class="grow"></span>' +
        '<button type="button" class="cc-del" data-del="' + i + '">删除</button>' +
        '</div>' +
        '<input class="input" data-f="keywords" placeholder="触发关键词（逗号分隔，常量可留空）" value="' +
        esc((it.keywords || []).join('，')) + '">' +
        '<textarea class="textarea" data-f="content" rows="3" placeholder="输入自定义设定内容…">' +
        esc(it.content || '') + '</textarea>' +
        '<div class="cc-actions"><button type="button" class="btn sm primary" data-done="' +
        i + '">完成</button></div>' +
        '</div>';
    }

    function bodyHtml() {
      return '<div class="cc-timeline">' + GROUPS.map((g) => {
        const rows = [];
        list.forEach((it, i) => { if (it.position === g.pos) rows.push([it, i]); });
        return '<div class="cc-group">' +
          '<div class="cc-glabel"><i class="cc-dot ' + g.dot + '"></i>' + g.label + '</div>' +
          '<div class="cc-card">' +
          (rows.length ? rows.map(([it, i]) => entryHtml(it, i)).join('') :
            '<div class="cc-row muted">' + g.hint + '</div>') +
          '<button type="button" class="cc-add" data-add="' + g.pos + '">＋ 添加设定</button>' +
          '</div></div>';
      }).join('') + '</div>' +
      '<p class="tiny muted" style="margin:10px 0 0">启用的设定随消息发送给 AI；' +
      '常量每次都生效，非常量按关键词命中。</p>';
    }

    return U.openModal({
      title: '自定义设定',
      html: '<div class="cc-sub">' + esc(charName) + '</div>' + bodyHtml(),
      footer: '<button class="btn" id="cc-export">导出</button>' +
        '<button class="btn" id="cc-import">导入</button>' +
        '<span class="grow"></span>' +
        '<button class="btn primary" data-close>完成</button>',
      onMount(mask) {
        // 从表单里把当前展开编辑的条目写回 list
        function flush() {
          const el = mask.querySelector('.cc-item');
          if (!el) return;
          const i = Number(el.getAttribute('data-i'));
          const val = (f) => {
            const n = el.querySelector('[data-f="' + f + '"]');
            if (!n) return '';
            return n.type === 'checkbox' ? n.checked : n.value;
          };
          list[i] = {
            enabled: val('enabled'), constant: val('constant'),
            position: list[i] ? list[i].position : 'afterMain',
            keywords: String(val('keywords') || '').split(/[,，]/)
              .map((s) => s.trim()).filter(Boolean),
            content: String(val('content') || '').trim(),
          };
        }
        function rerender() {
          mask.querySelector('.modal-body').innerHTML =
            '<div class="cc-sub">' + esc(charName) + '</div>' + bodyHtml();
        }
        mask.addEventListener('click', (e) => {
          const t = e.target;
          const attr = (name) => t && t.hasAttribute ? t.getAttribute(name) : null;
          if (t && t.hasAttribute && t.hasAttribute('data-edit')) {
            flush();
            editing = attr('data-edit');
            rerender();
          } else if (t && t.hasAttribute && t.hasAttribute('data-add')) {
            flush();
            const pos = attr('data-add');
            list.push({ enabled: true, constant: false,
              position: pos, keywords: [], content: '' });
            editing = pos + ':' + (list.length - 1);
            rerender();
          } else if (t && t.hasAttribute && t.hasAttribute('data-del')) {
            const i = Number(attr('data-del'));
            list.splice(i, 1);
            editing = '';
            rerender();
          } else if (t && t.hasAttribute && t.hasAttribute('data-done')) {
            const i = Number(attr('data-done'));
            if (!list[i].constant && !list[i].keywords.length) {
              U.toast('非常量设定至少要填一个触发关键词', false);
              return;
            }
            flush();
            editing = '';
            saveCustom(list.filter((it) => it.content));
            list = list.filter((it) => it.content);
            rerender();
            U.toast('已保存');
          }
        });
        // 导出 / 导入（原站同款功能）
        mask.querySelector('#cc-export').addEventListener('click', () => {
          if (!list.length) { U.toast('没有可导出的设定', false); return; }
          const blob = new Blob([JSON.stringify({ version: 1,
            settings: list }, null, 2)], { type: 'application/json' });
          const a = document.createElement('a');
          a.href = URL.createObjectURL(blob);
          a.download = (charName || 'custom') + '-自定义设定.json';
          a.click();
          URL.revokeObjectURL(a.href);
          U.toast('已导出 ' + list.length + ' 条设定');
        });
        mask.querySelector('#cc-import').addEventListener('click', () => {
          const inp = document.createElement('input');
          inp.type = 'file';
          inp.accept = 'application/json,.json';
          inp.onchange = () => {
            const f = inp.files && inp.files[0];
            if (!f) return;
            const rd = new FileReader();
            rd.onload = () => {
              try {
                const data = JSON.parse(String(rd.result || ''));
                const items = Array.isArray(data) ? data :
                  (Array.isArray(data.settings) ? data.settings : null);
                if (!items) { U.toast('文件格式不正确', false); return; }
                let ok = 0;
                items.forEach((it) => {
                  if (!it || !String(it.content || '').trim()) return;
                  const pos = ['beforeMain', 'afterMain', 'afterUser']
                    .includes(it.position) ? it.position : 'afterMain';
                  list.push({ enabled: it.enabled !== false,
                    constant: !!it.constant, position: pos,
                    keywords: Array.isArray(it.keywords) ? it.keywords : [],
                    content: String(it.content).trim() });
                  ok += 1;
                });
                saveCustom(list);
                rerender();
                U.toast('已导入 ' + ok + ' 条设定');
              } catch (err) {
                U.toast('导入失败：文件读取异常', false);
              }
            };
            rd.readAsText(f, 'utf-8');
          };
          inp.click();
        });
      },
    });
  }

  /* 「记忆」面板：记忆区——摘要 / 结构化条目由后端在对话中自动生成，
     存 chat_memory_items，这里只做查看 / 删除，不用手写 */
  function openMemoryPanel() {
    if (!state.charId) { U.toast('请先打开一个角色', false); return; }
    const charName = T.str(T.pick(state.char, ['name', 'title']), '当前角色');

    function emptyHtml() {
      return '<div class="mem-empty">' +
        '<div class="mem-brain">🧠</div>' +
        '<p class="mem-empty-title">暂无记忆</p>' +
        '<p class="tiny muted">AI 会在对话过程中自动生成记忆摘要</p>' +
        '</div>';
    }

    function itemHtml(it, i) {
      const lines = String(it.content || '').split('\n').filter((l) => l.trim());
      const when = String(it.createdAt || '').slice(0, 16).replace('T', ' ');
      const round = Number(it.round) || 0;
      return '<div class="mem-item" data-i="' + i + '">' +
        '<div class="mem-head">' +
        (round > 0 ? '<span class="mem-round">第 ' + round + ' 轮</span>' : '') +
        '<span class="tiny muted">' + esc(when) + '</span>' +
        '<span class="grow"></span>' +
        '<button type="button" class="cc-del" data-del="' + i + '">删除</button>' +
        '</div>' +
        '<ul class="mem-lines">' + lines.map((l) =>
          '<li>' + esc(l.replace(/^[-·*]\s*/, '')) + '</li>').join('') + '</ul>' +
        '</div>';
    }

    U.openModal({
      title: '记忆区',
      html: '<div class="cc-sub">' + esc(charName) + '</div>' +
        '<div id="mem-body">' + emptyHtml() + '</div>' +
        '<p class="tiny muted" style="margin:10px 0 0">摘要由 AI 在对话中自动生成，' +
        '并自动带给后续对话作为上下文；不需要手动填写。</p>',
      footer: '<button class="btn" data-close>关闭</button>',
      onMount(mask) {
        const body = mask.querySelector('#mem-body');
        function rerender(list) {
          if (!list.length) { body.innerHTML = emptyHtml(); return; }
          body.innerHTML = '<div class="mem-list">' +
            list.map(itemHtml).join('') + '</div>';
          Array.prototype.forEach.call(body.querySelectorAll('[data-del]'), (btn) => {
            btn.addEventListener('click', () => {
              const it = list[Number(btn.getAttribute('data-del'))];
              if (!it) return;
              A.del('/api/chat/memories', undefined, { params: { id: it.id } })
                .then(() => { rerender(list.filter((x) => x !== it)); U.toast('已删除'); })
                .catch((e) => U.toast(e.message || '删除失败', false));
            });
          });
        }
        rerender([]);
        A.get('/api/chat/memories', { characterId: state.charId })
          .then((data) => {
            rerender(T.asList(data, ['memories', 'list']).filter((m) => m && m.content));
          })
          .catch(() => {});
      },
    });
  }

  /* ---------------------------------------------------------- 对话设置面板 */
  /* 齿轮按钮弹出的「对话设置」（与源站一致分三组）：
     显示设置 → 字体大小/行间距/思维链/CG调试/阅读安全模式/禁用作者样式（存 localStorage）
     记忆设置 → 压缩保留轮数/增强记忆 + 高级选项（summaryEnabled 存 summaryConfig，
               其余存 promptEngine.memory）
     模型参数 → 温度/TopP/存在惩罚/频率惩罚/最大回复长度/上下文窗口
               （存服务端 modelConfig；面板只展示上一轮真实下发值，不参与计算） */
  const DISPLAY_KEY = 'chat.display';
  const DISPLAY_DEFAULT = {
    fontSize: 1, lineHeight: 1.6, showThinking: false, cgDebug: true,
    aigcIllust: true,
    disableCustomStyle: false, disableAuthorStyle: false,
    promptLog: false,
  };
  // 与源站一致的取值范围
  const DISPLAY_RANGE = { fontSize: [0.8, 1.5], lineHeight: [1.2, 2.5] };
  const MODEL_DEFAULT = {
    temperature: 0.7, topP: 0.8, presencePenalty: 0,
    frequencyPenalty: 0, maxTokens: 8192, contextWindow: 8192,
  };
  // 与后端 server.py 的 MODEL_PARAM_RANGES 完全一致，越界一律钳制
  const MODEL_RANGE = {
    temperature: [0, 1], topP: [0, 1], presencePenalty: [0, 1],
    frequencyPenalty: [0, 1], maxTokens: [256, 8192],
    contextWindow: [1024, 32768],
  };
  const MODEL_INT_KEYS = ['maxTokens', 'contextWindow'];
  const MEMORY_DEFAULT = {
    summaryEnabled: true, compressKeepRounds: 6,
    topK: 8, injectDepth: 4, maxTokens: 600, waitMs: 8000,
  };
  const MEMORY_RANGE = {
    compressKeepRounds: [1, 24], topK: [1, 20], injectDepth: [0, 10],
    maxTokens: [200, 2000], waitMs: [1000, 15000],
  };
  const MEMORY_KEYS = ['summaryEnabled', 'compressKeepRounds', 'topK',
    'injectDepth', 'maxTokens', 'waitMs'];

  function clampNum(v, min, max, dft) {
    const n = Number(v);
    if (!isFinite(n)) return dft;
    return Math.min(max, Math.max(min, n));
  }

  function displayPrefs() {
    const v = S.get(DISPLAY_KEY, {});
    const o = (v && typeof v === 'object') ? v : {};
    return {
      fontSize: clampNum(o.fontSize, DISPLAY_RANGE.fontSize[0],
        DISPLAY_RANGE.fontSize[1], DISPLAY_DEFAULT.fontSize),
      lineHeight: clampNum(o.lineHeight, DISPLAY_RANGE.lineHeight[0],
        DISPLAY_RANGE.lineHeight[1], DISPLAY_DEFAULT.lineHeight),
      showThinking: o.showThinking === true,
      cgDebug: o.cgDebug !== false,
      aigcIllust: o.aigcIllust !== false,
      disableCustomStyle: o.disableCustomStyle === true,
      disableAuthorStyle: o.disableAuthorStyle === true,
      promptLog: o.promptLog === true,
    };
  }

  function saveDisplay(d) { S.set(DISPLAY_KEY, d); }

  /* 显示设置即时生效：字号/行距作用于聊天气泡；两个「样式禁用」开关
     控制角色自定义 CSS 是否注入（阅读安全模式 / 禁用作者样式 → 都不注入） */
  function applyDisplay() {
    const d = displayPrefs();
    const box = $('chat-scroll');
    if (box) {
      box.style.setProperty('--chat-font', (13.5 * d.fontSize).toFixed(2) + 'px');
      box.style.setProperty('--chat-line', String(d.lineHeight));
      box.classList.toggle('no-author-style',
        !!(d.disableCustomStyle || d.disableAuthorStyle));
      // AICG 插图开关：即时显隐历史 CG 条（.cg-strip）与流式内联 CG（.cg-inline），
      // 无需重渲染整列消息。只隐藏 .cg-strip 会导致关闭开关后内联 CG 仍显示。
      box.querySelectorAll('.cg-strip, .cg-inline').forEach((el) => {
        el.style.display = d.aigcIllust ? '' : 'none';
      });
    }
    applyCustomCss();
  }

  function sliderRow(key, label, hint, min, max, step, val, unit) {
    return '<div class="cs-row">' +
      '<div class="cs-label"><b>' + esc(label) + '</b>' +
      (hint ? '<small>' + esc(hint) + '</small>' : '') + '</div>' +
      '<input type="range" class="cs-range" data-cs="' + esc(key) + '" min="' + min +
      '" max="' + max + '" step="' + step + '" value="' + val + '">' +
      '<span class="cs-val" data-csv="' + esc(key) + '">' +
      (unit === 'int' ? Math.round(val) : Number(val).toFixed(2)) + '</span>' +
      '</div>';
  }

  function switchRow(key, label, hint, on) {
    return '<div class="cs-row cs-switch-row">' +
      '<div class="cs-label"><b>' + esc(label) + '</b>' +
      (hint ? '<small>' + esc(hint) + '</small>' : '') + '</div>' +
      '<label class="switch"><input type="checkbox" data-cs="' + esc(key) + '"' +
      (on ? ' checked' : '') + '><i></i></label>' +
      '</div>';
  }

  function openChatSettings() {
    const d = displayPrefs();
    const mid = Number(state.modelRateId) || 0;
    let mem = Object.assign({}, MEMORY_DEFAULT);    // 服务端记忆配置
    let mdl = Object.assign({}, MODEL_DEFAULT);     // 服务端模型参数

    const html =
      '<div class="cs-group"><div class="cs-gtitle">显示设置</div>' +
      sliderRow('fontSize', '字体大小', '调整聊天界面的字体大小',
        DISPLAY_RANGE.fontSize[0], DISPLAY_RANGE.fontSize[1], 0.05, d.fontSize) +
      sliderRow('lineHeight', '行间距', '调整文字行与行之间的间距',
        DISPLAY_RANGE.lineHeight[0], DISPLAY_RANGE.lineHeight[1], 0.05, d.lineHeight) +
      switchRow('showThinking', '思维链', '', d.showThinking) +
      switchRow('aigcIllust', 'AICG 插图', '开启后展示 CG 图片，有图回复积分略微增加（+1）', d.aigcIllust) +
      switchRow('cgDebug', 'CG 调试', '', d.cgDebug) +
      switchRow('disableCustomStyle', '阅读安全模式', '关闭后显示角色的自定义样式', d.disableCustomStyle) +
      switchRow('disableAuthorStyle', '禁用作者样式', '', d.disableAuthorStyle) +
      '</div>' +
      '<div class="cs-group"><div class="cs-gtitle">调试</div>' +
      switchRow('promptLog', 'Prompt 快照', '开启后把每次发给 AI 的完整 prompt 落盘到数据库，便于调试/复现', d.promptLog) +
      '</div>' +
      '<div class="cs-group"><div class="cs-gtitle">记忆设置</div>' +
      sliderRow('compressKeepRounds', '基础记忆轮数',
        '保留最近 N 轮对话原文，更早的会被压缩成摘要',
        MEMORY_RANGE.compressKeepRounds[0], MEMORY_RANGE.compressKeepRounds[1],
        1, mem.compressKeepRounds, 'int') +
      switchRow('summaryEnabled', '增强记忆',
        '关闭后不检索、不注入记忆、不生成摘要与结构化记忆',
        mem.summaryEnabled) +
      '<details class="cs-adv"><summary>高级选项</summary>' +
      sliderRow('topK', '检索条数', '每轮最多注入多少条相关记忆',
        MEMORY_RANGE.topK[0], MEMORY_RANGE.topK[1], 1, mem.topK, 'int') +
      sliderRow('injectDepth', '注入深度', '0 表示紧贴本轮，数字越大越靠前',
        MEMORY_RANGE.injectDepth[0], MEMORY_RANGE.injectDepth[1], 1,
        mem.injectDepth, 'int') +
      sliderRow('maxTokens', '记忆预算', '注入记忆块的 token 上限',
        MEMORY_RANGE.maxTokens[0], MEMORY_RANGE.maxTokens[1], 50,
        mem.maxTokens, 'int') +
      sliderRow('waitMs', '召回等待（ms）', '这是上限，检索完成即返回',
        MEMORY_RANGE.waitMs[0], MEMORY_RANGE.waitMs[1], 500,
        mem.waitMs, 'int') +
      '</details>' +
      '<p class="tiny muted cs-note" id="cs-mem-status" hidden></p>' +
      '<p class="tiny muted cs-note">记忆摘要由 AI 在对话中自动生成。</p>' +
      '</div>' +
      '<div class="cs-group"><div class="cs-gtitle">模型参数</div>' +
      '<p class="tiny muted cs-note">注意：不是所有模型都支持以下参数，部分模型可能会忽略某些设置。</p>' +
      sliderRow('temperature', '温度 (Temperature)', '越高生成内容越随机，越低越确定',
        MODEL_RANGE.temperature[0], MODEL_RANGE.temperature[1], 0.05, mdl.temperature) +
      sliderRow('topP', 'Top-P', '控制生成文本的多样性',
        MODEL_RANGE.topP[0], MODEL_RANGE.topP[1], 0.05, mdl.topP) +
      sliderRow('presencePenalty', '存在惩罚', '减少重复已出现内容的倾向',
        MODEL_RANGE.presencePenalty[0], MODEL_RANGE.presencePenalty[1], 0.05, mdl.presencePenalty) +
      sliderRow('frequencyPenalty', '频率惩罚', '减少高频词语的重复使用',
        MODEL_RANGE.frequencyPenalty[0], MODEL_RANGE.frequencyPenalty[1], 0.05, mdl.frequencyPenalty) +
      sliderRow('maxTokens', '最大回复长度', '单次回复的最大 token 数量',
        MODEL_RANGE.maxTokens[0], MODEL_RANGE.maxTokens[1], 64, mdl.maxTokens, 'int') +
      sliderRow('contextWindow', '上下文窗口', '模型可接收的总 token 数（输入 + 输出）',
        MODEL_RANGE.contextWindow[0], MODEL_RANGE.contextWindow[1], 1024,
        mdl.contextWindow, 'int') +
      '<p class="tiny muted cs-note" id="cs-mdl-actual">实际下发值 = 设定值，除非输入把窗口占满；发送一轮后这里显示上一轮的「设定值 / 实际下发值」。</p>' +
      '</div>';

    const mask = U.openModal({
      title: '对话设置',
      html,
      footer: '<button class="btn" id="cs-reset">恢复默认</button>' +
        '<span class="grow"></span>' +
        '<button class="btn primary" data-close>完成</button>',
      onMount(m) {
        /* 打开后拉服务端配置（记忆 + 模型参数），拉到就把滑条拨到真实值。
           用户已点过「恢复默认」就不再回填，避免异步返回把默认值覆盖掉 */
        let resetDone = false;
        let memDirty = false;          // 用户已动过记忆控件 → 忽略晚到的回填
        A.get('/api/gva/narratium/summaryConfig/getByCharacter',
          { characterId: state.charId }).then((data) => {
            if (resetDone || memDirty || !data || typeof data !== 'object') return;
            if (typeof data.summaryEnabled === 'boolean') mem.summaryEnabled = data.summaryEnabled;
            setCheck('summaryEnabled', mem.summaryEnabled);
          }).catch(() => {});
        A.get('/api/gva/narratium/promptConfig/getByCharacter',
          { characterID: state.charId }).then((data) => {
            if (resetDone || memDirty || !data || typeof data !== 'object') return;
            const mc = (data.memory && typeof data.memory === 'object') ? data.memory : {};
            const rc = (mc.recall && typeof mc.recall === 'object') ? mc.recall : {};
            setMem('compressKeepRounds', mc.compressKeepRounds);
            setMem('injectDepth', mc.injectDepth);
            setMem('maxTokens', mc.maxTokens);
            setMem('topK', rc.topK);
            setMem('waitMs', rc.waitMs);
          }).catch(() => {});
        // 只读状态：最近一次召回条数 / 等待耗时（拿不到就保持隐藏，不编数据）
        const statusEl = m.querySelector('#cs-mem-status');
        A.get('/api/chat/memory-status',
          { characterId: state.charId, sessionId: state.sessionId }).then((data) => {
            if (!statusEl || !data || !data.available) return;
            const n = Math.max(0, Number(data.count) || 0);
            const ms = Math.max(0, Number(data.waitMs) || 0);
            statusEl.textContent = '最近一次召回：' + n + ' 条记忆 · 等待 ' +
              Math.round(ms) + ' ms';
            statusEl.hidden = false;
          }).catch(() => {});
        A.get('/api/gva/narratium/modelConfig/getByModelRate',
          { modelRateId: mid }).then((data) => {
            if (resetDone || !data || typeof data !== 'object') return;
            // 与后端同一套范围钳制，脏值不会把滑条位置和数字显示搞乱
            Object.keys(MODEL_DEFAULT).forEach((k) => {
              mdl[k] = clampNum(data[k], MODEL_RANGE[k][0], MODEL_RANGE[k][1],
                MODEL_DEFAULT[k]);
              setRange(k, mdl[k], MODEL_INT_KEYS.indexOf(k) !== -1 ? 'int' : '');
            });
          }).catch(() => {});
        // 只读状态：上一轮真实下发的参数（拿不到就保留"会随上下文浮动"的说明）
        const actualEl = m.querySelector('#cs-mdl-actual');
        A.get('/api/chat/model-params-status',
          { modelRateId: mid, sessionId: state.sessionId }).then((data) => {
            if (!actualEl || !data || !data.available) return;
            const sent = Number(data.maxTokens) || 0;
            const savedV = Number(data.maxTokensSaved) || 0;
            const est = Number(data.inputTokens) || 0;
            actualEl.textContent = '上一轮实际下发：temperature ' +
              Number(data.temperature || 0).toFixed(2) + ' · top_p ' +
              Number(data.topP || 0).toFixed(2) + ' · max_tokens 设定 ' +
              savedV + ' / 实际 ' + sent +
              (sent < savedV
                ? '（输入约 ' + est + ' token，窗口 ' + (Number(data.contextWindow) || 0) +
                  '，已按窗口压低）'
                : '（与设定一致）');
          }).catch(() => {});
        // AICG 插图开关：用户级偏好存后端 cg:preference.showCGInChat，
        // 打开面板以后端为准拨开关（跨设备/跨浏览器一致）
        A.get('/api/gva/narratium/cg/preference').then((data) => {
          if (resetDone || !data || typeof data !== 'object') return;
          if (typeof data.showCGInChat === 'boolean') {
            setCheck('aigcIllust', data.showCGInChat);
            // 顺手把后端真值回写 localStorage，保持两边一致
            const cur = displayPrefs();
            saveDisplay(Object.assign({}, cur, { aigcIllust: data.showCGInChat }));
          }
        }).catch(() => {});

        function setRange(key, val, unit) {
          const r = m.querySelector('[data-cs="' + key + '"]');
          const s = m.querySelector('[data-csv="' + key + '"]');
          if (r) r.value = val;
          if (s) s.textContent = unit === 'int' ? Math.round(val) : Number(val).toFixed(2);
        }
        function setCheck(key, on) {
          const c = m.querySelector('input[data-cs="' + key + '"]');
          if (c) c.checked = !!on;
        }
        /* 记忆数值：按统一范围钳制后写回滑条，保证显示值与后端一致 */
        function setMem(key, val) {
          const range = MEMORY_RANGE[key];
          if (!range) return;
          mem[key] = clampNum(val, range[0], range[1], MEMORY_DEFAULT[key]);
          setRange(key, mem[key], 'int');
        }

        /* 滑条 / 开关的值统一收集 */
        function collect() {
          const num = (key) => {
            const n = m.querySelector('[data-cs="' + key + '"]');
            return n ? Number(n.value) : 0;
          };
          const chk = (key) => {
            const n = m.querySelector('input[data-cs="' + key + '"]');
            return !!(n && n.checked);
          };
          return { num, chk };
        }

        /* 显示设置改完立刻生效 + 落 localStorage */
        function applyDisplayFields() {
          const { num, chk } = collect();
          saveDisplay({
            fontSize: num('fontSize'), lineHeight: num('lineHeight'),
            showThinking: chk('showThinking'), cgDebug: chk('cgDebug'),
            aigcIllust: chk('aigcIllust'),
            disableCustomStyle: chk('disableCustomStyle'),
            disableAuthorStyle: chk('disableAuthorStyle'),
            promptLog: chk('promptLog'),
          });
          applyDisplay();
        }

        /* 记忆设置：两处服务端配置，改动后统一防抖保存
           · summaryEnabled → summaryConfig（记忆总闸）
           · 其余数值 → promptEngine.memory（深合并，不冲掉别的记忆参数） */
        let memTimer = 0;
        function saveMem(silent) {
          clearTimeout(memTimer);
          memTimer = setTimeout(() => {
            MEMORY_KEYS.forEach((k) => {
              const el = m.querySelector('[data-cs="' + k + '"]');
              if (!el) return;
              if (el.type === 'checkbox') { mem[k] = !!el.checked; return; }
              const range = MEMORY_RANGE[k];
              mem[k] = clampNum(el.value, range[0], range[1], MEMORY_DEFAULT[k]);
            });
            const p1 = A.post('/api/gva/narratium/summaryConfig/saveByCharacter', {
              characterId: state.charId,
              summaryEnabled: mem.summaryEnabled,
            });
            const p2 = A.post('/api/gva/narratium/promptConfig/saveByCharacter', {
              characterID: state.charId,
              memory: {
                compressKeepRounds: mem.compressKeepRounds,
                injectDepth: mem.injectDepth,
                maxTokens: mem.maxTokens,
                recall: { topK: mem.topK, waitMs: mem.waitMs },
              },
            });
            Promise.all([p1, p2])
              .then(() => { if (!silent) U.toast('记忆设置已保存'); })
              .catch((e) => U.toast(e.message || '记忆设置保存失败', false));
          }, 500);
        }
        let mdlTimer = 0;
        function saveModel(silent) {
          clearTimeout(mdlTimer);
          mdlTimer = setTimeout(() => {
            const { num } = collect();
            const payload = { modelRateId: mid };
            Object.keys(MODEL_DEFAULT).forEach((k) => {
              mdl[k] = clampNum(num(k), MODEL_RANGE[k][0], MODEL_RANGE[k][1],
                MODEL_DEFAULT[k]);
              payload[k] = mdl[k];
            });
            A.post('/api/gva/narratium/modelConfig/saveByModelRate', payload)
              .then(() => { if (!silent) U.toast('模型参数已保存'); })
              .catch((e) => U.toast(e.message || '模型参数保存失败', false));
          }, 500);
        }

        /* AICG 插图开关：用户级偏好存后端 cg:preference.showCGInChat。
           关掉后 _match_cg 直接返回空，SSE 不再推 cg 帧，省生图开销 */
        let cgPrefTimer = 0;
        function saveCgPref(silent) {
          clearTimeout(cgPrefTimer);
          cgPrefTimer = setTimeout(() => {
            const on = collect().chk('aigcIllust');
            A.put('/api/gva/narratium/cg/preference', { showCGInChat: on })
              .then(() => { if (!silent) U.toast('AICG 插图偏好已保存'); })
              .catch((e) => U.toast(e.message || 'AICG 插图保存失败', false));
          }, 300);
        }

        m.addEventListener('input', (e) => {
          const t = e.target;
          if (!t || !t.getAttribute) return;
          const key = t.getAttribute('data-cs');
          if (!key) return;
          // 数值旁的即时显示
          const s = m.querySelector('[data-csv="' + key + '"]');
          if (s && t.type === 'range') {
            s.textContent = (MODEL_INT_KEYS.indexOf(key) !== -1 ||
              MEMORY_KEYS.indexOf(key) !== -1)
              ? Math.round(t.value) : Number(t.value).toFixed(2);
          }
          if (DISPLAY_DEFAULT.hasOwnProperty(key)) {
            applyDisplayFields();
          } else if (MEMORY_KEYS.indexOf(key) !== -1) {
            memDirty = true;
            saveMem();
          } else if (MODEL_DEFAULT.hasOwnProperty(key)) {
            saveModel();
          }
        });
        m.addEventListener('change', (e) => {
          const t = e.target;
          if (!t || t.type !== 'checkbox' || !t.getAttribute) return;
          const key = t.getAttribute('data-cs');
          if (DISPLAY_DEFAULT.hasOwnProperty(key)) {
            applyDisplayFields();
            if (key === 'aigcIllust') saveCgPref();   // 用户级偏好，同步后端
          }
          else if (MEMORY_KEYS.indexOf(key) !== -1) { memDirty = true; saveMem(); }
        });

        // 恢复默认：三组全部回默认值（显示设置即刻生效，记忆/模型参数回写服务端）
        m.querySelector('#cs-reset').addEventListener('click', () => {
          resetDone = true;
          // 显示设置：滑条/开关拨回默认 → 落 localStorage 并即时生效
          setRange('fontSize', DISPLAY_DEFAULT.fontSize, '');
          setRange('lineHeight', DISPLAY_DEFAULT.lineHeight, '');
          setCheck('showThinking', DISPLAY_DEFAULT.showThinking);
          setCheck('cgDebug', DISPLAY_DEFAULT.cgDebug);
          setCheck('aigcIllust', DISPLAY_DEFAULT.aigcIllust);
          setCheck('disableCustomStyle', DISPLAY_DEFAULT.disableCustomStyle);
          setCheck('disableAuthorStyle', DISPLAY_DEFAULT.disableAuthorStyle);
          setCheck('promptLog', DISPLAY_DEFAULT.promptLog);
          applyDisplayFields();
          // 记忆设置 + 模型参数：拨回默认并保存
          Object.keys(MODEL_DEFAULT).forEach((k) => setRange(k, MODEL_DEFAULT[k],
            MODEL_INT_KEYS.indexOf(k) !== -1 ? 'int' : ''));
          Object.keys(MEMORY_RANGE).forEach((k) => {
            mem[k] = MEMORY_DEFAULT[k];
            setRange(k, mem[k], 'int');
          });
          setCheck('summaryEnabled', MEMORY_DEFAULT.summaryEnabled);
          mem.summaryEnabled = MEMORY_DEFAULT.summaryEnabled;
          Object.keys(MODEL_DEFAULT).forEach((k) => { mdl[k] = MODEL_DEFAULT[k]; });
          saveMem(true);
          saveModel(true);
          saveCgPref(true);   // AICG 插图开关默认开，同步后端
          U.toast('已恢复默认设置');
        });
      },
    });
    return mask;
  }

  /* ---------------------------------------------------------- 模型切换 */
  const MODEL_KEY = 'chat.modelRateId';
  function modelNameOf(m) {
    return T.str(T.pick(m, ['modelName', 'poolName', 'name', 'title', 'modelID']), '未命名模型');
  }

  function currentModel() {
    const list = state.models || [];
    let hit = list.find((m) => Number(T.pick(m, ['id', 'ID', 'rateId'], 0)) === Number(state.modelRateId));
    if (!hit) hit = list.find((m) => T.pick(m, ['isDefault'], false)) || list[0] || null;
    return hit;
  }

  function renderModelPick() {
    const wrap = $('model-pick');
    if (!wrap) return;
    const cur = currentModel();
    wrap.hidden = !(state.models && state.models.length);
    if (!cur) return;
    state.modelRateId = Number(T.pick(cur, ['id', 'ID', 'rateId'], 0));
    $('model-name').textContent = modelNameOf(cur);
    const cur2 = $('model-menu-cur');
    if (cur2) cur2.textContent = '当前：' + modelNameOf(cur);
    const list = $('model-list');
    if (!list) return;
    list.innerHTML = state.models.map((m) => {
      const id = Number(T.pick(m, ['id', 'ID', 'rateId'], 0));
      const on = id === Number(state.modelRateId);
      return '<button type="button" class="mm-item' + (on ? ' on' : '') + '" data-mid="' + id + '">' +
        esc(modelNameOf(m)) +
        (T.str(m && m.description) ? '<small>' + esc(T.str(m.description)) + '</small>' : '') +
        '</button>';
    }).join('');
    Array.prototype.forEach.call(list.querySelectorAll('.mm-item'), (item) => {
      item.addEventListener('click', () => {
        state.modelRateId = Number(item.getAttribute('data-mid')) || 0;
        S.set(MODEL_KEY, state.modelRateId);
        renderModelPick();
        $('model-menu').hidden = true;
        U.toast('已切换模型：' + modelNameOf(currentModel()));
      });
    });
  }

  function loadModels() {
    return A.get('/api/chat/models', {}, { timeout: 8000 })
      .then((data) => {
        state.models = T.asList(data, ['models', 'list', 'records']);
        renderModelPick();
      })
      .catch(() => {
        // 接口不可用就保持隐藏，不影响聊天
        state.models = [];
        renderModelPick();
      });
  }

  function params() {
    return new URLSearchParams(location.search);
  }

  /* ---------------------------------------------------------- 角色 */
  function loadCharacter() {
    if (!state.charId) return Promise.resolve(false);
    return A.get('/api/gva/narratiumCharacter/findNarratiumCharacterByCharacterID',
      { characterId: state.charId }, { timeout: 10000 })
      .then((data) => {
        const ch = T.isObj(data) && T.isObj(data.character) ? data.character : data;
        if (!T.isObj(ch) || !T.str(T.pick(ch, ['characterName', 'name']))) return false;
        state.char = ch;
        renderCharacter();
        return true;
      })
      .catch(() => false);
  }

  function charName() {
    return T.str(T.pick(state.char, ['characterName', 'name']), '角色');
  }

  function renderCharacter() {
    const name = charName();
    const avatar = T.str(T.pick(state.char, ['avatarURL', 'avatarUrl', 'coverURL']));
    $('c-name').textContent = name;
    const img = $('c-avatar');
    img.src = avatar || U.placeholder(name.slice(0, 1), 1);
    U.safeImg(img, name, 1);
    const summary = T.str(T.pick(state.char, ['summary', 'description']), '暂无简介');
    $('c-summary').textContent = summary;
    const tags = Array.isArray(state.char.tags) ? state.char.tags : [];
    $('c-tags').innerHTML = tags.slice(0, 6).map((t) => '<span class="tag">' + esc(t) + '</span>').join('');
    const cid = state.charId;
    $('link-edit').href = '/app/creation?id=' + encodeURIComponent(cid);
    $('link-worldbook').href = '/app/worldbook?cid=' + encodeURIComponent(cid);
    $('chat-title').textContent = '与' + name + '的对话';
    applyCustomCss();
  }

  /* ---------------------------------------------------------- 会话 */
  function loadSessions() {
    return A.post(CHAT + 'list', { characterId: state.charId, page: 1, pageSize: 30 })
      .then((data) => {
        state.sessions = T.asList(data, ['list', 'records']);
        renderSessions();
        const q = params();
        const want = q.get('session');
        // 从探索页进入（?new=1）：强制开一条新对话，而不是续上最近一条
        if (q.get('new') === '1') {
          return createSession(true).then((sid) => {
            if (!sid) return false;
            try {
              const u = new URL(location.href);
              u.searchParams.delete('new');
              u.searchParams.set('session', sid);
              history.replaceState(null, '', u.pathname + '?' + u.searchParams.toString());
            } catch (e) { /* 老浏览器忽略，刷新时最多再建一条 */ }
            return loadSessions();
          });
        }
        let hit = want ? state.sessions.find((s) => T.str(T.pick(s, ['sessionId', 'id'])) === want) : null;
        if (!hit && state.sessions.length) hit = state.sessions[0];
        if (hit) return selectSession(T.str(T.pick(hit, ['sessionId', 'id'])));
        return createSession(false);
      })
      .catch((err) => {
        $('session-list').innerHTML = U.emptyHtml('会话列表加载失败', err.message);
        renderEmpty('对话不可用：' + err.message);
        return false;
      });
  }

  function renderSessions() {
    const box = $('session-list');
    if (!state.sessions.length) {
      box.innerHTML = U.emptyHtml('还没有对话', '直接发消息就会自动创建');
      return;
    }
    box.innerHTML = state.sessions.map((s, i) => {
      const sid = T.str(T.pick(s, ['sessionId', 'id']));
      const title = T.str(T.pick(s, ['title']), '未命名对话');
      const preview = T.str(T.pick(s, ['preview', 'lastMessage']));
      const active = sid === state.sessionId;
      const checked = state.selectedSessions.has(sid);
      const cb = state.multiSelect
        ? '<label class="session-cb" onclick="event.stopPropagation()"><input type="checkbox" data-sid="' + esc(sid) + '"' + (checked ? ' checked' : '') + '></label>'
        : '';
      return '<div class="session-item' + (active ? ' active' : '') + (state.multiSelect ? ' multi' : '') + '" data-sid="' + esc(sid) + '" data-i="' + i + '">' +
        cb +
        '<div class="session-body"><b>' + esc(title) + '</b><small>' + esc(preview.slice(0, 40) || '空对话') + '</small></div></div>';
    }).join('');
    Array.prototype.forEach.call(box.querySelectorAll('.session-item'), (el) => {
      el.addEventListener('click', (e) => {
        if (state.multiSelect) {
          const cb = el.querySelector('.session-cb input');
          if (cb) cb.checked = !cb.checked;
          const sid = el.getAttribute('data-sid');
          if (cb && cb.checked) state.selectedSessions.add(sid);
          else state.selectedSessions.delete(sid);
          syncBatchBar();
        } else {
          selectSession(el.getAttribute('data-sid'));
        }
      });
    });
  }

  function selectSession(sid) {
    if (!sid) return Promise.resolve(false);
    state.sessionId = sid;
    resetIntroActions();
    renderSessions();
    state.messages = [];
    state.autoFollow = true;
    renderMessages();
    return loadMessages();
  }

  function createSession(fresh) {
    return A.post(CHAT + 'create', { characterId: state.charId, fresh: !!fresh })
      .then((data) => {
        const sid = T.str(T.pick(data, ['sessionId', 'id']));
        if (!sid) throw new Error('未能创建会话');
        state.sessionId = sid;
        resetIntroActions();
        renderSessions();
        state.messages = [];
        state.autoFollow = true;
        renderMessages();
        return sid;
      })
      .catch((err) => {
        U.toast(err.status === 401 ? '请先登录后再创建对话' : ('创建对话失败：' + err.message), false);
        return false;
      });
  }

  function loadMessages() {
    if (!state.sessionId) return Promise.resolve(false);
    $('chat-scroll').innerHTML = '<div class="empty-center">正在加载消息…</div>';
    return A.post(CHAT + 'messagesOptimized', {
      sessionId: state.sessionId, pageSize: 60, orderBy: 'desc',
    }).then((data) => {
      const list = T.isObj(data) && Array.isArray(data.messages) ? data.messages : T.asList(data, ['messages']);
      state.messages = list.map((m) => ({
        id: T.pick(m, ['id', 'messageId']),
        role: T.str(m.role) === 'user' ? 'user' : 'assistant',
        content: T.str(m.content),
        // 后端新契约：正文去掉状态栏/选项，二者单独下发，由前端重建原生标签
        displayText: T.str(m.displayText || m.content),
        statusItems: Array.isArray(m.status) ? m.status : null,
        statusValues: (T.isObj(m.statusValues) ? m.statusValues : null),
        options: Array.isArray(m.options) ? m.options : null,
        createdAt: T.str(m.createdAt),
      }));
      renderMessages();
      applyDisplay();   // 历史消息渲染完成后按 AICG 开关显隐内联 CG / 状态条
      return true;
    }).catch((err) => {
      state.messages = [];
      renderEmpty('消息加载失败：' + err.message);
      return false;
    });
  }

  /* ---------------------------------------------------------- 消息渲染 */
  function extractOptions(block) {
    return String(block || '').split('\n').map((s) => s.trim()).filter(Boolean);
  }

  function splitPrompts(text) {
    const raw = T.str(text);
    const m = /<next_prompt[^>]*>([\s\S]*?)<\/next_prompt>/i.exec(raw);
    const visible = raw.replace(/<next_prompt[^>]*>[\s\S]*?<\/next_prompt>/gi, '').trim();
    return { visible, options: m ? extractOptions(m[1]) : [] };
  }

  /* 流式解析已独立到 parser.js：半包截断 / 图片与动作占位符 / 状态栏提取。
     renderer.js 只负责把占位符还原成受控 HTML。 */

  function userAvatar() {
    const u = state.profile && state.profile.user;
    return T.str(u && (u.headerImg || u.avatar));
  }

  /* 开场白允许少量富文本标签，其余一律剔除，防止注入 */
  function greetingHtml(src) {
    const s = String(src || '');
    if (s.indexOf('<') < 0) return esc(s);
    const ok = /^(b|strong|i|em|u|s|mark|br|p|div|span|ul|ol|li|blockquote|code|a|img)$/i;
    return s.replace(/<\/?([a-zA-Z][\w-]*)([^>]*)>/g, (m, tag, attrs) => {
      if (!ok.test(tag)) return '';
      const lower = tag.toLowerCase();
      if (lower === 'a') {
        const href = /href\s*=\s*"([^"]*)"/i.exec(attrs);
        if (!href || /^\s*javascript:/i.test(href[1])) return '';
        return '<a href="' + esc(href[1]) + '" target="_blank" rel="noopener">';
      }
      if (lower === 'img') {
        const src2 = /src\s*=\s*"([^"]*)"/i.exec(attrs);
        if (!src2 || /^\s*javascript:/i.test(src2[1])) return '';
        return '<img src="' + esc(src2[1]) + '" alt="">';
      }
      return '<' + (/^\//.test(m) ? '/' : '') + lower + '>';
    });
  }

  /* 正文渲染已独立到 renderer.js：占位符还原、标签消毒、状态栏/图片都由它负责。
     状态胶囊的配色也由 renderer 按属性名计算，不再在这里后置打 class。 */

  /* 角色自定义 CSS 换肤：作用域隔离交给 theme_manager.js，
     「阅读安全模式 / 禁用作者样式」开着时不注入（与源站一致）。 */
  function applyCustomCss() {
    const off = displayPrefs().disableCustomStyle || displayPrefs().disableAuthorStyle;
    const css = off ? '' : T.str(state.char && (state.char.customCSS || state.char.customCss));
    App.theme.applyCharacterCSS(css, state.charId);
  }

  /* 统一的消息正文渲染（相当于占位符方案里的 renderMessageDOM）：
     原始文本 → 拆出选项 → 白名单 HTML → 状态栏/选项按需显示。
     isStreaming=true 时状态栏整块先不渲染（等收尾随完整文本出现），选项也不渲染，
     避免流式期间半截状态栏跳动、选项反复出现。
     DOM 里直接保留 <say> / <book_status> 等原生标签（自定义元素），
     浏览器把它们当块级/自定义元素渲染，角色卡 customCSS 可直接命中。 */
  function renderMessageDOM(text, isStreaming, statusItems, options, statusValues) {
    const parsed = splitPrompts(text);
    let body = parsed.visible;
    if (isStreaming) {
      body = body
        .replace(/<book_status[^>]*>[\s\S]*?<\/book_status>/gi, '')
        .replace(/<book_status[^>]*$/i, '');
    }
    const opts = isStreaming
      ? []
      : (Array.isArray(options) ? options : parsed.options);
    // 状态数据两种形态：后端新契约 statusValues 字典（属性名任意），
    // 旧数据只有 status 数组 → 统一交给渲染器
    let statusData = null;
    if (statusValues && Object.keys(statusValues).length) {
      statusData = { values: statusValues };
    } else if (Array.isArray(statusItems) && statusItems.length) {
      statusData = { items: statusItems.map((it) => ({ label: T.str(it && it.name), value: it && it.value })) };
    }
    return {
      // 安全正文 + 结构化状态栏 → DOM（全部由 renderer.js 负责）
      html: App.renderer.render(body || '', statusData, isStreaming),
      options: opts,
    };
  }

  // isLast：只有最后一条消息才渲染剧情选项；点过选项/继续对话后旧选项自动收起
  function msgHtml(m, isLast) {
    const isUser = m.role === 'user';
    const body = isUser
      ? { html: esc(m.content || ''), options: [] }
      : renderMessageDOM(m.displayText || m.content, !!m.draft,
                         m.statusItems, m.options, m.statusValues);
    const name = isUser ? '我' : charName();
    const avatar = isUser ? userAvatar() : T.str(T.pick(state.char, ['avatarURL', 'avatarUrl']));
    const ph = U.placeholder(T.str(name).slice(0, 1), isUser ? 5 : 6);
    // 气泡容器升级为 content-renderer-v3-container（对齐原站），让作者 customCSS 能按作用域命中
    // theme-light 默认亮色；theme-dark 由「亮色模式」按钮切换
    const bubbleCls = isUser
      ? 'bubble'
      : 'bubble content-renderer-v3-container theme-light markdown-body';
    // draft 占位：对齐原站「内容正在加载中…」
    const bubbleInner = isUser
      ? body.html
      : (m.draft && !body.html
          ? '<span class="renderer-placeholder">内容正在加载中，请耐心等待…</span>'
          : body.html);
    return '<div class="msg ' + (isUser ? 'user' : 'ai') + '" data-mid="' + esc(T.str(m.id)) + '">' +
      '<img class="avatar" src="' + esc(avatar || ph) + '" alt="">' +
      '<div>' +
      // 历史消息里的 CG 是之前已经扣过费的，开关只影响新回复，这里不隐藏
      ((m.cgImages && m.cgImages.length)
        ? '<div class="cg-strip">' + m.cgImages.map((cg) => {
            const url = cg.url || cg.imageURL || '';
            const alt = esc(cg.imageName || 'CG');
            return '<figure class="cg-card">' +
              '<img src="' + esc(url) + '" alt="' + alt + '">' +
              '<figcaption class="cg-actions">' +
              '<button class="cg-share" type="button">分享到共创池</button>' +
              '<button class="cg-del" type="button">删除</button>' +
              '</figcaption>' +
              '</figure>';
          }).join('') + '</div>'
        : '') +
      '<div class="meta">' + esc(name) + '</div>' +
      '<div class="' + bubbleCls + '" data-bubble>' + bubbleInner + '</div>' +
      '<div class="msg-actions"><button data-copy="' + esc(T.str(m.id)) + '">复制</button>' +
      (isUser ? '' : '<button data-regen="' + esc(T.str(m.id)) + '">重新生成</button>') +
      '<button data-delmsg="' + esc(T.str(m.id)) + '">删除</button></div>' +
      '</div></div>' +
      (isLast && body.options.length
        ? '<div class="option-list"><div class="option-head"><span class="option-head-dot"></span><b>选择你的回复</b></div>' +
          body.options.map((o, i) => {
            // 去掉原文本里的 A、/ 1、 前缀，用数字圆标代替（对齐原站）
            const text = String(o).replace(/^\s*[A-Za-z]\s*[、.．:：]\s*/, '').replace(/^\s*\d+\s*[、.．:：]\s*/, '');
            return '<button class="option-btn" data-opt="' + esc(o) + '">' +
              '<span class="opt-num">' + (i + 1) + '</span>' +
              '<span class="opt-text">' + esc(text) + '</span>' +
              '<span class="opt-arrow" aria-hidden="true">›</span>' +
              '</button>';
          }).join('') + '</div>'
        : '');
  }

  /* 角色详细介绍网页：像源站新建对话一样，把 description 直接渲染成网页 */
  function charIntroBlock() {
    const desc = T.str(state.char && state.char.description);
    if (!desc) return '';
    return '<div class="char-intro"><iframe title="角色详细介绍" sandbox="allow-scripts"></iframe></div>';
  }

  function introDoc(html) {
    return '<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">' +
      '<style>html,body{margin:0}body{padding:18px 16px;background:#fff;color:#222;' +
      'font:14px/1.7 system-ui,-apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;' +
      'word-break:break-word}img{max-width:100%;height:auto}*{box-sizing:border-box}a{color:#7c5cfc}' +
      // 隐藏原生滚动条（与原站 sandbox 一致），滚动仍可用，只是不显示条
      '::-webkit-scrollbar{display:none!important;width:0!important;height:0!important}' +
      'html,body{-ms-overflow-style:none!important;scrollbar-width:none!important;overflow-x:clip!important}' +
      '</style>' +
      '</head><body>' +
      // 剪贴板拦截（与原站 sandbox 一致）：卡片脚本点「一键复制设定」时，writeText /
      // execCommand('copy') 不直写剪贴板，改发 sandbox-copy 给宿主捕获。
      // 必须排在卡片脚本之前注入，好让它的调用落在重写后的方法上。
      '<script>(function(){' +
      'function n(t){try{window.parent.postMessage({type:"sandbox-copy",text:String(t||"")},"*")}catch(e){}}' +
      'if(navigator.clipboard){navigator.clipboard.writeText=function(t){n(t);return Promise.resolve()}}' +
      'var o=document.execCommand?document.execCommand.bind(document):null;' +
      'document.execCommand=function(c){' +
      'if(c==="copy"){var el=document.activeElement,s=window.getSelection(),t="";' +
      'if(el&&(el.tagName==="TEXTAREA"||el.tagName==="INPUT"))t=el.value;else if(s)t=String(s);' +
      'n(t);return true}' +
      'return o?o.apply(null,arguments):false};' +
      '})();</script>' +
      String(html || '') + '</body></html>';
  }

  function fillIntro(box) {
    const f = box.querySelector('.char-intro iframe');
    if (!f) return;
    const desc = T.str(state.char && state.char.description);
    if (!desc) return;
    f.srcdoc = introDoc(desc);
  }

  /* 页面中下部的「发送并开始游戏」按钮（只在捕获到文字后出现） */
  const INTRO_SEND = '<div class="intro-send" hidden>' +
    '<button type="button" id="btn-send-intro">发送并开始游戏</button></div>';

  /* 捕获到介绍网页的文字：右上角提示 + 显示发送按钮 */
  function showIntroActions(text) {
    state.introText = text;
    const toast = $('intro-toast');
    const label = $('intro-toast-text');
    if (toast && label) {
      label.textContent = text.slice(0, 40);
      toast.title = text;
      toast.hidden = false;
    }
    const send = document.querySelector('.intro-send');
    if (send) send.hidden = false;
  }

  function resetIntroActions() {
    state.introText = '';
    const toast = $('intro-toast');
    if (toast) toast.hidden = true;
    const send = document.querySelector('.intro-send');
    if (send) send.hidden = true;
  }

  function copyIntroText() {
    const text = T.str(state.introText);
    if (!text) return;
    const done = () => {
      const btn = $('intro-copy');
      if (!btn) return;
      btn.textContent = '已复制';
      setTimeout(() => { if ($('intro-copy')) $('intro-copy').textContent = '复制'; }, 1500);
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done).catch(() => {
        if (fallbackCopy(text)) done();
      });
    } else if (fallbackCopy(text)) {
      done();
    }
  }

  function fallbackCopy(text) {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.left = '-9999px';
    document.body.appendChild(ta);
    ta.select();
    let ok = false;
    try { ok = document.execCommand('copy'); } catch (e) { /* noop */ }
    document.body.removeChild(ta);
    return ok;
  }

  /* 发送并开始游戏：把捕获到的文字粘进输入框并发送 */
  function sendIntro() {
    const text = T.str(state.introText).trim();
    if (!text) { U.toast('还没有捕获到可发送的文字', false); return; }
    const input = $('chat-input');
    if (input) { input.value = text; autoGrow(); }
    resetIntroActions();
    send(text);
  }

  function renderMessages() {
    const box = $('chat-scroll');
    // 有消息才显示右侧悬浮按钮组（与源站一致：无消息不渲染）
    const float = $('chat-float');
    if (float) float.hidden = !state.messages.length;
    const intro = state.messages.length ? '' : charIntroBlock();
    if (intro) {
      // 没有消息时：详细介绍网页占满整个聊天区（像源站）
      box.classList.add('intro-mode');
      box.innerHTML = intro + INTRO_SEND;
      fillIntro(box);
      const send = box.querySelector('.intro-send');
      if (send) send.hidden = !state.introText;
      box.scrollTop = 0;
      return;
    }
    box.classList.remove('intro-mode');
    resetIntroActions();
    if (!state.messages.length) {
      const first = T.str(T.pick(state.char, ['firstMes', 'firstMessage']));
      if (first) {
        const ph = U.placeholder(charName().slice(0, 1), 6);
        box.innerHTML = '<div class="msg ai"><img class="avatar" src="' +
          esc(T.str(T.pick(state.char, ['avatarURL'])) || ph) + '" alt="">' +
          '<div><div class="meta">' + esc(charName()) + '</div>' +
          '<div class="bubble markdown-body" style="white-space:pre-wrap">' + greetingHtml(first) + '</div></div></div>' +
          '<div class="empty-center small muted">发一条消息开始对话</div>';
      } else {
        box.innerHTML = '<div class="empty-center">还没有消息，说点什么开始吧。</div>';
      }
    } else {
      box.innerHTML = state.messages.map((m, i) =>
        msgHtml(m, i === state.messages.length - 1)).join('');
      Array.prototype.forEach.call(box.querySelectorAll('img.avatar'), (img, i) => U.safeImg(img, '头', i));
      bindMessageActions(box);
      if (state.autoFollow !== false) scrollBottom();
    }
  }

  function renderEmpty(text) {
    $('chat-scroll').innerHTML = '<div class="empty-center">' + esc(text) + '</div>';
  }

  /* CG 图：按关键词触发，插到对应 AI 消息下方 */
  function renderCgImages(cgList) {
    if (!cgList || !cgList.length) return;
    if (!displayPrefs().aigcIllust) return;   // AICG 插图开关关闭：不实时插入 CG 条
    const box = $('chat-scroll');
    const strip = document.createElement('div');
    strip.className = 'cg-strip';
    strip.innerHTML = cgList.map((cg) => {
      const url = cg.url || cg.imageURL || '';
      const alt = esc(cg.imageName || 'CG');
      return '<figure class="cg-card"><img src="' + esc(url) + '" alt="' + alt + '">' +
        '<figcaption>' + alt + '</figcaption></figure>';
    }).join('');
    box.appendChild(strip);
    scrollBottom();
  }

  function bindMessageActions(box) {
    Array.prototype.forEach.call(box.querySelectorAll('[data-opt]'), (btn) => {
      btn.addEventListener('click', () => {
        const text = btn.getAttribute('data-opt') || '';
        const input = $('chat-input');
        input.value = text;
        autoGrow();
        input.focus();
        send(text);
      });
    });
    Array.prototype.forEach.call(box.querySelectorAll('[data-copy]'), (btn) => {
      btn.addEventListener('click', () => {
        const id = btn.getAttribute('data-copy');
        const m = state.messages.find((x) => T.str(x.id) === id);
        if (!m) return;
        try {
          navigator.clipboard.writeText(m.displayText || splitPrompts(m.content).visible);
          U.toast('已复制');
        } catch (e) { U.toast('复制失败', false); }
      });
    });
    Array.prototype.forEach.call(box.querySelectorAll('[data-delmsg]'), (btn) => {
      btn.addEventListener('click', () => {
        const id = btn.getAttribute('data-delmsg');
        if (!id || id.indexOf('local-') === 0) { U.toast('这条消息还没有保存', false); return; }
        U.confirmDialog('删除消息', '确认删除这条消息？', () => {
          A.post(CHAT + 'deleteMessage', { messageId: id })
            .then(() => {
              state.messages = state.messages.filter((x) => T.str(x.id) !== id);
              renderMessages();
              U.toast('已删除');
            })
            .catch((err) => U.toast('删除失败：' + err.message, false));
        });
      });
    });
    Array.prototype.forEach.call(box.querySelectorAll('[data-regen]'), (btn) => {
      btn.addEventListener('click', () => {
        U.toast('重新生成：请重新发送上一条消息（本地模式会复用已保存内容）', false);
      });
    });
  }

  function scrollBottom() {
    const box = $('chat-scroll');
    box.scrollTop = box.scrollHeight;
    if (state.syncBottomBtn) state.syncBottomBtn();
  }

  function autoGrow() {
    const el = $('chat-input');
    el.style.height = 'auto';
    el.style.height = Math.min(el.scrollHeight, 160) + 'px';
  }

  /* ---------------------------------------------------------- 发送 / 流式 */
  function setBusy(busy) {
    state.streaming = busy;
    $('btn-send').disabled = busy;
    $('btn-stop').hidden = !busy;
    $('compose-state').textContent = busy ? '生成中…' : 'Enter 发送';
  }

  function send(text) {
    const msg = (text || '').trim();
    if (!msg) return;
    if (state.streaming) { U.toast('正在生成，请先停止', false); return; }
    const go = state.sessionId ? Promise.resolve(state.sessionId) : createSession(false);
    go.then((sid) => {
      if (!sid) return;
      const localId = 'local-' + Date.now();
      state.messages.push({ id: localId, role: 'user', content: msg, createdAt: '' });
      state.messages.push({ id: 'draft', role: 'assistant', content: '', draft: true });
      state.autoFollow = true;
      renderMessages();
      setBusy(true);
      $('chat-input').value = '';
      autoGrow();

      // 本次回复的流式状态机：缓冲原始片段，RAF 合帧后再解析 -> 渲染
      const streamStore = new App.ChatStream({
        shouldFlush: () => state.streamOn,
        parse: App.parseStream,
        render(parsed) {
          const draft = state.messages.find((m) => m.draft);
          if (draft) draft.statusData = parsed.statusData;
          const node = document.querySelector('[data-mid="draft"] [data-bubble]');
          if (node) {
            node.innerHTML = renderMessageDOM(parsed.safeText, true).html;
            // 状态栏独立成块：后续 status_update 只换这一块，不动正文
            if (draft && draft.statusData) {
              node.insertAdjacentHTML('beforeend',
                '<div class="stream-status">' + App.renderer.buildStatusBar(draft.statusData) + '</div>');
            }
          }
          if (state.autoFollow !== false) scrollBottom();
        },
      });

      const finish = (errMsg) => {
        // 丢掉还没落地的流式帧，避免它盖住收尾后的完整渲染
        streamStore.reset();
        const draft = state.messages.find((m) => m.draft);
        if (draft) {
          draft.draft = false;
          if (!draft.content) draft.content = errMsg || '（没有收到回复，请重试）';
        }
        setBusy(false);
        renderMessages();
        refreshSessionsSoon();
      };

      // 「自定」启用条目随请求带给后端注入（见 server._inject_custom）；
      // 记忆区摘要由后端自动维护并读库注入，前端不用再传
      const customs = customList().filter((it) =>
        it && it.enabled !== false && String(it.content || '').trim());
      // 本次回复的流式解析器由 ChatStream 持有：它决定什么时候解析、什么时候重绘
      state.stream = App.sse.streamChat(CHAT + 'chatStream', {
        sessionId: sid,
        characterId: state.charId,
        message: msg,
        // 选中的模型（后端 _rate_id 认 modelRateId；0/空 → 用默认接入）
        modelRateId: Number(state.modelRateId) || 0,
        customSettings: customs,
        // AICG 插图开关：本次请求即带，后端据此决定是否拼 CG 目录 / 兜底补图
        enableAICG: displayPrefs().aigcIllust !== false,
        // prompt 快照开关（前端 localStorage 控制，后端读这个字段决定是否落盘）
        promptLog: displayPrefs().promptLog === true,
      }, {
        onContent(piece) {
          const draft = state.messages.find((m) => m.draft);
          if (!draft) return;
          // 只入缓冲：解析 + 预处理 + 重绘统一由 ChatStream 在下一帧完成
          streamStore.appendChunk(piece);
          draft.content = streamStore.raw();     // 原始文本照旧保留（复制/落库都是它）
        },
        onClear() {
          streamStore.reset();
          const draft = state.messages.find((m) => m.draft);
          if (!draft) return;
          draft.content = '';
          draft.statusData = null;
          const node = document.querySelector('[data-mid="draft"] [data-bubble]');
          if (node) node.innerHTML = '';
        },
        onStatus(frame) {
          const draft = state.messages.find((m) => m.draft);
          if (!draft) return;
          const items = (Array.isArray(frame && frame.items) ? frame.items : []).map((it) => ({
            label: T.str(it && it.name), value: it && it.value,
          })).filter((it) => it.label || it.value);
          if (!items.length) return;
          draft.statusData = { items, values: frame && frame.values };
          // 只更新状态胶囊，正文不动（下一次文本帧会带着同一份数据重画）
          const node = document.querySelector('[data-mid="draft"] [data-bubble]');
          if (!node) return;
          let box = node.querySelector('.stream-status');
          if (!box) {
            box = document.createElement('div');
            box.className = 'stream-status';
            node.appendChild(box);
          }
          box.innerHTML = App.renderer.buildStatusBar(draft.statusData);
        },
        onImage(frame) {
          // image_trigger：正文里已内嵌图片的独立通知，按 URL 去重后置顶
          const url = T.str(frame && frame.url);
          if (!url) return;
          const node = document.querySelector('[data-mid="draft"] [data-bubble]');
          if (!node) return;
          const already = Array.from(node.querySelectorAll('img'))
            .some((img) => img.getAttribute('src') === url);
          if (already) return;
          const loading = node.querySelector('figure.cg-loading');
          if (loading) {
            // 流式期间占位的骨架屏 → 换成真实图片
            loading.classList.remove('cg-loading', 'sys-image-loading');
            const sp = loading.querySelector('.spinner');
            if (sp && sp.parentNode) sp.parentNode.removeChild(sp);
            const label = loading.querySelector('span');
            if (label && label.parentNode) label.parentNode.removeChild(label);
            let img = loading.querySelector('img');
            if (!img) {
              img = document.createElement('img');
              img.className = 'sys-image';
              img.alt = 'CG';
              img.loading = 'lazy';
              loading.appendChild(img);
            }
            img.src = url;
            return;
          }
          const fig = document.createElement('figure');
          fig.className = 'cg-inline sys-image-wrapper';
          const img = document.createElement('img');
          img.className = 'sys-image';
          img.alt = 'CG';
          img.loading = 'lazy';
          img.src = url;
          fig.appendChild(img);
          node.insertBefore(fig, node.firstChild);
        },
        onDone(frame) {
          const draft = state.messages.find((m) => m.draft);
          if (draft) {
            if (frame && frame.assistantMessageId) draft.id = frame.assistantMessageId;
            // 收尾用后端下发的展示态重建：正文无状态栏/选项，二者走结构化数据
            if (frame && typeof frame.displayText === 'string') {
              draft.displayText = frame.displayText;
              draft.content = frame.displayText;
            }
            if (frame && Array.isArray(frame.status)) draft.statusItems = frame.status;
            if (frame && frame.statusValues && typeof frame.statusValues === 'object') {
              draft.statusValues = frame.statusValues;
            }
            if (frame && Array.isArray(frame.options)) draft.options = frame.options;
          }
          if (frame && frame.cg) renderCgImages(frame.cg);
        },
        onCg(frame) {
          const cgList = (frame && frame.cg) || [];
          const draft = state.messages.find((m) => m.draft);
          if (draft) draft.cgImages = cgList;
          renderCgImages(cgList);
        },
        onUsage(frame) {
          const used = T.num(T.pick(frame, ['totalPointsDeducted', 'pointsConsumed'], 0), 0);
          if (used > 0) {
            const left = T.pick(frame, ['balance'], null);
            U.toast('本次消耗 ' + used + ' 积分' +
              (left === null ? '' : '，余额 ' + left));
          }
        },
        onError(err) {
          U.toast(err.message || '生成失败', false);
        },
        onClose(reason) {
          if (reason === 'stopped') {
            const draft = state.messages.find((m) => m.draft);
            if (draft && !draft.content) draft.content = '（已停止生成）';
          }
          finish(reason === 'stopped' ? '' : '');
        },
      });
    });
  }

  function stop() {
    if (!state.sessionId) return;
    A.post(CHAT + 'stop', { sessionId: state.sessionId }).catch(() => {});
    if (state.stream && state.stream.abort) state.stream.abort();
    setBusy(false);
  }

  let refreshTimer = 0;
  function refreshSessionsSoon() {
    clearTimeout(refreshTimer);
    refreshTimer = setTimeout(() => {
      A.post(CHAT + 'list', { characterId: state.charId, page: 1, pageSize: 30 })
        .then((data) => { state.sessions = T.asList(data, ['list', 'records']); renderSessions(); })
        .catch(() => {});
    }, 600);
  }

  /* ---------------------------------------------------------- 会话操作 */
  function restart() {
    if (state.streaming) stop();
    U.confirmDialog('重开对话', '会新建一条空白对话（旧对话仍保留在列表里）。', () => {
      createSession(true).then((sid) => { if (sid) U.toast('已重开对话'); });
    });
  }

  function newSession() {
    if (state.streaming) stop();
    createSession(true).then((sid) => { if (sid) U.toast('已新建对话'); });
  }

  function clearSession() {
    if (!state.sessionId) return;
    U.confirmDialog('清空对话', '会删除当前这条对话及其消息，确认吗？', () => {
      A.post(CHAT + 'delete', { sessionId: state.sessionId })
        .then(() => {
          state.sessionId = '';
          state.messages = [];
          renderMessages();
          loadSessions();
          U.toast('已清空');
        })
        .catch((err) => U.toast('清空失败：' + err.message, false));
    });
  }

  function setMultiSelect(on) {
    state.multiSelect = !!on;
    if (!on) state.selectedSessions.clear();
    renderSessions();
    syncBatchBar();
  }
  function syncBatchBar() {
    const bar = $('session-batch-bar');
    if (!bar) return;
    bar.hidden = !state.multiSelect;
    const all = $('batch-select-all');
    if (all) {
      const total = state.sessions.length;
      all.checked = total > 0 && state.selectedSessions.size === total;
    }
  }
  function batchDelete() {
    const sids = Array.from(state.selectedSessions);
    if (!sids.length) { U.toast('请先选择要删除的对话', false); return; }
    U.confirmDialog('删除所选对话', `将删除 ${sids.length} 条对话及其消息，确认吗？`, () => {
      Promise.all(sids.map((sid) =>
        A.post(CHAT + 'delete', { sessionId: sid }).catch(() => null)
      )).then(() => {
        // 如果删了当前会话，清掉当前消息
        if (sids.indexOf(state.sessionId) >= 0) {
          state.sessionId = '';
          state.messages = [];
          renderMessages();
        }
        setMultiSelect(false);
        loadSessions();
        U.toast('已删除所选对话');
      });
    });
  }

  /* ---------------------------------------------------------- 绑定 / 启动 */
  function bind() {
    // 输入栏工具行：流式开关 / 评论（预留）/ 自定 / 记忆
    const sBtn = $('tb-stream');
    if (sBtn) {
      sBtn.classList.toggle('on', !!state.streamOn);
      sBtn.textContent = state.streamOn ? '流式' : '非流式';
      sBtn.addEventListener('click', () => {
        state.streamOn = !state.streamOn;
        S.set('chat.streaming', state.streamOn ? 1 : 0);
        sBtn.classList.toggle('on', state.streamOn);
        sBtn.textContent = state.streamOn ? '流式' : '非流式';
        U.toast(state.streamOn ? '已开启流式输出' : '已切换为非流式（回复结束后一次显示）');
      });
    }
    const cBtn = $('tb-comment');
    if (cBtn) {
      // 预留：评论区尚未开放，接口就绪后在此接入
      cBtn.addEventListener('click', () => U.toast('评论功能即将开放，敬请期待'));
    }
    const cuBtn = $('tb-custom');
    if (cuBtn) cuBtn.addEventListener('click', openCustomPanel);
    const meBtn = $('tb-memory');
    if (meBtn) meBtn.addEventListener('click', openMemoryPanel);

    // 右侧悬浮按钮：齿轮=对话设置；箭头=回底（滚动不在底部时出现）
    const csBtn = $('btn-chat-settings');
    if (csBtn) csBtn.addEventListener('click', openChatSettings);
    const sbBtn = $('btn-scroll-bottom');
    const sBox = $('chat-scroll');
    if (sbBtn && sBox) {
      sbBtn.addEventListener('click', () => {
        sBox.scrollTo({ top: sBox.scrollHeight, behavior: 'smooth' });
      });
      // 滚动位置检测：距底部 80px 内视为「在底部」→ 藏起回底按钮
      const syncBottomBtn = () => {
        const away = sBox.scrollHeight - sBox.scrollTop - sBox.clientHeight > 80;
        sbBtn.hidden = !away;
        // 用户在往上翻看历史时暂停自动跟随，生成结束也不把他拉回底部
        state.autoFollow = !away;
      };
      sBox.addEventListener('scroll', syncBottomBtn, { passive: true });
      window.addEventListener('resize', syncBottomBtn);
      state.syncBottomBtn = syncBottomBtn;
      syncBottomBtn();
    }

    // 模型切换：点胶囊开合下拉，点外部关闭
    const mBtn = $('btn-model');
    if (mBtn) {
      mBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        const menu = $('model-menu');
        if (menu) menu.hidden = !menu.hidden;
      });
      document.addEventListener('click', (e) => {
        const menu = $('model-menu');
        if (!menu || menu.hidden) return;
        if (!e.target || !e.target.closest || !e.target.closest('.model-pick')) menu.hidden = true;
      });
    }
    // 介绍网页里的脚本（一键复制设定 / 打开开始等）postMessage 出来的文字：
    // 不直接发送，先捕获并在右上角提示，由用户手动复制或点「发送并开始游戏」
    window.addEventListener('message', (e) => {
      const d = e.data;
      if (!d || typeof d.text !== 'string') return;
      if (d.type !== 'sendMessage' && d.type !== 'sandbox-copy') return;
      const f = document.querySelector('.char-intro iframe');
      if (!f || e.source !== f.contentWindow) return;
      const text = d.text.trim();
      if (text) showIntroActions(text);
    });
    $('intro-copy').addEventListener('click', copyIntroText);
    $('intro-close').addEventListener('click', () => { $('intro-toast').hidden = true; });
    $('chat-scroll').addEventListener('click', (e) => {
      if (e.target && e.target.id === 'btn-send-intro') sendIntro();
    });
    $('btn-send').addEventListener('click', () => send($('chat-input').value));
    $('btn-stop').addEventListener('click', stop);
    $('btn-new-session').addEventListener('click', newSession);
    $('btn-restart').addEventListener('click', restart);
    $('btn-clear').addEventListener('click', clearSession);
    const multiBtn = $('btn-session-multi');
    if (multiBtn) multiBtn.addEventListener('click', () => setMultiSelect(!state.multiSelect));
    const batchDel = $('btn-batch-delete');
    if (batchDel) batchDel.addEventListener('click', batchDelete);
    const batchCancel = $('btn-batch-cancel');
    if (batchCancel) batchCancel.addEventListener('click', () => setMultiSelect(false));
    const batchAll = $('batch-select-all');
    if (batchAll) batchAll.addEventListener('change', () => {
      if (batchAll.checked) state.sessions.forEach((s) => {
        const sid = T.str(T.pick(s, ['sessionId', 'id']));
        state.selectedSessions.add(sid);
      }); else state.selectedSessions.clear();
      renderSessions();
    });
    const input = $('chat-input');
    input.addEventListener('input', autoGrow);
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        send(input.value);
      }
    });
  }

  function start(charId) {
    state.charId = charId;
    S.setLastWork(charId);
    Promise.all([loadCharacter(), S.session()]).then((results) => {
      const ok = results[0];
      state.profile = results[1];
      if (!ok) {
        $('c-name').textContent = '角色不存在或未发布';
        $('c-summary').textContent = '可能已被删除，请到创作页重新选择。';
        renderEmpty('这个角色读不到，去创作页检查一下吧。');
        return;
      }
      loadSessions();
    });
  }

  function fallbackFromWorks() {
    A.get('/api/gva/works/my', { page: 1, pageSize: 20 })
      .then((data) => {
        const list = T.asList(data, ['list', 'records']);
        if (list.length) {
          const cid = T.str(T.pick(list[0], ['characterId', 'ID', 'id']));
          if (cid) { start(cid); return; }
        }
        $('c-name').textContent = '还没有角色';
        $('c-summary').textContent = '先到创作页创建一个角色，然后点「去聊天」。';
        renderEmpty('还没有可聊的角色');
        $('session-list').innerHTML = '<a class="btn sm primary" href="/app/creation">去创作</a>';
      })
      .catch(() => {
        renderEmpty('请先登录，或从创作页进入聊天');
        $('c-name').textContent = '未登录';
        $('session-list').innerHTML = '<button class="btn sm primary" id="chat-login">登录</button>';
        const b = $('chat-login');
        if (b) b.addEventListener('click', () => U.loginDialog(() => {
          const cid = params().get('id') || S.lastWork();
          if (cid) start(cid); else fallbackFromWorks();
        }));
      });
  }

  function init() {
    U.mountTopbar('chat');
    applyDisplay();          // 对话设置里的显示偏好（字号/行距等）先行生效
    bind();
    loadModels();
    const cid = params().get('id') || S.lastWork();
    if (cid) start(cid); else fallbackFromWorks();
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
