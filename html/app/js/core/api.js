/* core/api.js — 统一网络层：超时、错误归一、401、字段兜底
   所有 /app/* 页面只通过 App.api 访问接口，不允许页面里裸写 fetch。 */
(function () {
  'use strict';

  const App = (window.App = window.App || {});

  class ApiError extends Error {
    constructor(message, status, data) {
      super(message || '请求失败');
      this.name = 'ApiError';
      this.status = status || 0;
      this.data = data === undefined ? null : data;
    }
  }

  function qs(params) {
    if (!params || typeof params !== 'object') return '';
    const pairs = [];
    Object.keys(params).forEach((k) => {
      const v = params[k];
      if (v === undefined || v === null || v === '') return;
      pairs.push(encodeURIComponent(k) + '=' + encodeURIComponent(String(v)));
    });
    return pairs.length ? '?' + pairs.join('&') : '';
  }

  function normalize(res, json) {
    // 本地接口统一 {code, msg, data}；个别接口直接回对象。
    if (json && typeof json === 'object' && !Array.isArray(json) &&
        Object.prototype.hasOwnProperty.call(json, 'code')) {
      const code = Number(json.code);
      if (code !== 0 && code !== 200) {
        throw new ApiError(json.msg || ('接口返回 code=' + json.code), code || res.status, json);
      }
      return json.data === undefined ? json : json.data;
    }
    return json;
  }

  async function request(path, opts) {
    const o = opts || {};
    const timeout = o.timeout === 0 ? 0 : (o.timeout || 8000);
    const ctrl = typeof AbortController === 'function' ? new AbortController() : null;
    let timer = null;
    if (ctrl && timeout) timer = setTimeout(() => ctrl.abort(), timeout);

    const headers = Object.assign({ Accept: 'application/json' }, o.headers || {});
    let body = o.body;
    if (body !== undefined && !(body instanceof FormData) && typeof body !== 'string') {
      headers['Content-Type'] = 'application/json';
      body = JSON.stringify(body);
    }

    let res;
    try {
      res = await fetch(path + qs(o.params), {
        method: o.method || 'GET',
        headers,
        body,
        credentials: 'same-origin',
        cache: 'no-store',
        signal: ctrl ? ctrl.signal : undefined,
      });
    } catch (err) {
      if (timer) clearTimeout(timer);
      if (err && err.name === 'AbortError') throw new ApiError('请求超时，请重试', 'timeout');
      throw new ApiError('网络连接失败，请检查本地服务是否在运行', 'network');
    }
    if (timer) clearTimeout(timer);

    const text = await res.text();
    let json = null;
    if (text) {
      try { json = JSON.parse(text); } catch (e) { json = null; }
    }

    if (res.status === 401) {
      throw new ApiError((json && json.msg) || '登录已失效，请重新登录', 401, json);
    }
    if (!res.ok) {
      throw new ApiError((json && json.msg) || ('请求失败（HTTP ' + res.status + '）'), res.status, json);
    }
    if (!json) {
      throw new ApiError('接口返回格式异常（HTTP ' + res.status + '）', res.status);
    }
    return normalize(res, json);
  }

  const api = {
    ApiError,
    request,
    get(path, params, opts) {
      return request(path, Object.assign({ method: 'GET', params }, opts || {}));
    },
    post(path, body, opts) {
      return request(path, Object.assign({ method: 'POST', body: body === undefined ? {} : body }, opts || {}));
    },
    put(path, body, opts) {
      return request(path, Object.assign({ method: 'PUT', body: body === undefined ? {} : body }, opts || {}));
    },
    del(path, body, opts) {
      return request(path, Object.assign({ method: 'DELETE', body }, opts || {}));
    },
    // 文件上传（multipart）：超时放宽
    upload(path, file, fields) {
      const fd = new FormData();
      Object.keys(fields || {}).forEach((k) => fd.append(k, String(fields[k])));
      fd.append('file', file);
      return request(path, { method: 'POST', body: fd, timeout: 30000 });
    },
  };

  /* ---------------------------------------------------------- 字段兜底 */
  function isObj(v) {
    return !!v && typeof v === 'object' && !Array.isArray(v);
  }
  function pick(obj, keys, fallback) {
    const list = Array.isArray(keys) ? keys : [keys];
    for (let i = 0; i < list.length; i += 1) {
      const v = isObj(obj) ? obj[list[i]] : undefined;
      if (v !== undefined && v !== null && v !== '') return v;
    }
    return fallback;
  }
  function asList(data, keys) {
    if (Array.isArray(data)) return data;
    if (!isObj(data)) return [];
    const names = (keys && keys.length ? keys : ['list', 'records', 'items', 'rows', 'data'])
      .concat(['list', 'records', 'items', 'rows']);
    for (let i = 0; i < names.length; i += 1) {
      if (Array.isArray(data[names[i]])) return data[names[i]];
    }
    return [];
  }
  function num(v, fallback) {
    const n = Number(v);
    return Number.isFinite(n) ? n : (fallback === undefined ? 0 : fallback);
  }
  function str(v, fallback) {
    return v === undefined || v === null ? (fallback || '') : String(v);
  }

  App.api = api;
  App.ApiError = ApiError;
  App.util = { qs, isObj, pick, asList, num, str };
})();
