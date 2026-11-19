
(function(){
  "use strict";
  var KEY = "lingshu.panel.prefs";
  var prefs = {theme:"auto", density:"cozy", refresh:60, open:true};
  try { Object.assign(prefs, JSON.parse(localStorage.getItem(KEY) || "{}")); } catch(e){}
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
    '<button id="ls-toggle" title="收起/展开">－</button></div>' +
    '<div class="ls-body">' +
    '<div class="ls-kpis" id="ls-kpis"><div class="ls-empty">加载中…</div></div>' +
    '<div id="ls-rows"></div>' +
    '<div class="ls-tools">' +
    '<select id="ls-theme"><option value="auto">跟随系统</option><option value="dark">深色</option><option value="light">浅色</option></select>' +
    '<select id="ls-density"><option value="cozy">标准密度</option><option value="compact">紧凑密度</option></select>' +
    '<select id="ls-refresh-interval"><option value="0">不自动刷新</option><option value="30">30 秒</option><option value="60">60 秒</option><option value="300">5 分钟</option></select>' +
    '</div></div>';
  document.body.appendChild(dock);

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
