/* 本地控制台：数据库控制台 + 原有本地数据视图 */
(function () {
  const $ = (s, root = document) => root.querySelector(s);
  const $$ = (s, root = document) => Array.from(root.querySelectorAll(s));

  let filter = '';
  let llmEditId = null;     // 正在编辑的接入（-1 表示新增）
  let llmMsg = null;        // 操作提示 {ok, text}
  let imgEditId = null;     // 正在编辑的绘图接入
  let imgMsg = null;        // 绘图接入操作提示
  let vidEditId = null;     // 正在编辑的视频接入
  let vidMsg = null;        // 视频接入操作提示
  let tplMsg = null;        // 模板接入操作提示 {ok, text}
  let tplReport = null;     // 上次导入/预览的适配报告
  let tplDraft = '';        // 模板 JSON 草稿（重渲染不丢）
  let creativeQ = '';       // 创意广场搜索词
  let creativeOpen = false; // 只看开源
  let creativePick = 0;     // 正在看源码的商品 id
  let creativeMsg = null;   // 创意广场操作提示
  let pointsQ = '';         // 积分管理搜索词
  let consRange = 'daily';  // 消耗管理：daily / weekly / monthly
  // 创作数据（作品 + 世界书）
  let cdQ = '';
  let cdStatus = '';
  let cdUser = 0;
  let cdPage = 1;
  let cdPageSize = 20;
  let cdMode = 'merge';
  let cdTarget = 0;
  let cdDraft = '';
  let cdPreview = null;
  let cdMsg = null;

  const dbState = {
    overview: null,
    tables: [],
    active: '',
    data: null,
    page: 0,
    pageSize: 50,
    filter: '',
    logs: [],
  };
  let dbToastTimer = null;

  const esc = (s) => String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');

  const j = async (url, opt) => {
    const r = await fetch(url, opt);
    if (!r.ok) {
      let msg = url + ' -> ' + r.status;
      try {
        const d = await r.json();
        if (d && d.msg) msg = d.msg;
      } catch (err) { /* ignore */ }
      throw new Error(msg);
    }
    return r.json();
  };

  const dbPost = async (url, body) => {
    const r = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {}),
    });
    let d = null;
    try { d = await r.json(); } catch (err) { /* ignore */ }
    if (!r.ok || !d || d.code !== 0) {
      throw new Error((d && d.msg) || ('请求失败：' + r.status));
    }
    return d;
  };

  // 原样返回回包（模板导入要看到 code!=0 时的说明与报告）
  const postJson = async (url, body) => {
    const r = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {}),
    });
    let d = null;
    try { d = await r.json(); } catch (err) { /* ignore */ }
    if (!d) throw new Error('返回不是 JSON（HTTP ' + r.status + '）');
    return d;
  };

  const statusBadge = (s) => {
    const n = Number(s);
    const cls = n >= 500 ? 'b5' : n >= 400 ? 'b4' : n ? 'b2' : 'b0';
    return `<span class="badge ${cls}">${n || '-'}</span>`;
  };

  const table = (cols, rows, render) => {
    if (!rows || !rows.length) return '<div class="empty">暂无数据</div>';
    return `<table><thead><tr>${cols.map((c) => `<th>${c}</th>`).join('')}</tr></thead>
      <tbody>${rows.map(render).join('')}</tbody></table>`;
  };

  const fmtNum = (n) => {
    const v = Number(n || 0);
    return v.toLocaleString('zh-CN');
  };

  const fmtTime = (s) => {
    if (!s) return '—';
    return String(s).replace('T', ' ')
      .replace(/\.\d+.*$/, '').replace(/([+-]\d\d:?\d\d|Z)$/, '');
  };

  const opClass = (op) => {
    const k = String(op || '').toLowerCase();
    if (k.includes('delete') || k.includes('drop') || k.includes('clear')) return 'op-danger';
    if (k.includes('create') || k.includes('recreate') || k.includes('connect')
      || k.includes('scan')) return 'op-warn';
    if (k.includes('save')) return 'op-ok';
    return 'op-info';
  };

  const prettyJson = (s) => {
    if (!s) return '';
    try { return JSON.stringify(JSON.parse(s), null, 2); } catch (err) { return String(s); }
  };

  const cellHtml = (v) => {
    if (v === null || v === undefined) return '<span class="null">NULL</span>';
    const s = String(v);
    return `<span class="cell-clip" title="${esc(s)}">${esc(s)}</span>`;
  };

  const dbTableUrl = () => {
    const qs = new URLSearchParams({
      name: dbState.active,
      limit: String(dbState.pageSize),
      offset: String(dbState.page * dbState.pageSize),
    });
    return '/local/api/db/table?' + qs.toString();
  };

  function dbToast(text, ok = true) {
    const el = $('#db-toast');
    if (!el) return;
    el.textContent = text;
    el.className = 'toast' + (ok ? '' : ' err');
    el.hidden = false;
    clearTimeout(dbToastTimer);
    dbToastTimer = setTimeout(() => { el.hidden = true; }, 4200);
  }

  function dbModalOpen(title, body, foot) {
    const back = $('#db-modal');
    if (!back) return;
    $('#db-modal-title').textContent = title;
    $('#db-modal-body').innerHTML = body;
    $('#db-modal-foot').innerHTML = foot;
    back.hidden = false;
    document.body.classList.add('modal-open');
    $$('[data-modal-cancel]', back).forEach((b) => { b.onclick = dbModalClose; });
    $('#db-modal-close').onclick = dbModalClose;
    back.onclick = (e) => { if (e.target === back) dbModalClose(); };
  }

  function dbModalClose() {
    const back = $('#db-modal');
    if (!back) return;
    back.hidden = true;
    document.body.classList.remove('modal-open');
    $('#db-modal-body').innerHTML = '';
    $('#db-modal-foot').innerHTML = '';
  }

  function renderTableList() {
    const f = dbState.filter.trim().toLowerCase();
    const items = dbState.tables.filter(
      (t) => !f || String(t.name).toLowerCase().includes(f));
    if (!items.length) return '<div class="empty small">没有匹配的数据表</div>';
    return items.map((t) => `
      <button type="button" class="table-item ${t.name === dbState.active ? 'on' : ''}"
        data-table="${esc(t.name)}">
        <span class="ti-name">${esc(t.name)}</span>
        <span class="ti-meta">${fmtNum(t.rows)} 行 · ${t.columns} 列</span>
      </button>`).join('');
  }

  function renderTablePanel() {
    if (!dbState.active) {
      return '<div class="empty">还没有数据表。点击「新建数据表」开始。</div>';
    }
    const d = dbState.data;
    if (!d || d.table !== dbState.active) {
      return '<div class="empty">读取失败，请刷新重试。</div>';
    }
    const cols = d.columns || [];
    const rows = d.rows || [];
    const total = Number(d.total || 0);
    const offset = Number(d.offset || 0);
    const page = dbState.page;
    const pages = Math.max(1, Math.ceil(total / dbState.pageSize));
    const from = total ? offset + 1 : 0;
    const to = offset + rows.length;
    const head = cols.map((c) => `<th>
      <span class="col-name">${esc(c.name)}</span>${c.pk ? '<span class="pk">PK</span>' : ''}
      <span class="col-type">${esc(c.type || '')}</span></th>`).join('');
    const body = rows.length
      ? rows.map((r, i) => `<tr>
          ${cols.map((c) => `<td>${cellHtml(r[c.name])}</td>`).join('')}
          <td class="ops">
            <button type="button" data-row-edit="${i}">编辑</button>
            <button type="button" class="danger-text" data-row-del="${i}">删除</button>
          </td></tr>`).join('')
      : `<tr><td colspan="${cols.length + 1}"><div class="empty">这张表还没有记录</div></td></tr>`;
    return `
      <div class="table-panel-head">
        <div>
          <h3>${esc(d.table)}</h3>
          <div class="meta">${fmtNum(total)} 行 · ${cols.length} 列</div>
        </div>
        <div class="table-panel-actions">
          <button type="button" class="btn primary small" data-db-act="add-row">+ 新增记录</button>
          <button type="button" class="btn danger small" data-db-act="clear-table">清空数据</button>
        </div>
      </div>
      <div class="table-scroll">
        <table class="data-table" style="min-width:${Math.max(720, cols.length * 108)}px">
          <thead><tr>${head}<th class="ops">操作</th></tr></thead>
          <tbody>${body}</tbody>
        </table>
      </div>
      <div class="table-foot">
        <span>显示 ${fmtNum(from)} - ${fmtNum(to)} / ${fmtNum(total)} 条</span>
        <div class="pager">
          <select id="db-page-size">
            ${[20, 50, 100, 200].map((n) =>
              `<option value="${n}"${n === dbState.pageSize ? ' selected' : ''}>每页 ${n} 条</option>`).join('')}
          </select>
          <button type="button" data-page="prev"${page <= 0 ? ' disabled' : ''}>上一页</button>
          <span>第 ${page + 1} / ${pages} 页</span>
          <button type="button" data-page="next"${to >= total ? ' disabled' : ''}>下一页</button>
        </div>
      </div>`;
  }

  function renderTerminal() {
    const logs = dbState.logs || [];
    if (!logs.length) {
      return '<div class="empty">暂无操作日志。刷新、查询或修改数据后，这里会显示 Python 后端的操作记录。</div>';
    }
    return logs.map((l) => {
      const detail = prettyJson(l.detail);
      return `<div class="term-entry">
        <div class="term-row">
          <span class="op ${opClass(l.op)}">${esc(l.op)}</span>
          <span class="term-table">${esc(l.table_name || '—')}</span>
          <span class="term-time">${esc(fmtTime(l.created_at))}</span>
        </div>
        ${detail ? `<pre class="term-json">${esc(detail)}</pre>` : ''}
      </div>`;
    }).join('');
  }

  function renderOverview(ov) {
    return `
      <div class="status-grid">
        <div class="conn-card">
          <div class="conn-head">
            <span class="status-dot"></span>
            <div>
              <div class="conn-title">应用数据连接正常</div>
              <div class="conn-sub">Application data connection</div>
            </div>
            <span class="chip">sqlite</span>
          </div>
          <div class="conn-grid">
            <div><span class="k">当前数据库</span><b class="mono">${esc(ov.database)}</b></div>
            <div><span class="k">主机 / 端口</span><b class="mono">${esc(ov.host)} / ${esc(ov.port)}</b></div>
            <div><span class="k">连接池</span><b class="mono">${esc(ov.pool)}</b></div>
            <div><span class="k">数据库大小</span><b class="mono">${esc(ov.size_human)}</b></div>
          </div>
          <div class="conn-path">
            <span class="k">完整路径</span>
            <code>${esc(ov.path)}</code>
          </div>
        </div>
        <div class="metric-card m1">
          <div class="k">数据表</div>
          <div class="v">${fmtNum(ov.tables)}</div>
          <div class="sub">TABLES</div>
        </div>
        <div class="metric-card m2">
          <div class="k">数据库记录</div>
          <div class="v">${fmtNum(ov.rows)}</div>
          <div class="sub">RECORDS</div>
        </div>
        <div class="metric-card m3">
          <div class="k">状态栏字段</div>
          <div class="v">${fmtNum(ov.columns)}</div>
          <div class="sub">COLUMNS</div>
        </div>
      </div>`;
  }

  function collectRowValues(cols) {
    const map = {};
    $$('#db-modal-body [data-col]').forEach((el) => { map[el.dataset.col] = el; });
    const out = {};
    cols.forEach((c) => {
      const el = map[c.name];
      if (!el || el.disabled) return;
      let v = el.value;
      const type = String(c.type || '');
      const isText = /TEXT|CHAR|CLOB|JSON/i.test(type);
      if (v === '') {
        if (isText) out[c.name] = '';
        return;
      }
      if (/INT|REAL|NUM|FLOAT|DOUBLE|DECIMAL/i.test(type)) {
        const n = Number(v);
        if (Number.isFinite(n)) v = n;
      }
      out[c.name] = v;
    });
    return out;
  }

  function openRowEditor(row) {
    const d = dbState.data;
    if (!d) return;
    const cols = d.columns || [];
    const editing = !!row;
    const pk = {};
    if (editing) {
      cols.filter((c) => c.pk).forEach((c) => { pk[c.name] = row[c.name]; });
      if (!Object.keys(pk).length && row.__rowid__ !== undefined) {
        pk.__rowid__ = row.__rowid__;
      }
    }
    const fields = cols.map((c) => {
      const v = editing ? row[c.name] : null;
      const text = v === null || v === undefined ? '' : String(v);
      const isPk = !!c.pk;
      const long = /content|description|payload|raw|prompt|summary|text|message|models|tags|reply|extras/i
        .test(c.name) || text.length > 120;
      const common = `data-col="${esc(c.name)}"${isPk && editing ? ' disabled' : ''}`;
      const placeholder = isPk && !editing ? '留空自动生成' : '';
      const input = long
        ? `<textarea ${common} placeholder="${placeholder}">${esc(text)}</textarea>`
        : `<input ${common} value="${esc(text)}" placeholder="${placeholder}">`;
      return `<label class="field${long ? ' wide' : ''}">
        <span>${esc(c.name)} <em>${esc(c.type || '')}</em>${isPk ? '<b class="pk">PK</b>' : ''}</span>
        ${input}
      </label>`;
    }).join('');

    dbModalOpen(
      editing ? `编辑记录 · ${d.table}` : `新增记录 · ${d.table}`,
      `<div class="form-grid">${fields}</div>`,
      `<button type="button" class="btn ghost" data-modal-cancel>取消</button>
       <button type="button" class="btn primary" id="db-row-save">${editing ? '保存修改' : '新增记录'}</button>`,
    );
    $('#db-row-save').onclick = async () => {
      const values = collectRowValues(cols);
      if (!Object.keys(values).length) {
        dbToast('请至少填写一个字段', false);
        return;
      }
      try {
        const out = await dbPost('/local/api/db/row/save', {
          table: d.table, values, pk: editing ? pk : null,
        });
        dbModalClose();
        dbToast(out.msg || '已保存');
        await dbLoadTable();
        await dbRefreshTables();
      } catch (err) {
        dbToast(err.message, false);
      }
    };
  }

  async function deleteRow(row) {
    const d = dbState.data;
    if (!d) return;
    const pk = {};
    (d.columns || []).filter((c) => c.pk).forEach((c) => { pk[c.name] = row[c.name]; });
    if (!Object.keys(pk).length && row.__rowid__ !== undefined) {
      pk.__rowid__ = row.__rowid__;
    }
    if (!window.confirm('确定删除这条记录？')) return;
    try {
      const out = await dbPost('/local/api/db/row/delete', { table: d.table, pk });
      dbToast(out.msg || '已删除');
      await dbLoadTable();
      await dbRefreshTables();
    } catch (err) {
      dbToast(err.message, false);
    }
  }

  function openCreateTable() {
    const colRow = (name = '', type = 'TEXT', pk = false, notnull = false, def = '') => `
      <div class="col-row">
        <input data-cname placeholder="字段名" value="${esc(name)}">
        <select data-ctype>
          ${['TEXT', 'INTEGER', 'REAL', 'BLOB', 'NUMERIC'].map((t) =>
            `<option value="${t}"${t === type ? ' selected' : ''}>${t}</option>`).join('')}
        </select>
        <label class="check"><input type="checkbox" data-cpk${pk ? ' checked' : ''}> PK</label>
        <label class="check"><input type="checkbox" data-cnotnull${notnull ? ' checked' : ''}> 非空</label>
        <input data-cdefault placeholder="默认值" value="${esc(def)}">
        <button type="button" data-cremove title="删除字段">×</button>
      </div>`;
    dbModalOpen('新建数据表', `
      <label class="field wide"><span>表名</span>
        <input id="db-new-table" placeholder="例如：my_table" autocomplete="off">
      </label>
      <div class="col-rows" id="db-col-rows">
        ${colRow('id', 'INTEGER', true, true)}
        ${colRow('name', 'TEXT')}
        ${colRow('created_at', 'TEXT')}
      </div>
      <button type="button" class="btn ghost small" id="db-add-col">+ 添加字段</button>
    `, `<button type="button" class="btn ghost" data-modal-cancel>取消</button>
        <button type="button" class="btn primary" id="db-create-submit">创建数据表</button>`);

    const rows = $('#db-col-rows');
    $('#db-add-col').onclick = () => { rows.insertAdjacentHTML('beforeend', colRow()); };
    rows.addEventListener('click', (e) => {
      const b = e.target.closest('[data-cremove]');
      if (b) {
        const row = b.closest('.col-row');
        if (rows.children.length > 1) row.remove();
      }
    });
    $('#db-create-submit').onclick = async () => {
      const name = $('#db-new-table').value.trim();
      if (!name) { dbToast('请填写表名', false); return; }
      const columns = [];
      $$('.col-row', rows).forEach((r) => {
        const cname = $('[data-cname]', r).value.trim();
        if (!cname) return;
        const type = $('[data-ctype]', r).value;
        const isPk = $('[data-cpk]', r).checked;
        columns.push({
          name: cname,
          type,
          pk: isPk,
          notnull: $('[data-cnotnull]', r).checked,
          default: $('[data-cdefault]', r).value.trim(),
          auto: isPk && type === 'INTEGER',
        });
      });
      if (!columns.length) { dbToast('至少填写一个字段', false); return; }
      try {
        const out = await dbPost('/local/api/db/table/create', { name, columns });
        dbModalClose();
        dbToast(out.msg || '数据表已创建');
        dbState.active = name;
        dbState.page = 0;
        await views.database();
      } catch (err) {
        dbToast(err.message, false);
      }
    };
  }

  async function dbAction(act, btn) {
    const original = btn ? btn.textContent : '';
    try {
      if (act === 'refresh') return await views.database();

      if (act === 'mirror') return await startMirror(btn);

      if (act === 'refresh-logs') {
        dbState.logs = await j('/local/api/db/logs?limit=60');
        const term = $('#db-terminal');
        if (term) term.innerHTML = renderTerminal();
        return;
      }

      if (act === 'scan') {
        if (btn) { btn.disabled = true; btn.textContent = '扫描中…'; }
        const out = await dbPost('/local/api/db/scan-json', {});
        const d = out.data || {};
        const names = (d.tables || []).join(', ') || '无';
        const skipped = (d.skipped || []).length;
        dbToast(`扫描完成：${d.files || 0} 个文件 · 导入 ${d.imported || 0} 条 · 表 ${names}`
          + (skipped ? ` · 跳过 ${skipped} 个模板文件` : ''));
        await views.database();
        return;
      }

      if (act === 'clear' || act === 'recreate' || act === 'drop-all') {
        const map = {
          clear: {
            action: 'clear',
            msg: '确定清空所有业务数据？将保留 meta / db_op_log / users / sessions / llm_providers。',
          },
          recreate: {
            action: 'recreate',
            msg: '确定删除所有业务数据表并重新创建空表？账号、会话与操作日志会保留。',
          },
          'drop-all': {
            action: 'drop_all',
            msg: '确定删除所有业务数据表？只保留 meta / db_op_log / users / sessions，站点数据接口会暂时不可用。',
          },
        }[act];
        if (!window.confirm(map.msg)) return;
        if (btn) btn.disabled = true;
        const out = await dbPost('/local/api/db/maintenance',
          { action: map.action, confirm: 'yes' });
        dbToast(out.msg || '操作完成');
        dbState.active = '';
        dbState.page = 0;
        await views.database();
        return;
      }

      if (act === 'add-row') return openRowEditor(null);

      if (act === 'clear-table') {
        if (!dbState.active) return;
        if (!window.confirm(`确定清空数据表「${dbState.active}」的所有记录？表结构会保留。`)) return;
        if (btn) btn.disabled = true;
        const out = await dbPost('/local/api/db/table/clear',
          { name: dbState.active, confirm: 'yes' });
        dbToast(out.msg || '已清空');
        dbState.page = 0;
        // 只重载右侧表数据、左栏行数与操作日志，不重建整个视图，避免页面像被刷新一样跳动
        await dbLoadTable();
        await dbRefreshTables();
        dbState.logs = await j('/local/api/db/logs?limit=60');
        const term = $('#db-terminal');
        if (term) term.innerHTML = renderTerminal();
        if (btn) { btn.disabled = false; btn.textContent = original; }
        return;
      }

      if (act === 'create-table') return openCreateTable();
    } catch (err) {
      dbToast(err.message, false);
      if (btn) {
        btn.disabled = false;
        btn.textContent = original;
      }
    }
  }

  async function startMirror(btn) {
    const original = btn ? btn.textContent : '';
    const sel = $('#db-mirror-surface');
    const surface = sel ? sel.value : 'works';
    const lim = parseInt(($('#db-mirror-limit') || {}).value || '60', 10) || 60;
    const bar = $('#db-mirror-progress');
    const fill = $('#db-mirror-fill');
    const label = $('#db-mirror-label');
    
    // 原站现在对热门榜 / 推荐 / 世界书等接口启用了鉴权，未登录会返回 401，
    // 所以所有数据面都提供登录入口（留空即游客模式）
    let username = '', password = '';
    const needAuthMsg = '原站部分接口（热门榜、推荐、世界书等）需要登录才能抓取。\n' +
                        '填入源站账号密码可抓全数据；留空则以游客模式镜像（这些接口会跳过并提示需登录）。';
    if (window.confirm(`启动手动反代镜像「${surface}」，条数上限 ${lim}？\n\n${needAuthMsg}`)) {
      username = (prompt('请输入源站用户名（可留空，游客模式直接取消）：') || '').trim();
      if (username) {
        password = prompt('请输入源站密码：') || '';
      }
    } else {
      return;
    }

    if (btn) { btn.disabled = true; btn.textContent = '镜像中…'; }
    if (bar) { bar.hidden = false; fill.style.width = '0%'; label.textContent = '启动镜像…'; }
    try {
      const out = await dbPost('/local/api/mirror/start', {
        surface, limit: lim, username, password
      });
      if (out.code !== 0) {
        dbToast(out.msg || '启动失败', false);
        if (btn) { btn.disabled = false; btn.textContent = original; }
        if (bar) bar.hidden = true;
        return;
      }
      const tick = setInterval(async () => {
        try {
          const st = await j('/local/api/mirror/status');
          const d = st.data || {};
          const pct = d.total ? Math.min(100, Math.round((d.done / d.total) * 100)) : (d.running ? 5 : 100);
          if (fill) fill.style.width = pct + '%';
          if (label) label.textContent = `${surface} · ${d.phase || ''} ${d.items} 条 · 错误 ${d.errors || 0}`;
          if (!d.running) {
            clearInterval(tick);
            if (btn) { btn.disabled = false; btn.textContent = original; }
            if (d.phase === 'error') dbToast('镜像失败：' + (d.message || ''), false);
            else dbToast(`镜像完成：${d.items} 条数据已入库`);
            await views.database();
          }
        } catch (err) {
          clearInterval(tick);
          if (btn) { btn.disabled = false; btn.textContent = original; }
        }
      }, 1000);
    } catch (err) {
      dbToast(err.message, false);
      if (btn) { btn.disabled = false; btn.textContent = original; }
      if (bar) bar.hidden = true;
    }
  }

  function updateConnectFields(root) {
    const typeEl = $('#db-type', root);
    const type = typeEl ? typeEl.value : 'sqlite';
    const isSqlite = type === 'sqlite';
    const hint = $('#db-connect-hint', root);
    const name = $('#db-name', root);
    ['#db-host', '#db-port', '#db-user', '#db-pass'].forEach((sel) => {
      const el = $(sel, root);
      if (!el) return;
      el.disabled = isSqlite;
      el.style.opacity = isSqlite ? '.55' : '1';
    });
    if (name) name.placeholder = isSqlite ? 'data/missai.db' : '数据库名';
    if (hint) {
      hint.textContent = isSqlite
        ? '本地后端内置 SQLite 驱动，填写 .db 文件路径即可切换；不存在会自动创建。'
        : 'PostgreSQL / MySQL 需要后端安装对应驱动；当前本地镜像仅内置 SQLite。';
    }
  }

  async function onConnect(e) {
    e.preventDefault();
    const type = $('#db-type').value;
    const body = {
      type,
      host: $('#db-host').value.trim(),
      port: $('#db-port').value.trim(),
      database: $('#db-name').value.trim(),
      username: $('#db-user').value.trim(),
      password: $('#db-pass').value,
    };
    if (!body.database) {
      dbToast('请填写 SQLite 路径或数据库名', false);
      return;
    }
    const btn = e.target.querySelector('button[type=submit]');
    if (btn) { btn.disabled = true; btn.textContent = '连接中…'; }
    try {
      const out = await dbPost('/local/api/db/connect', body);
      dbToast(out.msg || '已切换数据库');
      dbState.active = '';
      dbState.page = 0;
      dbState.filter = '';
      await views.database();
    } catch (err) {
      dbToast(err.message, false);
      if (btn) { btn.disabled = false; btn.textContent = '连接并切换数据库'; }
    }
  }

  async function dbLoadTable() {
    const panel = $('#db-table-panel');
    if (!dbState.active) {
      dbState.data = null;
      if (panel) panel.innerHTML = '<div class="empty">还没有数据表。点击「新建数据表」开始。</div>';
      return;
    }
    if (panel) {
      panel.innerHTML = '<div class="db-loading"><span class="spinner"></span> 正在读取数据表…</div>';
    }
    const want = dbState.active;
    try {
      const data = await j(dbTableUrl());
      if (want !== dbState.active) return;   // 用户已经点了别的表，丢弃旧响应
      dbState.data = data;
    } catch (err) {
      if (want !== dbState.active) return;
      dbState.data = null;
      if (panel) panel.innerHTML = `<div class="empty">读取失败：${esc(err.message)}</div>`;
      return;
    }
    if (panel) {
      panel.innerHTML = renderTablePanel();
      bindTablePanel(panel);
    }
  }

  async function dbRefreshTables() {
    let tables;
    try {
      tables = await j('/local/api/db/tables');
    } catch (err) {
      return;
    }
    dbState.tables = Array.isArray(tables) ? tables : [];
    const list = $('#db-table-list');
    if (list) {
      $$('[data-table]', list).forEach((b) => {
        const t = dbState.tables.find((x) => x.name === b.dataset.table);
        const meta = $('.ti-meta', b);
        if (t && meta) meta.textContent = `${fmtNum(t.rows)} 行 · ${t.columns} 列`;
      });
    }
    const sumRows = dbState.tables.reduce((a, t) => a + Number(t.rows || 0), 0);
    const sumCols = dbState.tables.reduce((a, t) => a + Number(t.columns || 0), 0);
    const m1 = $('.metric-card.m1 .v');
    const m2 = $('.metric-card.m2 .v');
    const m3 = $('.metric-card.m3 .v');
    if (m1) m1.textContent = fmtNum(dbState.tables.length);
    if (m2) m2.textContent = fmtNum(sumRows);
    if (m3) m3.textContent = fmtNum(sumCols);
  }

  function bindTablePanel(scope) {
    $$('[data-row-edit]', scope).forEach((b) => {
      b.addEventListener('click', () => {
        const row = (dbState.data && dbState.data.rows || [])[Number(b.dataset.rowEdit)];
        if (row) openRowEditor(row);
      });
    });
    $$('[data-row-del]', scope).forEach((b) => {
      b.addEventListener('click', () => {
        const row = (dbState.data && dbState.data.rows || [])[Number(b.dataset.rowDel)];
        if (row) deleteRow(row);
      });
    });
    $$('[data-page]', scope).forEach((b) => {
      b.addEventListener('click', () => {
        if (b.dataset.page === 'prev') dbState.page = Math.max(0, dbState.page - 1);
        else dbState.page += 1;
        dbLoadTable();
      });
    });
    const ps = $('#db-page-size', scope);
    if (ps) {
      ps.addEventListener('change', () => {
        dbState.pageSize = Number(ps.value) || 50;
        dbState.page = 0;
        dbLoadTable();
      });
    }
    $$('[data-db-act]', scope).forEach((b) => {
      b.addEventListener('click', () => dbAction(b.dataset.dbAct, b));
    });
  }

  function bindDatabase(root) {
    const listEl = $('#db-table-list', root);
    const searchEl = $('#db-table-filter', root);
    if (searchEl) {
      searchEl.value = dbState.filter;
      searchEl.addEventListener('input', () => {
        dbState.filter = searchEl.value;
        if (listEl) listEl.innerHTML = renderTableList();
      });
    }
    if (listEl) {
      listEl.addEventListener('click', (e) => {
        const b = e.target.closest('[data-table]');
        if (!b) return;
        dbState.active = b.dataset.table;
        dbState.page = 0;
        // 只切换高亮 + 刷新右侧表数据，不重建整个页面，也不重置左栏滚动
        $$('.table-item', listEl).forEach((x) => x.classList.toggle('on', x === b));
        dbLoadTable();
      });
    }

    // 顶部 / 卡片里的操作按钮（表数据面板里的按钮由 bindTablePanel 单独绑定）
    $$('[data-db-act]', root)
      .filter((b) => !b.closest('#db-table-panel'))
      .forEach((b) => {
        b.addEventListener('click', () => dbAction(b.dataset.dbAct, b));
      });

    bindTablePanel($('#db-table-panel', root) || root);

    const form = $('#db-connect-form', root);
    if (form) form.addEventListener('submit', onConnect);
    const typeEl = $('#db-type', root);
    if (typeEl) typeEl.addEventListener('change', () => updateConnectFields(root));
    updateConnectFields(root);
  }

  function openPointsAdjust(row) {
    const name = row.nickName || row.userName;
    dbModalOpen('调整积分 · ' + name, `
      <div class="pt-target">
        <div class="pt-target-name">${esc(name)}
          <span class="muted small">#${row.id} · ${esc(row.userName)}</span></div>
        <div class="muted small">当前积分 <b>${fmtNum(row.points)}</b> · 经验
          <b>${fmtNum(row.experience)}</b> · Lv.${row.level} ${esc(row.levelName)}
          ${row.isMaxLevel ? '（已满级）' : '· 下一级 ' + fmtNum(row.nextLevelExp)}</div>
      </div>
      <form id="pt-adjust-form" class="llm-form">
        <label>积分调整（正数增加 / 负数扣减）
          <input id="pt-adj-points" type="number" step="1" value="0" placeholder="0"></label>
        <label>经验调整（影响等级，正数增加 / 负数扣减）
          <input id="pt-adj-exp" type="number" step="1" value="0" placeholder="0"></label>
        <label>原因备注
          <input id="pt-adj-reason" maxlength="200"
            placeholder="如：活动奖励 / 违规扣减（会写入调整流水）"></label>
      </form>`,
      `<button type="button" class="btn ghost" data-modal-cancel>取消</button>
       <button type="button" class="btn primary" id="pt-adj-save">保存调整</button>`);
    const save = $('#pt-adj-save');
    if (!save) return;
    save.onclick = async () => {
      const pointsDelta = Number(($('#pt-adj-points') || {}).value || 0);
      const expDelta = Number(($('#pt-adj-exp') || {}).value || 0);
      if (!pointsDelta && !expDelta) return dbToast('积分与经验都为 0，无需保存', false);
      save.disabled = true;
      save.textContent = '保存中…';
      try {
        await dbPost('/local/api/points/adjust', {
          userId: row.id, pointsDelta, expDelta,
          reason: ($('#pt-adj-reason') || {}).value || '',
        });
        dbModalClose();
        dbToast('已调整 ' + name);
        views.points();
      } catch (err) {
        save.disabled = false;
        save.textContent = '保存调整';
        dbToast(err.message, false);
      }
    };
  }

  /* ---------------------------------------------------------- 规则管理 */
  const numField = (label, path, value, hint = '') => `
    <label>${esc(label)}<input type="number" min="0" data-rule="${path}"
      value="${Number(value) || 0}">${hint ? `<span class="muted small">${esc(hint)}</span>` : ''}</label>`;

  function rulesFormHtml(r) {
    const levels = r.levels || [];
    const ck = r.checkin || {};
    const chat = r.chat || {};
    const img = r.image || {};
    const ex = r.exp || {};
    const rewards = ck.consecutiveRewards || [];
    const rewardRows = rewards.map((it, i) => `
      <div class="rules-row">
        <label class="muted small">连签天数
          <input type="number" min="1" data-reward-days="${i}" value="${Number(it.days) || 0}"></label>
        <label class="muted small">奖励积分
          <input type="number" min="0" data-reward-bonus="${i}" value="${Number(it.bonus) || 0}"></label>
        <button type="button" class="btn danger small" data-reward-del="${i}">删除</button>
      </div>`).join('');
    return `
      <div class="panel">
        <h3>等级与经验成长</h3>
        <p class="muted small">等级门槛按累计经验升序排列，Lv.1 固定为 0 经验；保存后会自动重新编号。</p>
        <div class="table-scroll">
          <table class="rules-table">
            <thead><tr><th>等级</th><th>名称</th><th>所需累计经验</th><th>说明</th></tr></thead>
            <tbody>${levels.map((lv, i) => `
              <tr>
                <td>Lv.${i + 1}</td>
                <td><input data-level-name="${i}" value="${esc(lv.name || '')}" style="min-width:130px"></td>
                <td><input type="number" min="0" data-level-exp="${i}"
                  value="${Number(lv.exp) || 0}"${i === 0 ? ' disabled' : ''}></td>
                <td class="muted small">${i === 0 ? '起始等级' :
                  '比上一级多 ' + Math.max(0, Number(lv.exp) - Number(levels[i - 1].exp)) + ' 经验'}</td>
              </tr>`).join('')}</tbody>
          </table>
        </div>
        <div class="llm-form rules-grid">
          ${numField('发布作品最低等级', 'publishMinLevel', r.publishMinLevel, '1 ~ 等级总数')}
          ${numField('注册赠送经验', 'exp.register', ex.register)}
          ${numField('签到经验', 'exp.checkin', ex.checkin)}
          ${numField('首次发布经验', 'exp.publish', ex.publish)}
          ${numField('收藏经验', 'exp.favorite', ex.favorite, '每天每个目标一次')}
          ${numField('点赞经验', 'exp.like', ex.like, '每天每个目标一次')}
          ${numField('浏览角色经验', 'exp.view', ex.view, '每天每个角色一次')}
        </div>
      </div>

      <div class="panel">
        <h3>每日签到积分</h3>
        <div class="llm-form rules-grid">
          ${numField('签到基础积分', 'checkin.basePoints', ck.basePoints, '每天签到固定发放')}
          ${numField('周末额外积分', 'checkin.weekendBonus', ck.weekendBonus)}
          ${numField('每日赠送积分', 'checkin.dailyGiftPoints', ck.dailyGiftPoints, '随签到一起发放')}
          ${numField('签到周期（天）', 'checkin.cycleDays', ck.cycleDays)}
        </div>
        <h3 style="margin-top:16px">连签奖励档位</h3>
        <div class="rules-rewards">${rewardRows || '<div class="empty small">未设置连签奖励</div>'}</div>
        <button type="button" class="btn small" data-reward-add>+ 增加档位</button>
      </div>

      <div class="panel">
        <h3>聊天计费</h3>
        <div class="llm-form rules-grid">
          <label>计费模式
            <select data-rule="chat.mode">
              <option value="points"${String(chat.mode) === 'points' ? ' selected' : ''}>按次扣积分</option>
              <option value="token"${String(chat.mode) === 'token' ? ' selected' : ''}>按 token 折算积分</option>
            </select></label>
          ${numField('按次扣积分（points 模式）', 'chat.pointsPerCall', chat.pointsPerCall)}
          ${numField('多少 token 折 1 积分', 'chat.tokensPerPoint', chat.tokensPerPoint)}
          ${numField('每次最少计费 token', 'chat.minTokensPerCall', chat.minTokensPerCall)}
        </div>
        <p class="muted small">token 模式会按「输入 + 回复」估算 token（中文约 1 字 1 token，英文约 4 字符 1 token），再按兑换比例折算成积分扣除；余额不足会拒绝本次对话。</p>
      </div>

      <div class="panel">
        <h3>生图积分</h3>
        <div class="llm-form rules-grid">
          ${numField('每张图积分', 'image.pointsPerImage', img.pointsPerImage)}
          <label class="rules-check">
            <span>本地占位绘图免费</span>
            <input type="checkbox" data-rule="image.freeLocal"${img.freeLocal ? ' checked' : ''}>
          </label>
        </div>
        <p class="muted small">真实模型按生成张数 × 每张积分扣费；余额不足会拒绝本次生成。</p>
      </div>

      <div class="toolbar">
        <button type="button" class="btn primary" data-rules-act="save">保存规则</button>
        <button type="button" class="btn" data-rules-act="reset">恢复默认</button>
        <span class="muted small">规则保存到本地数据库，聊天、签到、发布、生图等立即生效。</span>
      </div>`;
  }

  function readRulesForm() {
    const el = (path) => document.querySelector(`#view-rules [data-rule="${path}"]`);
    const num = (path) => {
      const node = el(path);
      return node ? Number(node.value || 0) : 0;
    };
    const levels = Array.from(document.querySelectorAll('#view-rules [data-level-name]'))
      .map((node, i) => ({
        level: i + 1,
        name: node.value.trim(),
        exp: Number((document.querySelector(`#view-rules [data-level-exp="${i}"]`) || {}).value || 0),
      }));
    const rewards = Array.from(document.querySelectorAll('#view-rules [data-reward-days]'))
      .map((node, i) => ({
        days: Number(node.value || 0),
        bonus: Number((document.querySelector(`#view-rules [data-reward-bonus="${i}"]`) || {}).value || 0),
      }));
    return {
      levels,
      publishMinLevel: num('publishMinLevel'),
      exp: {
        register: num('exp.register'),
        checkin: num('exp.checkin'),
        publish: num('exp.publish'),
        favorite: num('exp.favorite'),
        like: num('exp.like'),
        view: num('exp.view'),
      },
      checkin: {
        basePoints: num('checkin.basePoints'),
        weekendBonus: num('checkin.weekendBonus'),
        dailyGiftPoints: num('checkin.dailyGiftPoints'),
        cycleDays: num('checkin.cycleDays'),
        consecutiveRewards: rewards,
      },
      chat: {
        mode: (el('chat.mode') || {}).value || 'points',
        pointsPerCall: num('chat.pointsPerCall'),
        tokensPerPoint: num('chat.tokensPerPoint'),
        minTokensPerCall: num('chat.minTokensPerCall'),
      },
      image: {
        pointsPerImage: num('image.pointsPerImage'),
        freeLocal: !!(el('image.freeLocal') || {}).checked,
      },
    };
  }

  function renderRulesView(rules) {
    const root = $('#view-rules');
    if (!root) return;
    const ck = rules.checkin || {};
    const chat = rules.chat || {};
    const img = rules.image || {};
    const chatText = String(chat.mode) === 'token'
      ? `${Number(chat.tokensPerPoint) || 0} token = 1 积分`
      : `每次 ${Number(chat.pointsPerCall) || 0} 积分`;
    root.innerHTML = `
      <div class="cards">
        <div class="card"><div class="k">发布最低等级</div>
          <div class="v">Lv.${Number(rules.publishMinLevel) || 1}</div></div>
        <div class="card"><div class="k">签到基础积分</div>
          <div class="v">${fmtNum(ck.basePoints)}</div></div>
        <div class="card"><div class="k">聊天计费</div>
          <div class="v" style="font-size:16px">${esc(chatText)}</div></div>
        <div class="card"><div class="k">生图每张积分</div>
          <div class="v">${fmtNum(img.pointsPerImage)}</div></div>
      </div>
      ${rulesFormHtml(rules)}`;

    root.querySelectorAll('[data-rules-act]').forEach((btn) => btn.addEventListener('click', async () => {
      const act = btn.dataset.rulesAct;
      if (act === 'reset') {
        if (!confirm('恢复默认规则？当前的等级、经验、签到、聊天与生图规则都会被覆盖。')) return;
        try {
          const d = await dbPost('/local/api/rules/reset', {});
          dbToast('已恢复默认规则');
          renderRulesView(d.data || {});
        } catch (err) { dbToast(err.message, false); }
        return;
      }
      if (act === 'save') {
        btn.disabled = true;
        btn.textContent = '保存中…';
        try {
          const d = await dbPost('/local/api/rules', { rules: readRulesForm() });
          dbToast('规则已保存并生效');
          renderRulesView(d.data || readRulesForm());
        } catch (err) {
          dbToast(err.message, false);
          btn.disabled = false;
          btn.textContent = '保存规则';
        }
      }
    }));

    const addBtn = root.querySelector('[data-reward-add]');
    if (addBtn) {
      addBtn.addEventListener('click', () => {
        const draft = readRulesForm();
        draft.checkin.consecutiveRewards.push({ days: 0, bonus: 0 });
        renderRulesView(draft);
      });
    }
    root.querySelectorAll('[data-reward-del]').forEach((btn) => btn.addEventListener('click', () => {
      const draft = readRulesForm();
      draft.checkin.consecutiveRewards.splice(Number(btn.dataset.rewardDel), 1);
      renderRulesView(draft);
    }));
  }

  const views = {
    async database() {
      const root = $('#view-database');
      if (!root) return;
      root.innerHTML = '<div class="db-loading"><span class="spinner"></span> 正在读取数据库…</div>';
      let ov, tables, logs;
      try {
        [ov, tables, logs] = await Promise.all([
          j('/local/api/db/overview'),
          j('/local/api/db/tables'),
          j('/local/api/db/logs?limit=60'),
        ]);
      } catch (err) {
        root.innerHTML = `<div class="db-card"><div class="empty">加载失败：${esc(err.message)}</div></div>`;
        return;
      }
      dbState.overview = ov;
      dbState.tables = Array.isArray(tables) ? tables : [];
      dbState.logs = Array.isArray(logs) ? logs : [];
      if (!dbState.active || !dbState.tables.some((t) => t.name === dbState.active)) {
        dbState.active = dbState.tables.length ? dbState.tables[0].name : '';
        dbState.page = 0;
      }
      let data = null;
      if (dbState.active) {
        try {
          data = await j(dbTableUrl());
        } catch (err) {
          data = null;
        }
      }
      dbState.data = data;

      root.innerHTML = `
        <section class="db-hero">
          <div class="hero-top">
            <div>
              <div class="eyebrow">PYTHON · SQL DATABASE</div>
              <h1>后端控制台</h1>
              <p class="hero-sub">直接连接本地 SQLite，实时查看与操作每一张表。</p>
            </div>
            <div class="hero-actions">
              <div class="mirror-controls" title="手动反代镜像：抓原站数据按对应表入库">
                <select id="db-mirror-surface" class="mirror-surface">
                  <option value="works">探索作品</option>
                  <option value="creative">创意广场</option>
                  <option value="drawing">绘图广场</option>
                  <option value="characters">角色详情</option>
                  <option value="characters-full">角色全量（含世界书+图片）</option>
                  <option value="assets">样式入库</option>
                </select>
                <input id="db-mirror-limit" class="mirror-limit" type="number"
                  min="1" max="500" value="60" title="条数上限（先跑通用小值）">
                <button type="button" class="btn accent" data-db-act="mirror">⤓ 手动镜像</button>
              </div>
              <button type="button" class="btn ghost" data-db-act="refresh">↻ 刷新</button>
              <button type="button" class="btn ghost" data-db-act="scan">⌕ 扫描 json/</button>
              <button type="button" class="btn danger" data-db-act="clear">清空所有数据</button>
              <button type="button" class="btn danger" data-db-act="recreate">重新创建数据表</button>
              <button type="button" class="btn danger solid" data-db-act="drop-all">删除所有数据表</button>
            </div>
          </div>
          <div class="mirror-bar" id="db-mirror-progress" hidden>
            <div class="mirror-bar-track"><div class="mirror-bar-fill" id="db-mirror-fill"></div></div>
            <span class="mirror-bar-label" id="db-mirror-label"></span>
          </div>
          ${renderOverview(ov)}
        </section>

        <section class="db-card">
          <div class="card-head">
            <h2>数据库连接</h2>
            <span class="chip soft">SQLite / PostgreSQL / MySQL</span>
          </div>
          <form id="db-connect-form" class="connect-form">
            <label>数据库类型
              <select id="db-type">
                <option value="sqlite" selected>SQLite</option>
                <option value="postgresql">PostgreSQL</option>
                <option value="mysql">MySQL</option>
              </select>
            </label>
            <label>主机
              <input id="db-host" value="${esc(ov.host)}" placeholder="localhost">
            </label>
            <label>端口
              <input id="db-port" value="${esc(ov.port)}" placeholder="0">
            </label>
            <label>数据库名 / SQLite 路径
              <input id="db-name" value="${esc(ov.path)}" title="${esc(ov.path)}" placeholder="data/missai.db">
            </label>
            <label>用户名
              <input id="db-user" placeholder="（可选）">
            </label>
            <label>密码
              <input id="db-pass" type="password" placeholder="（可选）" autocomplete="new-password">
            </label>
            <div class="form-actions">
              <span class="connect-hint" id="db-connect-hint"></span>
              <button type="submit" class="btn primary">连接并切换数据库</button>
            </div>
          </form>
        </section>

        <section class="db-card">
          <div class="card-head">
            <h2>数据表</h2>
            <div class="head-tools">
              <input id="db-table-filter" class="table-search" placeholder="搜索表名…"
                value="${esc(dbState.filter)}">
              <button type="button" class="btn ghost small" data-db-act="create-table">+ 新建数据表</button>
            </div>
          </div>
          <div class="explorer">
            <aside class="table-list" id="db-table-list">${renderTableList()}</aside>
            <div class="table-panel" id="db-table-panel">${renderTablePanel()}</div>
          </div>
        </section>

        <section class="db-card terminal-card">
          <div class="card-head">
            <h2>Python 数据库操作终端</h2>
            <button type="button" class="btn ghost small" data-db-act="refresh-logs">刷新日志</button>
          </div>
          <div class="terminal" id="db-terminal">${renderTerminal()}</div>
        </section>`;

      bindDatabase(root);
    },

    async logs() {
      const rows = await j('/local/api/logs?limit=500');
      const f = filter.toLowerCase();
      const hit = f ? rows.filter((r) => (r.path || '').toLowerCase().includes(f)) : rows;
      $('#view-logs').innerHTML = `
        <div class="toolbar">
          <input id="logFilter" placeholder="按路径过滤，如 /api/gva" value="${esc(filter)}">
          <span class="muted">${hit.length} 条 / 共 ${rows.length} 条</span>
        </div>
        <div class="panel">
          ${table(['时间', '方法', '路径', '状态', '耗时(ms)', '用户', '体积', '备注'], hit, (r) =>
            `<tr><td class="muted">${esc(r.ts)}</td><td>${esc(r.method)}</td>
             <td class="path">${esc(r.path)}</td><td>${statusBadge(r.status)}</td>
             <td class="muted">${r.duration_ms}</td><td class="muted">${r.user_id || '-'}</td>
             <td class="muted">${r.bytes || 0}</td><td class="muted">${esc(r.note)}</td></tr>`)}
        </div>`;
      const el = $('#logFilter');
      el.addEventListener('input', (e) => {
        filter = e.target.value;
        views.logs();
        const again = $('#logFilter');
        again.focus();
        again.setSelectionRange(again.value.length, again.value.length);
      });
    },

    async users() {
      const [users, sessions] = await Promise.all([
        j('/local/api/users'), j('/local/api/sessions')]);
      $('#view-users').innerHTML = `
        <div class="panel">
          <h3>本地登录</h3>
          <form id="localLogin" class="login-inline">
            <input id="li-user" placeholder="用户名或邮箱" autocomplete="username">
            <input id="li-pass" type="password" placeholder="密码" autocomplete="current-password">
            <button type="submit">登录</button>
            <span class="muted" id="li-msg">默认管理员 admin / admin123456</span>
          </form>
        </div>
        <div class="panel"><h3>本地账号（${users.length}）</h3>
          ${table(['ID', '用户名', '昵称', '角色', '邮箱', '注册时间', '最后登录'], users, (r) =>
            `<tr><td>${r.id}</td><td>${esc(r.user_name)}</td><td>${esc(r.nick_name)}</td>
             <td>${r.role === 'admin' ? '<b class="admin">管理员</b>' : '普通'}</td>
             <td class="muted">${esc(r.email || '-')}</td><td class="muted">${esc(r.created_at)}</td>
             <td class="muted">${esc(r.last_login_at || '-')}</td></tr>`)}
        </div>
        <div class="panel"><h3>会话（${sessions.length}）</h3>
          ${table(['Token', '用户', '过期时间', 'IP', 'UA'], sessions, (r) =>
            `<tr><td class="mono">${esc((r.token || '').slice(0, 16))}…</td><td>${r.user_id}</td>
             <td class="muted">${esc(r.expires_at)}</td><td class="muted">${esc(r.ip)}</td>
             <td class="muted">${esc((r.user_agent || '').slice(0, 60))}</td></tr>`)}
        </div>`;

      $('#localLogin').addEventListener('submit', async (e) => {
        e.preventDefault();
        const msg = $('#li-msg');
        const id = $('#li-user').value.trim();
        const pw = $('#li-pass').value;
        if (!id || !pw) { msg.textContent = '请输入账号和密码'; return; }
        msg.textContent = '登录中…';
        try {
          const r = await fetch('/api/gva/base/email/passwordLogin', {
            method: 'POST', credentials: 'include',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ email: id, username: id, password: pw }),
          });
          const data = await r.json();
          if (data.code !== 0) { msg.textContent = data.msg || '登录失败'; return; }
          msg.textContent = '登录成功：' + ((data.data.user || {}).nickName || '') + '，可返回站点';
          views.users();
        } catch (err) {
          msg.textContent = '请求失败：' + err;
        }
      });
    },

    async points() {
      const root = $('#view-points');
      if (!root) return;
      root.innerHTML = '<div class="db-loading"><span class="spinner"></span> 正在读取积分数据…</div>';
      const qs = pointsQ ? ('?q=' + encodeURIComponent(pointsQ)) : '';
      let d;
      try {
        d = await j('/local/api/points' + qs);
      } catch (err) {
        root.innerHTML = `<div class="panel"><div class="empty">加载失败：${esc(err.message)}</div></div>`;
        return;
      }
      const s = d.summary || {};
      const rows = d.users || [];
      const ledger = d.ledger || [];
      const tiers = s.tiers || [];
      const levelCount = (lv) => ((s.levelCounts || [])
        .find((x) => Number(x.level) === Number(lv)) || {}).count || 0;
      const levelBadge = (r) => (Number(r.level) >= Number(s.publishMinLevel || 2)
        ? `<span class="badge b2">Lv.${r.level} ${esc(r.levelName)}</span>`
        : `<span class="badge b0">Lv.${r.level} ${esc(r.levelName)}</span>`);
      const deltaText = (v) => {
        const n = Number(v || 0);
        return n > 0 ? `<b class="admin">+${fmtNum(n)}</b>`
          : `<b class="danger-text">${fmtNum(n)}</b>`;
      };

      root.innerHTML = `
        <div class="cards">
          <div class="card"><div class="k">账号总数</div>
            <div class="v">${fmtNum(s.totalUsers)}</div></div>
          <div class="card"><div class="k">积分总量</div>
            <div class="v">${fmtNum(s.totalPoints)}</div></div>
          <div class="card"><div class="k">今日签到</div>
            <div class="v">${fmtNum(s.checkedInToday)}</div></div>
          <div class="card"><div class="k">可发布账号（Lv.${s.publishMinLevel || 2}+）</div>
            <div class="v">${fmtNum(s.canPublish)}</div></div>
        </div>

        <div class="panel">
          <h3>用户积分 / 经验（${rows.length}）</h3>
          <p class="muted small">成长规则：注册 +${s.rewardRegister} 经验 · 签到
            +${s.rewardCheckin} · 首次发布 +${s.rewardPublish}；发布作品需 Lv.${s.publishMinLevel}。
            积分用于生成消耗，点「调整」可人工加减（会写入下方流水）。</p>
          <div class="toolbar">
            <input id="pt-q" placeholder="搜索用户名 / 昵称 / 邮箱…" value="${esc(pointsQ)}">
            <button type="button" data-pt="search">搜索</button>
            <button type="button" data-pt="refresh">刷新</button>
            <span class="muted">等级分布：${tiers.map((t) =>
              `Lv.${t.level} ${esc(t.name)} ${fmtNum(levelCount(t.level))}`).join(' · ')}</span>
          </div>
          ${table(['账号', '积分', '经验 / 等级', '成长进度', '签到', '已发布', '最近登录', '操作'],
            rows, (r) => `
            <tr>
              <td><div class="cd-title">${esc(r.nickName || r.userName)}</div>
                <div class="cd-id">#${r.id} ${esc(r.userName)}${r.role === 'admin' ? ' · 管理员' : ''}</div></td>
              <td><b>${fmtNum(r.points)}</b></td>
              <td>${levelBadge(r)}<div class="muted small">${fmtNum(r.experience)}
                ${r.isMaxLevel ? '（满级）' : '/ ' + fmtNum(r.nextLevelExp)}</div></td>
              <td class="nowrap"><div class="pt-bar"><span style="width:${r.levelProgress}%"></span></div>
                <span class="muted small">${r.levelProgress}%</span></td>
              <td class="nowrap">${r.checkinDays} 天
                ${r.lastCheckin ? `<div class="muted small">最近 ${esc(r.lastCheckin)}</div>` : ''}</td>
              <td>${r.publishedCount}</td>
              <td class="muted nowrap">${esc(fmtTime(r.lastLoginAt))}</td>
              <td class="llm-ops"><button type="button" data-pt="adjust" data-id="${r.id}">调整</button></td>
            </tr>`)}
        </div>

        <div class="panel">
          <h3>最近调整流水（${ledger.length}）</h3>
          ${table(['时间', '账号', '类型', '变动', '调整后', '原因'], ledger, (r) => `
            <tr><td class="muted nowrap">${esc(fmtTime(r.created_at))}</td>
              <td>${esc(r.nickName || r.userName || ('#' + r.user_id))}
                <div class="cd-id">#${r.user_id}</div></td>
              <td>${r.kind === 'exp'
                ? '<span class="badge b4">经验</span>' : '<span class="badge b2">积分</span>'}</td>
              <td>${deltaText(r.delta)}</td>
              <td>${fmtNum(r.balance)}</td>
              <td class="muted">${esc(r.reason || '—')}</td></tr>`)}
        </div>`;

      const qEl = $('#pt-q');
      if (qEl) {
        qEl.addEventListener('keydown', (e) => {
          if (e.key === 'Enter') { pointsQ = qEl.value.trim(); views.points(); }
        });
      }
      root.querySelectorAll('[data-pt]').forEach((b) => b.addEventListener('click', () => {
        const act = b.dataset.pt;
        if (act === 'search') { pointsQ = qEl ? qEl.value.trim() : ''; return views.points(); }
        if (act === 'refresh') return views.points();
        if (act === 'adjust') {
          const row = rows.find((x) => Number(x.id) === Number(b.dataset.id));
          if (row) openPointsAdjust(row);
        }
      }));
    },

    async consumption() {
      const root = $('#view-consumption');
      if (!root) return;
      root.innerHTML = '<div class="db-loading"><span class="spinner"></span> 正在统计消耗数据…</div>';
      let d;
      try {
        d = await j('/local/api/consumption?range=' + consRange);
      } catch (err) {
        root.innerHTML = `<div class="panel"><div class="empty">加载失败：${esc(err.message)}</div></div>`;
        return;
      }
      const totals = d.totals || {};
      const series = d.series || [];
      const users = d.users || [];
      const works = d.works || [];
      const kinds = d.byKind || [];
      const rangeLabel = { daily: '每日（近 30 天）', weekly: '每周（近 12 周）',
        monthly: '每月（近 12 月）' }[consRange] || consRange;
      const card = (label, t) => `
        <div class="card">
          <div class="k">${label}</div>
          <div class="v">${fmtNum(t.tokens)}<span class="muted small"> token</span></div>
          <div class="muted small" style="margin-top:4px">${fmtNum(t.points)} 积分 ·
            ${fmtNum(t.calls)} 次${t.users ? ' · ' + fmtNum(t.users) + ' 人' : ''}</div>
        </div>`;
      const cell = (c) => (c ? `
        <b>${fmtNum(c.tokens)}</b>
        <div class="muted small">${fmtNum(c.points)} 积分 · ${fmtNum(c.calls)} 次</div>`
        : '<span class="muted">—</span>');
      const maxPoints = Math.max(1, ...series.map((x) => Number(x.points || 0)));

      root.innerHTML = `
        <div class="cards">
          ${card('今日消耗', totals.today || {})}
          ${card('本周消耗', totals.week || {})}
          ${card('本月消耗', totals.month || {})}
          ${card('累计消耗', totals.all || {})}
        </div>

        <div class="panel">
          <h3>消耗趋势</h3>
          <div class="toolbar">
            ${['daily', 'weekly', 'monthly'].map((k) => `
              <button type="button" data-cr="range" data-range="${k}"
                class="${consRange === k ? 'on' : ''}">${
                  { daily: '每日', weekly: '每周', monthly: '每月' }[k]}</button>`).join('')}
            <button type="button" data-cr="refresh">刷新</button>
            <span class="muted small">${rangeLabel} · 更新于 ${esc(fmtTime(d.updatedAt))}</span>
          </div>
          <p class="muted small">按时间聚合所有用户的 token 与积分消耗。聊天按 token 或积分计费，
            生图按张扣除积分；数据从本次功能上线后开始记录。</p>
          ${table(['时间', 'Token 消耗', '积分消耗', '调用次数'], series, (r) => `
            <tr>
              <td class="nowrap">${esc(r.label)}</td>
              <td><b>${fmtNum(r.tokens)}</b></td>
              <td class="nowrap"><b>${fmtNum(r.points)}</b>
                <div class="cr-bar"><span style="width:${
                  Math.round(Number(r.points || 0) / maxPoints * 100)}%"></span></div></td>
              <td>${fmtNum(r.calls)}</td>
            </tr>`)}
        </div>

        <div class="panel">
          <h3>用户消耗（${users.length}）</h3>
          <p class="muted small">按累计积分消耗排序。每个用户都可查看今日、本周、本月与累计的
            token / 积分消耗量。</p>
          ${table(['用户', '今日', '本周', '本月', '累计'], users, (r) => `
            <tr>
              <td><div class="cd-title">${esc(r.name)}</div>
                <div class="cd-id">#${r.id}${r.userName ? ' ' + esc(r.userName) : ''}</div></td>
              <td>${cell(r.today)}</td>
              <td>${cell(r.week)}</td>
              <td>${cell(r.month)}</td>
              <td>${cell(r.total)}</td>
            </tr>`)}
        </div>

        <div class="panel">
          <h3>作品消耗（${works.length}）</h3>
          <p class="muted small">聊天作品按角色聚合 token / 积分消耗；生图作品按生成结果记录积分消耗。
            分类累计：${kinds.length ? kinds.map((k) =>
              `${esc(k.kindName)} ${fmtNum(k.tokens)} token / ${fmtNum(k.points)} 积分`
            ).join(' · ') : '暂无'}
          </p>
          ${table(['作品', '类型', '今日', '本周', '本月', '累计'], works, (r) => `
            <tr>
              <td><div class="cd-title">${esc(r.name)}</div>
                <div class="cd-id">${esc(r.id)}</div></td>
              <td><span class="badge ${r.kind === 'image' ? 'b4' : 'b2'}">${esc(r.kindName)}</span></td>
              <td>${cell(r.today)}</td>
              <td>${cell(r.week)}</td>
              <td>${cell(r.month)}</td>
              <td>${cell(r.total)}</td>
            </tr>`)}
        </div>`;

      root.querySelectorAll('[data-cr]').forEach((b) => b.addEventListener('click', () => {
        if (b.dataset.cr === 'refresh') return views.consumption();
        if (b.dataset.cr === 'range') {
          consRange = b.dataset.range || 'daily';
          return views.consumption();
        }
      }));
    },

    async rules() {
      const root = $('#view-rules');
      if (!root) return;
      root.innerHTML = '<div class="db-loading"><span class="spinner"></span> 正在读取规则…</div>';
      try {
        const data = await j('/local/api/rules');
        renderRulesView(data.rules || {});
      } catch (err) {
        root.innerHTML = `<div class="panel"><div class="empty">加载失败：${esc(err.message)}</div></div>`;
      }
    },

    async favorites() {
      const rows = await j('/local/api/favorites');
      $('#view-favorites').innerHTML = `<div class="panel"><h3>收藏 / 点赞 / 关注（${rows.length}）</h3>
        ${table(['类型', '目标 ID', '标题', '时间', '原始数据'], rows, (r) =>
          `<tr><td>${esc(r.kind)}</td><td class="mono">${esc(r.target_id)}</td>
           <td>${esc(r.title || '-')}</td><td class="muted">${esc(r.created_at)}</td>
           <td class="mono muted">${esc((r.payload || '').slice(0, 80))}</td></tr>`)}
      </div>`;
    },

    async chat() {
      const rows = await j('/local/api/chat');
      $('#view-chat').innerHTML = `<div class="panel"><h3>对话记录（${rows.length} 条）</h3>
        ${rows.length ? rows.slice().reverse().map((r) => `
          <div class="msg ${r.role === 'user' ? 'user' : 'assistant'}">
            <div class="who">${r.role === 'user' ? '我' : 'AI'} · ${esc(r.character_id || '')} · ${esc(r.created_at)}</div>
            <div>${esc(r.content).slice(0, 600)}</div>
          </div>`).join('') : '<div class="empty">暂无对话</div>'}
      </div>`;
    },

    async images() {
      const rows = await j('/local/api/images');
      const gif = rows.filter((r) => r.animated).length;
      $('#view-images').innerHTML = `<div class="panel"><h3>图片库（${rows.length} 张 · 动图 ${gif} 张）</h3>
        <p class="muted">数据库 <code>data/image.db</code> 只存相对项目根的路径；
          实际路径由 Python 后端按当前项目目录自动拼接（鼠标悬停看完整路径）。</p>
        ${table(['预览', '请求地址', '本地相对路径', '类型', '字节', '来源', '时间'], rows, (r) =>
          `<tr><td><img class="thumb" src="${esc(r.url_key)}" onerror="this.style.opacity=.2"></td>
           <td class="path">${esc(r.url_key)}</td>
           <td class="path" title="${esc(r.abs_path)}">${esc(r.local_path)}</td>
           <td>${r.animated ? '<b class="admin">动图</b>' : esc(r.kind || '')}</td>
           <td class="muted">${r.bytes}</td>
           <td class="muted">${esc(r.source || '')}</td>
           <td class="muted">${esc(r.created_at)}</td></tr>`)}
      </div>`;
    },

    async llm() {
      const d = await j('/local/api/llm');
      const presets = d.presets || [];
      const provs = d.providers || [];
      const models = d.models || [];
      const provModels = models.filter((m) => Number(m.providerId) > 0);
      const editing = provs.find((p) => Number(p.id) === Number(llmEditId)) || null;
      const kind = editing ? editing.kind : ((presets[0] || {}).key || 'openai');
      const preset = presets.find((p) => p.key === kind) || {};
      const badge = (s) => (s === 'ok' ? '<span class="badge b2">已连通</span>'
        : s === 'error' ? '<span class="badge b5">失败</span>'
          : '<span class="badge b0">未检测</span>');
      const def = models.find((m) => m.isDefault);
      const timeoutLocal = !!d.timeoutLocal;

      $('#view-llm').innerHTML = `
        <div class="cards">
          <div class="card"><div class="k">接入条数</div><div class="v">${provs.length}</div></div>
          <div class="card"><div class="k">可选模型</div><div class="v">${provModels.length}</div></div>
          <div class="card"><div class="k">聊天默认模型</div>
            <div class="v" style="font-size:16px">${esc(def ? def.modelName : '本地兜底')}</div></div>
        </div>

        <div class="panel">
          <h3>${editing ? '编辑接入 #' + editing.id : '新增接入'} <span class="muted">· OpenAI 协议</span></h3>
          ${llmMsg ? `<div class="alert ${llmMsg.ok ? 'ok' : 'err'}">${esc(llmMsg.text)}</div>` : ''}
          <form id="llmForm" class="llm-form">
            <label>平台预设
              <select id="llm-kind">${presets.map((p) =>
                `<option value="${esc(p.key)}"${p.key === kind ? ' selected' : ''}>${esc(p.label)}</option>`).join('')}</select>
            </label>
            <label>名称
              <input id="llm-name" placeholder="如：我的中转站" value="${esc(editing ? editing.name : '')}">
            </label>
            <label>API 地址（base_url）
              <input id="llm-base" placeholder="${esc((editing && editing.base_url) || preset.base_url || 'https://api.openai.com/v1')}"
                value="${esc(editing ? editing.base_url : (preset.base_url || ''))}">
            </label>
            <label>API 密钥（apikey）
              <input id="llm-key" type="password" autocomplete="new-password"
                placeholder="${editing && editing.apiKeySet ? '已保存 ' + esc(editing.apiKeyMasked) + '，留空表示不改' : 'sk-…'}">
            </label>
            <div class="llm-actions">
              <button type="submit">保存并检测模型</button>
              ${editing ? '<button type="button" class="ghost" data-llm="cancel">取消编辑</button>' : ''}
            </div>
          </form>
          <p class="muted small">${esc(preset.hint || '')} —— 数据库里只存 API 地址与密钥；
            检测只保留聊天模型（自动过滤 image / video / embedding / tts 等非对话模型），
            结果见下方，聊天界面「选择模型」显示的就是这些。</p>
        </div>

        <div class="panel"><h3>超时回退 <span class="muted">· 模型回复超时的兜底策略</span></h3>
          <label class="check">
            <input type="checkbox" id="llm-timeout-local"${timeoutLocal ? ' checked' : ''}>
            模型回复超时/失败时，自动切换本地模式回复
          </label>
          <p class="muted small">本地模式只用于测试、不作保底。关闭时模型回复超时/失败会直接报错，
            不会悄悄改用本地回复；开启后超时才自动用本地模式回复。
            模型还在正常出字（连接正常）时，永远不会中途切换成本地模式。</p>
        </div>

        <div class="panel"><h3>已保存的接入（${provs.length}）</h3>
          ${table(['ID', '预设', '名称', 'API 地址', '密钥', '状态', '模型数', '默认', '操作'], provs, (r) => `
            <tr><td>${r.id}</td><td class="muted">${esc((r.preset || {}).label || r.kind)}</td>
             <td>${esc(r.name)}</td><td class="path">${esc(r.base_url)}</td>
             <td class="mono">${esc(r.apiKeyMasked || '—')}</td>
             <td>${badge(r.status)}${r.status === 'error' && r.last_error
               ? `<div class="muted small">${esc(String(r.last_error).slice(0, 200))}</div>` : ''}</td>
             <td>${r.modelCount}</td>
             <td>${r.isDefault ? '<b class="admin">默认</b>' : '—'}</td>
             <td class="llm-ops">
               <button data-llm="edit" data-id="${r.id}">编辑</button>
               <button data-llm="check" data-id="${r.id}">重新检测</button>
               <button data-llm="select" data-id="${r.id}">设为默认</button>
               <button data-llm="delete" data-id="${r.id}">删除</button></td></tr>`)}
        </div>

        <div class="panel"><h3>检测到的聊天模型（${provModels.length}）
          <span class="muted"> · 聊天界面「选择模型」列表</span></h3>
          <p class="muted small">每次「检测」按 <code>GET {base_url}/models</code> 拉取，只保留聊天模型；
            聊天时后端按选中的模型调 <code>POST {base_url}/chat/completions</code>。</p>
          ${table(['模型', 'rateId', '所属接入', '价格', '操作'], provModels, (r) => `
            <tr><td class="path">${esc(r.modelName)}</td><td class="muted">${r.ID}</td>
             <td class="muted">${esc(r.poolName)}</td><td class="muted">${r.price}</td>
             <td class="llm-ops">${r.isDefault ? '<b class="admin">当前默认</b>'
               : `<button data-llm="use" data-id="${r.providerId}" data-model="${esc(r.modelId)}">设为默认模型</button>`}</td></tr>`)}
        </div>`;

      const kindSel = $('#llm-kind');
      const presetBases = new Set(presets.map((p) => String(p.base_url || '').trim()).filter(Boolean));
      const presetLabels = new Set(presets.map((p) => String(p.label || '').trim()).filter(Boolean));
      kindSel.addEventListener('change', () => {
        const p = presets.find((x) => x.key === kindSel.value) || {};
        const base = $('#llm-base');
        // 预设地址要真正填进输入框（只写 placeholder 的话提交的是空值）
        if (!base.value.trim() || presetBases.has(base.value.trim())) {
          base.value = p.base_url || '';
        }
        base.placeholder = p.base_url || '';
        const nm = $('#llm-name');
        if (!nm.value.trim() || presetLabels.has(nm.value.trim())) nm.value = p.label || '';
      });

      $('#llmForm').addEventListener('submit', async (e) => {
        e.preventDefault();
        const btn = e.target.querySelector('button[type=submit]');
        btn.disabled = true;
        btn.textContent = '检测中…';
        try {
          const r = await fetch('/local/api/llm/check', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              id: llmEditId || undefined,
              kind: $('#llm-kind').value,
              name: $('#llm-name').value.trim(),
              base_url: $('#llm-base').value.trim(),
              api_key: $('#llm-key').value,
            }),
          });
          const out = await r.json();
          llmMsg = { ok: out.code === 0, text: out.msg };
          if (out.code === 0 && out.data) llmEditId = out.data.id;
        } catch (err) {
          llmMsg = { ok: false, text: '请求失败：' + err };
        }
        views.llm();
      });

      const tlBox = $('#llm-timeout-local');
      if (tlBox) tlBox.addEventListener('change', async () => {
        const want = tlBox.checked;
        try {
          const r = await fetch('/local/api/llm/timeout-local', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ enabled: want }),
          });
          const out = await r.json();
          llmMsg = { ok: out.code === 0, text: out.msg };
          if (out.code !== 0) tlBox.checked = !want;   // 保存失败拨回去
        } catch (err) {
          llmMsg = { ok: false, text: '请求失败：' + err };
          tlBox.checked = !want;
        }
        views.llm();
      });

      $('#view-llm').querySelectorAll('button[data-llm]').forEach((b) => {
        b.addEventListener('click', async () => {
          const act = b.dataset.llm;
          const id = Number(b.dataset.id);
          if (act === 'cancel') { llmEditId = null; llmMsg = null; return views.llm(); }
          if (act === 'edit') { llmEditId = id; llmMsg = null; return views.llm(); }
          if (act === 'delete' && !window.confirm('删除这条接入？模型列表会一起清掉。')) return;
          const url = act === 'check' ? '/local/api/llm/check'
            : act === 'delete' ? '/local/api/llm/delete' : '/local/api/llm/select';
          const body = act === 'use' ? { id, model: b.dataset.model } : { id };
          try {
            const r = await fetch(url, {
              method: 'POST', headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify(body),
            });
            const out = await r.json();
            llmMsg = { ok: out.code === 0, text: out.msg };
            if (act === 'delete') llmEditId = null;
          } catch (err) {
            llmMsg = { ok: false, text: '请求失败：' + err };
          }
          views.llm();
        });
      });
    },

    async draw() {
      const [d, v, tplOut] = await Promise.all([
        j('/local/api/image-models'),
        j('/local/api/video-models').catch(() => ({ presets: [], providers: [], models: [] })),
        j('/local/api/templates').catch(() => null)]);
      const vidPresets = v.presets || [];
      const vidProvs = v.providers || [];
      const vidModels = v.models || [];
      const editingVid = vidProvs.find((p) => Number(p.id) === Number(vidEditId)) || null;
      const vidKind = editingVid ? editingVid.kind : ((vidPresets[0] || {}).key || 'openai');
      const vidPreset = vidPresets.find((p) => p.key === vidKind) || {};
      const tplData = (tplOut && tplOut.data) || {};
      const tplRecipes = tplData.recipes || [];
      const tplFiles = tplData.templateFiles || [];
      const tplDir = tplData.templateDir || 'json';
      const presets = d.presets || [];
      const provs = d.providers || [];
      const allModels = d.models || [];
      const provModels = allModels.filter((m) => Number(m.providerId) > 0);
      const works = d.works || [];
      const editing = provs.find((p) => Number(p.id) === Number(imgEditId)) || null;
      const kind = editing ? editing.kind : ((presets[0] || {}).key || 'openai');
      const preset = presets.find((p) => p.key === kind) || {};
      const def = allModels.find((m) => m.isDefault);
      const badge = (s) => (s === 'ok' ? '<span class="badge b2">已连通</span>'
        : s === 'error' ? '<span class="badge b5">失败</span>'
          : '<span class="badge b0">未检测</span>');

      // 模板适配报告：告诉用户哪些模板进来了、哪些被裁了
      const tplTag = (text, cls) => `<span class="badge ${cls}">${esc(text)}</span>`;
      const tplNames = (rows) => (rows || [])
        .map((x) => `<b class="admin">${esc(x.name)}</b>`).join('、');
      const tplReportHtml = (rep) => (!rep ? '' : `
        <div class="tpl-report">
          <div class="small">${tplTag('格式 ' + rep.format, 'b0')}
            ${tplTag('来源 ' + rep.source, 'b0')}
            ${tplTag('分类 ' + rep.categories, 'b0')}
            ${tplTag('模板 ' + rep.recipes, 'b0')}</div>
          ${(rep.added || []).length ? `<p class="small">新增：${tplNames(rep.added)}</p>` : ''}
          ${(rep.replaced || []).length ? `<p class="small">覆盖：${tplNames(rep.replaced)}</p>` : ''}
          ${(rep.skipped || []).length ? `<p class="small">跳过：${(rep.skipped || [])
            .map((x) => `${esc(x.name)}（${esc(x.reason)}）`).join('；')}</p>` : ''}
          ${(rep.warnings || []).length ? `<ul class="tpl-notes">${(rep.warnings || [])
            .slice(0, 12).map((w) => `<li>${esc(w)}</li>`).join('')}</ul>` : ''}
        </div>`);

      $('#view-draw').innerHTML = `
        <div class="cards">
          <div class="card"><div class="k">绘图接入</div><div class="v">${provs.length}</div></div>
          <div class="card"><div class="k">可选绘图模型</div><div class="v">${allModels.length}</div></div>
          <div class="card"><div class="k">默认绘图模型</div>
            <div class="v" style="font-size:16px">${esc(def ? def.modelName : '本地占位绘图')}</div></div>
          <div class="card"><div class="k">已生成作品</div><div class="v">${Number(d.workCount || works.length)}</div></div>
        </div>

        <div class="panel">
          <h3>${editing ? '编辑绘图接入 #' + editing.id : '新增绘图接入'}
            <span class="muted">· OpenAI 兼容 /images/generations</span></h3>
          ${imgMsg ? `<div class="alert ${imgMsg.ok ? 'ok' : 'err'}">${esc(imgMsg.text)}</div>` : ''}
          <form id="imgForm" class="llm-form">
            <label>平台预设
              <select id="img-kind">${presets.map((p) =>
                `<option value="${esc(p.key)}"${p.key === kind ? ' selected' : ''}>${esc(p.label)}</option>`).join('')}</select>
            </label>
            <label>名称
              <input id="img-name" placeholder="如：我的绘图中转站" value="${esc(editing ? editing.name : '')}">
            </label>
            <label>API 地址（base_url）
              <input id="img-base" placeholder="${esc((editing && editing.base_url) || preset.base_url || 'https://api.openai.com/v1')}"
                value="${esc(editing ? editing.base_url : (preset.base_url || ''))}">
            </label>
            <label>API 密钥（apikey）
              <input id="img-key" type="password" autocomplete="new-password"
                placeholder="${editing && editing.apiKeySet ? '已保存 ' + esc(editing.apiKeyMasked) + '，留空表示不改' : 'sk-…'}">
            </label>
            <label>可选尺寸（逗号分隔）
              <input id="img-sizes" placeholder="1024x1024,1024x1536,1536x1024"
                value="${esc(editing ? (editing.sizes || []).join(',') : ((preset.sizes || []).join(',')))}">
            </label>
            <div class="llm-actions">
              <button type="submit">保存并检测模型</button>
              ${editing ? '<button type="button" class="ghost" data-img="cancel">取消编辑</button>' : ''}
            </div>
          </form>
          <p class="muted small">${esc(preset.hint || '')} —— 数据库里只存 API 地址、密钥、模型列表与尺寸；
            检测走 <code>GET {base_url}/models</code>，生成走 <code>POST {base_url}/images/generations</code>。
            没接真实模型时，绘图页会用「本地占位绘图」生成渐变图，保证流程可用。</p>
        </div>

        <div class="panel"><h3>已保存的绘图接入（${provs.length}）</h3>
          ${table(['ID', '预设', '名称', 'API 地址', '密钥', '状态', '模型数', '默认', '操作'], provs, (r) => `
            <tr><td>${r.id}</td><td class="muted">${esc((r.preset || {}).label || r.kind)}</td>
             <td>${esc(r.name)}</td><td class="path">${esc(r.base_url)}</td>
             <td class="mono">${esc(r.apiKeyMasked || '—')}</td>
             <td>${badge(r.status)}${r.status === 'error' && r.last_error
               ? `<div class="muted small">${esc(String(r.last_error).slice(0, 200))}</div>` : ''}</td>
             <td>${r.modelCount}</td>
             <td>${r.isDefault ? '<b class="admin">默认</b>' : '—'}</td>
             <td class="llm-ops">
               <button data-img="edit" data-id="${r.id}">编辑</button>
               <button data-img="check" data-id="${r.id}">重新检测</button>
               <button data-img="select" data-id="${r.id}">设为默认</button>
               <button data-img="delete" data-id="${r.id}">删除</button></td></tr>`)}
        </div>

        <div class="panel"><h3>检测到的绘图模型（${provModels.length}）
          <span class="muted"> · 绘图页「模型」下拉</span></h3>
          <p class="muted small">每次「检测」按 <code>GET {base_url}/models</code> 拉取，并优先保留名字里带
            dall-e / gpt-image / wanx / seedream / flux / kolors 等关键词的模型。</p>
          ${provModels.length ? table(['模型', 'rateId', '所属接入', '操作'], provModels, (r) => `
            <tr><td class="path">${esc(r.modelName)}</td><td class="muted">${r.ID}</td>
             <td class="muted">${esc(r.providerName)}</td>
             <td class="llm-ops">${r.isDefault ? '<b class="admin">当前默认</b>'
               : `<button data-img="use" data-id="${r.providerId}" data-model="${esc(r.modelId)}">设为默认模型</button>`}</td></tr>`)
            : '<div class="empty small">还没有检测到绘图模型；未接入时绘图页用「本地占位绘图」。</div>'}
        </div>

        <div class="panel">
          <h3>${editingVid ? '编辑视频接入 #' + editingVid.id : '新增视频接入'}
            <span class="muted">· OpenAI 兼容 /models 检测</span></h3>
          ${vidMsg ? `<div class="alert ${vidMsg.ok ? 'ok' : 'err'}">${esc(vidMsg.text)}</div>` : ''}
          <form id="vidForm" class="llm-form">
            <label>平台预设
              <select id="vid-kind">${vidPresets.map((p) =>
                `<option value="${esc(p.key)}"${p.key === vidKind ? ' selected' : ''}>${esc(p.label)}</option>`).join('')}</select>
            </label>
            <label>名称
              <input id="vid-name" placeholder="如：我的视频中转站" value="${esc(editingVid ? editingVid.name : '')}">
            </label>
            <label>API 地址（base_url）
              <input id="vid-base" placeholder="${esc((editingVid && editingVid.base_url) || vidPreset.base_url || 'https://api.openai.com/v1')}"
                value="${esc(editingVid ? editingVid.base_url : (vidPreset.base_url || ''))}">
            </label>
            <label>API 密钥（apikey）
              <input id="vid-key" type="password" autocomplete="new-password"
                placeholder="${editingVid && editingVid.apiKeySet ? '已保存 ' + esc(editingVid.apiKeyMasked) + '，留空表示不改' : 'sk-…'}">
            </label>
            <label>可选时长规格（逗号分隔）
              <input id="vid-specs" placeholder="5s,10s"
                value="${esc(editingVid ? (editingVid.specs || []).join(',') : ((vidPreset.specs || []).join(',')))}">
            </label>
            <div class="llm-actions">
              <button type="submit">保存并检测模型</button>
              ${editingVid ? '<button type="button" class="ghost" data-vid="cancel">取消编辑</button>' : ''}
            </div>
          </form>
          <p class="muted small">${esc(vidPreset.hint || '')} —— 数据库里只存 API 地址、密钥、模型列表与时长规格；
            检测走 <code>GET {base_url}/models</code>，并优先保留名字里带
            video / sora / veo / seedance / kling / hailuo 等关键词的模型。
            选「本地占位视频」不联网，可先跑通面板流程。</p>
        </div>

        <div class="panel"><h3>已保存的视频接入（${vidProvs.length}）</h3>
          ${vidProvs.length ? table(['ID', '预设', '名称', 'API 地址', '密钥', '状态', '模型数', '默认', '操作'], vidProvs, (r) => `
            <tr><td>${r.id}</td><td class="muted">${esc((r.preset || {}).label || r.kind)}</td>
             <td>${esc(r.name)}</td><td class="path">${esc(r.base_url)}</td>
             <td class="mono">${esc(r.apiKeyMasked || '—')}</td>
             <td>${badge(r.status)}${r.status === 'error' && r.last_error
               ? `<div class="muted small">${esc(String(r.last_error).slice(0, 200))}</div>` : ''}</td>
             <td>${r.modelCount}</td>
             <td>${r.isDefault ? '<b class="admin">默认</b>' : '—'}</td>
             <td class="llm-ops">
               <button data-vid="edit" data-id="${r.id}">编辑</button>
               <button data-vid="check" data-id="${r.id}">重新检测</button>
               <button data-vid="select" data-id="${r.id}">设为默认</button>
               <button data-vid="delete" data-id="${r.id}">删除</button></td></tr>`)
            : '<div class="empty small">还没有视频接入，先在上面新增一条。</div>'}
        </div>

        <div class="panel"><h3>检测到的视频模型（${vidModels.length}）
          <span class="muted"> · 供视频模板 / 后续生成使用</span></h3>
          ${vidModels.length ? table(['模型', 'rateId', '所属接入', '操作'], vidModels, (r) => `
            <tr><td class="path">${esc(r.modelName)}</td><td class="muted">${r.ID}</td>
             <td class="muted">${esc(r.providerName)}</td>
             <td class="llm-ops">${r.isDefault ? '<b class="admin">当前默认</b>'
               : `<button data-vid="use" data-id="${r.providerId}" data-model="${esc(r.modelId)}">设为默认模型</button>`}</td></tr>`)
            : '<div class="empty small">还没有检测到视频模型；先新增视频接入并点「保存并检测模型」。</div>'}
        </div>

        <div class="panel">
          <h3>模板接入 · 行业通用 JSON
            <span class="muted">· 当前 ${tplRecipes.length} 个模板 /
              ${Number(tplData.totalCategories || 0)} 个分类</span></h3>
          <p class="muted small">把别人做好的模板 JSON 粘进来就能用：通用格式
            <code>missai.template/v1</code>、<code>{"templates":[…]}</code> 清单、裸数组、单模板对象，
            以及 SD WebUI 参数 / ComfyUI API 工作流的尽力提取都支持。导入前按 ai-creator 前端的校验规则裁剪，
            被裁掉或归一的部分会在下面写明原因。导入后去
            <a class="admin" href="/ai-creator" target="_blank">/ai-creator</a> 选模板即可生成。</p>
          <p class="muted small">内置模板：<code>${esc(tplDir)}/</code>
            ${tplFiles.length
              ? `· ${tplFiles.length} 个文件（${tplFiles.map((f) => esc(f.file)).join('、')}）`
              : '· 目录里还没有 json 文件'}
            —— 往这里再放 <code>*.json</code>（原生模板结构）就自动算内置，
            「恢复内置」会按该目录重建，并在提示里告诉你是从哪几个文件读的。
            ${tplFiles.some((f) => f.error) ? `<br>有文件读不出来：${tplFiles
              .filter((f) => f.error).map((f) => esc(f.file + '（' + f.error + '）')).join('；')}` : ''}</p>
          ${tplMsg ? `<div class="alert ${tplMsg.ok ? 'ok' : 'err'}">${esc(tplMsg.text)}</div>` : ''}
          <form id="tplForm" class="llm-form">
            <label>导入方式
              <select id="tpl-mode">
                <option value="merge">合并（同 key 覆盖）</option>
                <option value="replace">覆盖整包</option>
              </select>
            </label>
            <label>模板 JSON 链接（可选）
              <input id="tpl-url" placeholder="https://…/template.json">
            </label>
            <label>或选本地文件
              <input id="tpl-file" type="file" accept=".json,application/json">
            </label>
            <div class="llm-actions">
              <button type="button" class="ghost" data-tpl="sample">填入示例</button>
              <button type="button" class="ghost" data-tpl="preview">校验预览</button>
              <button type="submit">导入</button>
              <button type="button" class="ghost" data-tpl="export">导出当前模板</button>
              <button type="button" class="ghost" data-tpl="reset">恢复内置</button>
            </div>
          </form>
          <label class="tpl-editor">模板 JSON
            <textarea id="tpl-json" spellcheck="false"
              placeholder='{"schema":"missai.template/v1","id":"my-tpl","title":"我的模板","prompt":{"template":"{subject}，高清细节"},"inputs":[{"key":"subject","label":"主体","type":"text","required":true}]}'>${esc(tplDraft)}</textarea>
          </label>
          ${tplReportHtml(tplReport)}
          ${tplRecipes.length ? table(['分类', '模板', 'key', '字段', '版本', '积分', '来源', '操作'],
            tplRecipes, (r) => `
            <tr><td class="muted">${esc(r.categoryName)}</td>
             <td>${esc(r.name)}</td>
             <td class="path">${esc(r.recipeKey)}</td>
             <td>${Number(r.fieldCount || 0)}</td>
             <td class="muted">v${Number(r.version || 0)}</td>
             <td class="muted">${Number(r.points || 0)}</td>
             <td>${r.origin === 'builtin' ? '<span class="badge b0">内置</span>'
               : `<span class="badge b2">导入</span>${r.author
                 ? `<div class="muted small">${esc(r.author)}</div>` : ''}`}</td>
             <td class="llm-ops"><button data-tpl="delete" data-key="${esc(r.recipeKey)}">删除</button></td></tr>`)
            : '<div class="empty small">还没有模板：点「填入示例」再「导入」，或把别人的模板 JSON 粘进来。</div>'}
        </div>

        <div class="panel"><h3>最近生成的作品（${works.length}）</h3>
          ${works.length ? table(['预览', '提示词', '模型', '尺寸', '种子', '作者', '时间', '操作'], works, (w) => `
            <tr><td><img class="thumb" src="${esc(w.url_key)}" alt="" onerror="this.style.opacity=.2"></td>
             <td class="path">${esc(String(w.prompt || '').slice(0, 120))}</td>
             <td class="mono">${esc(w.model)}</td><td class="muted">${esc(w.size)}</td>
             <td class="muted">${Number(w.seed) > 0 ? esc(w.seed) : '—'}</td>
             <td class="muted">${esc(w.author)}</td><td class="muted">${esc(w.created_at)}</td>
             <td class="llm-ops"><button data-img="del-work" data-id="${w.id}">删除</button></td></tr>`)
            : '<div class="empty small">还没有作品，去绘图页生成第一张。</div>'}
          <p class="muted small">绘图页：<a class="admin" href="/ai-gallery" target="_blank">/ai-gallery</a>
            —— 作品图落 <code>html/image/</code> 并登记进图片库（source=ai）。</p>
        </div>`;

      const kindSel = $('#img-kind');
      const imgPresetBases = new Set(presets.map((p) => String(p.base_url || '').trim()).filter(Boolean));
      const imgPresetLabels = new Set(presets.map((p) => String(p.label || '').trim()).filter(Boolean));
      kindSel.addEventListener('change', () => {
        const p = presets.find((x) => x.key === kindSel.value) || {};
        const base = $('#img-base');
        // 同聊天接入：预设地址要填进 value，不能只当 placeholder
        if (!base.value.trim() || imgPresetBases.has(base.value.trim())) {
          base.value = p.base_url || '';
        }
        base.placeholder = p.base_url || '';
        const nm = $('#img-name');
        if (!nm.value.trim() || imgPresetLabels.has(nm.value.trim())) nm.value = p.label || '';
        const sizes = $('#img-sizes');
        if (!sizes.value.trim()) sizes.value = (p.sizes || []).join(',');
      });

      $('#imgForm').addEventListener('submit', async (e) => {
        e.preventDefault();
        const btn = e.target.querySelector('button[type=submit]');
        btn.disabled = true;
        btn.textContent = '检测中…';
        try {
          const r = await fetch('/local/api/image-models/check', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              id: imgEditId || undefined,
              kind: $('#img-kind').value,
              name: $('#img-name').value.trim(),
              base_url: $('#img-base').value.trim(),
              api_key: $('#img-key').value,
              sizes: $('#img-sizes').value.trim(),
            }),
          });
          const out = await r.json();
          imgMsg = { ok: out.code === 0, text: out.msg };
          if (out.code === 0 && out.data) imgEditId = out.data.id;
        } catch (err) {
          imgMsg = { ok: false, text: '请求失败：' + err };
        }
        views.draw();
      });

      $('#view-draw').querySelectorAll('button[data-img]').forEach((b) => {
        b.addEventListener('click', async () => {
          const act = b.dataset.img;
          const id = Number(b.dataset.id);
          if (act === 'cancel') { imgEditId = null; imgMsg = null; return views.draw(); }
          if (act === 'edit') { imgEditId = id; imgMsg = null; return views.draw(); }
          if (act === 'delete' && !window.confirm('删除这条绘图接入？模型列表会一起清掉。')) return;
          if (act === 'del-work' && !window.confirm('删除这张作品记录？图片文件会保留在图片库里。')) return;
          const url = act === 'check' ? '/local/api/image-models/check'
            : act === 'delete' ? '/local/api/image-models/delete'
              : act === 'del-work' ? '/local/api/image/works/delete'
                : '/local/api/image-models/select';
          const body = act === 'use' ? { id, model: b.dataset.model } : { id };
          try {
            const r = await fetch(url, {
              method: 'POST', headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify(body),
            });
            const out = await r.json();
            imgMsg = { ok: out.code === 0, text: out.msg };
            if (act === 'delete') imgEditId = null;
          } catch (err) {
            imgMsg = { ok: false, text: '请求失败：' + err };
          }
          views.draw();
        });
      });

      // ---- 视频接入：预设切换 / 保存并检测 / 编辑 / 删除 / 设为默认
      const vidKindSel = $('#vid-kind');
      const vidPresetBases = new Set(vidPresets.map((p) => String(p.base_url || '').trim()).filter(Boolean));
      const vidPresetLabels = new Set(vidPresets.map((p) => String(p.label || '').trim()).filter(Boolean));
      vidKindSel.addEventListener('change', () => {
        const p = vidPresets.find((x) => x.key === vidKindSel.value) || {};
        const base = $('#vid-base');
        if (!base.value.trim() || vidPresetBases.has(base.value.trim())) {
          base.value = p.base_url || '';
        }
        base.placeholder = p.base_url || '';
        const nm = $('#vid-name');
        if (!nm.value.trim() || vidPresetLabels.has(nm.value.trim())) nm.value = p.label || '';
        const specs = $('#vid-specs');
        if (!specs.value.trim()) specs.value = (p.specs || []).join(',');
      });

      $('#vidForm').addEventListener('submit', async (e) => {
        e.preventDefault();
        const btn = e.target.querySelector('button[type=submit]');
        btn.disabled = true;
        btn.textContent = '检测中…';
        try {
          const r = await fetch('/local/api/video-models/check', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              id: vidEditId || undefined,
              kind: $('#vid-kind').value,
              name: $('#vid-name').value.trim(),
              base_url: $('#vid-base').value.trim(),
              api_key: $('#vid-key').value,
              specs: $('#vid-specs').value.trim(),
            }),
          });
          const out = await r.json();
          vidMsg = { ok: out.code === 0, text: out.msg };
          if (out.code === 0 && out.data) vidEditId = out.data.id;
        } catch (err) {
          vidMsg = { ok: false, text: '请求失败：' + err };
        }
        views.draw();
      });

      $('#view-draw').querySelectorAll('button[data-vid]').forEach((b) => {
        b.addEventListener('click', async () => {
          const act = b.dataset.vid;
          const id = Number(b.dataset.id);
          if (act === 'cancel') { vidEditId = null; vidMsg = null; return views.draw(); }
          if (act === 'edit') { vidEditId = id; vidMsg = null; return views.draw(); }
          if (act === 'delete' && !window.confirm('删除这条视频接入？模型列表会一起清掉。')) return;
          const url = act === 'check' ? '/local/api/video-models/check'
            : act === 'delete' ? '/local/api/video-models/delete'
              : '/local/api/video-models/select';
          const body = act === 'use' ? { id, model: b.dataset.model } : { id };
          try {
            const r = await fetch(url, {
              method: 'POST', headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify(body),
            });
            const out = await r.json();
            vidMsg = { ok: out.code === 0, text: out.msg };
            if (act === 'delete') vidEditId = null;
          } catch (err) {
            vidMsg = { ok: false, text: '请求失败：' + err };
          }
          views.draw();
        });
      });

      // ---- 模板接入：文件 / 粘贴 / 链接 → 预览 → 导入
      const tplJson = $('#tpl-json');
      tplJson.addEventListener('input', () => { tplDraft = tplJson.value; });

      const tplBody = () => {
        const body = { mode: $('#tpl-mode').value };
        const url = $('#tpl-url').value.trim();
        if (url) body.url = url;
        else if (tplDraft.trim()) body.json = tplDraft;
        return body;
      };

      $('#tpl-file').addEventListener('change', async (e) => {
        const file = e.target.files && e.target.files[0];
        if (!file) return;
        try {
          tplDraft = await file.text();
          tplReport = null;
          tplMsg = { ok: true, text: '已读入 ' + file.name + '（'
            + Math.max(1, Math.round(file.size / 1024)) + ' KB），点「校验预览」或「导入」' };
        } catch (err) {
          tplMsg = { ok: false, text: '读取文件失败：' + err };
        }
        views.draw();
      });

      $('#tplForm').addEventListener('submit', async (e) => {
        e.preventDefault();
        const body = tplBody();
        if (!body.url && !body.json) {
          tplMsg = { ok: false, text: '先粘贴模板 JSON，或填链接／选文件' };
          return views.draw();
        }
        const btn = e.target.querySelector('button[type=submit]');
        btn.disabled = true;
        btn.textContent = '导入中…';
        try {
          const out = await postJson('/local/api/templates', body);
          tplMsg = { ok: out.code === 0, text: out.msg };
          tplReport = (out.data && out.data.report) || null;
          if (out.code === 0) tplDraft = '';
        } catch (err) {
          tplMsg = { ok: false, text: '请求失败：' + err };
        }
        views.draw();
      });

      $('#view-draw').querySelectorAll('button[data-tpl]').forEach((b) => {
        b.addEventListener('click', async () => {
          const act = b.dataset.tpl;
          const body = tplBody();
          try {
            if (act === 'sample') {
              const out = await j('/local/api/templates/sample');
              tplDraft = JSON.stringify(out.data || out, null, 2);
              tplReport = null;
              tplMsg = { ok: true, text: '已填入示例模板，可以直接「导入」' };
              return views.draw();
            }
            if (act === 'preview') {
              if (!body.url && !body.json) {
                tplMsg = { ok: false, text: '先粘贴模板 JSON，或填链接／选文件' };
                return views.draw();
              }
              const out = await postJson('/local/api/templates/preview', body);
              tplMsg = { ok: out.code === 0,
                text: out.code === 0 ? '适配完成，确认无误再点「导入」' : out.msg };
              tplReport = (out.data && out.data.report) || null;
              return views.draw();
            }
            if (act === 'export') {
              const out = await j('/local/api/templates/export');
              const text = JSON.stringify(out.data || out, null, 2);
              const a = document.createElement('a');
              a.href = URL.createObjectURL(new Blob([text], { type: 'application/json' }));
              a.download = 'ai-draw-templates.json';
              a.click();
              setTimeout(() => URL.revokeObjectURL(a.href), 4000);
              tplMsg = { ok: true, text: '已导出当前模板目录，可以直接发给别人' };
              return views.draw();
            }
            if (act === 'reset' && !window.confirm(
              '用内置模板目录（' + tplDir + '/）重建模板列表？导入的模板会一起清掉。')) return;
            if (act === 'delete' && !window.confirm('删除模板 ' + b.dataset.key + '？')) return;
            const url = act === 'reset' ? '/local/api/templates/reset'
              : act === 'delete' ? '/local/api/templates/delete'
                : '/local/api/templates';
            const out = await postJson(url, act === 'delete' ? { recipeKey: b.dataset.key } : {});
            tplMsg = { ok: out.code === 0, text: out.msg };
            if (act === 'reset' && out.data && (out.data.files || []).length) {
              const errs = out.data.files.filter((f) => f.error);
              if (errs.length) {
                tplMsg.text += '；读不出来的文件：' + errs.map((f) => f.file).join('、');
              }
            }
            tplReport = null;
          } catch (err) {
            tplMsg = { ok: false, text: '请求失败：' + err };
          }
          views.draw();
        });
      });
    },

    async works() {
      const [rows, tags, apis] = await Promise.all([
        j('/local/api/works?limit=200'), j('/local/api/tags?limit=80'),
        j('/local/api/content_apis')]);
      const brief = (s, n) => {
        const t = String(s == null ? '' : s).replace(/<[^>]+>/g, ' ')
          .replace(/\s+/g, ' ').trim();
        return t ? esc(t.slice(0, n)) : '—';
      };
      $('#view-works').innerHTML = `
        <div class="cards">
          <div class="card"><div class="k">作品</div><div class="v">${rows.length}</div></div>
          <div class="card"><div class="k">标签</div><div class="v">${tags.length}</div></div>
          <div class="card"><div class="k">内容接口</div><div class="v">${apis.length}</div></div>
        </div>
        <div class="panel"><h3>文字内容（${rows.length}）</h3>
          <p class="muted">卡片上的名称、简介、作者、评分、标签存在 <code>works</code> /
             <code>work_tags</code> 表；线上不通时按这里的文字还原接口回包。</p>
          ${table(['封面', '名称', '简介', '标签', '作者', '评分', '来源接口', '更新时间'], rows, (r) => `
            <tr><td>${r.cover_url ? `<img class="thumb" src="${esc(r.cover_url)}" onerror="this.style.opacity=.2">` : '—'}</td>
             <td class="wname">${esc(r.name) || '—'}</td>
             <td class="muted wsum">${brief(r.summary, 90)}</td>
             <td class="wtags">${(r.tags || []).map((t) => `<span class="admin">${esc(t)}</span>`).join('') || '—'}</td>
             <td class="wauthor">${esc(r.author_name) || '—'}</td>
             <td class="wnum">${r.rate != null ? Number(r.rate).toFixed(2) : '—'}</td>
             <td class="wsrc" title="${esc(r.path)}">${esc(r.path)}</td>
             <td class="muted wtime">${esc(String(r.updated_at || '').slice(0, 19).replace('T', ' '))}</td></tr>`)}
        </div>
        <div class="panel"><h3>标签统计（${tags.length} 种）</h3>
          ${table(['标签', '作品数', '最近更新'], tags, (r) =>
            `<tr><td><span class="admin">${esc(r.tag)}</span></td><td>${r.works}</td>
             <td class="muted">${esc(r.updated_at)}</td></tr>`)}</div>
        <div class="panel"><h3>内容接口（离线兜底）</h3>
          ${table(['接口', '形状', '条目', '载荷', '最近抓取'], apis, (r) =>
            `<tr><td class="path">${esc(r.path)}</td><td class="mono muted">${esc(r.shape)}</td>
             <td>${r.items}</td>
             <td class="muted">${r.payload_bytes == null ? '—'
               : Math.max(1, Math.round(r.payload_bytes / 1024)) + ' KB'}</td>
             <td class="muted">${esc(r.fetched_at)}</td></tr>`)}</div>`;
    },

    async creation() {
      const root = $('#view-creation');
      if (!root) return;
      root.innerHTML = '<div class="db-loading"><span class="spinner"></span> 正在读取创作数据…</div>';
      const qs = new URLSearchParams({
        q: cdQ, status: cdStatus, userId: String(cdUser),
        page: String(cdPage), pageSize: String(cdPageSize),
      });
      let ov, page;
      try {
        [ov, page] = await Promise.all([
          j('/local/api/creation/overview'),
          j('/local/api/creation/works?' + qs.toString()),
        ]);
      } catch (err) {
        root.innerHTML = `<div class="panel"><div class="empty">加载失败：${esc(err.message)}</div></div>`;
        return;
      }
      const users = ov.users || [];
      const rows = page.list || [];
      const total = Number(page.total || 0);
      const pages = Number(page.pages || 1);
      const badgeOf = (s) => ({
        draft: '<span class="badge b0">草稿</span>',
        published: '<span class="badge b2">已发布</span>',
        scheduled: '<span class="badge b4">定时发布</span>',
        archived: '<span class="badge b0">已归档</span>',
      }[s] || `<span class="badge b0">${esc(s || '—')}</span>`);
      const userOpts = ['<option value="0">按文件里的 userId / 当前账号</option>']
        .concat(users.map((u) => `<option value="${u.id}"${Number(cdTarget) === Number(u.id)
          ? ' selected' : ''}>${esc(u.nick_name || u.user_name)}（${u.works} 个作品）</option>`))
        .join('');
      const filterOpts = ['<option value="0">全部作者</option>']
        .concat(users.filter((u) => Number(u.works) > 0).map((u) =>
          `<option value="${u.id}"${Number(cdUser) === Number(u.id) ? ' selected' : ''}>
            ${esc(u.nick_name || u.user_name)}（${u.works}）</option>`))
        .join('');
      const statusOpts = [['', '全部状态'], ['draft', '草稿'], ['published', '已发布'],
        ['scheduled', '定时发布'], ['archived', '已归档']].map(([v, t]) =>
        `<option value="${v}"${cdStatus === v ? ' selected' : ''}>${t}</option>`).join('');
      const preview = cdPreview;
      const pvHtml = preview ? `
        <div class="alert ${preview.works ? 'ok' : 'err'}">将写入：${preview.works} 个作品 ·
          ${preview.entries} 条世界书（共 ${preview.total} 条记录）</div>
        ${(preview.warnings || []).length ? `<div class="alert err">
          ${preview.warnings.map((w) => esc(w)).join('<br>')}</div>` : ''}
        ${(preview.items || []).length ? `<table class="cd-wb"><thead><tr>
          <th>characterId</th><th>标题</th><th>内容</th><th>状态</th><th>世界书</th>
          </tr></thead><tbody>${preview.items.map((it) => `<tr>
            <td class="mono">${esc(it.characterId)}</td><td>${esc(it.title)}</td>
            <td>${it.body ? '作品整包' : '仅世界书'}</td><td class="muted">${esc(it.status)}</td>
            <td>${it.worldbook}</td></tr>`).join('')}</tbody></table>` : ''}` : '';

      root.innerHTML = `
        <div class="cards">
          <div class="card"><div class="k">作品</div><div class="v">${ov.works || 0}</div></div>
          <div class="card"><div class="k">已发布</div><div class="v">${ov.published || 0}</div></div>
          <div class="card"><div class="k">草稿 / 定时</div>
            <div class="v">${ov.draft || 0} / ${ov.scheduled || 0}</div></div>
          <div class="card"><div class="k">世界书条目</div><div class="v">${ov.entries || 0}</div></div>
          <div class="card"><div class="k">作者</div><div class="v">${ov.authors || 0}</div></div>
        </div>

        <div class="panel">
          <h3>作品 + 世界书 <span class="muted">· 数据源 <code>my_works</code> /
            <code>worldbook_entries</code></span></h3>
          <p class="muted small">创作页保存的整包就在这两张表里：列表看作品，点「查看」开整包
            （提示词 / 开场白 / 自定义世界书），右侧按钮直接导出成可再导入的 JSON。</p>
          <div class="toolbar">
            <input id="cd-q" placeholder="搜索标题 / characterId / 简介" value="${esc(cdQ)}">
            <select id="cd-status">${statusOpts}</select>
            <select id="cd-user">${filterOpts}</select>
            <button data-cd="search">搜索</button>
            <button data-cd="refresh">刷新</button>
            <span class="muted">共 ${total} 个作品 · 第 ${page.page || 1}/${pages} 页</span>
          </div>
          ${cdMsg ? `<div class="alert ${cdMsg.ok ? 'ok' : 'err'}">${esc(cdMsg.text)}</div>` : ''}
          ${table(['封面', '作品', '作者', '状态', '世界书', '更新时间', '操作'], rows, (r) => `
            <tr>
              <td>${r.coverURL
                ? `<img class="thumb" src="${esc(r.coverURL)}" onerror="this.style.opacity=.15">`
                : '<span class="muted">—</span>'}</td>
              <td><div class="cd-title">${esc(r.title) || '未命名'}</div>
                <div class="cd-id">${esc(r.characterId)}</div></td>
              <td class="nowrap">${esc(r.user.name)}</td>
              <td>${badgeOf(r.status)}</td>
              <td class="nowrap">${r.worldbookCount}
                <span class="muted small">（自定义 ${r.worldbookCustom}）</span></td>
              <td class="muted nowrap">${esc(fmtTime(r.updatedAt))}</td>
              <td class="cd-ops">
                <button data-cd="detail" data-uid="${r.userId}"
                  data-cid="${esc(r.characterId)}">查看</button>
                <button data-cd="export-full" data-uid="${r.userId}"
                  data-cid="${esc(r.characterId)}">导出</button>
                <button data-cd="export-wb" data-uid="${r.userId}"
                  data-cid="${esc(r.characterId)}">仅世界书</button>
                <button data-cd="delete" data-uid="${r.userId}"
                  data-cid="${esc(r.characterId)}">删除</button>
              </td></tr>`)}
          <div class="toolbar" style="margin-top:12px">
            <button data-cd="prev"${Number(page.page || 1) <= 1 ? ' disabled' : ''}>上一页</button>
            <button data-cd="next"${Number(page.page || 1) >= pages ? ' disabled' : ''}>下一页</button>
            <span class="muted">每页</span>
            <select id="cd-size">${[10, 20, 50, 100].map((n) =>
              `<option value="${n}"${Number(cdPageSize) === n ? ' selected' : ''}>${n}</option>`).join('')}</select>
            <span class="muted">当前筛选导出：</span>
            <button data-cd="export-all">作品 + 世界书</button>
            <button data-cd="export-wb-all">只导出世界书</button>
          </div>
        </div>

        <div class="panel">
          <h3>导入 JSON <span class="muted">· 作品信息 / 世界书</span></h3>
          <p class="muted small">支持本页导出的 <code>${esc(ov.schema || 'missai.creation/v1')}</code> 整包、
            单个作品对象（<code>characterId</code> + 作品字段），以及只含世界书的
            <code>{characterId, worldbook:[…]}</code>。导入前先「解析预览」确认会被写成什么。</p>
          <form id="cd-import-form" class="llm-form">
            <label>导入方式
              <select id="cd-mode">
                <option value="merge"${cdMode === 'merge' ? ' selected' : ''}>合并（同 characterId 覆盖字段）</option>
                <option value="replace"${cdMode === 'replace' ? ' selected' : ''}>整包覆盖（清掉该作品旧世界书）</option>
              </select>
            </label>
            <label>归属用户
              <select id="cd-target">${userOpts}</select>
            </label>
            <label>本地文件
              <input id="cd-file" type="file" accept=".json,application/json">
            </label>
            <div class="llm-actions">
              <button type="button" class="ghost" data-cd="preview">解析预览</button>
              <button type="submit">导入</button>
              <button type="button" class="ghost" data-cd="clear">清空</button>
            </div>
          </form>
          <label class="tpl-editor">作品 / 世界书 JSON
            <textarea id="cd-json" spellcheck="false"
              placeholder='{"schema":"missai.creation/v1","works":[{"characterId":"char-xxx","payload":{"name":"作品名"},"worldbook":[{"entryId":"wb_1","custom":true,"payload":{"keys":["关键词"],"content":"设定内容"}}]}]}'>${esc(cdDraft)}</textarea>
          </label>
          ${pvHtml}
        </div>`;

      const go = () => views.creation();
      root.querySelectorAll('[data-cd]').forEach((b) => b.addEventListener('click', async () => {
        const act = b.dataset.cd;
        const uid = Number(b.dataset.uid || 0);
        const cid = b.dataset.cid || '';
        if (act === 'search') {
          const el = $('#cd-q');
          cdQ = el ? el.value.trim() : '';
          cdPage = 1;
          return go();
        }
        if (act === 'refresh' || act === 'clear') {
          if (act === 'clear') { cdDraft = ''; cdPreview = null; cdMsg = null; }
          return go();
        }
        if (act === 'prev') { cdPage = Math.max(1, cdPage - 1); return go(); }
        if (act === 'next') { cdPage = Math.min(pages, cdPage + 1); return go(); }
        if (act === 'export-all' || act === 'export-wb-all') {
          return window.open('/local/api/creation/export?' + new URLSearchParams({
            userId: String(cdUser || 0),
            scope: act === 'export-wb-all' ? 'worldbook' : 'full',
          }).toString(), '_blank');
        }
        if (act === 'export-full' || act === 'export-wb') {
          return window.open('/local/api/creation/export?' + new URLSearchParams({
            userId: String(uid), characterId: cid,
            scope: act === 'export-wb' ? 'worldbook' : 'full',
          }).toString(), '_blank');
        }
        if (act === 'delete') {
          if (!window.confirm('删除这个作品和它的全部世界书？\n' + cid)) return;
          try {
            await dbPost('/local/api/creation/delete',
              { userId: uid, characterId: cid, confirm: 'yes' });
            cdMsg = { ok: true, text: '已删除 ' + cid };
            dbToast('已删除');
          } catch (err) {
            cdMsg = { ok: false, text: '删除失败：' + err.message };
            dbToast(cdMsg.text, false);
          }
          return go();
        }
        if (act === 'detail') {
          return openDetail(uid, cid);
        }
        if (act === 'preview') {
          const el = $('#cd-json');
          const text = el ? el.value.trim() : '';
          if (!text) return dbToast('先粘贴或选择 JSON 文件', false);
          try {
            const out = await postJson('/local/api/creation/preview', { json: text });
            if (out.code !== 0) {
              cdMsg = { ok: false, text: out.msg || '预览失败' };
              return go();
            }
            cdPreview = out.data;
            cdMsg = { ok: true, text: '解析完成，确认无误再点「导入」' };
          } catch (err) {
            cdMsg = { ok: false, text: '预览失败：' + err };
          }
          return go();
        }
      }));

      const statusSel = $('#cd-status');
      if (statusSel) statusSel.addEventListener('change', (e) => {
        cdStatus = e.target.value; cdPage = 1; go();
      });
      const userSel = $('#cd-user');
      if (userSel) userSel.addEventListener('change', (e) => {
        cdUser = Number(e.target.value) || 0; cdPage = 1; go();
      });
      const sizeSel = $('#cd-size');
      if (sizeSel) sizeSel.addEventListener('change', (e) => {
        cdPageSize = Number(e.target.value) || 20; cdPage = 1; go();
      });
      const modeSel = $('#cd-mode');
      if (modeSel) modeSel.addEventListener('change', (e) => { cdMode = e.target.value; });
      const targetSel = $('#cd-target');
      if (targetSel) targetSel.addEventListener('change', (e) => {
        cdTarget = Number(e.target.value) || 0;
      });
      const qEl = $('#cd-q');
      if (qEl) qEl.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') { cdQ = e.target.value.trim(); cdPage = 1; go(); }
      });
      const ta = $('#cd-json');
      if (ta) ta.addEventListener('input', () => { cdDraft = ta.value; });
      const fileEl = $('#cd-file');
      if (fileEl) fileEl.addEventListener('change', () => {
        const f = fileEl.files && fileEl.files[0];
        if (!f) return;
        if (f.size > 20 * 1024 * 1024) return dbToast('文件超过 20MB，先拆小一点', false);
        const fr = new FileReader();
        fr.onload = () => {
          cdDraft = String(fr.result || '');
          cdPreview = null;
          cdMsg = { ok: true, text: '已读入 ' + f.name + '，点「解析预览」看会写什么' };
          go();
        };
        fr.onerror = () => dbToast('读取文件失败', false);
        fr.readAsText(f, 'utf-8');
      });
      const form = $('#cd-import-form');
      if (form) form.addEventListener('submit', async (e) => {
        e.preventDefault();
        const el = $('#cd-json');
        const text = el ? el.value.trim() : '';
        if (!text) return dbToast('还没有 JSON', false);
        const btn = e.target.querySelector('button[type=submit]');
        btn.disabled = true;
        btn.textContent = '导入中…';
        try {
          const out = await postJson('/local/api/creation/import', {
            json: text, mode: cdMode, targetUserId: Number(cdTarget) || 0,
          });
          cdMsg = { ok: out.code === 0, text: out.msg || (out.code === 0 ? '导入完成' : '导入失败') };
          if (out.code === 0) { cdDraft = ''; cdPreview = null; cdPage = 1; }
          else if (out.data) cdPreview = out.data;
          dbToast(cdMsg.text, cdMsg.ok);
        } catch (err) {
          cdMsg = { ok: false, text: '导入失败：' + err };
          dbToast(cdMsg.text, false);
        }
        return go();
      });

      async function openDetail(uid2, cid2) {
        let out;
        try {
          out = await j('/local/api/creation/work?' + new URLSearchParams({
            userId: String(uid2), characterId: cid2,
          }).toString());
        } catch (err) {
          return dbToast('读取作品失败：' + err.message, false);
        }
        const d = out.data || {};
        const p = d.payload || {};
        const entries = d.worldbook || [];
        const statusText = { draft: '草稿', published: '已发布',
          scheduled: '定时发布', archived: '已归档' }[d.status] || d.status || '';
        const kv = [
          ['characterId', d.characterId],
          ['作者', (d.user || {}).name || ''],
          ['状态', statusText],
          ['标题', d.title],
          ['简介', d.summary],
          ['开场白', p.firstMes],
          ['描述', p.description],
          ['人设', p.personality],
          ['场景', p.scenario],
          ['标签', Array.isArray(p.tags) ? p.tags.join('、') : ''],
          ['创建', fmtTime(d.createdAt)],
          ['更新', fmtTime(d.updatedAt)],
        ].filter(([, v]) => v !== '' && v != null);
        const body = `
          <div class="toolbar">
            <button type="button" data-cd-modal="export">导出整包</button>
            <button type="button" data-cd-modal="export-wb">仅导出世界书</button>
            <button type="button" data-cd-modal="copy">复制完整 JSON</button>
          </div>
          <dl class="cd-kv">${kv.map(([k, v]) =>
            `<dt>${esc(k)}</dt><dd>${esc(String(v))}</dd>`).join('')}</dl>
          <h4 class="cd-h4">世界书（${entries.length} 条）</h4>
          ${entries.length ? `<table class="cd-wb"><thead><tr>
            <th>#</th><th>ID</th><th>类型</th><th>keys</th><th>内容</th><th>位置</th>
            </tr></thead><tbody>${entries.map((e, i) => {
              const ep = e.payload || {};
              let keys = ep.keys;
              if (keys && !Array.isArray(keys)) keys = keys.keys;
              return `<tr><td>${i + 1}</td>
                <td class="mono">${esc(e.entryId)}</td>
                <td>${e.custom ? '<span class="badge b4">自定义</span>'
                  : '<span class="badge b0">内置</span>'}</td>
                <td class="muted">${esc(Array.isArray(keys) ? keys.join('、') : '')}</td>
                <td class="cd-wb-content">${esc(String(ep.content || '').slice(0, 400))}</td>
                <td class="muted">${esc(e.position)}</td></tr>`;
            }).join('')}</tbody></table>`
            : '<div class="empty small">这件作品还没有世界书条目</div>'}
          <details class="cd-json-box"><summary>作品整包 JSON（payload）</summary>
            <pre class="cd-json">${esc(JSON.stringify(p, null, 2))}</pre></details>`;
        dbModalOpen('作品 · ' + (d.title || d.characterId), body,
          '<button type="button" class="btn ghost" data-modal-cancel>关闭</button>');
        const back = $('#db-modal');
        $$('[data-cd-modal]', back).forEach((b) => {
          b.onclick = async () => {
            const act = b.dataset.cdModal;
            if (act === 'export' || act === 'export-wb') {
              window.open('/local/api/creation/export?' + new URLSearchParams({
                userId: String(uid2), characterId: cid2,
                scope: act === 'export-wb' ? 'worldbook' : 'full',
              }).toString(), '_blank');
              return;
            }
            if (act === 'copy') {
              const text = JSON.stringify(d, null, 2);
              try {
                await navigator.clipboard.writeText(text);
                dbToast('已复制完整 JSON');
              } catch (err) {
                const box = document.createElement('textarea');
                box.value = text;
                document.body.appendChild(box);
                box.select();
                document.execCommand('copy');
                box.remove();
                dbToast('已复制完整 JSON');
              }
            }
          };
        });
      }
    },

    async creative() {
      const qs = new URLSearchParams({ limit: '300' });
      if (creativeQ) qs.set('q', creativeQ);
      if (creativeOpen) qs.set('open', '1');
      const list = await j('/local/api/creative?' + qs.toString());
      const pick = creativePick
        ? await j('/local/api/creative/item?id=' + creativePick).catch(() => null) : null;
      const s = list.stats || {};
      const rows = list.items || [];
      const kb = (n) => (n == null || n === '') ? '—'
        : (n > 1024 ? Math.round(n / 1024) + ' KB' : n + ' B');
      const pickSrc = pick ? (pick.promptContent || pick.pairedCssPrompt || '') : '';
      const cards = [
        ['商品', s.items], ['开源', s.open_source], ['有源码', s.with_source],
        ['评论', s.comments], ['购买', s.purchases], ['我的创意', s.user_items],
      ];
      $('#view-creative').innerHTML = `
        <div class="cards">
          ${cards.map(([k, v]) => `<div class="card"><div class="k">${k}</div>
            <div class="v">${v == null ? '—' : v}</div></div>`).join('')}
        </div>
        <div class="panel">
          <h3>创意广场 · 本地镜像（${rows.length} 件${s.last_sync ? ' · 最近更新 '
            + esc(String(s.last_sync).slice(0, 19).replace('T', ' ')) : ''}）</h3>
          <p class="muted">商品、源码、评论、购买记录都存在本地 SQLite（<code>creative_items</code> /
            <code>creative_comments</code> / <code>creative_user_items</code>）。
            页面直接读这里，只有本地缺数据或点「同步」时才回源线上。</p>
          <div class="toolbar">
            <input id="cv-q" placeholder="搜索标题 / 作者 / 商品 ID" value="${esc(creativeQ)}">
            <button data-act="search">搜索</button>
            <button data-act="toggle-open">${creativeOpen ? '✓ 只看开源' : '只看开源'}</button>
            <button data-act="sync">从线上同步</button>
            <button data-act="export-open">导出开源 JSON</button>
            <button data-act="export-all">导出全部 JSON</button>
          </div>
          ${creativeMsg ? `<div class="alert ${creativeMsg.ok ? 'ok' : 'err'}">
            ${esc(creativeMsg.text)}</div>` : ''}
          ${table(['ID', '标题 / 类型', '作者', '价格', '销量', '赞 / 踩', '状态', '源码', '来源', '更新时间', ''],
            rows, (r) => `
            <tr><td class="c-id">${r.id}</td>
              <td><div class="ctitle">${esc(r.title) || '—'}</div>
                <div class="muted small">${esc(r.item_type) || '—'}
                  ${(r.tags || []).map((t) => `<span class="admin">${esc(t)}</span>`).join('')}</div></td>
              <td class="nowrap">${esc(r.author_name) || '—'}</td>
              <td>${r.price == null ? '—' : r.price}</td>
              <td>${r.sales_count || 0}</td>
              <td class="nowrap">${r.like_count || 0} / ${r.dislike_count || 0}</td>
              <td class="nowrap">${esc(r.status) || '—'}${r.is_open_source ? ' · 开源' : ''}</td>
              <td class="nowrap">${r.source_bytes ? kb(r.source_bytes) : '<span class="muted">无</span>'}</td>
              <td class="muted nowrap">${esc(r.source)}</td>
              <td class="muted nowrap">${esc(String(r.updated_at || '').slice(0, 19).replace('T', ' '))}</td>
              <td class="cops"><button data-pick="${r.id}">查看</button>
                <a href="/local/api/creative/source?id=${r.id}">下载</a></td></tr>`)}
        </div>
        ${pick ? `<div class="panel">
          <h3>${esc(pick.title) || '商品'} · #${pick.id}</h3>
          <div class="src-head">
            <span class="badge b2">${esc(pick.itemType) || '—'}</span>
            ${pick.isOpenSource ? '<span class="badge b2">开源</span>' : '<span class="badge b0">非开源</span>'}
            <span class="muted">作者：${esc(pick.authorName) || '—'} · 价格：${pick.price || 0}
              · 更新：${esc(String(pick.updatedAt || '').slice(0, 19).replace('T', ' '))}</span>
            <button data-copy-src ${pickSrc ? '' : 'disabled'}>复制源码</button>
            <a class="dl" href="/local/api/creative/source?id=${pick.id}">下载 .txt</a>
            <button data-close-src>关闭</button>
          </div>
          ${pickSrc
            ? `<pre class="code" id="cv-src">${esc(pickSrc)}</pre>`
            : `<div class="empty">本地没有这个商品的源码正文。非开源商品只有在线上购买（或作者本人）时才会返回
                 <code>promptContent</code>；开源商品同步后会自动带源码。</div>`}
        </div>` : ''}`;

      const root = $('#view-creative');
      const input = root.querySelector('#cv-q');
      root.querySelectorAll('[data-act]').forEach((b) => b.addEventListener('click', async () => {
        const act = b.dataset.act;
        if (act === 'search') { creativeQ = (input.value || '').trim(); return views.creative(); }
        if (act === 'toggle-open') { creativeOpen = !creativeOpen; return views.creative(); }
        if (act === 'export-open') {
          return window.open('/local/api/creative/export?scope=open', '_blank');
        }
        if (act === 'export-all') {
          return window.open('/local/api/creative/export?scope=all', '_blank');
        }
        if (act === 'sync') {
          creativeMsg = { ok: false, text: '功能未开放' };
          return views.creative();
        }
      }));
      if (input) {
        input.addEventListener('keydown', (e) => {
          if (e.key === 'Enter') { creativeQ = (input.value || '').trim(); views.creative(); }
        });
      }
      root.querySelectorAll('[data-pick]').forEach((b) => b.addEventListener('click', () => {
        creativePick = Number(b.dataset.pick);
        views.creative();
      }));
      root.querySelectorAll('[data-close-src]').forEach((b) => b.addEventListener('click', () => {
        creativePick = 0;
        views.creative();
      }));
      root.querySelectorAll('[data-copy-src]').forEach((b) => b.addEventListener('click', async () => {
        const pre = root.querySelector('#cv-src');
        const text = pre ? pre.textContent : '';
        if (!text) return;
        try {
          await navigator.clipboard.writeText(text);
          creativeMsg = { ok: true, text: '源码已复制到剪贴板' };
        } catch (err) {
          const ta = document.createElement('textarea');
          ta.value = text;
          document.body.appendChild(ta);
          ta.select();
          document.execCommand('copy');
          ta.remove();
          creativeMsg = { ok: true, text: '源码已复制到剪贴板' };
        }
        views.creative();
      }));
    },
  };

  document.querySelectorAll('#tabs button').forEach((b) => {
    b.addEventListener('click', () => {
      document.querySelectorAll('#tabs button').forEach((x) => x.classList.remove('on'));
      document.querySelectorAll('.view').forEach((x) => x.classList.remove('on'));
      b.classList.add('on');
      document.getElementById('view-' + b.dataset.view).classList.add('on');
      run(b.dataset.view);
    });
  });

  function run(name) {
    const fn = views[name];
    if (!fn) return;
    fn().catch((e) => {
      const el = document.getElementById('view-' + name);
      if (el) el.innerHTML = '<div class="empty">加载失败：' + esc(e.message) + '</div>';
    });
  }

  run('database');
})();
