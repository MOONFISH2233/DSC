# -*- coding: utf-8 -*-
"""
一轮问答的时间到底花在哪？逐阶段计时。

A/B 对拍量到「dsc 每轮比 API 慢 ~24 秒」，但那是**总账**。
这个探针把一轮拆开：抢锁 / 开标签页 / 导航 / 切开关 / 取基线 /
发送 / 等回答 —— 看哪一段值得动刀。

只读（不改仓库），动的是活浏览器。
用法：py -3.11 _phase_probe.py [轮数]
"""
import sys
import os
import time
import collections

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import deepseek_ask as ds

ROUNDS = int(sys.argv[1]) if len(sys.argv) > 1 else 4
QUESTION = '只回答两个字：收到'

# 给每个阶段套一层计时。**包模块属性**（不是局部名）—— ask_in_session
# 里是全局查找，所以补在这里就生效。
TIMES = collections.defaultdict(list)
_orig = {}


def wrap(name, label=None):
    fn = getattr(ds, name)
    _orig[name] = fn

    def inner(*a, **k):
        t0 = time.time()
        try:
            return fn(*a, **k)
        finally:
            TIMES[label or name].append(time.time() - t0)
    setattr(ds, name, inner)


for n in ('ensure_browser', 'ensure_page', 'set_think', 'set_search',
          'send_question', 'wait_answer', 'last_answer_text', 'tab_for'):
    if hasattr(ds, n):
        wrap(n)

# 取连接的耗时（第一次会拉起浏览器，单独记）
t0 = time.time()
page, started = ds.connect()
TIMES['connect（首次会起浏览器）'].append(time.time() - t0)
print(f'浏览器：{"新起的" if started else "接管已有的"}')

print(f'\n跑 {ROUNDS} 轮，每轮问「{QUESTION}」\n')
for i in range(1, ROUNDS + 1):
    t0 = time.time()
    p, text, err = ds.ask_in_session(QUESTION, think=False, new_chat=False,
                                     key='phase-probe')
    dt = time.time() - t0
    print('  第 %d 轮：%.1f 秒  →  %r%s'
          % (i, dt, (text or '')[:20], f'  ← {err}' if err else ''))

print('\n' + '=' * 66)
print('  各阶段累计（轮数=%d）' % ROUNDS)
print('=' * 66)
rows = []
for name, ts in TIMES.items():
    tot = sum(ts)
    rows.append((tot, name, len(ts), tot / len(ts)))
rows.sort(reverse=True)
for tot, name, n, avg in rows:
    print('  %-26s 合计 %6.1f 秒   调用 %2d 次   每次 %5.2f 秒'
          % (name, tot, n, avg))
print()
print('  ★ 注：wait_answer 里大部分是**模型生成时间**（等对方），')
print('    不能算我们的开销。真正能砍的是其余那几段 + wait_answer 的尾部等待。')
