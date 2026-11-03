/* pages/chat_store.js — 流式状态机：片段缓冲 + RAF 合帧。

「解析 -> 预处理 -> 渲染」三段都收在同一帧里做：
模型片段先进 buffer，requestAnimationFrame 触发后才交给 parse，
再由 render 落到 DOM——一帧最多重绘一次，避免每个字都触发 innerHTML 重排。
页面本身不直接拿原始模型文本写 DOM。
*/
(function () {
  'use strict';
  const App = (window.App = window.App || {});

  class ChatStream {
    constructor(options) {
      const opts = options || {};
      this.buffer = '';            // 本轮收到的原始片段（未解析）
      this.isStreaming = false;
      this.latest = null;          // 最近一次解析结果（{ text, statusData, ... }）
      this.frame = 0;              // 待执行的 RAF 句柄，0 = 没有排帧
      this.parse = typeof opts.parse === 'function' ? opts.parse : null;
      this.render = typeof opts.render === 'function' ? opts.render : null;
      // 非流式模式：照常攒 buffer，但不排帧（结束后由完整渲染接管）
      this.shouldFlush = typeof opts.shouldFlush === 'function'
        ? opts.shouldFlush : function () { return true; };
    }

    raw() {
      return this.buffer;
    }

    /* 收到一段模型输出：只入缓冲，渲染交给 RAF 合帧 */
    appendChunk(text) {
      this.isStreaming = true;
      this.buffer += String(text == null ? '' : text);
      this.scheduleRender();
      return this.buffer;
    }

    /* 一帧最多渲染一次：同一帧里收到的所有片段合并成一次解析 + 一次重绘 */
    scheduleRender() {
      if (this.frame || !this.render || !this.shouldFlush()) return;
      this.frame = requestAnimationFrame(() => {
        this.frame = 0;
        this.flush();
      });
    }

    /* 立刻按当前 buffer 走一遍「解析 -> 预处理 -> 渲染」 */
    flush() {
      if (!this.render || !this.shouldFlush()) return this.latest;
      const raw = this.buffer;
      this.latest = this.parse ? this.parse(raw) : { text: raw };
      this.render(this.latest, raw);
      return this.latest;
    }

    /* 丢弃未落地的帧和缓冲（清屏 / 收尾完整重绘前调用），不触发渲染 */
    reset() {
      if (this.frame) {
        cancelAnimationFrame(this.frame);
        this.frame = 0;
      }
      this.buffer = '';
      this.latest = null;
      this.isStreaming = false;
    }

    /* 流结束：把最后一段补上，之后不再接收片段 */
    finish() {
      this.isStreaming = false;
      return this.flush();
    }
  }

  App.ChatStream = ChatStream;
})();
