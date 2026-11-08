/* 本地控制台入口 —— 由服务端注入到站点页面里
 *
 * 在原站左侧导航栏末尾加一条「控制台」按钮，指向 /app/console。
 * 页面 HTML 保持原样（不改磁盘文件），只在回包时插一个 script；
 * React 重新渲染会把外来节点清掉，所以用 MutationObserver 持续补回。
 */
(function () {
  'use strict';

  var MARK = "data-local-console-entry";
  var LABEL = "控制台";
  var HREF = "/app/console";
  var ICON = '<rect width="7" height="9" x="3" y="3" rx="1"></rect>' +
             '<rect width="7" height="5" x="14" y="3" rx="1"></rect>' +
             '<rect width="7" height="9" x="14" y="12" rx="1"></rect>' +
             '<rect width="7" height="5" x="3" y="16" rx="1"></rect>';

  function build(model) {
    var a = model.cloneNode(true);
    a.setAttribute("href", HREF);
    a.setAttribute("title", LABEL);
    a.removeAttribute("target");
    a.removeAttribute(MARK);
    a.setAttribute(MARK, "1");

    // 标签 / tooltip 文本全部换成「控制台」（保持原有 class 不动）
    var walker = document.createTreeWalker(a, NodeFilter.SHOW_TEXT, null);
    var node;
    while ((node = walker.nextNode())) {
      if (node.nodeValue && node.nodeValue.trim()) node.nodeValue = LABEL;
    }

    // 图标换成「控制台」的方块图标，保留原 svg 的尺寸与描边属性
    var svg = a.querySelector("svg");
    if (svg) {
      svg.setAttribute("class", "lucide lucide-layout-dashboard");
      svg.innerHTML = ICON;
    }
    return a;
  }

  function ensure() {
    var aside = document.querySelector("aside");
    if (!aside) return;
    var groups = aside.querySelectorAll("nav > div.space-y-1");
    if (!groups.length) return;
    var group = groups[0];
    if (group.querySelector("[" + MARK + "]")) return;
    var model = group.querySelector("a[href]");
    if (!model) return;
    group.appendChild(build(model));
  }

  var timer = null;
  function schedule() {
    if (timer) return;
    timer = setTimeout(function () { timer = null; ensure(); }, 50);
  }

  ensure();
  if (window.MutationObserver) {
    new MutationObserver(schedule).observe(document.documentElement, {
      childList: true, subtree: true
    });
  }
  window.addEventListener("popstate", schedule);
  document.addEventListener("click", schedule, true);
})();
