/* 隐藏首页顶部的广告轮播条 —— 由服务端注入到站点页面里
 *
 * 原站登录后会在搜索栏上方轮播促销条（新人专享月卡 / APP 下载 / 签到活动…），
 * 桌面端与移动端是两个不同 class 的容器。页面 HTML 保持原样（不改磁盘文件），
 * 只在回包时插一个 script，用样式规则把它整体隐藏 —— 不删节点，避免和 React 抢 DOM。
 */
(function () {
  "use strict";

  var MARK = "data-local-hide-ads";

  // 1) 精确规则：轮播条的两个容器（桌面 lg 版 / 移动版）
  var css = [
    /* 桌面：div.hidden.lg:block.min-h-[100px] */
    "div.lg\\:block.min-h-\\[100px\\],",
    /* 移动：div.lg:hidden.px-4.min-h-[84px] */
    "div.lg\\:hidden.min-h-\\[84px\\],",
    /* 兜底：sweep() 按促销文案打过标的节点 */
    "[data-local-ad]",
    " { display: none !important; }"
  ].join("\n");

  function ensureStyle() {
    if (document.getElementById(MARK)) return;
    var s = document.createElement("style");
    s.id = MARK;
    s.textContent = css;
    (document.head || document.documentElement).appendChild(s);
  }

  // 2) 兜底：文案里带促销关键词的块（上游换 class 也能盖住）
  var KEY = /(新人专享|立即抢购|限时一次|每日签到领积分|限购一次|超值月卡|签到基础积分翻倍)/;

  function sweep() {
    var walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, null);
    var node;
    while ((node = walker.nextNode())) {
      var text = node.nodeValue || "";
      if (!KEY.test(text)) continue;
      var el = node.parentElement;
      if (!el) continue;
      // 往上找促销卡片（最多 6 层），交给 CSS 隐藏
      for (var i = 0; i < 6 && el; i++) {
        var cls = typeof el.className === "string" ? el.className : "";
        if (/rounded-xl/.test(cls) || /min-h-\[/.test(cls)) {
          el.setAttribute("data-local-ad", "1");
          break;
        }
        el = el.parentElement;
      }
    }
  }

  var timer = null;
  function schedule() {
    if (timer) return;
    timer = setTimeout(function () {
      timer = null;
      ensureStyle();
      sweep();
    }, 60);
  }

  ensureStyle();
  sweep();
  if (window.MutationObserver) {
    new MutationObserver(schedule).observe(document.documentElement, {
      childList: true, subtree: true, characterData: true
    });
  }
  window.addEventListener("popstate", schedule);
})();
