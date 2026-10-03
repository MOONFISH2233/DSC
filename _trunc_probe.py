# -*- coding: utf-8 -*-
"""
探针：回答被截断时，「继续生成」按钮**过多久**才出现？

这是决定稳定性窗口能不能降的关键：
  按钮出现得很快（< 1 秒）→ 2.5 秒的窗口绰绰有余，5 秒纯属浪费
  按钮出现得很慢（> 2.5 秒）→ 窗口不能降，否则会把半截回答当成品交出去

复现截断的老办法（第十六轮记的）：让它把 1 到 9000 逐个列出来。

只读，不改任何东西。用法：py -3.11 _trunc_probe.py
"""
import sys
import os
import time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import deepseek_ask as ds

page, _ = ds.connect()
print(f'STABLE_NORMAL={ds.STABLE_NORMAL}  2×兜底={ds.STABLE_NORMAL * 2}')

# ★ 必须**开新对话**再逼它。上一次在累积了上下文的对话里问，
#   模型只答了 626 字就收了（没触发截断，白等三分钟）。
if not ds.ensure_page(page, new_chat=True):
    print('⚠️  开新对话失败')
    sys.exit(1)
print('已开新对话')

q = ('把整数 1 到 9000 全部原样逐个列出来，用中文逗号分隔。'
     '不许省略、不许用省略号、不许只给代码、不许解释，直接从 1 开始写。')
print('问题:', q)
baseline = ds.last_answer_text(page)
ds.send_question(page, q, baseline)

t0 = time.time()
last_len, last_grow = 0, None
btn_first_seen = None
samples = []
while time.time() - t0 < 180:
    txt = ds.last_answer_text(page) or ''
    n = len(txt)
    t = time.time() - t0
    if n > last_len:
        last_len, last_grow = n, t
    btn = ds.find_continue_button(page) is not None
    samples.append((t, n, btn))
    if btn and btn_first_seen is None:
        btn_first_seen = t
    # 按钮出现后再看 5 秒
    if btn_first_seen is not None and t - btn_first_seen > 5:
        break
    time.sleep(0.2)

print(f'\n文本最终 {last_len} 字')
print(f'末次变长于    {last_grow:.2f}s')
if btn_first_seen is None:
    print('⚠️  这轮**没有截断**（没出现继续生成按钮）—— 换个更长的任务再试')
else:
    lag = btn_first_seen - (last_grow or 0)
    print(f'按钮首次出现  {btn_first_seen:.2f}s')
    print(f'→ 落后文本停长 {lag:+.2f} 秒')
    print()
    print(f'★ 判读：窗口 STABLE_NORMAL={ds.STABLE_NORMAL}s '
          f'{"够用 ✅" if lag <= ds.STABLE_NORMAL else "不够 ❌ 不能降窗口"}')

print('\n按钮出现前后各 8 个采样点：')
idx = next((i for i, s in enumerate(samples) if s[2]), len(samples) - 1)
for s in samples[max(0, idx - 8):idx + 8]:
    mark = '  ← 按钮出现' if abs(s[0] - (btn_first_seen or -1)) < 1e-9 else ''
    print(f'    {s[0]:7.2f}s  {s[1]:>7} 字  按钮={s[2]}{mark}')
