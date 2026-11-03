/* publish-jump.js：创作页发布成功后自动跳回作品列表刷新。
   配合 db.list_my_works 的 isPublished 修复，归墟等作品发布后会在列表显示"已发布"。 */
(function () {
  if (window.__publishJumpInstalled) return;
  window.__publishJumpInstalled = true;
  var PUB = /\/work\/publish(\?|$)/;
  function maybeJump() {
    // 只在创作编辑页触发（不干扰 worldbook / cg 子页，也不在其他页面触发）
    if (!/^\/creation(\/|$)/.test(location.pathname)) return;
    if (location.pathname.indexOf("/worldbook") >= 0 || location.pathname.indexOf("/cg") >= 0) return;
    setTimeout(function () {
      if (/^\/creation(\/|$)/.test(location.pathname)) {
        location.href = "/creation?pub=1&t=" + Date.now();
      }
    }, 1000);
  }
  var orig = window.fetch;
  if (typeof orig === "function") {
    window.fetch = function () {
      var args = arguments;
      var p = orig.apply(this, args);
      return p.then(function (resp) {
        try {
          var u = typeof args[0] === "string" ? args[0]
            : (args[0] && (args[0].url || "")) || "";
          if (PUB.test(u) && resp && resp.status === 200) {
            resp.clone().json().then(function (j) {
              if (j && j.code === 0) maybeJump();
            }).catch(function () {});
          }
        } catch (e) {}
        return resp;
      });
    };
  }
})();
