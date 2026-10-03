# -*- coding: utf-8 -*-
"""
临时探针：clean() 的改动**能不能影响工具调用解析**（尤其 Write 的 content）。

问的是：「缺 content」这类失败会不会是第十八轮改出来的？
做法：拿一批真实的工具调用形状，分别过**旧 clean** 和 **新 clean**，
      再走完整的 parse_reply → retry_reason，比对结果。

只读，不改任何东西。用法：py -3.11 _clean_impact.py
"""
import sys
import os
import re
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import deepseek_ask as ds
import claude_shim as cs

# 旧判据（第十八轮之前）
_OLD_RE = re.compile(
    r'^\s*(已深度思考|深度思考|思考中|正在思考|已思考|Thought about|Thinking)'
    r'[^\n]{0,40}\n+',
    re.M,
)


def clean_old(text):
    return _OLD_RE.sub('', text or '').strip()


TOOLS = [{'name': 'Write'}, {'name': 'PowerShell'}, {'name': 'Read'}]

BODY = 'x' * 400          # 假装是一段长文件内容

# 真实形状：模型常见的几种「开头 + 工具调用」组合
SHAPES = [
    ('真标题 + JSON', '已深度思考（用时 12 秒）\n\n{"tool_use": {"name": "Write", "input": {"file_path": "a.py", "content": "%s"}}}' % BODY),
    ('真标题 + 代码块',
     '已深度思考（用时 12 秒）\n\n{"tool_use": {"name": "Write", "input": {"file_path": "a.py"}}}\n```python\n%s\n```' % BODY),
    ('正文首行 + JSON', '思考中台是一种架构\n{"tool_use": {"name": "Write", "input": {"file_path": "a.py", "content": "%s"}}}' % BODY),
    ('正文首行 + 裸写法', 'Thinking about it, the answer is 42.\n{"name": "Write", "arguments": {"file_path": "a.py", "content": "%s"}}' % BODY),
    ('正文首行 + 代码块',
     '思考中台是一种架构\n{"tool_use": {"name": "Write", "input": {"file_path": "a.py"}}}\n```python\n%s\n```' % BODY),
    ('无开头，直接 JSON', '{"tool_use": {"name": "Write", "input": {"file_path": "a.py", "content": "%s"}}}' % BODY),
    # ★ 被网页截断的（这才是「缺 content」的现场）
    ('被截断：content 没写完', '已深度思考（用时 8 秒）\n\n{"tool_use": {"name": "Write", "input": {"file_path": "a.py", "content": "%s' % BODY[:80]),
    ('被截断：正文首行在前', '思考中台是一种架构\n{"tool_use": {"name": "Write", "input": {"file_path": "a.py", "content": "%s' % BODY[:80]),
    # 边界：首行里带围栏（会改变围栏奇偶 —— 缺陷 39 的判据）
    ('首行带围栏', '思考中 ```python\n{"tool_use": {"name": "Write", "input": {"file_path": "a.py"}}}\n%s\n```' % BODY),
]


def summarize(raw):
    """把解析结果压成可比较的形状。"""
    kind, payload, prose = cs.parse_and_align(raw, TOOLS)
    if kind == 'tools':
        got = []
        for t in payload:
            inp = t.get('input') or {}
            got.append((t.get('name'),
                        sorted(inp.keys()),
                        {k: (len(v) if isinstance(v, str) else v)
                         for k, v in sorted(inp.items())}))
        return ('tools', got, cs.retry_reason((kind, payload, prose), TOOLS))
    return (kind, str(payload)[:60], cs.retry_reason((kind, payload, prose), TOOLS))


print('%-26s %-6s %s' % ('形状', '差异', '说明'))
print('-' * 96)
diff = 0
for name, raw in SHAPES:
    a = summarize(clean_old(raw))
    b = summarize(ds.clean(raw))
    same = (a == b)
    if not same:
        diff += 1
    print('%-26s %-6s' % (name, '一致' if same else '★不同'))
    if not same:
        print('    旧 clean → %s' % (a,))
        print('    新 clean → %s' % (b,))

print()
print('--- %d/%d 个形状解析结果完全一致 ---' % (len(SHAPES) - diff, len(SHAPES)))
print()
print('★ 关键结论：只要「缺 content」在两边**同时**成立或同时不成立，')
print('  就说明第十八轮没碰过这条路径，e2e 那次红是偶发。')
sys.exit(0)
