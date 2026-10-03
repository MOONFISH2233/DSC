# -*- coding: utf-8 -*-
"""
大批量量「生成途中文本停顿多久」—— 用来决定 STABLE_NORMAL(2.5 秒) 还能不能再降。

上一版探针只跑了 5 个回答（最大停顿 0.16 秒），样本太少不敢动这个旋钮。
这个版本跑很多轮、问句覆盖各种形状（短答 / 长列 / 代码 / 中文 / 工具调用形状），
把**每一个**变长间隔都记录下来。

判读：窗口必须**显著大于**实测最大停顿，否则「模型还在想」会被判成「写完了」，
把半截回答交出去（这个项目最怕的一类）。

用法：py -3.11 _pause_probe2.py [轮数]
"""
import sys
import os
import time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import deepseek_ask as ds

ROUNDS = int(sys.argv[1]) if len(sys.argv) > 1 else 20

QUESTIONS = [
    # ★ 第一批全是「≤1000 字」的短回答，停顿最多 1.82 秒。
    #   但真正的长回答（工具调用、长文件、长列表）可能**停顿更久** ——
    #   而 STABLE_NORMAL 要挡的正是那种。所以这一批刻意选**长输出**的题。
    '把整数 1 到 3000 全部原样逐个列出来，用中文顿号分隔，一个都不能少。',
    '写一个 Python 脚本，包含 25 个函数，每个函数带 docstring 和中文注释。',
    '把 1 到 500 里所有的质数全部列出来，用逗号分隔，然后逐个说明它们的特点。',
    '用表格形式列出 30 个国家的首都、人口、面积，每个一行。',
    '详细写一份 1000 字的说明：TCP 三次握手、四次挥手，要分步骤。',
    '生成一个 JSON 数组，包含 40 个对象，每个对象有 name/age/city/email 四个字段。',
]
PROBE = 0.1                      # 采样间隔：比 POLL_FAST 还密，才能看见真实节奏

page, _ = ds.connect()
print(f'STABLE_NORMAL={ds.STABLE_NORMAL}  采样间隔={PROBE}  轮数={ROUNDS}')
print('（每轮换一个问题，同一个对话里连着问）\n')

all_gaps, rounds_info = [], []
for i in range(1, ROUNDS + 1):
    q = QUESTIONS[(i - 1) % len(QUESTIONS)]
    full = q + ('　（第 %d 轮，请重新完整回答一遍）' % i if i > len(QUESTIONS) else '')
    baseline = ds.last_answer_text(page)
    ds.send_question(page, full, baseline)

    t0 = time.time()
    last_len, last_grow, grows = 0, None, []
    while time.time() - t0 < 150:
        txt = ds.last_answer_text(page) or ''
        n = len(txt)
        t = time.time() - t0
        if n > last_len:
            last_len, last_grow = n, t
            grows.append(t)
        if last_grow is not None and t - last_grow > 8.0:
            break                       # 比生产的 2.5 秒宽得多，用来看尾部
        time.sleep(PROBE)

    gaps = [round(b - a, 3) for a, b in zip(grows, grows[1:])]
    if gaps:
        all_gaps.extend(gaps)
        # ★ 原始数据落盘（追加）—— 尾巴有多长要**跨批累积**才看得准。
        #   一次跑 20 轮的样本不够决定这个旋钮，攒几批再一起判读。
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               '_pause_gaps.txt'), 'a', encoding='utf-8') as f:
            f.write('%s\t%d\t%s\n' % (time.strftime('%H:%M:%S'), last_len,
                                      ','.join('%.2f' % g for g in gaps)))
    rounds_info.append((i, last_len, len(grows), max(gaps) if gaps else 0))
    print('  第 %2d 轮：%5d 字  变长 %3d 次  途中最大间隔 %s 秒'
          % (i, last_len, len(grows), ('%.2f' % max(gaps)) if gaps else '—'))

print()
print('=' * 70)
if all_gaps:
    s = sorted(all_gaps, reverse=True)
    n = len(s)
    print(f'  途中变长间隔  n={n}')
    print(f'  最大 {s[0]:.2f} 秒    第 2 大 {s[1] if n > 1 else 0:.2f}    第 3 大 {s[2] if n > 2 else 0:.2f}')
    print(f'  中位 {s[n // 2]:.2f} 秒')
    for th in (0.5, 1.0, 1.5, 2.0, 2.5):
        c = sum(1 for g in s if g > th)
        print(f'  超过 {th:.1f} 秒的：{c} 个'
              + ('   ← 降到这个窗口就会误判' if c else ''))
    print()
    print('  ★ 判读：STABLE_NORMAL 必须**显著大于**这里的最大值。')
    print('    最大值远小于它 → 有下调空间；接近它 → 一个字都不能降。')
