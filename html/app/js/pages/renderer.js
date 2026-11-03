/* pages/renderer.js — 安全正文渲染器。

只做一件事：把解析器（parser.js）给出的安全结构变成 DOM 字符串。
这里不解析模型原文、不发请求：输入已经是占位符/白名单标签，
输出是可直接写进 innerHTML 的受控 HTML。
*/
(function () {
  'use strict';
  const App = (window.App = window.App || {});
  const esc = (v) => (App.ui && App.ui.esc ? App.ui.esc(v) : String(v == null ? '' : v));
  const ph = (txt, seed) => (App.ui && App.ui.placeholder
    ? App.ui.placeholder(txt, seed)
    : esc(String(txt || '?').slice(0, 1)));

  const CG_LOAD_TOKEN = '\u0000CGLOAD\u0000';

  /* 放行 say / book_status / item 等角色卡自定义标签，其余一律剔除 */
  const RICH_TAGS = /^(b|strong|i|em|u|s|mark|br|p|div|span|ul|ol|li|blockquote|code|pre|a|img|h1|h2|h3|h4|h5|h6|hr|table|thead|tbody|tr|th|td|small|sub|sup|say|book_status|item|center)$/i;

  /* 模型常把整段（含台词）塞进 <say>，或只开不闭合：括号内的动作留在
     <say>，括号外的台词/旁白移出，保证气泡不会变成一整块粉色动作框。 */
  function normalizeSayMarkup(src) {
    // 模型偷懒不写 <say>、整段直接用「（动作）」时，按段落补标签（与后端一致）
    const normalized = String(src || '').split(/(\n{2,})/).map((p) => {
      const t = p.trim();
      return (t && /^(?:（[^（）]{2,}）[，。、；：！？…,;:.!?\s]*)+$/.test(t))
        ? '<say>' + t + '</say>' : p;
    }).join('');
    if (!/<say>/i.test(normalized)) return normalized;
    return normalized.replace(/<say>([\s\S]*?)(?:<\/say>|(?=<say>)|$)/gi, function (m, inner) {
      const actions = inner.match(/（[^（）]*）/g) || [];
      const rest = inner.replace(/（[^（）]*）/g, '').trim();
      const parts = [];
      if (actions.length) parts.push('<say>' + actions.join('\n') + '</say>');
      if (rest) parts.push(rest);
      return parts.join('\n');
    });
  }

  /* CG 占位：骨架屏垫在图片下面，图片加载完自然盖住；没加载完也能先占位 */
  function cgSkeleton() {
    return '<span class="cg-skeleton" aria-hidden="true"></span>';
  }
  function cgFigure(url) {
    return '<figure class="cg-inline sys-image-wrapper">' + cgSkeleton() +
      '<img src="' + esc(url) + '" class="sys-image" alt="CG" loading="lazy"' +
      ' onerror="this.onerror=null;this.src=\'' + ph('CG', 3) + '\'">' +
      '</figure>';
  }
  function cgLoading() {
    return '<figure class="cg-inline cg-loading sys-image-wrapper sys-image-loading">' +
      cgSkeleton() + '<div class="spinner"></div><span>展开绘卷中...</span>' +
      '</figure>';
  }

  /* 状态栏：结构化数据 → 原生 <book_status>/<item>，作者 customCSS 仍可命中；
     优先遍历 values 字典（属性名任意），items 仅作旧结构兜底。 */
  function pillClass(name) {
    const n = String(name || '');
    if (/san|灵力|法力|查克拉|精神|理智|体力|生命|hp|mp/i.test(n)) return 'tag-san';
    if (/好感|亲密|信任|爱意|心动|favor/i.test(n)) return 'tag-favor';
    return 'tag-neutral';
  }

  function buildStatusBar(statusData) {
    const values = statusData && statusData.values;
    if (values && typeof values === 'object' && Object.keys(values).length) {
      return '<book_status>' + Object.entries(values).map(([name, value]) =>
        '<item class="' + pillClass(name) + '">' + esc(String(name)) + '：<b>'
        + esc(String(value)) + '</b></item>').join('') + '</book_status>';
    }
    const items = (statusData && statusData.items) || [];
    if (!items.length) return '';
    return '<book_status>' + items.map((it) => {
      const name = String((it && it.label) || '');
      const value = esc(String((it && it.value == null) ? '' : it.value));
      return '<item class="' + pillClass(name) + '">'
        + (name ? esc(name) + '：' : '') + '<b>' + value + '</b></item>';
    }).join('') + '</book_status>';
  }

  function renderRich(src, isStreaming) {
    let s = String(src || '');
    // 解析器只给占位符：这里还原成受控标签/骨架屏
    const TK = App.streamTokens
      || { IMAGE: '【IMAGE】', SAY: '【SAY】', SAY_END: '【/SAY】' };
    s = s.split(TK.SAY_END).join('</say>')
      .split(TK.SAY).join('<say>')
      .split(TK.IMAGE).join(isStreaming ? CG_LOAD_TOKEN : '');
    s = normalizeSayMarkup(s);
    const cgUrls = [];
    // CG：闭合的 <image> 先换成占位符（URL 留给消毒后注入，避免 class 被洗掉）
    s = s.replace(/<image>([\s\S]*?)<\/image>/gi, (m, u) => {
      u = String(u || '').trim();
      if (!u || !/^(https?:\/\/|\/)/i.test(u)) {
        // 流式期间模型给的是 ID，地址还没替换 → 先显示骨架屏
        return isStreaming ? CG_LOAD_TOKEN : '';
      }
      cgUrls.push(u);
      return '\u0000CGIMG' + (cgUrls.length - 1) + '\u0000';
    });
    if (s.indexOf('<') < 0 && s.indexOf('\u0000CG') < 0) return esc(s);

    // 修复 <book_status>：LLM 常输出未闭合 <item> 和重复开标签，
    // 这里按 <item> 切分后重建平级 item
    s = s.replace(/<book_status[^>]*>([\s\S]*?)(?:<\/book_status[^>]*>|(?=<\/?book_status[^>]*>|$))/gi, (m, inner) => {
      let body = inner.replace(/<\/?book_status[^>]*>/gi, '');
      const parts = body.split(/<item[^>]*>/i)
        .map((t) => t.replace(/<\/item>/gi, '').trim())
        .filter(Boolean)
        .filter((t) => /[\u4e00-\u9fa5a-zA-Z0-9]/.test(t));
      if (!parts.length) return '<book_status></book_status>';
      const items = parts.map((p) => {
        // 修 <b> 未闭合：<b>文字<b> → <b>文字</b>；块尾未闭合也补上
        let fixed = p.replace(/<b>([^<]*?)<b>/gi, '<b>$1</b>');
        fixed = fixed.replace(/<b>([^<]*?)$/i, '<b>$1</b>');
        return '<item>' + fixed + '</item>';
      }).join('');
      return '<book_status>' + items + '</book_status>';
    });

    s = s.replace(/<\/?([a-zA-Z][\w-]*)([^>]*)>/g, (m, tag, attrs) => {
      if (!RICH_TAGS.test(tag)) return '';
      const lower = tag.toLowerCase();
      if (m.startsWith('</')) return '</' + lower + '>';
      let out = '<' + lower;
      const href = /href\s*=\s*"([^"]*)"/i.exec(attrs);
      if (href && /^(https?:|mailto:|tel:)/i.test(href[1])) out += ' href="' + esc(href[1]) + '"';
      const src2 = /src\s*=\s*"([^"]*)"/i.exec(attrs);
      if (src2 && !/^\s*javascript:/i.test(src2[1])) out += ' src="' + esc(src2[1]) + '"';
      out += '>';
      return out;
    });
    // 消毒完再注入图片 DOM，保证 class / 结构不被白名单洗掉
    s = s.replace(/\u0000CGIMG(\d+)\u0000/g, (m, i) => cgFigure(cgUrls[Number(i)]));
    s = s.replace(/\u0000CGLOAD\u0000/g, cgLoading());
    return s;
  }

  /* 渲染入口：安全正文 +（可选）结构化状态栏 */
  function render(safeText, statusData, streaming) {
    return renderRich(safeText, !!streaming)
      + (streaming ? '' : buildStatusBar(statusData));
  }

  App.renderer = {
    render,
    renderRich,
    buildStatusBar,
    normalizeSayMarkup,
  };
})();
