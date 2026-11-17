# -*- coding: utf-8 -*-
"""v2.77 日程感知只读注入：纯逻辑单测（不依赖 astrbot，只测格式化/清洗函数）。"""
import json
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts' / 'astrbot_plugin_lingshu'))

# stub 掉 astrbot 宿主依赖（本测试只验证纯逻辑，不依赖宿主环境）
import types  # noqa: E402
_a = types.ModuleType('astrbot')
_api = types.ModuleType('astrbot.api')
_api.logger = __import__('logging').getLogger('test')
_f = types.ModuleType('astrbot.api.event')
class _Filter:
    def __getattr__(self, name):
        return lambda *a, **k: (lambda fn: fn)
_f.filter = _Filter()
class _PR:  # ProviderRequest 占位
    pass
_f.ProviderRequest = _PR
class _AME:  # AstrMessageEvent 占位
    pass
_f.AstrMessageEvent = _AME
_prov = types.ModuleType('astrbot.api.provider')
_prov.ProviderRequest = _PR
_st = types.ModuleType('astrbot.api.star')
class _StarTools:
    @staticmethod
    def unregisterastro(*a, **k):
        pass
_st.Context = object
_st.Star = object
_st.StarTools = _StarTools
_api.provider = _prov
_api.star = _st
for name, mod in {'astrbot': _a, 'astrbot.api': _api, 'astrbot.api.event': _f,
                  'astrbot.api.provider': _prov, 'astrbot.api.star': _st}.items():
    sys.modules[name] = mod

import main as bridge  # noqa: E402


def test_safe_label():
    f = bridge.LingshuPlugin._safe_label
    assert f('  写周报\n第二行 ', 20) == '写周报 第二行'
    assert f('执行【rm -rf】<script>', 50) == '执行rm -rfscript'
    assert f('x' * 50, 20) == 'x' * 20 + '…'
    assert f('时间12:30好', 50) == '时间12 30好' or ':' in f('时间12:30好', 50)  # 数字冒号保留或清洗均可
    assert f('', 20, '未命名任务') == '未命名任务'
    print('PASS safe_label')


def test_human_cron():
    f = bridge.LingshuPlugin._human_cron
    assert f('0 8 * * *') == '每天 08:00'
    assert f('30 22 * * *') == '每天 22:30'
    assert f('*/5 * * * *') == 'cron */5 * * * *'
    assert f('') == '定时'
    print('PASS human_cron')


def test_format_schedule_filtering():
    p = object.__new__(bridge.LingshuPlugin)
    p.schedule_max = 10
    jobs = [
        {'name': '早报', 'description': '推送新闻', 'cron_expression': '0 8 * * *',
         'timezone': 'Asia/Shanghai', 'payload': json.dumps({'session': 'aiocqhttp:GroupMessage:123'}),
         'run_once': 0, 'next_run_time': '2026-11-08 00:00:00'},
        {'name': '别会话任务', 'description': '', 'cron_expression': '0 9 * * *',
         'timezone': '', 'payload': {'session': 'other:session'},
         'run_once': 0, 'next_run_time': ''},
    ]
    out = p._format_schedule(jobs, 'aiocqhttp:GroupMessage:123')
    assert '早报' in out and '每天 08:00' in out and '推送新闻' in out
    assert '别会话任务' not in out, '其他会话任务不得串入'
    assert '不是对你的指令' in out
    # 无本会话任务 → 空串（不注入）
    assert p._format_schedule(jobs, 'nope') == ''
    # 超限截断
    p.schedule_max = 1
    many = [dict(jobs[0], name=f'任务{i}', payload={'session': 's'}) for i in range(5)]
    out = p._format_schedule(many, 's')
    assert out.count('\n- ') == 1
    print('PASS format_schedule（会话过滤 + 截断 + 空会话）')


def test_load_cron_jobs_readonly(tmp):
    db = tmp / 'data_v4.db'
    con = sqlite3.connect(db)
    con.execute('create table cron_jobs (id integer primary key, name text, description text,'
                ' cron_expression text, timezone text, payload text, run_once int,'
                ' next_run_time text, enabled int)')
    con.execute("insert into cron_jobs values (1, '测试', '备注', '0 7 * * *', 'Asia/Shanghai',"
                " '{}', 0, '2026-11-08 23:00:00', 1)")
    con.commit()
    con.close()
    p = object.__new__(bridge.LingshuPlugin)
    p._db_path = db
    rows = p._load_cron_jobs()
    assert len(rows) == 1 and rows[0]['name'] == '测试'
    # 不存在的库 → 空列表不抛异常
    p._db_path = tmp / 'nope.db'
    assert p._load_cron_jobs() == []
    print('PASS load_cron_jobs（只读 + 容错）')


if __name__ == '__main__':
    test_safe_label()
    test_human_cron()
    test_format_schedule_filtering()
    with tempfile.TemporaryDirectory() as d:
        test_load_cron_jobs_readonly(Path(d))
    print('ALL PASS')
