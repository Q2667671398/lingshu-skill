#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一次性运维脚本：删除并重建云端商店仓库（保持仓库名不变）。

用途：历史 commit 的提交者是 token 所属账号（lingshu），想抹掉重来时——
删掉远端仓库 → 空仓库重建 → 本地 force push（作者是你自己）→ 重新发布
面板/拓展（走 v2.50 的 author_name 配置，新 commit 都是你的）。

仓库（与 panel_cloud.py 的默认商店源一致，名字不能改，商店引用着）：
- Gitee  主源： htmcwy_0/lingshu-skill
- GitHub 镜像： Q2667671398/lingshu-skill

token 来源：~/.brain-memory/config.json 的 panel.cloud.gitee_token /
github_token（和面板发布用的同一份）。

⚠️ 不可逆：远端仓库连同全部 commit、star、issue 一起没了（本地 git 历史还在）。
GitHub 删除接口需要 token 带 delete_repo 权限，没有会 HTTP 403，脚本会提示。
"""

import json
import sys
import urllib.request
from pathlib import Path

GITEE_REPO = "htmcwy_0/lingshu-skill"
GITHUB_REPO = "Q2667671398/lingshu-skill"
REPO_NAME = "lingshu-skill"          # 重建后的仓库名，保持不变
HOME = Path.home() / ".brain-memory"


def _req(method: str, url: str, token: str, payload: dict | None = None) -> tuple[int, str]:
    """一次 API 请求，返回 (状态码, 响应摘要)。非 2xx 不抛，由调用方判断。"""
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "User-Agent": "lingshu-rebuild/1.0",
        "Content-Type": "application/json",
        **({"Authorization": f"token {token}"} if token else {}),
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read()[:300].decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()[:300].decode("utf-8", "replace")
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        return 0, str(exc)


def _tokens() -> dict:
    # 优先环境变量 GITEE_TOKEN / GITHUB_TOKEN（避免直接改 config 的权限问题）
    import os
    gitee = os.environ.get("GITEE_TOKEN", "").strip()
    github = os.environ.get("GITHUB_TOKEN", "").strip()
    if gitee or github:
        return {"gitee_token": gitee, "github_token": github}
    cfg = json.loads((HOME / "config.json").read_text("utf-8"))
    return (cfg.get("panel.cloud") or {})


def main() -> int:
    c = _tokens()
    gitee_token = str(c.get("gitee_token", "")).strip()
    github_token = str(c.get("github_token", "")).strip()
    if not gitee_token and not github_token:
        print("config.json 里没有 panel.cloud 的 gitee_token / github_token，先在面板配置好。")
        return 2

    print(f"""即将执行（不可逆）：
  1. 删除 Gitee 仓库  https://gitee.com/{GITEE_REPO}
  2. 重建空仓库       https://gitee.com/{GITEE_REPO}（私有仓库关掉，公开）
  3. 删除 GitHub 仓库 https://github.com/{GITHUB_REPO}
  4. 重建空仓库       https://github.com/{GITHUB_REPO}
之后你需要手动：git push --force（本地历史是你的作者）→ 面板重新发布面板/拓展。
本地 git 历史、star、issue、远端历史都会丢（本地历史还在，star/issue 回不来）。""")

    if input("\n确认继续？输入大写 YES：").strip() != "YES":
        print("已取消。")
        return 1

    ok = True
    if gitee_token:
        print(f"\n[Gitee] 删除 {GITEE_REPO} …")
        code, detail = _req("DELETE", f"https://gitee.com/api/v5/repos/{GITEE_REPO}", gitee_token)
        print("  删除" + ("成功" if code in (200, 204) else f"失败 HTTP {code}：{detail}"))
        if code not in (200, 204):
            ok = False
        print(f"[Gitee] 重建 {REPO_NAME} …")
        code, detail = _req("POST", "https://gitee.com/api/v5/user/repos", gitee_token,
                            {"name": REPO_NAME, "private": False, "auto_init": False,
                             "description": "灵枢（lingshu）技能与面板/拓展商店镜像"})
        print("  重建" + ("成功" if code in (200, 201) else f"失败 HTTP {code}：{detail}"))
        if code not in (200, 201):
            ok = False
    if github_token:
        print(f"\n[GitHub] 删除 {GITHUB_REPO} …")
        code, detail = _req("DELETE", f"https://api.github.com/repos/{GITHUB_REPO}", github_token)
        print("  删除" + ("成功" if code in (200, 204) else f"失败 HTTP {code}：{detail}"))
        if code == 403:
            print("  （403 = token 没有 delete_repo 权限：Settings → Developer settings → "
                  "Personal access tokens 勾上 delete_repo，或手动在网页上删除）")
        if code not in (200, 204):
            ok = False
        print(f"[GitHub] 重建 {REPO_NAME} …")
        code, detail = _req("POST", "https://api.github.com/user/repos", github_token,
                            {"name": REPO_NAME, "private": False, "auto_init": False,
                             "description": "灵枢（lingshu）技能与面板/拓展商店镜像"})
        print("  重建" + ("成功" if code in (200, 201) else f"失败 HTTP {code}：{detail}"))
        if code not in (200, 201):
            ok = False

    print(f"""
{'全部完成' if ok else '有步骤失败，看上面的输出'}。下一步手动操作：

  1. 本地强推（远端现在是空仓库，本地历史作者是"小丁"，不会变成别人）：
       git push --force origin main        # Gitee
       git push --force github main        # GitHub（按你实际 remote 名）
  2. 确认 config 里 panel.cloud.author_name = 你的名字（v2.50 新配置）
  3. 面板 → 云端 → 重新发布各面板与拓展（index.json 也会重发，
     新 commit 全部是你的作者）
""")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
