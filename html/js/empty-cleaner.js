// empty-cleaner.js —— 把"空数据却仍在显示加载骨架"的内容区替换为干净空态
// 登录态专属接口（如热力橱窗 hot_chart）在未登录/无数据时会永久显示骨架占位，
// 这里在页面稳定后检测：若区块只有骨架(animate-pulse)且无真实卡片，则改为"暂无内容"。
(function () {
  "use strict";

  function cleanSection(scope) {
    var s = document.querySelector('section[data-home-anchor-scope="' + scope + '"]');
    if (!s) return;
    // 内容区 = 区块最后一个子 div（第一个是标题栏）
    var content = s.querySelector(':scope > div:last-child') || s;
    // 有真实卡片（图片/链接）就不动
    if (content.querySelector('img[src], a[href]')) return;
    var pulses = content.querySelectorAll('[class*="animate-pulse"]');
    if (!pulses.length) return;
    // 全是骨架 → 替换为干净空态
    content.innerHTML = '<div class="px-4 py-12 text-center text-sm text-slate-500">暂无内容，敬请期待</div>';
  }

  function run() {
    setTimeout(function () {
      cleanSection("hot-chart");
    }, 3500);
  }

  if (document.readyState === "complete") run();
  else window.addEventListener("load", run);
})();
