/* 全局搜索 / 命令面板（Ctrl/⌘ + K）。
 * 命令来源：左侧导航页 + 固定动作；输入 ≥2 字时并查 /api/search（记忆原文）。
 * 渲染一律 textContent；结果列表是静态骨架 + 文本节点。
 */
(function () {
  "use strict";
  if (window.LSCommand) return;

  var ACTIONS = [
    { label: "刷新页面", hint: "reload", run: function () { location.reload(); } },
    { label: "打开运行洞察", hint: "insights", run: function () { var d = document.getElementById("ls-dock"); if (d) { d.classList.remove("ls-collapsed"); d.scrollIntoView({ block: "nearest" }); } } },
    { label: "切换主题（深/浅/跟随）", hint: "theme", run: function () { var s = document.getElementById("ls-theme"); if (s) { s.value = s.value === "light" ? "dark" : s.value === "dark" ? "auto" : "light"; s.onchange(); } } },
    { label: "切换密度（标准/紧凑）", hint: "density", run: function () { var s = document.getElementById("ls-density"); if (s) { s.value = s.value === "compact" ? "cozy" : "compact"; s.onchange(); } } }
  ];

  var overlay = null, input = null, list = null, items = [], active = 0, searchSeq = 0;

  function build() {
    overlay = document.createElement("div");
    overlay.id = "ls-cmd";
    overlay.style.cssText =
      "position:fixed;inset:0;z-index:10000;background:rgba(0,0,0,.45);display:none;" +
      "align-items:flex-start;justify-content:center;padding-top:12vh";
    var box = document.createElement("div");
    box.style.cssText =
      "width:min(620px,92vw);background:var(--surface);border:1px solid var(--line);" +
      "border-radius:14px;box-shadow:0 24px 70px rgba(0,0,0,.5);overflow:hidden";
    var head = document.createElement("div");
    head.style.cssText = "padding:10px 12px;border-bottom:1px solid var(--line)";
    input = document.createElement("input");
    input.placeholder = "搜索记忆、跳转页面、执行动作…（Esc 关闭）";
    input.style.cssText = "width:100%;background:transparent;border:0;outline:0;color:var(--text);font-size:15px;padding:4px";
    head.appendChild(input);
    list = document.createElement("div");
    list.style.cssText = "max-height:52vh;overflow:auto;padding:6px";
    box.appendChild(head);
    box.appendChild(list);
    overlay.appendChild(box);
    overlay.addEventListener("click", function (e) { if (e.target === overlay) close(); });
    document.body.appendChild(overlay);
    input.addEventListener("input", function () { query(input.value); });
    input.addEventListener("keydown", function (e) {
      if (e.key === "Escape") { close(); return; }
      if (e.key === "ArrowDown") { e.preventDefault(); move(1); }
      else if (e.key === "ArrowUp") { e.preventDefault(); move(-1); }
      else if (e.key === "Enter") { e.preventDefault(); runActive(); }
    });
  }

  function row(text, hint, onPick) {
    var d = document.createElement("div");
    d.style.cssText = "display:flex;gap:10px;padding:8px 10px;border-radius:8px;cursor:pointer;align-items:baseline";
    var t = document.createElement("div");
    t.textContent = text;
    t.style.cssText = "flex:1;color:var(--text);font-size:13px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap";
    d.appendChild(t);
    if (hint) {
      var h = document.createElement("div");
      h.textContent = hint;
      h.style.cssText = "color:var(--faint);font-size:11px";
      d.appendChild(h);
    }
    d.addEventListener("mouseenter", function () { setActive(items.indexOf(onPick)); });
    d.addEventListener("click", function () { onPick.run(); });
    return d;
  }

  function renderList() {
    list.innerHTML = "";
    if (!items.length) {
      var e = document.createElement("div");
      e.textContent = "没有匹配项";
      e.style.cssText = "padding:12px;color:var(--muted);font-size:12px";
      list.appendChild(e);
      return;
    }
    items.forEach(function (it, i) {
      var el = row(it.label, it.hint, it);
      el.dataset.idx = String(i);
      if (i === active) el.style.background = "var(--surface2)";
      list.appendChild(el);
    });
  }

  function setActive(i) {
    active = i;
    Array.prototype.forEach.call(list.children, function (el, k) {
      el.style.background = k === i ? "var(--surface2)" : "";
    });
  }

  function move(d) {
    if (!items.length) return;
    active = (active + d + items.length) % items.length;
    setActive(active);
    var el = list.children[active];
    if (el && el.scrollIntoView) el.scrollIntoView({ block: "nearest" });
  }

  function runActive() {
    var it = items[active];
    if (it) it.run();
  }

  function navItems(q) {
    var out = [];
    document.querySelectorAll("nav button[data-tab]").forEach(function (b) {
      var label = (b.textContent || "").replace(/\s+/g, " ").trim();
      var tab = b.dataset.tab;
      out.push({
        label: "跳转：" + label, hint: "页面", text: label + " " + tab,
        run: function () { close(); if (typeof switchTab === "function") switchTab(tab); }
      });
    });
    ACTIONS.forEach(function (a) { out.push({ label: a.label, hint: a.hint, text: a.label + " " + a.hint, run: function () { close(); a.run(); } }); });
    return out.filter(function (x) { return !q || x.text.toLowerCase().indexOf(q.toLowerCase()) >= 0; });
  }

  function query(q) {
    q = (q || "").trim();
    active = 0;
    items = navItems(q);
    renderList();
    if (q.length < 2) return;
    var seq = ++searchSeq;
    if (typeof api !== "function") return;
    api("/api/search?q=" + encodeURIComponent(q) + "&k=8").then(function (rows) {
      if (seq !== searchSeq) return;
      var extra = (rows || []).map(function (r) {
        var m = r.memory || r;
        var txt = String(m.text || "").slice(0, 90);
        return {
          label: "记忆：" + txt, hint: "打开并筛选记忆", text: txt,
          run: function () {
            close();
            // 落到记忆页后把搜索框填成这条记忆的原文片段，而不是只停在列表顶部。
            // 先填再切页：switchTab 自己会拉一次列表，避免重复请求。
            var box = document.getElementById("search");
            if (box) box.value = txt.slice(0, 24);
            if (typeof switchTab === "function") switchTab("memories");
          }
        };
      });
      items = navItems(q).slice(0, 6).concat(extra);
      renderList();
    }).catch(function (err) {
      // 以前这里是空的 catch：接口一挂，列表只剩导航项，用户以为"搜不到"。
      // 现在把失败也当成一条结果摆出来，至少让人知道是搜索坏了而不是没内容。
      console.warn("命令面板搜索失败:", err);
      var tip = (err && err.message) ? err.message : "接口不可用";
      items = navItems(q).slice(0, 6).concat([{
        label: "搜索失败：" + tip, hint: "重试",
        text: "搜索失败 " + tip,
        run: function () { query(input.value); }
      }]);
      renderList();
    });
  }

  function open() {
    if (!overlay) build();
    overlay.style.display = "flex";
    input.value = "";
    query("");
    setTimeout(function () { input.focus(); }, 0);
  }

  function close() {
    if (overlay) overlay.style.display = "none";
    searchSeq++;
  }

  document.addEventListener("keydown", function (e) {
    if ((e.metaKey || e.ctrlKey) && (e.key === "k" || e.key === "K")) {
      e.preventDefault();
      if (overlay && overlay.style.display === "flex") close(); else open();
    }
  });

  window.LSCommand = { open: open, close: close };
})();
