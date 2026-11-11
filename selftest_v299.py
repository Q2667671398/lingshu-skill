# -*- coding: utf-8 -*-
"""v2.99 self-test (temp, delete after run). ASCII output only."""
import sys
sys.path.insert(0, r"e:\丁厚同\文档\程序\灵枢-skill\scripts")

import core, memory, panel_server  # noqa: E402
print("imports OK")

v = core.embed_texts(["hello world"])
print("embed_texts fallback dims:", len(v[0]))

import fallback_embed as fe  # noqa: E402
a = fe.encode("用户喜欢用PostgreSQL存数据")
b = fe.encode("用户偏好把数据存在PostgreSQL里")
c = fe.encode("今天天气很好适合出去爬山")
sim_ab = fe.cosine(a, b)
sim_ac = fe.cosine(a, c)
print("sim_ab =", round(sim_ab, 4), "sim_ac =", round(sim_ac, 4),
      "PASS" if sim_ab > sim_ac else "FAIL")
print("bad-input:", fe.encode(None), fe.encode(""), fe.cosine(None, a))
print("_embed_wanted(new mem):", memory._embed_wanted({"id": "t", "text": "x"}))
print("READONLY:", memory.READONLY)
