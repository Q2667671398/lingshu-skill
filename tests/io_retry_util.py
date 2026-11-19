# -*- coding: utf-8 -*-
"""测试侧公共工具：Windows 瞬时文件占用重试。

背景：本机 360 / 搜索索引器会在文件刚写完时短暂持有独占句柄，测试读"刚由自己写入"
的库文件会偶发 PermissionError（或 WinError 5/32/33/145）。生产写路径有
`core._replace_retry` 兜底，读路径没有；读路径属于生产代码，本任务只动 tests/，
因此在测试执行器层做有限次重试：

  - 只重试 PermissionError 与 Windows 瞬时占用类 OSError；
  - AssertionError 绝不重试（断言失败=业务回归，不能靠重试掩盖）；
  - 每个测试函数都在自己的临时 HOME 里重建现场，重跑是安全的。

用法（各测试文件 main 的循环里）：
    try:
        run_with_io_retry(fn)
        print(f"PASS {fn.__name__}")
    except ...
"""
from __future__ import annotations

import time

# 5=拒绝访问 32=共享冲突 33=锁冲突 145=目录非空（rmtree 与占用竞态时的表象）
TRANSIENT_WINERRORS = {5, 32, 33, 145}

DEFAULT_ATTEMPTS = 3


def is_transient_io_error(exc: BaseException) -> bool:
    if isinstance(exc, PermissionError):
        return True
    if isinstance(exc, OSError):
        return getattr(exc, "winerror", None) in TRANSIENT_WINERRORS
    return False


def run_with_io_retry(fn, attempts: int = DEFAULT_ATTEMPTS):
    """执行 fn()；遇瞬时文件占用重试（退避 0.3s/0.6s），其余异常原样抛出。"""
    for i in range(attempts):
        try:
            return fn()
        except AssertionError:
            raise
        except OSError as exc:
            if not is_transient_io_error(exc) or i + 1 >= attempts:
                raise
            print(f"  ~ {getattr(fn, '__name__', fn)}: 瞬时文件占用（{exc}），"
                  f"重试 {i + 1}/{attempts - 1}")
            time.sleep(0.3 * (i + 1))
