# -*- coding: utf-8 -*-
"""评分/保留度纯函数回归测试（mem_score.py 拆分）。

覆盖：数值安全转换、艾宾浩斯保留度/唤醒值/唤醒值遗忘、文本相似度、重要性打分、
零向量余弦、以及 memory.py 的调用面（星号导入）契约。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

sys.path.insert(0, str(Path(__file__).resolve().parent))  # tests/ 公共测试工具（瞬时 IO 重试）
from io_retry_util import run_with_io_retry  # noqa: E402

import mem_score  # noqa: E402
import memory  # noqa: E402

DAY = 86400
NOW = 1_800_000_000


def test_num_coercion_is_safe():
    """坏值必须回退默认值：None/bool/坏字符串/NaN/Infinity 都不能污染计算。"""
    assert mem_score._num(None, -1.0) == -1.0
    assert mem_score._num(True, 2.0) == 2.0          # bool 是 int 子类，按坏值回退
    assert mem_score._num(False, 2.0) == 2.0
    assert mem_score._num("0.8") == 0.8
    assert mem_score._num("不是数字", 0.25) == 0.25
    assert mem_score._num([]) == 0.0
    assert mem_score._num(float("nan"), 0.5) == 0.5
    assert mem_score._num(float("inf"), 0.5) == 0.5
    assert mem_score._num(1e308) == 1e308            # 极值原样保留（不溢出成 inf）


def test_retention_activation_and_forget_curve():
    """保留度：刚访问=1、越久越衰减、Δt 负数不反涨；唤醒值 0~1 且用进废退。"""
    fresh = {"created": NOW, "last_access": NOW, "importance": 0.5,
             "stability": mem_score.DEFAULT_STABILITY}
    assert mem_score.retention(fresh, NOW) == 1.0
    old = dict(fresh, created=NOW - 1000 * DAY, last_access=NOW - 1000 * DAY)
    assert 0.0 <= mem_score.retention(old, NOW) < 1e-10
    future = dict(fresh, created=NOW + 10 ** 9, last_access=NOW + 10 ** 9)
    assert mem_score.retention(future, NOW) == 1.0   # Δt 负数按 0 截断
    broken = dict(fresh, stability="坏值")            # 坏 stability 回落默认值
    assert mem_score.retention(broken, NOW) == 1.0
    # 成长型记忆（mistake）更抗遗忘：同样 2 天，保留度高于普通 fact
    plain = {"created": NOW - 2 * DAY, "last_access": NOW - 2 * DAY,
             "kind": "fact", "stability": mem_score.DEFAULT_STABILITY}
    mistake = dict(plain, kind="mistake")
    assert mem_score.retention(mistake, NOW) > mem_score.retention(plain, NOW)
    # 唤醒值：新鲜 + importance 0.5 → 0.5*1 + 0.08；极值 importance 10 → 封顶 1.0
    assert mem_score.activation(fresh, NOW) == 0.58
    assert mem_score.activation(dict(fresh, importance=10), NOW) == 1.0
    assert mem_score.activation(dict(old, importance=-5), NOW) == 0.0
    assert 0.0 <= mem_score.activation({"created": NOW, "importance": "0.9"}, NOW) <= 1.0
    # 唤醒值遗忘：低激活的 hippo 记忆降 dust + 清 embedding + 双向断边；pinned/identity 不动
    a = {"id": "a", "layer": "hippo", "created": NOW - 1000 * DAY,
         "last_access": NOW - 1000 * DAY, "importance": 0.0,
         "stability": DAY, "embedding": [0.1, 0.2], "links": ["b"]}
    b = {"id": "b", "layer": "cortex", "created": NOW, "last_access": NOW,
         "importance": 0.9, "stability": mem_score.DEFAULT_STABILITY, "links": ["a"]}
    pinned = {"id": "c", "layer": "hippo", "pinned": True, "created": NOW - 1000 * DAY,
              "last_access": NOW - 1000 * DAY, "importance": 0.0, "stability": DAY}
    identity = dict(pinned, id="d", pinned=False, kind="identity")
    hit = mem_score.activation_forget([a, b, pinned, identity], NOW)
    assert [m["id"] for m in hit] == ["a"], hit
    assert a["layer"] == "dust" and "embedding" not in a and a["links"] == []
    assert b["links"] == []                          # 别的记忆指向它的出边被剪掉
    assert pinned["layer"] == "hippo" and identity["layer"] == "hippo"


def test_terms_similarity_and_idf_edges():
    """分词/相似度空输入不炸；IDF 常见词权重低；relevance 落在 0~1。"""
    assert mem_score.terms("") == set()
    assert mem_score.terms(None) == set()
    t = mem_score.terms("版本0")
    assert "版本" in t and "0" in t                  # 编号必须保留为独立 term
    assert mem_score.jaccard(set(), {"甲"}) == 0.0
    assert mem_score.jaccard({"甲", "乙"}, {"乙"}) == 0.5
    assert mem_score.build_idf([]) == {}
    idf = mem_score.build_idf([{"甲"}, {"甲", "乙"}])
    assert idf["甲"] < idf["乙"]                     # 越常见权重越低
    assert mem_score.relevance({"甲"}, {"甲"}, idf) == 1.0
    assert mem_score.relevance({"甲"}, {"乙"}, idf) == 0.0
    assert mem_score.relevance(set(), set(), {}) == 0.0


def test_heuristic_importance_bounds():
    """重要性打分永远落在 [0.05, 1.0]；correction 抬升；情绪/实体极值不越界。"""
    assert mem_score.heuristic_importance("", "unknown_kind") == 0.5
    assert 0.05 <= mem_score.heuristic_importance("", "") <= 1.0
    assert mem_score.heuristic_importance("被纠正过的说法", "fact", correction=True) >= 0.75
    extreme = mem_score.heuristic_importance(
        "记住这件事", "identity", entities=[f"e{i}" for i in range(50)], emotional=999)
    assert 0.05 <= extreme <= 1.0
    assert extreme == 1.0
    # importance 安全取键：坏类型沉底 0，字符串数字可解析
    assert mem_score._imp_key_safe({"importance": None}) == 0.0
    assert mem_score._imp_key_safe({"importance": "0.7"}) == 0.7
    assert mem_score._imp_key_safe({}) == 0.0


def test_cosine_zero_vector_and_dim_mismatch():
    """余弦边界：零向量/空向量/维度不一致返回 0.0，同向返回 1。"""
    assert mem_score._cosine_sim([], []) == 0.0
    assert mem_score._cosine_sim([0.0, 0.0], [1.0, 2.0]) == 0.0    # 0 向量 → 0
    assert mem_score._cosine_sim([1.0, 2.0], [0.0, 0.0]) == 0.0
    assert mem_score._cosine_sim([1.0, 2.0], [1.0, 2.0, 3.0]) == 0.0  # 维度不符
    assert abs(mem_score._cosine_sim([1.0, 2.0, 3.0], [1.0, 2.0, 3.0]) - 1.0) < 1e-9
    assert mem_score._cosine_sim([1.0, 0.0], [0.0, 1.0]) == 0.0


def test_memory_surface_reexport_contract():
    """调用面契约：memory 仍能按旧名字用；搬到 mem_score 的函数不留同名空壳。"""
    public = ["terms", "jaccard", "build_idf", "relevance", "retention",
              "activation", "activation_forget", "heuristic_importance",
              "ACTIVATION_FORGET_THRESHOLD"]
    private = ["_num", "_imp_key_safe", "_KIND_STABILITY_FACTOR", "_cosine_sim"]
    for name in public + private:
        assert hasattr(memory, name), f"memory 缺少 {name}"
        assert getattr(memory, name) is getattr(mem_score, name), f"{name} 不是同一个对象"
    for name in ["terms", "jaccard", "build_idf", "relevance", "retention",
                 "activation", "activation_forget", "heuristic_importance"]:
        assert getattr(memory, name).__module__ == "mem_score", name
    ns: dict = {}
    exec("from memory import *", ns)                 # noqa: S102 —— 旧调用方式必须无感
    for name in public:
        assert name in ns, f"from memory import * 少了 {name}"
    src = (ROOT / "scripts" / "memory.py").read_text(encoding="utf-8")
    for name in public + private:
        assert f"def {name}(" not in src, f"memory.py 仍保留 {name} 的同名定义"
    assert "from mem_score import *" in src


def _ensure_utf8_stdout() -> None:
    """stdout/stderr 切 UTF-8——Windows CI 是 cp1252，print 中文会直接崩。"""
    for _name in ("stdout", "stderr"):
        _s = getattr(sys, _name, None)
        _fn = getattr(_s, "reconfigure", None)
        if callable(_fn):
            try:
                _fn(encoding="utf-8", errors="replace")
                continue
            except (OSError, ValueError, AttributeError):
                pass
        try:
            import io
            _buf = getattr(_s, "buffer", None)
            if _buf is not None:
                setattr(sys, _name,
                        io.TextIOWrapper(_buf, encoding="utf-8", errors="replace",
                                         line_buffering=True))
        except Exception:
            pass


if __name__ == "__main__":
    _ensure_utf8_stdout()
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            run_with_io_retry(fn)
            print(f"PASS {fn.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {fn.__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"ERROR {fn.__name__}: {type(exc).__name__}: {exc}")
    print(f"{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
