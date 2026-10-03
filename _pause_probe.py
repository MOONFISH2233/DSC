# -*- coding: utf-8 -*-
"""
探针：生成过程中，文本会不会**中途停顿**？停多久？

这决定 STABLE_NORMAL（2.5 秒）和那个 2× 兜底（5 秒）能不能往下降。
  途中最大停顿  << 阈值  → 降阈值安全
  途中最大停顿  >= 阈值  → 降阈值会把半截回答判成写完了（绝不能降）

只读，不改任何东西。用法：py -3.11 _pause_probe.py
"""
import sys
import os
import time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import deepseek_ask as ds

QUESTIONS = [
    '用大约 400 字介绍一下快速排序的原理。',
    '把整数 1 到 200 逐个写出来，用顿号分隔，一个都不能少。',
    '写一段 300 字左右的说明文，介绍光合作用。',
    '用 Python 写一个冒泡排序函数，带注释和示例。',
    '列出 20 个中国省会城市，每个配一句简介。',
]

page, _ = ds.connect()
print(f'STABLE_NORMAL={ds.STABLE_NORMAL}  2×兜底={ds.STABLE_NORMAL * 2}  '
      f'POLL_FAST={ds.POLL_FAST} POLL_SLOW={ds.POLL_SLOW}')
print()

all_gaps = []
for q in QUESTIONS:
    print('=' * 74)
    print('问题:', q)
    baseline = ds.last_answer_text(page)
    ds.send_question(page, q, baseline)

    t0 = time.time()
    last_len, last_grow = 0, None
    grows = []          # 每次「文本变长」的时刻
    while time.time() - t0 < 150:
        txt = ds.last_answer_text(page) or ''
        n = len(txt)
        t = time.time() - t0
        if n > last_len:
            last_len, last_grow = n, t
            grows.append(t)
        # 文本停长 8 秒就认为这轮结束（比生产的 5 秒宽，用来观察）
        if last_grow is not None and t - last_grow > 8.0:
            break
        time.sleep(0.15)

    tail = (time.time() - t0) - (last_grow or 0)
    print(f'  最终 {last_len} 字，变长 {len(grows)} 次，'
          f'末次变长 {last_grow:.2f}s，尾部等了 {tail:.2f}s')
    if len(grows) >= 2:
        gaps = [round(b - a, 2) for a, b in zip(grows, grows[1:])]
        gaps.sort(reverse=True)
        print(f'  变长间隔最大的 5 个: {gaps[:5]}')
        # 只把「生成途中」的间隔计入（排除最后一段）
        all_gaps.extend(gaps[1:] if len(gaps) > 1 else [])
    else:
        print('  ⚠️  只变长一次（整段一次刷出来），拿不到间隔')

print()
print('=' * 74)
if all_gaps:
    all_gaps.sort(reverse=True)
    n = len(all_gaps)
    print(f'全部「途中」变长间隔  n={n}   最大 {all_gaps[0]:.2f}s   '
          f'第 2 大 {all_gaps[1] if n > 1 else "-"}s')
    print(f'  中位 {all_gaps[n//2]:.2f}s')
    print(f'  超过 1.0s 的: {sum(1 for g in all_gaps if g > 1.0)} 个')
    print(f'  超过 2.5s 的: {sum(1 for g in all_gaps if g > 2.5)} 个')
    print(f'  超过 5.0s 的: {sum(1 for g in all_gaps if g > 5.0)} 个')
    print()
    print('★ 判读：途中最大间隔若远小于阈值，说明途中停顿只是「逐段渲染」的')
    print('  正常节奏，阈值可以降；若接近甚至超过阈值，降阈值就会截断。')
