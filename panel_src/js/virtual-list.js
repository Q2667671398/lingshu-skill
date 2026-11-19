/* 虚拟列表（分块懒渲染，保留卡片自然高度）。
 * 为什么不做定高窗口：记忆卡长短不一，强行定高会把内容裁掉。
 * 做法：先渲 80 条，底部放哨兵；滚动接近时再追加 80 条。
 * 对几千条记忆足够消除"一次性 innerHTML 卡死主线程"的问题。
 */
(function () {
  "use strict";
  if (window.LSVirtual) return;
  var CHUNK = 80;

  function render(containerId, rows, htmlFn, opts) {
    var box = document.getElementById(containerId);
    if (!box || !Array.isArray(rows)) return false;
    opts = opts || {};
    var chunk = opts.chunk || CHUNK;
    box.innerHTML = "";
    var i = 0, sentinel = document.createElement("div");
    sentinel.className = "ls-more";
    sentinel.style.cssText = "padding:10px 12px;color:var(--muted);font-size:12px;text-align:center";
    box.appendChild(sentinel);

    function more() {
      if (i >= rows.length) { sentinel.textContent = ""; return; }
      var frag = document.createDocumentFragment();
      var end = Math.min(i + chunk, rows.length);
      for (; i < end; i++) {
        var tpl = document.createElement("template");
        tpl.innerHTML = htmlFn(rows[i]);
        frag.appendChild(tpl.content);
      }
      box.insertBefore(frag, sentinel);
      sentinel.textContent = i < rows.length ? "继续滚动加载…（" + i + "/" + rows.length + "）" : "";
    }

    if (!box._lsObserver) {
      box._lsObserver = new IntersectionObserver(function (es) {
        if (es[0] && es[0].isIntersecting) more();
      }, { rootMargin: "600px" });
    }
    box._lsObserver.disconnect();
    box._lsObserver.observe(sentinel);
    more();
    return true;
  }

  window.LSVirtual = { render: render, chunk: CHUNK };
})();
