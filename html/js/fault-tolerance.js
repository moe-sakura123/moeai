// fault-tolerance.js —— 前端容错：内容接口 502/失败 → 空成功响应
// 库为空时，把 /api/gva/* 的失败请求改写为空数据，页面渲染空态，不再无限重试转圈。
(function () {
  "use strict";
  var origFetch = window.fetch;
  if (!origFetch) return;

  var CONTENT_PREFIX = "/api/gva/";

  function isEmptyShape(url) {
    // 对象型接口（返回单个对象而非列表）：角色详情 / 内容偏好 / 目录
    return /findNarratiumCharacter|content-preferences|\/preferences|\/catalog/i.test(url || "");
  }

  function emptyResponse(url) {
    var data;
    if (isEmptyShape(url)) {
      // 对象型接口（返回单个对象而非列表）
      data = {};
    } else {
      // 列表型接口：覆盖常见数组字段，避免组件读 .length 时崩溃
      data = {
        list: [], items: [], records: [], rows: [], works: [],
        total: 0, count: 0, page: 1, pageSize: 0, hasMore: false,
      };
    }
    var body = JSON.stringify({ code: 0, msg: "", data: data });
    return new Response(body, {
      status: 200,
      headers: { "Content-Type": "application/json", "X-Local": "empty-fallback" },
    });
  }

  window.fetch = function (input, init) {
    var req = input;
    var url = "";
    var method = "GET";
    try {
      if (typeof input === "string") {
        url = input;
        method = (init && init.method) || "GET";
      } else if (input instanceof Request) {
        url = input.url;
        method = (init && init.method) || input.method || "GET";
      }
    } catch (e) { /* ignore */ }

    var isContentGet = url.indexOf(CONTENT_PREFIX) !== -1 && String(method).toUpperCase() === "GET";

    if (!isContentGet) {
      return origFetch.apply(this, arguments);
    }

    return origFetch.apply(this, arguments).then(
      function (res) {
        // 502 / 5xx → 空成功，让前端渲染空态而非无限加载
        if (res && res.status >= 500) {
          return emptyResponse(url);
        }
        return res;
      },
      function () {
        // 网络失败 → 空成功
        return emptyResponse(url);
      }
    );
  };
})();
