/* pages/parser.js — 流式解析器。

模型吐出的是一段「半包」文本：标签可能只收到一半（"<sa"），状态栏可能还没
闭合。这里把原始缓冲整理成安全结构，页面只拿占位符渲染，永远不把模型原始
标签直接写进 DOM。

parseStream(rawBuffer) -> { safeText, statusData }
  1. 末尾只有 < 没有 > → 截断到 < 之前
  2. <image>…</image>（含未闭合的开标签）→ 【IMAGE】占位符
  3. <say> 动作块 → 【SAY】/【/SAY】占位符，未闭合的自动补齐
  4. <book_status> 提取数值后从正文里剔除
*/
(function () {
  'use strict';
  const App = (window.App = window.App || {});

  // 占位符：前端渲染层只认这三个记号，模型原文里的标签一律不落地
  const IMAGE_TOKEN = '【IMAGE】';
  const SAY_TOKEN = '【SAY】';
  const SAY_END_TOKEN = '【/SAY】';

  const BOOK_STATUS_BLOCK_RE = /<book_status[^>]*>([\s\S]*?)<\/book_status>/gi;

  /* 状态栏数据：属性名不写死（好感度 / SAN 只是常见两种），按 items 原样给出。
     favor / san 只是给需要单独展示的页面留的便捷字段。 */
  function extractStatusData(text) {
    const m = /<book_status[^>]*>([\s\S]*?)<\/book_status>/i.exec(String(text || ''));
    if (!m) return null;
    const items = [];
    const re = /<item[^>]*>([\s\S]*?)<\/item>/gi;
    let it;
    while ((it = re.exec(m[1])) !== null) {
      const plain = it[1].replace(/<[^>]*>/g, '').replace(/\s+/g, ' ').trim();
      if (!plain) continue;
      const num = /-?\d+/.exec(plain);
      items.push({
        label: plain.replace(/[：:]\s*-?\d+\s*$/, '').trim(),
        value: num ? Number(num[0]) : null,
      });
    }
    if (!items.length) return null;
    const find = (re2) => {
      const hit = items.find((x) => re2.test(x.label));
      return hit && hit.value !== null ? hit.value : null;
    };
    return {
      items,
      favor: find(/好感|亲密度|信任|favor/i),
      san: find(/san/i),
    };
  }

  /* 末尾的 < 还没等到 >（如 "<boo"）→ 整段截掉，避免闪出裸标签 */
  function clampDanglingTag(text) {
    const lt = text.lastIndexOf('<');
    if (lt !== -1 && text.indexOf('>', lt) === -1) return text.slice(0, lt);
    return text;
  }

  /* 只截掉「最后一个还没闭合」的块，前面已闭合的块保持显示 */
  function cutOpenBlock(text, name) {
    const low = text.toLowerCase();
    const i = low.lastIndexOf('<' + name);
    if (i !== -1 && !new RegExp('</' + name + '>', 'i').test(text.slice(i))) {
      return text.slice(0, i);
    }
    return text;
  }

  function parseStream(rawBuffer) {
    let text = clampDanglingTag(String(rawBuffer || ''));
    text = cutOpenBlock(text, 'book_status');
    text = cutOpenBlock(text, 'next_prompt');

    // 状态栏：先取数值，再把整块从正文里剔除（前端用结构数据重建原生标签）
    const statusData = extractStatusData(text);
    text = text.replace(BOOK_STATUS_BLOCK_RE, '');

    // 图片：闭合的直接换占位符；还没收到 </image> 的，从最后一个开标签起整段换掉
    text = text.replace(/<image[^>]*>[\s\S]*?<\/image>/gi, IMAGE_TOKEN);
    const imgAt = text.toLowerCase().lastIndexOf('<image');
    if (imgAt !== -1) text = text.slice(0, imgAt) + IMAGE_TOKEN;

    // 动作块：先补齐未闭合的 <say>，再把标签换成占位符
    const open = (text.match(/<say(?=[\s>])/gi) || []).length;
    const close = (text.match(/<\/say>/gi) || []).length;
    if (open > close) {
      // 补在选项块之前，别把选项也框进动作块里
      const np = text.toLowerCase().indexOf('<next_prompt');
      const at = np === -1 ? text.length : np;
      text = text.slice(0, at) + SAY_END_TOKEN.repeat(open - close) + text.slice(at);
    }
    text = text.replace(/<say(?=[\s>])[^>]*>/gi, SAY_TOKEN)
      .replace(/<\/say>/gi, SAY_END_TOKEN);

    return { safeText: text, statusData };
  }

  App.parseStream = parseStream;
  App.extractStatusData = extractStatusData;
  App.streamTokens = {
    IMAGE: IMAGE_TOKEN,
    SAY: SAY_TOKEN,
    SAY_END: SAY_END_TOKEN,
  };
})();
