// no-feed-cache.js —— 关掉原站前端的首页 feed 缓存，探索页每次直读本地 SQLite
// 原站把首页/探索流（含热力橱窗 最新/24H/周榜/月榜）的卡片列表缓存在浏览器
// localStorage（键前缀 missai:home-feed:v1:，5 分钟内算 fresh、最长留 24 小时），
// 首屏先渲染这份快照再发请求。所以本地库清空后，页面上仍会看到旧卡片。
// 这里把这几个键的读写截断：读永远未命中 → 前端照常走接口 → 服务端从本地库取；
// 写直接丢弃 → 浏览器里不再积累副本。其余 localStorage（登录态、UI 偏好等）不受影响。
(function () {
  "use strict";

  // 原站有两套首屏快照缓存：首页/探索流（home-feed）与游玩历史页（history-feed）。
  // 行为一致：数据只认本地数据库，浏览器里不留副本。
  var PREFIXES = ["missai:home-feed:", "missai:history-feed:"];

  function isFeedKey(k) {
    if (typeof k !== "string") return false;
    for (var i = 0; i < PREFIXES.length; i++) {
      if (k.indexOf(PREFIXES[i]) === 0) return true;
    }
    return false;
  }

  var ls;
  try {
    ls = window.localStorage;
  } catch (e) {
    return;                       // 隐私模式下访问会抛错，直接放弃（前端会走异常分支）
  }
  if (!ls || typeof ls.getItem !== "function") return;

  // 先把浏览器里已存的旧快照清掉，避免本次改动后还残留上一批数据
  try {
    var stale = [];
    for (var i = 0; i < ls.length; i++) {
      var k = ls.key(i);
      if (isFeedKey(k)) stale.push(k);
    }
    for (var j = 0; j < stale.length; j++) ls.removeItem(stale[j]);
  } catch (e) { /* 清不掉也不影响下面的截断 */ }

  var origGet = ls.getItem.bind(ls);
  var origSet = ls.setItem.bind(ls);

  ls.getItem = function (key) {
    if (isFeedKey(key)) return null;      // 未命中 → 前端不发快照，直接等接口数据
    return origGet(key);
  };
  ls.setItem = function (key, value) {
    if (isFeedKey(key)) return;           // 不再落盘新快照
    return origSet(key, value);
  };

  window.__localNoFeedCache = true;       // 自测/排障用标记
})();
