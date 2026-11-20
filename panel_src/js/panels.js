
/* 自定义面板管理 UI：列表面板/启用/删除/打开目录/发布/安装。
   逻辑独立成块，不动主脚本；复用全局 api()/toast()/esc()/$( alias)。 */
(function(){
  const $id = (i) => document.getElementById(i);

  function overlay(){ return `
  <div id="pm-overlay" class="hidden">
    <div id="pm-card">
      <div id="pm-tabs">
        <button data-pm-tab="installed" class="on">已安装</button>
        <button data-pm-tab="market">面板市场</button>
        <span style="flex:1"></span>
        <button id="pm-close" title="关闭">✕</button>
      </div>
      <div id="pm-body"></div>
    </div>
  </div>`;}

  // 打开覆盖层（首次点击时才注入 DOM）
  function open(){
    let ov = $id("pm-overlay");
    if (!ov){ document.body.insertAdjacentHTML("beforeend", overlay()); ov = $id("pm-overlay");
      ov.addEventListener("click", e => { if (e.target === ov) close(); });
      $id("pm-close").onclick = close;
      ov.querySelectorAll("[data-pm-tab]").forEach(b => b.onclick = () => {
        ov.querySelectorAll("[data-pm-tab]").forEach(x => x.classList.toggle("on", x === b));
        b.dataset.pmTab === "market" ? renderMarket() : renderInstalled();
      });
    }
    ov.classList.remove("hidden");
    renderInstalled();
  }
  function close(){ const ov = $id("pm-overlay"); if (ov) ov.classList.add("hidden"); }
  $id("btn-panels").onclick = open;
  // Esc 关闭：其它浮层（弹窗/命令面板）都认 Esc，这里不认会显得半截
  document.addEventListener("keydown", e => {
    if (e.key !== "Escape") return;
    const ov = $id("pm-overlay");
    if (ov && !ov.classList.contains("hidden")) close();
  });

  async function ppost(action, extra){
    return api("/api/panels", {method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify(Object.assign({action}, extra || {}))});
  }

  // ---------------- 已安装标签页 ----------------
  async function renderInstalled(){
    const body = $id("pm-body");
    body.innerHTML = '<div class="empty">加载中…</div>';
    try{
      const d = await api("/api/panels");
      const rows = (d.panels || []).map(p => {
        const isActive = p.name === d.active || (p.name === "default" && d.active === "default");
        const act = isActive ? '<span class="pm-tag">当前使用</span>' : "";
        const src = p.source === "builtin" ? '<span class="pm-tag">内置</span>' : "";
        const err = p.valid === false ? `<span class="pm-tag err">损坏</span>` : "";
        let btns = "";
        if (p.name !== "default" && p.valid !== false && !isActive)
          btns += `<button class="btn mini" data-pm-act="activate" data-id="${esc(p.name)}">启用</button>`;
        if (p.name !== "default" && p.valid !== false)
          btns += `<button class="btn mini" data-pm-act="upload" data-id="${esc(p.name)}">上传到市场</button>`;
        if (p.name !== "default")
          btns += `<button class="btn mini" data-pm-act="delete" data-id="${esc(p.name)}"
            style="background:rgba(201,107,95,.14);color:var(--red-text)">删除</button>`;
        return `<div class="pm-item">
          <div class="grow"><div class="nm">${esc(p.title)}<small>v${esc(p.version)} · ${esc(p.name)}</small>
            ${act}${src}${err}</div>
          <div class="ds">${esc(p.invalid_reason || p.description || "")}</div></div>${btns}</div>`;
      }).join("");
      body.innerHTML = `
        <div class="pm-toolbar">
          <span class="sub" style="flex:1;word-break:break-all">已安装目录：${esc(d.installed_dir)}</span>
          <button class="btn mini" data-pm-act="open-folder">打开文件夹</button>
          <button class="btn mini" data-pm-act="scaffold">新建面板（脚手架）</button>
          <button class="btn mini" data-pm-act="refresh">刷新</button>
        </div>
        ${rows || '<div class="empty">还没有已安装的自定义面板</div>'}`;
    }catch(e){ body.innerHTML = `<div class="empty">加载失败：${esc(e.message)}</div>`; }
  }

  // ---------------- 面板市场标签页（本机共享库 + 云端市场 Gitee/GitHub） ----------------
  let cloudCfg = null; // 云端配置状态缓存（status）
  async function renderMarket(){
    const body = $id("pm-body");
    body.innerHTML = '<div class="empty">加载中…</div>';
    try{
      const d = await api("/api/panels/market");
      const inst = await api("/api/panels");
      const installedNames = new Set((inst.panels || [])
        .filter(p => p.source === "installed").map(p => p.name));
      let html = `<div class="pm-toolbar">
          <span class="sub" style="flex:1;word-break:break-all">本机市场目录：${esc(d.market_dir)}
          （拷贝目录/zip 即可流通；下方「云端市场」可跨设备分享）</span>
          <button class="btn mini" data-pm-act="open-folder-market">打开文件夹</button>
          <button class="btn mini" data-pm-act="refresh-market">刷新</button>
        </div>`;
      // 云端市场区：配置 + 在线面板卡片（带截图预览）
      html += await renderCloudSection(inst);
      // 本机市场列表
      const all = [...(d.panels || []), ...(d.zips || [])];
      if (all.length){
        html += '<div class="sub" style="margin:14px 0 6px">本机市场中的面板（安装=复制到已安装目录）：</div>';
        html += all.map(p => {
          const done = installedNames.has(p.name);
          return `<div class="pm-item">
          <div class="grow"><div class="nm">${esc(p.title)}<small>v${esc(p.version)} · ${esc(p.name)}
            ${p.zip ? '<span class="pm-tag">zip</span>' : ""}</small></div>
          <div class="ds">${esc(p.description || "")}</div></div>
          ${done ? '<span class="pm-tag">已安装</span>'
                 : `<button class="btn mini" data-pm-act="install" data-id="${esc(p.name)}">安装</button>`}
        </div>`;}).join("");
      } else {
        html += '<div class="empty">本机市场目录为空——可从下方云端市场一键下载安装。</div>';
      }
      body.innerHTML = html;
    }catch(e){ body.innerHTML = `<div class="empty">加载失败：${esc(e.message)}</div>`; }
  }

  // 云端市场区块：未配置→引导表单；已配置→在线面板列表（截图卡片）+ 发布入口
  async function renderCloudSection(inst){
    let out = '<div class="sub" style="margin:14px 0 6px">☁ 云端市场（Gitee / GitHub 仓库托管）</div>';
    try{ cloudCfg = await api("/api/panels/cloud"); }catch(e){ cloudCfg = null; }
    const st = cloudCfg && cloudCfg.status || {};
    if (!st.gitee_ready){
      // 首次配置表单：Gitee 必填（浏览可匿名，发布需 token）
      return out + `<div class="card" style="max-width:640px">
        <div class="ds" style="margin-bottom:10px">填一个公开 Gitee 仓库（如
          <code>yourname/lingshu-panels</code>）作为面板云端仓库；要发布还需
          <a href="https://gitee.com/profile/personal_access_tokens" target="_blank" rel="noopener">私人令牌</a>（勾选 projects 权限）。
          GitHub 可选，配置后自动镜像同步。</div>
        <div style="display:grid;gap:8px">
          <input id="pc-gitee-repo" placeholder="Gitee 仓库 owner/repo（必填）" style="width:100%">
          <input id="pc-gitee-token" placeholder="Gitee 私人令牌（发布需要）" style="width:100%">
          <input id="pc-github-repo" placeholder="GitHub 仓库 owner/repo（可选，镜像）" style="width:100%">
          <input id="pc-github-token" placeholder="GitHub token（配了 GitHub 才需要）" style="width:100%">
          <div><button class="btn mini" data-pm-act="cloud-save-cfg">保存云端配置</button></div>
        </div></div>`;
    }
    const errs = (cloudCfg && cloudCfg.errors || []).filter(Boolean);
    if (errs.length)
      out += `<div class="ds" style="color:var(--red);margin:4px 0">⚠ ${esc(errs.join("；"))}
        （云端不可达，可先浏览本机市场）</div>`;
    const panels = (cloudCfg && cloudCfg.panels) || [];
    out += `<div class="pm-toolbar">
        <span class="sub" style="flex:1">云端共 ${panels.length} 个面板
          ${cloudCfg && cloudCfg.cached ? "（缓存，60s 自动刷新）" : ""}</span>
        <button class="btn mini" data-pm-act="cloud-refresh">刷新云端</button>
        <button class="btn mini" data-pm-act="cloud-cfg">重新配置</button>
      </div>`;
    if (panels.length){
      out += '<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:12px;margin:8px 0">';
      out += panels.map(p => {
        const shot = p.screenshot
          ? `<img src="/api/panels/cloud/shot?name=${encodeURIComponent(p.name)}&ext=${encodeURIComponent(p.screenshot)}"
               style="width:100%;height:130px;object-fit:cover;border-radius:8px;border:1px solid var(--line);cursor:zoom-in"
               data-pm-act="cloud-view" data-id="${esc(p.name)}" data-ext="${esc(p.screenshot)}" alt="">`
          : `<div style="width:100%;height:130px;border-radius:8px;border:1px dashed var(--line);
               display:flex;align-items:center;justify-content:center;color:var(--faint);font-size:12px">暂无截图</div>`;
        const installed = (inst.panels || []).some(x => x.source === "installed" && x.name === p.name);
        return `<div class="card" style="padding:10px">
          ${shot}
          <div class="nm" style="margin:8px 0 2px">${esc(p.title)}
            <small>v${esc(p.version)} · ${esc(p.name)}</small></div>
          <div class="ds" style="min-height:34px;max-height:52px;overflow:hidden">${esc(p.description || "")}</div>
          <div style="display:flex;gap:6px;margin-top:8px;flex-wrap:wrap">
            ${installed ? '<span class="pm-tag">已安装</span>'
              : `<button class="btn mini" data-pm-act="cloud-download" data-id="${esc(p.name)}">下载安装</button>`}
            <button class="btn mini" data-pm-act="cloud-view" data-id="${esc(p.name)}" data-ext="${esc(p.screenshot || "")}">详情</button>
          </div></div>`;
      }).join("");
      out += '</div>';
    } else {
      out += '<div class="empty">云端仓库还是空的——做完面板点下面的「发布到云端」，或先安装本机已有的。</div>';
    }
    // 发布入口：选一个已安装面板 + 可选截图
    const mine = (inst.panels || []).filter(p => p.source === "installed" && p.valid !== false);
    out += `<div class="sub" style="margin:12px 0 6px">发布我的面板到云端${st.publish_ready ? "" : "（需先在配置里填 Gitee token）"}：</div>`;
    out += mine.length ? mine.map(p => `<div class="pm-item">
        <div class="grow"><div class="nm">${esc(p.title)}<small>v${esc(p.version)} · ${esc(p.name)}</small></div>
        <div class="ds">${esc(p.description || "")}</div></div>
        <label class="btn mini" style="cursor:pointer">选截图
          <input type="file" accept="image/png,image/jpeg,image/webp" style="display:none"
            data-pm-shot="${esc(p.name)}"></label>
        <button class="btn mini" data-pm-act="cloud-publish" data-id="${esc(p.name)}">发布</button>
      </div>`).join("")
      : '<div class="ds">已安装目录里还没有可发布的面板。</div>';
    return out;
  }
  // 选中截图文件暂存（发布时随面板一起上传）
  const pendingShots = {};
  document.addEventListener("change", (e) => {
    const inp = e.target.closest("[data-pm-shot]");
    if (!inp || !inp.files || !inp.files[0]) return;
    const f = inp.files[0];
    if (f.size > 2 * 1024 * 1024){ toast("截图超过 2MB，请压缩后再选", true); inp.value = ""; return; }
    const r = new FileReader();
    r.onload = () => { pendingShots[inp.dataset.pmShot] = r.result;
      toast("截图已就绪，点「发布」一起上传"); };
    r.readAsDataURL(f);
  });

  // 统一按钮动作分发（事件委托，body 重渲染不用重绑）
  document.addEventListener("click", async (e) => {
    const btn = e.target.closest("[data-pm-act]");
    if (!btn) return;
    const act = btn.dataset.pmAct;
    if (act === "refresh"){ renderInstalled(); return; }
    if (act === "refresh-market"){ renderMarket(); return; }
    if (act === "open-folder"){
      try{ await ppost("open-folder", {which: "installed"}); }catch(err){ toast(err.message, true); } return;
    }
    if (act === "open-folder-market"){
      try{ await ppost("open-folder", {which: "market"}); }catch(err){ toast(err.message, true); } return;
    }
    if (act === "scaffold"){
      const nm = prompt("面板名（小写字母/数字/-/_，也是目录名）：");
      if (!nm) return;
      const tt = prompt("展示名（面板标题）：", nm) || nm;
      btn.disabled = true;
      try{
        await api("/api/panels/scaffold", {method:"POST", headers:{"Content-Type":"application/json"},
          body: JSON.stringify({name: nm.trim(), title: tt.trim()})});
        toast("已创建面板骨架，打开文件夹即可编辑；改完点「启用」即刻生效");
        renderInstalled();
      }catch(err){ toast(err.message, true); }
      btn.disabled = false;
      return;
    }
    if (act === "activate"){
      btn.disabled = true;
      try{ await ppost("activate", {name: btn.dataset.id});
        toast("已启用，正在热加载面板…"); location.reload(); }
      catch(err){ toast(err.message, true); btn.disabled = false; }
      return;
    }
    if (act === "delete"){
      if (!confirm("确定删除该面板目录？此操作不可恢复。")) return;
      try{ await ppost("delete", {name: btn.dataset.id, source: curTab()}); toast("已删除");
        curTab() === "market" ? renderMarket() : renderInstalled(); }
      catch(err){ toast(err.message, true); }
      return;
    }
    if (act === "upload"){
      btn.disabled = true;
      try{
        await ppost("upload", {name: btn.dataset.id});
        toast("已上传到面板市场（剪切移动）");
        renderMarket();
      }catch(err){ toast(err.message, true); btn.disabled = false; }
      return;
    }
    if (act === "install"){
      btn.disabled = true;
      try{
        const r = await ppost("install", {name: btn.dataset.id});
        toast(`已安装 ${r.title || r.name} v${r.version}，可在「已安装」里启用`);
        renderMarket();
      }catch(err){ toast(err.message, true); btn.disabled = false; }
      return;
    }
    // ---------------- 云端市场动作 ----------------
    if (act === "cloud-refresh"){
      btn.disabled = true;
      renderMarket(); // renderCloudSection 会带 force=false 重新拉（有 60s 缓存）
      return;
    }
    if (act === "cloud-save-cfg" || act === "cloud-cfg"){
      if (act === "cloud-cfg"){ // 重新配置：清空云端配置后回到引导表单
        btn.disabled = true;
        try{
          await api("/api/panels/cloud", {method:"POST", headers:{"Content-Type":"application/json"},
            body: JSON.stringify({action:"clear-config"})});
          toast("云端配置已清除，请重新填写");
        }catch(err){ toast(err.message, true); }
        btn.disabled = false;
        renderMarket();
        return;
      }
      btn.disabled = true;
      try{
        await api("/api/panels/cloud", {method:"POST", headers:{"Content-Type":"application/json"},
          body: JSON.stringify({action:"save-config",
            gitee_repo: ($id("pc-gitee-repo")||{}).value || "",
            gitee_token: ($id("pc-gitee-token")||{}).value || "",
            github_repo: ($id("pc-github-repo")||{}).value || "",
            github_token: ($id("pc-github-token")||{}).value || ""})});
        toast("云端配置已保存");
        renderMarket();
      }catch(err){ toast(err.message, true); btn.disabled = false; }
      return;
    }
    if (act === "cloud-download"){
      btn.disabled = true;
      btn.textContent = "下载中…";
      try{
        const r = await api("/api/panels/cloud", {method:"POST", headers:{"Content-Type":"application/json"},
          body: JSON.stringify({action:"download", name: btn.dataset.id})});
        toast(`已下载安装 ${r.title || r.name} v${r.version}，可在「已安装」里启用`);
        renderMarket();
      }catch(err){ toast(err.message, true); btn.disabled = false; btn.textContent = "下载安装"; }
      return;
    }
    if (act === "cloud-publish"){
      btn.disabled = true;
      try{
        const r = await api("/api/panels/cloud", {method:"POST", headers:{"Content-Type":"application/json"},
          body: JSON.stringify({action:"publish", name: btn.dataset.id,
            screenshot: pendingShots[btn.dataset.id] || ""})});
        toast(`已发布到：${(r.pushed || []).join("、")}` + (r.errors && r.errors.length ? `（部分失败：${r.errors.join("；")}）` : ""));
        delete pendingShots[btn.dataset.id];
        renderMarket();
      }catch(err){ toast(err.message, true); btn.disabled = false; }
      return;
    }
    if (act === "cloud-view"){
      // 详情弹窗：截图大图 + 描述 + 版本信息；无截图则只展示文字
      const nm = btn.dataset.id, ext = btn.dataset.ext || "";
      let shotHtml = "";
      if (ext) shotHtml = `<img src="/api/panels/cloud/shot?name=${encodeURIComponent(nm)}&ext=${encodeURIComponent(ext)}"
        style="width:100%;border-radius:8px;border:1px solid var(--line)" alt="">`;
      const p = ((cloudCfg || {}).panels || []).find(x => x.name === nm) || {};
      alert(`【${p.title || nm}】v${p.version || "?"}\n作者：${p.author || "未署名"}\n更新：${p.updated || "?"}\n大小：${p.size ? Math.round(p.size/1024) + " KB" : "?"}\n\n${p.description || "（无描述）"}`);
      return;
    }
  });
  // 当前在哪个标签页（installed / market）：删除按钮的 source 参数要用
  function curTab(){
    const on = $id("pm-tabs") && $id("pm-tabs").querySelector(".on");
    return on ? on.dataset.pmTab : "installed";
  }

  // 回退提示：服务端注入的激活面板名——若激活了自定义面板，全屏 iframe 热加载它。
  // 兜底三层：iframe onerror/3s 超时 → 提示并自动切回 default；iframe 内 JS 崩溃
  // 只崩 iframe 自身，不影响外壳；面板扫描 invalid 根本不会被激活（后端预检）。
  const ACT = window.__LINGSHU_ACTIVE__ || {panel: "default"};
  function loadCustom(pname, entry){
    const fr = document.createElement("iframe");
    fr.id = "custom-panel-frame";
    // entry 来自面板 manifest（/api/panels 列表下发），不是写死的 index.html——
    // manifest 允许作者自定义入口文件名，写死会让这类面板 404 → 被超时兜底误回退
    fr.src = "/api/panels/file?panel=" + encodeURIComponent(pname) +
      "&path=" + encodeURIComponent(entry || "index.html") + "&as_page=1";
    fr.style.cssText = "position:fixed;inset:0;width:100vw;height:100vh;border:none;z-index:800;background:#14181e";
    fr.onerror = () => fallback(pname, "iframe 加载失败");
    document.body.appendChild(fr);
    // 超时兜底：3 秒内 iframe 没触发 load（说明资源坏了/接口 404），自动回退
    let ok = false;
    fr.addEventListener("load", () => { ok = true; });
    setTimeout(() => { if (!ok) fallback(pname, "加载超时"); }, 3000);
    // 悬浮逃生口：永远能一键回到默认面板
    const escBtn = document.createElement("button");
    escBtn.id = "panel-escape";
    escBtn.textContent = "⇱ 返回默认面板";
    escBtn.style.cssText = "position:fixed;right:14px;top:14px;z-index:850;cursor:pointer;" +
      "background:#1b2028;color:#e6e1d5;border:1px solid #2a3140;border-radius:9px;padding:7px 14px;font-size:12px";
    escBtn.onclick = async () => {
      try{ await api("/api/panels", {method:"POST", headers:{"Content-Type":"application/json"},
        body: JSON.stringify({action:"deactivate"})}); }catch(e){}
      location.reload();
    };
    document.body.appendChild(escBtn);
  }
  async function fallback(pname, why){
    const fr = document.getElementById("custom-panel-frame");
    if (fr) fr.remove();
    const escEl = document.getElementById("panel-escape");
    if (escEl) escEl.remove();
    try{ await api("/api/panels", {method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({action:"deactivate"})}); }catch(e){}
    toast(`自定义面板「${pname}」${why}，已回退默认面板`, true);
  }
  if (ACT.panel && ACT.panel !== "default"){
    // 激活面板必须是合法已安装面板：列表里 valid=false 或干脆不在 → 直接回退
    api("/api/panels").then(d => {
      const hit = (d.panels || []).find(p => p.name === ACT.panel &&
        p.source === "installed" && p.valid !== false);
      hit ? loadCustom(ACT.panel, hit.entry)
          : fallback(ACT.panel, "面板无效或已损坏");
    }).catch(() => {});  // 列表都拉不到：保持默认面板，别添乱
  }

  // 回退提示（服务端旧通道，保留兼容）
  const FB = window.__LINGSHU_FALLBACK__;
  if (FB && FB.panel) setTimeout(() =>
    toast(`自定义面板「${FB.panel}」加载失败已回退默认面板：${FB.reason || "未知原因"}`, true), 600);
})();
