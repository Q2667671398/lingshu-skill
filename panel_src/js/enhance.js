
(function(){
  "use strict";
  var KEY = "lingshu.panel.prefs";
  var prefs = {theme:"auto", density:"cozy", refresh:60, open:true};
  var firstVisit = false;
  try { firstVisit = !localStorage.getItem(KEY); } catch(e){}
  try { Object.assign(prefs, JSON.parse(localStorage.getItem(KEY) || "{}")); } catch(e){}
  // 窄屏（手机/小窗）默认收起：展开态要占掉大半个视口，把正文全遮住。
  // 只在"用户从没调过"时这么定，之后尊重他自己的选择。
  if (firstVisit && window.innerWidth && window.innerWidth <= 760) prefs.open = false;
  function save(){ try{ localStorage.setItem(KEY, JSON.stringify(prefs)); }catch(e){} }
  function apply(){
    var t = prefs.theme;
    if (t === "auto") t = (window.matchMedia && matchMedia("(prefers-color-scheme: light)").matches) ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", t);
    document.documentElement.setAttribute("data-density", prefs.density === "compact" ? "compact" : "cozy");
  }
  apply();

  var dock = document.createElement("section");
  dock.id = "ls-dock";
  if (!prefs.open) dock.className = "ls-collapsed";
  dock.innerHTML =
    '<div class="ls-head"><span class="ls-dot" id="ls-dot"></span><b>运行洞察</b>' +
    '<button id="ls-refresh" title="立即刷新">刷新</button>' +
    '<button id="ls-toggle" title="收起/展开（双击标题栏同样可收起）">－</button></div>' +
    '<div class="ls-body">' +
    '<div class="ls-kpis" id="ls-kpis"><div class="ls-empty">加载中…</div></div>' +
    '<div id="ls-rows"></div>' +
    '<div class="ls-tools">' +
    '<select id="ls-theme"><option value="auto">跟随系统</option><option value="dark">深色</option><option value="light">浅色</option></select>' +
    '<select id="ls-density"><option value="cozy">标准密度</option><option value="compact">紧凑密度</option></select>' +
    '<select id="ls-refresh-interval"><option value="0">不自动刷新</option><option value="30">30 秒</option><option value="60">60 秒</option><option value="300">5 分钟</option></select>' +
    '</div></div>';
  document.body.appendChild(dock);

  /* ---------------------------------------------------------------- 拖拽 + 位置记忆
     浮窗是 position:fixed，默认停在右下角，会盖住正文（尤其是窄屏和长卡片）。
     这里把标题栏做成真的可拖：拖走、松手记位置、下次打开还在那；也能双击收起。
     位置一律夹在视口内，避免拖到屏幕外就再也抓不回来。 */
  var positioned = false;   // 是否已被换成 left/top 绝对定位（false = 还在用 CSS 贴右下角）
  function dockSize(){
    var r = dock.getBoundingClientRect();
    return {w: r.width || 340, h: r.height || 60};
  }
  function clampPos(x, y){
    var s = dockSize();
    var maxX = Math.max(0, (window.innerWidth || 0) - s.w - 4);
    var maxY = Math.max(0, (window.innerHeight || 0) - s.h - 4);
    return {x: Math.min(Math.max(0, x), maxX), y: Math.min(Math.max(0, y), maxY)};
  }
  function place(x, y){
    var p = clampPos(x, y);
    dock.style.left = p.x + "px";
    dock.style.top = p.y + "px";
    dock.style.right = "auto";
    dock.style.bottom = "auto";
    positioned = true;
    return p;
  }
  // 首次：没存过位置就**保留 CSS 的 right/bottom 定位**（内容还在加载，此时量高度会偏小，
  // 算出来的 top 会把浮窗顶出视口）；等真的开始拖了，再把当前矩形换算成 left/top。
  if (prefs.pos && isFinite(prefs.pos.x) && isFinite(prefs.pos.y)) place(prefs.pos.x, prefs.pos.y);

  var drag = null;
  var head = dock.querySelector(".ls-head");
  head.addEventListener("pointerdown", function(e){
    if (e.button !== 0 || (e.target.closest && e.target.closest("button"))) return;
    var r = dock.getBoundingClientRect();
    place(r.left, r.top);                  // 固定成 left/top（数值不变，不会跳）
    drag = {dx: e.clientX - r.left, dy: e.clientY - r.top, id: e.pointerId};
    try { head.setPointerCapture(e.pointerId); } catch (_) {}
    dock.classList.add("ls-dragging");
    // 不要在这里 preventDefault：Chrome 会随之取消 mousedown/click/dblclick 这些
    // 兼容事件，"双击标题栏收起"就永远收不到。禁止选中与滚动交给 CSS
    // （.ls-head 的 user-select:none / touch-action:none）。
  });
  head.addEventListener("pointermove", function(e){
    if (!drag || e.pointerId !== drag.id) return;
    place(e.clientX - drag.dx, e.clientY - drag.dy);
  });
  function endDrag(e){
    if (!drag || (e && e.pointerId !== drag.id)) return;
    var r = dock.getBoundingClientRect();
    var p = place(r.left, r.top);          // 松手时再夹一次：拖动中滚动/缩放都可能越界
    prefs.pos = {x: Math.round(p.x), y: Math.round(p.y)};
    save();
    dock.classList.remove("ls-dragging");
    drag = null;
  }
  head.addEventListener("pointerup", endDrag);
  head.addEventListener("pointercancel", endDrag);
  head.addEventListener("dblclick", function(e){
    if (e.target.closest && e.target.closest("button")) return;
    $("ls-toggle").onclick();
  });
  // 窗口变小/收起展开都会改变尺寸：把浮窗重新夹回视口内（位置也顺手纠正，避免下次打开跑出去）。
  // 还没被拖动过（仍由 CSS 贴右下角）就什么都不做——CSS 自己会跟着视口走。
  function reflow(){
    if (!positioned) return;
    var r = dock.getBoundingClientRect();
    var p = place(r.left, r.top);
    prefs.pos = {x: Math.round(p.x), y: Math.round(p.y)};
    save();
  }
  window.addEventListener("resize", reflow);


  var $ = function(id){ return document.getElementById(id); };
  $("ls-theme").value = prefs.theme;
  $("ls-density").value = prefs.density;
  $("ls-refresh-interval").value = String(prefs.refresh);
  $("ls-theme").onchange = function(){ prefs.theme = this.value; save(); apply(); };
  $("ls-density").onchange = function(){ prefs.density = this.value; save(); apply(); };
  $("ls-refresh-interval").onchange = function(){ prefs.refresh = Number(this.value) || 0; save(); schedule(); };
  $("ls-toggle").onclick = function(){
    prefs.open = dock.classList.toggle("ls-collapsed") ? false : true;
    this.textContent = prefs.open ? "－" : "＋"; save();
    reflow();   // 收起/展开会改变尺寸，位置要重新夹回视口
  };
  if (!prefs.open) $("ls-toggle").textContent = "＋";

  function el(tag, cls, text){
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }
  function kpi(k, v, s){
    var box = el("div","ls-kpi");
    box.appendChild(el("div","k",k));
    box.appendChild(el("div","v",v));
    if (s) box.appendChild(el("div","s",s));
    return box;
  }
  function row(k, v){
    var r = el("div","ls-row");
    r.appendChild(el("div","k",k));
    r.appendChild(el("div","v",v));
    return r;
  }
  function fmtTokens(n){ return (n == null) ? "—" : (n >= 1000 ? (n/1000).toFixed(1)+"k" : String(n)); }

  var timer = null;
  function schedule(){
    if (timer) { clearTimeout(timer); timer = null; }
    if (prefs.refresh > 0) timer = setTimeout(load, prefs.refresh * 1000);
  }

  async function load(){
    var dot = $("ls-dot"), kpis = $("ls-kpis"), rows = $("ls-rows");
    try {
      var fn = (typeof api === "function") ? function(p){ return api(p); }
                                             : function(p){ return fetch(p).then(function(r){ return r.json(); }); };
      var res = await fn("/api/insights?light=1");
      if (!res || !res.ok || !res.data) throw new Error((res && (res.error || "bad response")) || "bad response");
      var d = res.data;
      dot.className = "ls-dot" + (res.warnings && res.warnings.length ? " bad" : "");
      kpis.innerHTML = "";
      kpis.appendChild(kpi("版本", d.version || "—", "灵枢"));
      kpis.appendChild(kpi("原文库", (d.rag && d.rag.messages != null) ? d.rag.messages : "—",
                           (d.rag && d.rag.sessions != null) ? d.rag.sessions + " 个会话" : "未建库"));
      kpis.appendChild(kpi("收尾清点", (d.checkpoints && d.checkpoints.count) || 0, "个仓库有记录"));
      kpis.appendChild(kpi("群聊鉴权", d.guard ? (d.guard.enabled ? (d.guard.shadow ? "影子" : "拦截中") : "关闭") : "—",
                           d.guard && d.guard.events ? ("最近 " + d.guard.events.length + " 条") : ""));
      rows.innerHTML = "";
      if (d.rag) rows.appendChild(row("检索库", d.rag.path || "—"));
      if (d.embed) rows.appendChild(row("嵌入", (d.embed.url || "未配置（走降级向量）") +
        (d.embed.cooldown_sec ? "  · 冷却 " + d.embed.cooldown_sec + "s" : "")));
      if (d.checkpoints && d.checkpoints.last) {
        var c = d.checkpoints.last;
        rows.appendChild(row("上次收尾", (c.repo || "") + " @ " + (c.head || "") + (c.version ? " · v" + c.version : "")));
      }
      if (d.guard && d.guard.events && d.guard.events.length) {
        var e = d.guard.events[d.guard.events.length - 1];
        rows.appendChild(row("最近鉴权", (e.action || "") + " · " + (e.tool || "") + " · " + (e.sender || "")));
      }
      if (d.failures && d.failures.total) {
        var f = d.failures.top && d.failures.top[0];
        rows.appendChild(row("近24h异常", d.failures.total + " 次" +
          (f ? (" · " + f.kind + " ×" + f.count) : "")));
      }
      if (d.ab) {
        var ab = d.ab, abMode = ab.mode || "legacy";
        var abText = ab.total
          ? (ab.total + " 次 · 均值节省 " +
             (ab.mean_saved_pct == null ? "—" : ab.mean_saved_pct + "%"))
          : "未采样";
        rows.appendChild(row("A/B 样本", abText + " · 当前模式 " + abMode));
      }
      if (res.warnings && res.warnings.length) rows.appendChild(row("降级", res.warnings.join("；")));
      if (!rows.children.length) rows.appendChild(el("div","ls-empty","暂无更多数据"));
    } catch (err) {
      dot.className = "ls-dot bad";
      kpis.innerHTML = "";
      kpis.appendChild(el("div","ls-empty","读取失败：" + (err && err.message ? err.message : err)));
    }
    schedule();
  }

  $("ls-refresh").onclick = function(){ if (timer) clearTimeout(timer); load(); };
  load();
})();
