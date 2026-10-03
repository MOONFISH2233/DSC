# -*- coding: utf-8 -*-
"""
临时探针：clean() 的误伤面。只读，不改任何东西。

用法：py -3.11 _clean_probe.py
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import deepseek_ask as ds

# (说明, 输入, 期望保留的首行是什么)
CASES = [
    # ── 必须**原样保留**（旧版在这里吃掉了用户正文）──
    ('中文正文以「思考中」开头', '思考中台是一种架构\n第二行内容', '思考中台是一种架构'),
    ('英文正文以 Thinking 开头',
     'Thinking about it, the answer is 42.\nMore text.', 'Thinking about it, the answer is 42.'),
    ('以「深度思考」开头的正文', '深度思考是一种方法论\n正文', '深度思考是一种方法论'),
    ('以「正在思考」开头的正文', '正在思考这个问题的人很多\n正文', '正在思考这个问题的人很多'),
    ('以「已思考」开头的正文', '已思考的部分先放一边\n正文', '已思考的部分先放一边'),
    ('以「思考中」开头的单行回答', '思考中台', '思考中台'),
    ('正文里举 JSON 例子', '{"name": "张三", "age": 18}\n注意 name 必填。', '{"name": "张三", "age": 18}'),

    # ── 必须**删掉**（真思考标题，功能不能丢）──
    ('真标题：已深度思考（用时 N 秒）', '已深度思考（用时 12 秒）\n\n正文在此', '正文在此'),
    ('真标题：半角括号', '已深度思考(用时 12 秒)\n\n正文在此', '正文在此'),
    ('真标题：分秒', '已深度思考（用时 1 分 12 秒）\n\n正文在此', '正文在此'),
    ('真标题：单独一行没后缀', '思考中\n正文在此', '正文在此'),
    ('真标题：省略号', '思考中...\n正文在此', '正文在此'),
    ('真标题：Thinking…', 'Thinking...\nHere is the answer.', 'Here is the answer.'),
    ('真标题：英文带时长', 'Thinking for 12 seconds\nHere is the answer.', 'Here is the answer.'),
]

fails = 0
for name, raw, want_head in CASES:
    got = ds.clean(raw)
    ok = got.split('\n')[0] == want_head
    if not ok:
        fails += 1
    print(f'{"OK " if ok else "❌ "} {name}')
    if not ok:
        print(f'      输入 : {raw!r}')
        print(f'      期望首行: {want_head!r}')
        print(f'      实得   : {got!r}')

print()
print(f'--- clean(): {len(CASES) - fails} 通过 / {fails} 失败 ---')

# 流式那一半：可吐前缀必须和非流式拿到的**是同一份文本**
print()
sp = ds.streamable_prefix
SP_CASES = [
    ('正文首行不该被压住',
     '思考中台是一种架构', '思考中台是一种架构'),
    ('标题没写完 → 压住',
     '已深度思考（用时 3', ''),
    ('标题到齐 → 删掉后吐',
     '已深度思考（用时 12 秒）\n\n正文在此', '正文在此'),
    ('英文正文首行不该被压住',
     'Thinking about it, the answer is 42.', 'Thinking about it, the answer is 42.'),
]
sp_fails = 0
for name, raw, want in SP_CASES:
    got = sp(raw)
    ok = got == want
    if not ok:
        sp_fails += 1
    print(f'{"OK " if ok else "❌ "} {name}')
    if not ok:
        print(f'      输入 : {raw!r}\n      期望 : {want!r}\n      实得 : {got!r}')
print()
print(f'--- streamable_prefix(): {len(SP_CASES) - sp_fails} 通过 / {sp_fails} 失败 ---')
sys.exit(1 if (fails or sp_fails) else 0)
