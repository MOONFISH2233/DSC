# -*- coding: utf-8 -*-
"""
临时探针：操作栏能不能**提前**判定生成结束？

★ 第一版探针写错了（测的是**上一条**回答的操作栏 —— 没等新回答开始就开测，
  于是 t=0 就看到「操作栏已出现」）。这版按生产的判据来：
  先用 baseline 确认**新回答真的开始了**，再开始记时。
  （这个项目记过：写用例出错多半是测试的错，不是代码的错。）

量的是：操作栏出现的那一刻，文本是不是已经不再长了？
  晚于 / 等于 → 可靠，可用来省掉稳定性等待
  早于        → 用了会截断，绝不能拿它当判据

只读，不改任何东西。用法：py -3.11 _done_signal_probe.py
"""
import sys
import os
import time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import deepseek_ask as ds

# 候选取法：从最后一条回答往上找「第一个含操作栏的祖先」（最多 4 层）。
# 不能走太远 —— ds-virtual-list-visible-items 那层含**所有**可见消息的操作栏。
JS_DONE = """
const els = document.querySelectorAll('.ds-assistant-message-main-content');
if (!els.length) return false;
let f = els[els.length - 1];
for (let i = 0; i < 4 && f; i++, f = f.parentElement) {
  try { if (f.querySelector('.ds-button--iconLabelTertiary')) return true; }
  catch (e) {}
}
return false;
"""

QUESTIONS = [
    '用大约 400 字介绍一下快速排序的原理、时间复杂度和适用场景。',
    '把整数 1 到 80 逐个用中文写出来，用顿号分隔，一个都不能少。',
]

page, _ = ds.connect()
print(f'STABLE_NORMAL = {ds.STABLE_NORMAL}   POLL_FAST = {ds.POLL_FAST}   '
      f'POLL_SLOW = {ds.POLL_SLOW}')
print(f'生产里的 answer_done_rendered() 现在返回：{ds.answer_done_rendered(page)}')

for q in QUESTIONS:
    print('\n' + '=' * 74)
    print('问题:', q)
    baseline = ds.last_answer_text(page)
    ds.send_question(page, q, baseline)

    # ── 阶段 1：确认新回答开始了（不在这一步之前记任何东西）──
    t_send = time.time()
    started = False
    while time.time() - t_send < 90:
        txt = ds.last_answer_text(page) or ''
        if txt and txt != baseline:
            started = True
            break
        time.sleep(0.2)
    if not started:
        print('  ⚠️  新回答没开始')
        continue
    print(f'  新回答开始于      {time.time() - t_send:.2f} 秒（相对发送）')

    # ── 阶段 2：记时 ──
    t0 = time.time()
    last_len, last_grow_at = 0, None
    first_done_at = None
    grew_after_done, done_snapshots = 0, []
    while time.time() - t0 < 120:
        txt = ds.last_answer_text(page) or ''
        n = len(txt)
        done = bool(page.run_js(JS_DONE))
        t = time.time() - t0
        if n > last_len:
            last_len, last_grow_at = n, t
        if done and first_done_at is None:
            first_done_at = t
        elif done and first_done_at is not None and n > last_len:
            pass
        if first_done_at is not None:
            done_snapshots.append((t, n))
            if n > last_len:
                grew_after_done += 1
            if t - first_done_at > 4.0:
                break
        time.sleep(0.15)
        # 保险：万一操作栏一直不出现
        if first_done_at is None and t > 100:
            break

    if first_done_at is None:
        print('  ⚠️  120 秒内操作栏没出现')
        print(f'      （文本长度 {last_len}，最后变长于 {last_grow_at:.2f} 秒）')
        continue
    gap = first_done_at - (last_grow_at or 0)
    ok = gap >= -0.05
    print(f'  文本最终长度      {last_len} 字')
    print(f'  文本最后变长于    {last_grow_at:.2f} 秒')
    print(f'  操作栏首次出现于  {first_done_at:.2f} 秒')
    print(f'  → 差值 {gap:+.2f} 秒   '
          f'{"✅ 晚于文本停长，可作提前信号" if ok else "❌ 早于文本停长，用了会截断"}')
    print(f'  操作栏出现后文本又长了 {grew_after_done} 次')
    print(f'  若用旧代码：这一轮会再多等 '
          f'{max(0.0, ds.STABLE_NORMAL * 2 - min(gap if ok else 0, 0)):.1f} 秒左右')
