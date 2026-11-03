/* pages/theme_manager.js — 角色卡 customCSS 的作用域隔离。

作者的样式只允许作用在「当前聊天室」里，绝不能污染全站：
- 所有选择器加 `.chat-room-${charId}` 前缀，style 标签统一插到 head
- :root / html / body 映射到聊天室容器（作者常在这里定义 CSS 变量）
- @media / @supports / @container / @layer / @scope 递归处理内部选择器
- @keyframes / @font-face 原样保留（里面是 0% / from 这类关键帧选择器）
- @import 丢弃（外部样式无法作用域化）
*/
(function () {
  'use strict';
  const App = (window.App = window.App || {});

  const STYLE_ID = 'chat-custom-css';
  const ROOM_ID = 'chat-scroll';
  const ROOM_PREFIX = 'chat-room-';

  /* 角色 ID 只保留安全字符，避免拼出非法选择器 */
  function roomClass(charId) {
    const id = String(charId || '').replace(/[^\w-]/g, '');
    return ROOM_PREFIX + (id || 'x');
  }

  function scopeCustomCss(css, scope) {
    const clean = String(css || '')
      .replace(/\/\*[\s\S]*?\*\//g, '')
      // 无块语句的 @ 规则（无法作用域化）直接丢弃
      .replace(/@(import|charset|namespace)[^;]*;/gi, '');
    const prefixSelector = (sel) => sel.split(',').map((raw) => {
      let s = raw.trim();
      if (!s) return '';
      if (/^(:root|html|body)$/i.test(s)) return scope;
      s = s.replace(/^(?:(?::root|html|body)\s+)+/i, '');
      return scope + ' ' + s;
    }).filter(Boolean).join(', ');

    const walk = (block) => {
      let out = '';
      let i = 0;
      while (i < block.length) {
        const open = block.indexOf('{', i);
        if (open < 0) { out += block.slice(i); break; }
        const prelude = block.slice(i, open).trim();
        // 配对右括号：跳过字符串里的花括号
        let depth = 1;
        let j = open + 1;
        let quote = '';
        while (j < block.length && depth > 0) {
          const ch = block[j];
          if (quote) {
            if (ch === '\\') { j += 2; continue; }
            if (ch === quote) quote = '';
          } else if (ch === '"' || ch === "'") quote = ch;
          else if (ch === '{') depth += 1;
          else if (ch === '}') depth -= 1;
          j += 1;
        }
        const inner = block.slice(open + 1, j - 1);
        if (/^@(media|supports|container|layer|scope)\b/i.test(prelude)) {
          out += prelude + '{' + walk(inner) + '}';
        } else if (prelude.charAt(0) === '@') {
          if (!/^@import\b/i.test(prelude)) out += prelude + '{' + inner + '}';
        } else if (prelude) {
          out += prefixSelector(prelude) + '{' + inner + '}';
        }
        i = j;
      }
      return out;
    };
    return walk(clean);
  }

  function roomEl() {
    return document.getElementById(ROOM_ID);
  }

  /* 注入（或替换）当前角色的样式；cssText 为空则清掉并摘除作用域 class。 */
  function applyCharacterCSS(cssText, charId) {
    const room = roomEl();
    const cls = roomClass(charId);
    const text = String(cssText || '');

    if (room) {
      // 同时只保留当前角色这一个作用域 class
      Array.from(room.classList)
        .filter((c) => c.indexOf(ROOM_PREFIX) === 0)
        .forEach((c) => room.classList.remove(c));
    }

    const el = document.getElementById(STYLE_ID);
    if (!text.trim()) {
      if (el && el.parentNode) el.parentNode.removeChild(el);
      return '';
    }
    const scoped = scopeCustomCss(text, '.' + cls);
    const style = el || document.createElement('style');
    style.id = STYLE_ID;
    if (!el) document.head.appendChild(style);
    style.textContent = scoped;
    if (room) room.classList.add(cls);
    return scoped;
  }

  function clearCharacterCSS() {
    const el = document.getElementById(STYLE_ID);
    if (el && el.parentNode) el.parentNode.removeChild(el);
    const room = roomEl();
    if (room) {
      Array.from(room.classList)
        .filter((c) => c.indexOf(ROOM_PREFIX) === 0)
        .forEach((c) => room.classList.remove(c));
    }
  }

  App.theme = { applyCharacterCSS, clearCharacterCSS, scopeCustomCss };
})();
