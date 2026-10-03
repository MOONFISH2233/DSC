# -*- coding: utf-8 -*-
"""
假设：**模型的回答和上一轮一模一样时，wait_answer 认不出「新回答开始了」**。

因为「回答开始了没有」的判据是 `txt != baseline`（内容比对），
而 baseline 是**上一条回答的文本**。同一句话问两遍、模型答得一字不差，
新回答就和 baseline 相等 → 判成「没开始」→ 干等 90 秒报错。

先看页面上到底有几条回答、各是什么 —— 这是决定性证据。
只读。
"""
import sys
import os
import time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import deepseek_ask as ds

page, _ = ds.connect()
els = ds.answers(page)
print(f'页面上有 {len(els)} 条 AI 回答：')
for i, e in enumerate(els):
    try:
        t = (e.run_js('return (this.innerText||"").slice(0,60)') or '')
    except Exception as ex:
        t = f'<{ex}>'
    print(f'  [{i}] {t!r}')

print()
print(f'last_answer_text → {ds.last_answer_text(page)!r}')
print()
if len(els) >= 2:
    print('★ 如果上面有好几条内容相同的回答，就说明**模型确实答了**，')
    print('  是「内容比对」这个判据认不出来 —— 那是真 bug，不是限流。')
