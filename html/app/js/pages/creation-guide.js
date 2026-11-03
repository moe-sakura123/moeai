/* pages/creation-guide.js — HTML/CSS 教程页：模板库 + 实时试验台
   纯本地渲染：预览放在 sandbox 的 iframe 里，脚本只来自本页注入的标签归一器。 */
(function () {
  'use strict';
  const U = App.ui;
  const $ = (id) => document.getElementById(id);
  const esc = U.esc;

  /* ---------------------------------------------------------- 模板 */
  const TEMPLATES = [
    {
      id: 'status',
      title: '状态面板',
      level: '入门',
      desc: '三条状态：体力 / 精神 / 备注，最常用的信息条。',
      html: `<status class="hp">体力：72/100</status>
<status class="mp">精神：轻度疲惫</status>
<status class="note">状态：在壁炉边烤火</status>`,
      css: `/* 可改：边框色、底色、文字色 */
.markdown-body status {
  display: block;
  margin: 6px 0;
  padding: 6px 12px;
  border-left: 3px solid #d4af37;
  background: rgba(212, 175, 55, .08);
  color: #f0d98a;
  border-radius: 0 6px 6px 0;
  font-weight: 600;
}
/* 可改：第二条换个颜色 */
.markdown-body status.note {
  border-left-color: #7c9cff;
  background: rgba(124, 156, 255, .08);
  color: #c3d0ff;
}`,
    },
    {
      id: 'letter',
      title: '信纸与章节分隔',
      level: '进阶',
      desc: '米色信纸 + 楷体，适合书信、卷宗、回忆段落。',
      html: `<letter>
亲爱的旅人：<br><br>
当你读到这封信时，我已启程北行。炉火边的钥匙留在原处——若你愿意，替我照看那盆银铃草。<br><br>
—— 艾琳
</letter>`,
      css: `/* 可改：纸色 #f6efdc、边框色 #d9c9a3、字体栈 */
.markdown-body letter {
  display: block;
  margin: 10px 0;
  padding: 18px 22px;
  color: #2b2118;
  background: #f6efdc;
  border: 1px solid #d9c9a3;
  border-radius: 4px;
  box-shadow: 0 6px 18px rgba(0, 0, 0, .28);
  font-family: "Kaiti", "STKaiti", serif;
  line-height: 1.9;
}`,
    },
    {
      id: 'sms',
      title: '手机短信风',
      level: '高级',
      desc: '左右气泡 + 发送人标签，适合现代都市题材。',
      html: `<sms from="艾琳">今晚的雨很大，你带伞了吗？</sms>
<sms from="我" mine>带了，别担心。</sms>`,
      css: `/* 可改：气泡宽度、圆角、左右配色 */
.markdown-body sms {
  display: block;
  margin: 6px 0;
  max-width: 78%;
  padding: 8px 12px;
  border-radius: 14px;
  background: #2a2a3a;
  color: #e6e6f0;
}
.markdown-body sms::before {
  content: attr(from);
  display: block;
  margin-bottom: 2px;
  font-size: 11px;
  opacity: .6;
}
/* 右侧气泡：带 mine 属性 */
.markdown-body sms[mine] {
  margin-left: auto;
  background: #4c3b9e;
  border-bottom-right-radius: 4px;
}`,
    },
    {
      id: 'sprite',
      title: '立绘卡片',
      level: '进阶',
      desc: '固定宽高的背景图容器；示例用内联 data: 图，不联网。',
      html: `<sprite class="char-alice"></sprite>
<div style="font-size:12px;color:#9ca3af;margin-top:4px">↑ 换成你自己的图：url("https://…") 或 data: 内联图</div>`,
      css: `/* 可改：高度 220px、圆角、背景图 url() */
.markdown-body sprite.char-alice {
  display: block;
  width: 100%;
  height: 220px;
  border-radius: 10px;
  border: 1px solid rgba(255, 255, 255, .15);
  background: url("data:image/svg+xml;charset=utf-8,%3Csvg xmlns='http://www.w3.org/2000/svg' width='120' height='220'%3E%3Cdefs%3E%3ClinearGradient id='g' x1='0' y1='0' x2='1' y2='1'%3E%3Cstop offset='0' stop-color='%232a2a3a'/%3E%3Cstop offset='1' stop-color='%2359708f'/%3E%3C/linearGradient%3E%3C/defs%3E%3Crect width='120' height='220' fill='url(%23g)'/%3E%3Ccircle cx='60' cy='74' r='24' fill='%23d8d8e4'/%3E%3Crect x='36' y='98' width='48' height='106' rx='16' fill='%23d8d8e4'/%3E%3C/svg%3E") center / cover no-repeat;
}`,
    },
    {
      id: 'reveal',
      title: '点击揭示 · 悬浮密信',
      level: '高级',
      desc: '默认隐藏 + hover 显示，演示“隐藏必须配套显示规则”。',
      html: `<span class="observe-wrap-v2">
  <span class="secret-trigger-v2">📜 悬停查看密信</span>
  <span class="secret-popup-v2">城门已闭，夜风裹着远处钟声……</span>
</span>`,
      css: `/* 成对写：先隐藏，再给 :hover 显示，否则会被强制可见 */
.markdown-body .observe-wrap-v2 { position: relative; display: inline-block; }
.markdown-body .secret-trigger-v2 {
  padding: 2px 8px;
  border-bottom: 1px dashed #d4af37;
  color: #f0d98a;
  cursor: pointer;
}
.markdown-body .secret-popup-v2 {
  display: none;
  position: absolute;
  left: 0;
  top: 130%;
  z-index: 9;
  width: 240px;
  padding: 10px 12px;
  background: #241d2f;
  border: 1px solid rgba(212, 175, 55, .5);
  border-radius: 8px;
  color: #ffe9b0;
  font-size: 12px;
  box-shadow: 0 10px 24px rgba(0, 0, 0, .4);
}
.markdown-body .observe-wrap-v2:hover .secret-popup-v2 { display: block; }`,
    },
    {
      id: 'online',
      title: '呼吸灯 · 在线状态',
      level: '高级',
      desc: '纯 @keyframes 循环动画，只建议用在状态指示上。',
      html: `<online-pill><dot></dot>艾琳 · 在线</online-pill>`,
      css: `/* 可改：1.6s 节奏、两种绿色 */
.markdown-body online-pill {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  padding: 4px 12px;
  border-radius: 999px;
  background: rgba(52, 211, 153, .12);
  border: 1px solid rgba(52, 211, 153, .4);
  color: #a7f3d0;
  font-size: 12px;
  font-weight: 600;
}
.markdown-body online-pill dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: #34d399;
  animation: breathe 1.6s ease-in-out infinite;
}
@keyframes breathe {
  0%, 100% { opacity: .35; transform: scale(.85); }
  50% { opacity: 1; transform: scale(1.15); }
}
@media (prefers-reduced-motion: reduce) {
  .markdown-body online-pill dot { animation: none; opacity: 1; }
}`,
    },
    {
      id: 'accordion',
      title: '纯 CSS 手风琴',
      level: '入门',
      desc: 'checkbox + label + 相邻选择器，不写 JS 也能展开收起。',
      html: `<input type="checkbox" id="ch1" class="acc-toggle" hidden>
<label for="ch1" class="acc-head">▶ 展开第一章</label>
<div class="acc-body">第一章内容：雨夜、旧城门、一盏没熄的灯。</div>`,
      css: `/* 可改：标题底色、正文颜色 */
.markdown-body .acc-head {
  display: block;
  padding: 8px 12px;
  background: #2a2a3a;
  border-radius: 8px;
  color: #e6e6f0;
  font-weight: 600;
  cursor: pointer;
}
.markdown-body .acc-body { display: none; padding: 10px 12px; color: #c9c9d6; }
.markdown-body .acc-toggle:checked + .acc-head + .acc-body { display: block; }`,
    },
  ];

  /* ---------------------------------------------------------- 预览渲染器 */
  const BASE_CSS = `
html, body { margin: 0; }
body {
  padding: 14px 16px; background: #111119; color: #e7e7ef;
  font: 13px/1.7 system-ui, -apple-system, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
  word-break: break-word;
}
.markdown-body { max-width: 640px; margin: 0 auto; }
.markdown-body img { max-width: 100%; height: auto; border-radius: 6px; }
.markdown-body table { border-collapse: collapse; width: 100%; }
.markdown-body th, .markdown-body td { border: 1px solid #3a3a4a; padding: 5px 8px; }
.markdown-body p { margin: 8px 0; }
`;

  // 近似站点渲染：自定义标签加 tag-名字 / data-tag，p、span 内的标签按内联显示
  const NORMALIZER = [
    '<script>(function(){',
    'var STD={};("a b i em strong p span div section h1 h2 h3 h4 h5 h6 ul ol li details summary ' +
      'table thead tbody tr td th img pre code blockquote q label input button select textarea ' +
      'br hr mark small sub sup style").split(" ").forEach(function(t){STD[t]=1;});',
    'document.querySelectorAll(".markdown-body *").forEach(function(el){',
    '  var t=el.tagName.toLowerCase();',
    '  if(STD[t])return;',
    '  var n=t.replace(/_/g,"-");',
    '  el.classList.add("tag-"+n);',
    '  el.setAttribute("data-tag",n);',
    '  var p=el.parentElement ? el.parentElement.tagName.toLowerCase() : "";',
    '  if(p==="p"||p==="span"){el.style.display="inline";}else{el.style.display="block";}',
    '});',
    '})();<\/script>',
  ].join('\n');

  function buildDoc(html, css) {
    const safeHtml = String(html || '').replace(/<script[\s\S]*?<\/script>/gi, '');
    return '<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">' +
      '<style>' + BASE_CSS + '</style>' +
      '<style>' + String(css || '') + '</style>' +
      '</head><body><div class="markdown-body">' + safeHtml + '</div>' +
      NORMALIZER + '</body></html>';
  }

  /* ---------------------------------------------------------- 试验台 */
  const lab = { html: '', css: '', tpl: 'status', timer: 0, pane: 'html' };

  function paintChips() {
    const box = $('lab-chips');
    if (!box) return;
    box.innerHTML = TEMPLATES.map((t) =>
      '<button type="button" class="chip' + (t.id === lab.tpl ? ' on' : '') + '" data-tpl="' + esc(t.id) + '">' +
      esc(t.title) + '</button>').join('');
    Array.prototype.forEach.call(box.querySelectorAll('[data-tpl]'), (btn) => {
      btn.addEventListener('click', () => loadTemplate(btn.getAttribute('data-tpl')));
    });
  }

  function loadTemplate(id, scroll) {
    const t = TEMPLATES.find((x) => x.id === id) || TEMPLATES[0];
    lab.tpl = t.id;
    lab.html = t.html;
    lab.css = t.css;
    $('lab-html').value = t.html;
    $('lab-css').value = t.css;
    paintChips();
    renderPreview();
    if (scroll) {
      const sec = $('lab');
      if (sec) sec.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
  }

  function renderPreview() {
    const frame = $('lab-preview');
    if (!frame) return;
    frame.srcdoc = buildDoc($('lab-html').value, $('lab-css').value);
  }

  function schedulePreview() {
    clearTimeout(lab.timer);
    lab.timer = setTimeout(renderPreview, 320);
  }

  function switchPane(pane) {
    lab.pane = pane;
    Array.prototype.forEach.call(document.querySelectorAll('#lab-tabs button'), (b) => {
      b.classList.toggle('on', b.getAttribute('data-pane') === pane);
    });
    $('lab-html').hidden = pane !== 'html';
    $('lab-css').hidden = pane !== 'css';
  }

  function copyText(text, okMsg) {
    const done = () => U.toast(okMsg || '已复制');
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done).catch(() => fallbackCopy(text, done));
      return;
    }
    fallbackCopy(text, done);
  }

  function fallbackCopy(text, done) {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    try {
      document.execCommand('copy');
      done();
    } catch (e) {
      U.toast('复制失败，请手动选中复制', false);
    }
    document.body.removeChild(ta);
  }

  function bindLab() {
    Array.prototype.forEach.call(document.querySelectorAll('#lab-tabs button'), (b) => {
      b.addEventListener('click', () => switchPane(b.getAttribute('data-pane')));
    });
    $('lab-html').addEventListener('input', schedulePreview);
    $('lab-css').addEventListener('input', schedulePreview);
    $('lab-copy').addEventListener('click', () => copyText($('lab-html').value, 'HTML 已复制'));
    $('lab-copy-css').addEventListener('click', () => copyText($('lab-css').value, 'CSS 已复制'));
    $('lab-reset').addEventListener('click', () => loadTemplate(lab.tpl));
    Array.prototype.forEach.call(document.querySelectorAll('[data-load-tpl]'), (b) => {
      b.addEventListener('click', () => loadTemplate(b.getAttribute('data-load-tpl'), true));
    });
  }

  /* ---------------------------------------------------------- 模板库卡片 */
  function renderCards() {
    const box = $('tpl-grid');
    if (!box) return;
    box.innerHTML = TEMPLATES.map((t) =>
      '<div class="tpl-card" data-card="' + esc(t.id) + '">' +
      '<div class="row between"><h4>' + esc(t.title) + '</h4>' +
      '<span class="badge">' + esc(t.level) + '</span></div>' +
      '<div class="tiny muted">' + esc(t.desc) + '</div>' +
      '<div class="tpl-preview"><iframe sandbox="allow-scripts" title="' + esc(t.title) + ' 预览"></iframe></div>' +
      '<div class="row">' +
      '<button class="btn sm" data-copy-html="' + esc(t.id) + '">复制 HTML</button>' +
      '<button class="btn sm" data-copy-css="' + esc(t.id) + '">复制 CSS</button>' +
      '<button class="btn sm primary" data-load="' + esc(t.id) + '">载入试验台</button>' +
      '</div></div>').join('');

    Array.prototype.forEach.call(box.querySelectorAll('[data-card]'), (card) => {
      const t = TEMPLATES.find((x) => x.id === card.getAttribute('data-card'));
      const frame = card.querySelector('iframe');
      if (t && frame) frame.srcdoc = buildDoc(t.html, t.css);
    });
    Array.prototype.forEach.call(box.querySelectorAll('[data-copy-html]'), (b) => {
      b.addEventListener('click', () => {
        const t = TEMPLATES.find((x) => x.id === b.getAttribute('data-copy-html'));
        if (t) copyText(t.html, 'HTML 已复制');
      });
    });
    Array.prototype.forEach.call(box.querySelectorAll('[data-copy-css]'), (b) => {
      b.addEventListener('click', () => {
        const t = TEMPLATES.find((x) => x.id === b.getAttribute('data-copy-css'));
        if (t) copyText(t.css, 'CSS 已复制');
      });
    });
    Array.prototype.forEach.call(box.querySelectorAll('[data-load]'), (b) => {
      b.addEventListener('click', () => loadTemplate(b.getAttribute('data-load'), true));
    });
  }

  /* ---------------------------------------------------------- 目录高亮 */
  function bindToc() {
    const links = Array.prototype.slice.call(document.querySelectorAll('.guide-toc a'));
    const secs = links.map((a) => $(a.getAttribute('href').slice(1))).filter(Boolean);
    if (!secs.length) return;
    function sync() {
      let cur = secs[0].id;
      secs.forEach((s) => {
        if (s.getBoundingClientRect().top <= 120) cur = s.id;
      });
      links.forEach((a) => a.classList.toggle('on', a.getAttribute('href') === '#' + cur));
    }
    window.addEventListener('scroll', sync, { passive: true });
    sync();
  }

  /* ---------------------------------------------------------- 启动 */
  function init() {
    U.mountTopbar('creation');
    renderCards();
    bindLab();
    bindToc();
    loadTemplate('status');
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
