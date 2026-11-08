# -*- coding: utf-8 -*-
"""测试：鸡生蛋防线——install_skill 从不同目录的源装灵枢走子进程，且成功"""
import sys, json, hashlib
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'scripts'))
import growth

src = ROOT
installed = growth._find_installed('lingshu')
print('installed:', installed)
same = src.resolve() == installed.resolve()
print('source==installed?', same)

r = growth.install_skill('lingshu', from_dir=str(src), force=True, accept_risk=True)
print('state:', r.get('state'))
print('path:', r.get('path'))

# 校验装好的副本哈希与 dev manifest 一致
mf = json.loads((src/'manifest.json').read_text(encoding='utf-8-sig'))
dst = Path(r['path']).parent
def norm(b):
    if b.startswith(b'\xef\xbb\xbf'): b = b[3:]
    return b.replace(b'\r\n', b'\n')
bad = []
for rel, info in mf['files'].items():
    p = dst/rel
    if not p.is_file():
        bad.append((rel, 'missing')); continue
    if hashlib.sha256(norm(p.read_bytes())).hexdigest() != info['sha256'].lower():
        bad.append((rel, 'hash'))
print('mismatches:', bad or 'NONE')

ok = (r.get('state') == 'installed') and not bad
print('RESULT:', 'PASS' if ok else 'FAIL')
sys.exit(0 if ok else 1)
