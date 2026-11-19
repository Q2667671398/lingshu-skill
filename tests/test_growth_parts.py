# -*- coding: utf-8 -*-
"""growth.py 纯计算部分（growth_parts.py）回归测试。

拆分背景：growth.py 里"纯计算、无 IO"的一组函数（文本工具 / frontmatter 解析与生成 /
版本容错 / 日期窗口 / 生成物识别）搬到 scripts/growth_parts.py，growth.py 末尾
`from growth_parts import *` 保留旧调用面。本文件只测这些纯函数的边界，外加调用面契约；
不读写真库，所以 import growth 只为验证 re-export 是否无感。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import growth  # noqa: E402
import growth_parts as gp  # noqa: E402

NOW = 1_800_000_000
DAY = 86400


def test_text_utils_edges():
    """文本工具边界：空输入不炸、中文分隔符、去重保序、超长截断补省略号。"""
    assert gp._split(None) == []
    assert gp._split("") == []
    assert gp._split(" 甲、乙，丙;丁；戊\n己/庚|辛 ") == [
        "甲", "乙", "丙", "丁", "戊", "己", "庚", "辛"]
    assert gp._split("、、") == []                 # 全是分隔符 → 空列表
    # 非空值统一 str 后去重（None 会变成 "None"，与拆分前语义一致）
    assert gp._dedupe([" a ", "b", "a", "", "  ", None, 3]) == ["a", "b", "None", "3"]
    assert gp._dedupe([]) == []
    assert gp._clip(None, 5) == ""
    assert gp._clip("  a   b\n c ", 100) == "a b c"   # 空白折叠成单空格
    assert gp._clip("abcdef", 6) == "abcdef"          # 恰好等于上限不截断
    assert gp._clip("abcdef", 5) == "abcde…"          # 超限截断补省略号
    assert gp._clip("abcdef", 0) == "…"


def test_frontmatter_and_compose_roundtrip():
    """frontmatter 解析边界 + _compose 生成结果可被自己读回。"""
    fm = ("---\nname: demo\ndescription: \"含: 冒号\"\nversion: 3\n"
          "tags: |\n  甲\n  乙\nnote: >\n  折行\n  文本\n---\n\n# 标题\n正文\n")
    meta, body = gp.parse_frontmatter(fm)
    assert meta["name"] == "demo"
    assert meta["description"] == "含: 冒号"        # 引号包裹 + 值里带冒号
    assert meta["version"] == "3"                    # 原样字符串，数值容错交给 _ver
    assert meta["tags"] == "甲\n乙"                  # | 块标量保留换行
    assert meta["note"] == "折行 文本"               # > 块标量折叠为空格
    assert body == "# 标题\n正文\n"
    # 无 frontmatter / 带 BOM / 缺闭界行：都原样返回，不猜
    assert gp.parse_frontmatter("\ufeff# 无头") == ({}, "# 无头")
    assert gp.parse_frontmatter("---\nname: x\n") == ({}, "---\nname: x\n")
    assert gp.parse_frontmatter("") == ({}, "")
    # _compose 生成的 frontmatter 必须能被 parse_frontmatter 原样读回
    raw = gp._compose("demo", "用于测试  多空格", "正文", 3, "2026-01-02", "2026-01-03")
    meta2, body2 = gp.parse_frontmatter(raw)
    assert meta2["name"] == "demo"
    assert meta2["description"] == "用于测试 多空格"   # 生成时多空格折叠
    assert (meta2["origin"], meta2["version"]) == ("灵枢", "3")
    assert body2 == "正文\n"


def test_slug_version_and_generated_detection():
    """技能名归一化/拒绝、版本容错、旧版生成文件识别。"""
    # 中文名折算 + 大小写/空格/下划线归一化；中文名本身合法
    assert gp._require_slug(" 灵枢 ") == "lingshu"
    assert gp._require_slug("My Skill_1") == "my-skill-1"
    assert gp._require_slug("质检评审") == "质检评审"
    for bad in ("", "-abc", "a" * 64, "bad/name"):
        try:
            gp._require_slug(bad)
            assert False, f"应拒绝非法技能名 {bad!r}"
        except ValueError:
            pass
    for reserved in ("con", "COM1", "lpt9"):          # Windows 保留设备名，大小写不敏感
        try:
            gp._require_slug(reserved)
            assert False, f"应拒绝 Windows 保留名 {reserved!r}"
        except ValueError:
            pass
    assert gp._SLUG_TO_LEGACY_NAME["lingshu"] == "灵枢"
    # 版本容错：整数→int，语义化版本/坏值→非空字符串，绝不抛
    assert gp._ver(3) == 3
    assert gp._ver("2.11.2") == "2.11.2"
    assert gp._ver(None) == "1"
    assert gp._ver("  ") == "1"
    # 旧版生成物识别：整份生成 → True；混入用户手写 / 无签名 → False
    legacy = "# lingshu\n\n**何时用**：x\n\n由灵枢在全局化时生成\n"
    assert gp._looks_generated(legacy) is True
    assert gp._looks_generated(legacy + "用户手写说明\n") is False
    assert gp._looks_generated("# lingshu\n") is False
    # 纯函数：字段坏类型（None/list）也不能让词袋炸
    assert gp._kdoc({"title": "版本0", "body": None, "points": None, "tags": ["甲"]}) == {
        "版本", "0", "甲"}


def test_fade_phase_window_boundaries():
    """知识淡出相位：30/180 天窗口边界、豁免、时间不明、显式 now。"""
    def phase(days_ago, **kw):
        return gp.knowledge_fade_phase({"updated": NOW - days_ago * DAY, **kw}, NOW)

    assert phase(0) == "active"
    assert phase(29) == "active"
    assert phase(30) == "fading"        # 恰好 30 天进入 fading
    assert phase(179) == "fading"
    assert phase(180) == "dormant"      # 恰好 180 天进入 dormant
    assert phase(4000) == "dormant"
    # 豁免始终 active：pinned / E3 / E4；E1 不豁免
    assert phase(4000, pinned=True) == "active"
    assert phase(4000, evidence_level="E3") == "active"
    assert phase(4000, evidence_level="E4") == "active"
    assert phase(200, evidence_level="E1") == "dormant"
    # 时间不明不推测；updated 缺失/坏值回退 created
    assert gp.knowledge_fade_phase({}, NOW) == "active"
    assert gp.knowledge_fade_phase({"updated": 0, "created": 0}, NOW) == "active"
    assert gp.knowledge_fade_phase({"created": NOW - 100 * DAY}, NOW) == "fading"
    assert gp.knowledge_fade_phase(
        {"updated": "坏值", "created": NOW - 40 * DAY}, NOW) == "fading"
    # 显式 now 生效（now 相对条目时间前移 → 相位回转）
    assert gp.knowledge_fade_phase({"updated": NOW - 60 * DAY}, NOW - 40 * DAY) == "active"
    assert gp.knowledge_fade_phase({"updated": NOW - 200 * DAY}, NOW - 100 * DAY) == "fading"
    # 窗口常量与标记对外可见（growth 侧仍能用）
    assert (gp.FADE_ACTIVE_DAYS, gp.FADE_DORMANT_DAYS) == (30, 180)
    assert gp._FADE_PENALTY == 0.03
    assert set(gp._FADE_MARK) == {"active", "fading", "dormant"}
    assert gp._protected_knowledge({}) is False
    assert gp._protected_knowledge({"pinned": True}) is True
    assert gp._protected_knowledge({"evidence_level": "E4"}) is True


def test_streak_and_fmt_ago():
    """连续天数计算（今天没动从昨天起算）与时长格式化三档。"""
    import brain
    orig_now_ts = brain.now_ts
    brain.now_ts = lambda: NOW                 # 固定时间源，避免跨午夜抖动
    try:
        d = [brain.fmt_date(NOW - i * DAY) for i in range(4)]
        assert gp._streak(set(d)) == 4                  # 连续 4 天
        assert gp._streak({d[1], d[2]}) == 2            # 今天还没动：从昨天起算
        assert gp._streak(set()) == 0
        assert gp._streak({d[0], d[2]}) == 1            # 中间断一天就停
        assert gp._streak({d[3]}) == 0                  # 只在 3 天前动过，连续中断
    finally:
        brain.now_ts = orig_now_ts
    # 时长格式化：负数钳 0；分钟/小时/天三档；整数值去掉 ".0"
    assert gp._fmt_ago(-5) == "0 分钟"
    assert gp._fmt_ago(0) == "0 分钟"
    assert gp._fmt_ago(1800) == "30 分钟"
    assert gp._fmt_ago(3600) == "1 小时"      # 恰好一小时进小时档
    assert gp._fmt_ago(5400) == "1.5 小时"
    assert gp._fmt_ago(DAY) == "1 天"
    assert gp._fmt_ago(3 * DAY) == "3 天"


def test_growth_surface_reexport_contract():
    """调用面契约：旧名字仍挂在 growth 上且同一对象；growth.py 不留同名空壳。"""
    functions = ["_split", "_dedupe", "_clip", "_kdoc", "_require_slug",
                 "parse_frontmatter", "_compose", "_ver", "_fmt_ago", "_streak",
                 "knowledge_fade_phase", "_protected_knowledge", "_looks_generated"]
    consts = ["SLUG_RE", "_WINDOWS_RESERVED", "_SKILL_NAME_ALIASES",
              "_SLUG_TO_LEGACY_NAME", "FADE_ACTIVE_DAYS", "FADE_DORMANT_DAYS",
              "_FADE_PENALTY", "_FADE_MARK", "_LEGACY_AGENTS_SIGNATURE"]
    for name in functions + consts:
        assert hasattr(growth, name), f"growth 缺少 {name}"
        assert getattr(growth, name) is getattr(gp, name), f"{name} 不是同一对象"
    for name in functions:
        assert getattr(growth, name).__module__ == "growth_parts", name
    ns: dict = {}
    exec("from growth import *", ns)                 # noqa: S102 —— 旧调用方式必须无感
    for name in ["parse_frontmatter", "SLUG_RE", "FADE_ACTIVE_DAYS", "knowledge_fade_phase"]:
        assert name in ns, f"from growth import * 少了 {name}"
    src = (ROOT / "scripts" / "growth.py").read_text(encoding="utf-8")
    assert "from growth_parts import *" in src
    assert src.index("from growth_parts import *") < src.index('if __name__ == "__main__":')
    for name in functions:
        assert f"def {name}(" not in src, f"growth.py 仍保留 {name} 的同名定义"
    for name in consts:
        assert f"\n{name} = " not in src, f"growth.py 仍保留 {name} 的赋值（会复制成两份）"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {fn.__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"ERROR {fn.__name__}: {type(exc).__name__}: {exc}")
    print(f"{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
