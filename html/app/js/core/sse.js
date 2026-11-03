/* core/sse.js — 流式对话解析（容错：非 JSON 帧跳过、[DONE]、clear_content、断流保留已收内容） */
(function () {
  'use strict';
  const App = (window.App = window.App || {});

  /**
   * streamChat(path, payload, handlers)
   * handlers: { onContent(text), onClear(), onStatus(data), onImage(data), onDone(data), onUsage(data), onCg(data), onError(err), onClose(reason) }
   * 事件分流：message→onContent / reset→onClear / status_update→onStatus /
   * image_trigger→onImage / usage→onUsage / done→onDone / error→onError
   * onClose 必定恰好触发一次，reason ∈ done(正常结束) | stopped(用户停止) | interrupted(断流) | error(请求失败)
   * 返回 { abort() }
   */
  function streamChat(path, payload, handlers) {
    const h = handlers || {};
    const ctrl = new AbortController();
    let stopped = false;
    let finished = false;

    const emit = (name, arg) => {
      try { if (typeof h[name] === 'function') h[name](arg); }
      catch (e) { if (App.guard) App.guard.report(e && e.message); }
    };

    // 收尾：onClose 只发一次（多条 return 路径都会走到这里），调用方靠它复位「生成中」状态
    let closed = false;
    const close = (reason) => {
      if (closed) return;
      closed = true;
      emit('onClose', reason);
    };

    (async () => {
      let res;
      try {
        res = await fetch(path, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
          body: JSON.stringify(payload || {}),
          credentials: 'same-origin',
          cache: 'no-store',
          signal: ctrl.signal,
        });
      } catch (err) {
        emit('onError', new Error(err && err.name === 'AbortError' ? '已停止生成' : '连接失败，请检查本地服务'));
        close(err && err.name === 'AbortError' ? 'stopped' : 'error');
        return;
      }

      if (!res.ok) {
        let msg = '对话请求失败（HTTP ' + res.status + '）';
        try {
          const txt = await res.text();
          const obj = txt ? JSON.parse(txt) : null;
          if (obj && obj.msg) msg = obj.msg;
        } catch (e) { /* 保持默认提示 */ }
        emit('onError', new Error(msg));
        close('error');
        return;
      }

      const ctype = res.headers.get('content-type') || '';
      // 非流式回包（个别情况下服务端直接回 JSON）→ 当作一次性内容
      if (ctype.indexOf('event-stream') < 0) {
        try {
          const text = await res.text();
          let obj = null;
          try { obj = text ? JSON.parse(text) : null; } catch (e) { obj = null; }
          const data = obj && obj.data !== undefined ? obj.data : obj;
          const content = (data && (data.message || data.content)) || '';
          if (content) emit('onContent', content);
          emit('onDone', data || {});
          close('done');
        } catch (err) {
          emit('onError', err);
          close('error');
        }
        return;
      }

      const reader = res.body.getReader();
      const decoder = new TextDecoder('utf-8');
      let buffer = '';

      const handleBlock = (block) => {
        let name = '';
        const data = [];
        block.split('\n').forEach((line) => {
          if (!line || line.charAt(0) === ':') return;
          if (line.indexOf('event:') === 0) { name = line.slice(6).trim(); return; }
          if (line.indexOf('data:') === 0) data.push(line.slice(5).trim());
        });
        const raw = data.join('\n');
        if (!raw) return;
        if (raw === '[DONE]') { finished = true; return; }
        let frame;
        try { frame = JSON.parse(raw); } catch (e) { return; }
        if (!frame || typeof frame !== 'object') return;
        // 优先按 SSE 事件名分流；没有 event 行的旧回包退回 frame.type
        const type = name || frame.type || '';
        if (type === 'message' || type === 'content' || type === 'delta') {
          const text = typeof frame.text === 'string' ? frame.text : frame.content;
          if (typeof text === 'string' && text) emit('onContent', text);
        } else if (type === 'reset' || type === 'clear_content') {
          emit('onClear');
        } else if (type === 'status_update') {
          emit('onStatus', frame);
        } else if (type === 'image_trigger') {
          emit('onImage', frame);
        } else if (type === 'usage') {
          emit('onUsage', frame);
        } else if (type === 'error') {
          emit('onError', new Error(frame.message || '生成失败'));
        } else if (type === 'cg') {
          emit('onCg', frame);
        } else if (type === 'done') {
          emit('onDone', frame);
          finished = true;
        }
      };

      try {
        while (!finished) {
          const step = await reader.read();
          if (step.done) break;
          buffer += decoder.decode(step.value, { stream: true });
          buffer = buffer.replace(/\r\n/g, '\n');
          let idx = buffer.indexOf('\n\n');
          while (idx >= 0) {
            handleBlock(buffer.slice(0, idx));
            buffer = buffer.slice(idx + 2);
            idx = buffer.indexOf('\n\n');
          }
        }
        if (!finished && buffer.trim()) handleBlock(buffer);
      } catch (err) {
        // 断流：不当作失败，已收内容保留；由调用方收尾
        if (!stopped) emit('onError', new Error('连接中断，已保留已生成内容'));
      } finally {
        try { reader.releaseLock(); } catch (e) { /* noop */ }
        close(stopped ? 'stopped' : (finished ? 'done' : 'interrupted'));
      }
    })();

    return {
      abort() {
        stopped = true;
        try { ctrl.abort(); } catch (e) { /* noop */ }
      },
    };
  }

  App.sse = { streamChat };
})();
