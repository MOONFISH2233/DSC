# -*- coding: utf-8 -*-
"""
自测套件 —— 改完代码先跑这个，别等用户发现问题。

    python selftest.py            # 全部
    python selftest.py --fast     # 只跑不联网的（秒级）
    python selftest.py --live     # 只跑联网的（几十秒）

分两层：
  单元测试（快、离线）  —— 解析逻辑、提示词构造、会话指纹
  集成测试（慢、联网）  —— 真的发一条消息、真的传一个附件
"""
import argparse
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

PASS, FAIL, SKIP = [], [], []


def check(name, cond, detail=''):
    (PASS if cond else FAIL).append(name)
    mark = '✅' if cond else '❌'
    print(f'  {mark} {name}' + (f'   {detail}' if detail and not cond else ''))


# 上层（三个 server）**不允许**直接调这些 —— 必须走 ds.ask_in_session()。
#
# 名单随着公共入口一起成长：
#   · 一开始只拦 wait_answer / send_question（ds.ask() 的内部）
#   · 抽了 ask_in_session 之后，ensure_browser / ensure_page / set_think / ask
#     也变成了「内部件」—— 绕过它们自己拼一串，就是同类 bug 的温床。
# 光拦旧的那两个是不够的：新入口的每一层都得纳入检查，否则「收敛」只防住了上次那种写法。
FORBIDDEN_IN_SERVERS = ('wait_answer', 'send_question', 'ensure_browser',
                        'ensure_page', 'set_think', 'set_search', 'set_toggle',
                        'ask')


def _bypass_calls(path, names=FORBIDDEN_IN_SERVERS):
    """
    找出源码里对指定函数的调用，返回 [(函数名, 行号)]。

    用 ast 而不是正则 —— 正则只能匹配 `ds.wait_answer(` 这一种写法，
    换成 `from deepseek_ask import wait_answer` 就拦不住了；
    而且「按行首 # 判注释」对三引号字符串完全失效。
    ast 看的是真正的 Call 节点，什么别名都逃不掉，注释和字符串也不会误报。
    """
    import ast
    try:
        tree = ast.parse(open(path, encoding='utf-8').read())
    except Exception:
        return []
    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if isinstance(f, ast.Attribute) and f.attr in names:
            hits.append((f.attr, node.lineno))
        elif isinstance(f, ast.Name) and f.id in names:
            hits.append((f.id, node.lineno))
    return hits


def _ask_web_without_attachments(path):
    """
    找出源码里**没传 attachments** 的 ask_web(...) 调用行号。

    ★ 为什么要单独盯这一条：重试那处漏传过。而重试是**开新对话**做的 ——
      图片既不在附件里、也不在对话历史里，可提示词里还写着
      「★ 上面这个附件就是这个文件的内容本身」。模型找不到附件，只能回
      「我无法看到附件中的图片内容」，然后再调一次 Read → 再触发重试 → 死循环。
      （第九轮那个「读 21 张图要 21 轮」就是这么复现的：当时修的是提示词，
       而十一、十二轮新加的重试路径把它绕过去了。）

    ★ 用 ast 不用正则：正则只能匹配字面写法，改成多行、加注释、换关键字传参
      就漏了；ast 看的是真正的 Call 节点，认得出第 4 个位置参数，
      也认得出 attachments= 关键字写法。
    """
    import ast
    try:
        tree = ast.parse(open(path, encoding='utf-8').read())
    except Exception:
        return []
    bad = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if not (isinstance(f, ast.Name) and f.id == 'ask_web'):
            continue
        has = (len(node.args) >= 4
               or any(k.arg == 'attachments' for k in node.keywords))
        if not has:
            bad.append(node.lineno)
    return bad


def section(title):
    print()
    print(f'─── {title} ───')


# ══════════════════════════════════════════════════════════
# 单元测试：不联网
# ══════════════════════════════════════════════════════════

def test_unit():
    import claude_shim as cs
    # ★ 伪流式 / TRUNCATED_WARN 这些在 deepseek_ask 里，这一节也要用。
    #   （main() 里也有个 ds_init，但那是另一个作用域 —— 这里得自己导。）
    import deepseek_ask as ds_init

    section('单元 · 回答解析')

    r = cs.parse_reply('{"tool_use": {"name": "Read", "input": {"file_path": "a.txt"}}}')
    check('工具调用能解析', r[0] == 'tools' and r[1][0]['name'] == 'Read', str(r))

    r = cs.parse_reply('```json\n{"tool_use": {"name": "Bash", "input": {"command": "ls"}}}\n```')
    check('带代码块的工具调用', r[0] == 'tools' and r[1][0]['name'] == 'Bash', str(r))

    # ★ OpenAI 风格的写法 —— 实测模型会混用，只认一种的话整个任务会断掉
    #   （踩过：21 张图的整理任务废在 {"tool_calls":[{"name":...,"arguments":...}]}）
    r = cs.parse_reply('{"tool_calls":[{"name":"Read","arguments":{"file_path":"a.jpg"}}]}')
    check('★ OpenAI 风格 tool_calls/arguments 也认',
          r[0] == 'tools' and r[1][0]['name'] == 'Read'
          and r[1][0]['input'].get('file_path') == 'a.jpg', str(r))

    r = cs.parse_reply('{"name":"Read","arguments":{"file_path":"b.jpg"}}')
    check('裸的 {name, arguments} 也认', r[0] == 'tools' and r[1][0]['name'] == 'Read', str(r))

    # ★ 一次多个 —— 读 21 个文件时 1 轮 vs 21 轮的区别
    r = cs.parse_reply('{"tool_use":[{"name":"Read","input":{"file_path":"1.jpg"}},'
                       '{"name":"Read","input":{"file_path":"2.jpg"}},'
                       '{"name":"Read","input":{"file_path":"3.jpg"}}]}')
    check('★ 一次多个工具调用', r[0] == 'tools' and len(r[1]) == 3, str(r)[:120])

    r = cs.parse_reply('{"tool_calls":[{"name":"A","arguments":{}},{"name":"B","arguments":{}}]}')
    check('★ OpenAI 风格也能一次多个', r[0] == 'tools' and len(r[1]) == 2, str(r)[:120])

    # ★ 先解释一段、再给工具调用 —— 模型失败后特别爱这么写。
    #   整段当回答返回的话，用户看到的就是一坨 JSON 文本、任务断掉。
    #   （真实场景：卸载软件第一步失败，模型解释了原因再重试，就因为这段解释废了。）
    mixed = ('好，那条命令挂了，原因很明确：日志路径写错了。\n'
             '这次不用绕了 —— 干脆不写日志，先让卸载跑起来。\n\n'
             '{"tool_use": {"name": "PowerShell", "input": {"command": "msiexec /X{ABC}"}}}')
    r = cs.parse_reply(mixed)
    check('★ 解释文字里夹带的工具调用能抠出来',
          r[0] == 'tools' and r[1][0]['name'] == 'PowerShell', str(r)[:120])
    check('★ 抠出来的是命令本身，不是那段解释',
          'msiexec' in (r[1][0]['input'].get('command') or ''), str(r[1][0]['input'])[:80])

    # 夹带多个也要认
    mixed2 = ('先说明一下：\n'
              '{"tool_calls":[{"name":"Read","arguments":{"file_path":"a"}},'
              '{"name":"Read","arguments":{"file_path":"b"}}]}')
    r = cs.parse_reply(mixed2)
    check('★ 夹带多个也能抠出来', r[0] == 'tools' and len(r[1]) == 2, str(r)[:120])

    # ★ 回归：夹带的工具调用**超过 2 万字**也要能抠出来。
    #   早先括号配对只扫前 20000 字符，理由是「免得挨个括号试太慢」—— 可配对
    #   本来就是 O(n) 的一次遍历。而模型把长文件内容内联进 JSON 时，调用本身
    #   就能上万字（写 figs.py 那种画图脚本就是），于是配对永远到不了头 →
    #   抠不出来 → 整坨 JSON 当「回答」返回，用户看到一屏 JSON、任务断掉。
    big_leak = ('先说明一下：\n\n{"tool_use": {"name": "Write", "input": {"file_path": "D:'
                + chr(92) + 'big.py", "content": "' + ('x' * 70 + chr(92) + 'n') * 300 + '"}}}')
    r = cs.parse_reply(big_leak)
    check(f'★ 夹带的工具调用超 2 万字也能抠出来（{len(big_leak)} 字）',
          r[0] == 'tools' and len(r[1][0]['input'].get('content') or '') > 20000,
          f'{r[0]} / {str(r)[:60]}')

    # 但真正的长回答（含 JSON 代码示例）不能误判成工具调用
    legit = ('下面是一个 JSON 示例，演示请求格式：\n\n'
             '```json\n{"name": "张三", "age": 18}\n```\n\n'
             '注意 name 字段是必填的。')
    r = cs.parse_reply(legit)
    check('正常回答里的 JSON 示例不误判', r[0] == 'reply', str(r[0]))

    # ★ 最关键的一条：长 Markdown 必须原样保留，不能被 JSON 包装毁掉
    long_md = '# 标题\n\n段落。\n\n- 项目一\n- 项目二\n\n```python\nprint("hi")\n```\n\n结尾 $env:PATH'
    r = cs.parse_reply(long_md)
    check('长 Markdown 当作回答', r[0] == 'reply')
    check('长 Markdown 一字不改', r[1] == long_md, f'长度 {len(r[1])} vs {len(long_md)}')

    r = cs.parse_reply('用 $env:PATH 这个变量，路径 D:\\创业\\a.txt')
    check('回答含 $ 和反斜杠不被破坏', r[1] == '用 $env:PATH 这个变量，路径 D:\\创业\\a.txt', repr(r[1]))

    r = cs.parse_reply('{这不是 JSON，是普通文字}')
    check('花括号开头的普通文字不被误判', r[0] == 'reply', str(r[0]))

    check('空回答不崩', cs.parse_reply('')[0] == 'reply')

    r = cs.parse_reply('{"reply": "老格式"}')
    check('兼容老格式 reply', r[1] == '老格式', str(r))

    section('单元 · 长内容走代码块（不转义协议）')
    # ★ 回归：模型写文件内容时几乎不可能每次都把 JSON 转义写对 ——
    #   代码里的三引号 docstring、len("abc") 会把 JSON 字符串提前截断，
    #   整个工具调用作废。（实测反复栽在写 Python 画图脚本上。）
    #   协议改成：长内容放 JSON 后面的代码块里，一个字符都不用转义。
    Q3 = '"' * 3
    B = chr(92)
    body = ('# -*- coding: utf-8 -*-\n' + Q3 + 'docstring' + Q3 + '\n'
            'import os\n'
            'if __name__ == "__main__":\n'
            '    print(len("abc"))\n')

    raw = ('{"tool_use": {"name": "Write", "input": {"file_path": "C:' + B +
           'Users' + B + 'x' + B + 'figs.py"}}}\n'
           '```python\n' + body + '\n```')
    r = cs.parse_reply(raw)
    ok = r[0] == 'tools'
    check('JSON + 代码块能解析成工具调用', ok, str(r)[:100])
    if ok:
        inp = r[1][0]['input']
        check('路径正确（反斜杠没被吃）',
              inp.get('file_path') == 'C:' + B + 'Users' + B + 'x' + B + 'figs.py',
              repr(inp.get('file_path')))
        got = inp.get('content', '')
        check('★ 代码块内容原样挂上', got.strip() == body.strip(), f'{len(got)} 字')
        check('★ 三引号没丢', Q3 in got)
        check('★ __name__ 没丢', '__name__' in got)

    # Bash 的长命令同理
    r3 = cs.parse_reply('{"tool_use": {"name": "Bash", "input": {"description": "跑"}}}\n'
                        '```bash\ncd D:' + B + 'x; python a.py\n```')
    check('Bash 长命令也能走代码块',
          r3[0] == 'tools' and 'python a.py' in (r3[1][0]['input'].get('command') or ''),
          str(r3)[:100])

    # ★ PowerShell 的命令同理 —— 而且这条是**回归用例**。
    #   Windows 上 Claude Code 的命令工具叫 PowerShell（不是 Bash）。_BLOCK_FIELD
    #   里漏了它的话，_attach_code_block 会掉进「填第一个空字段」的兜底分支：
    #   兜底按 ('content','command',...) 的顺序填，于是代码块内容进了 `content`
    #   而 `command` 空着 —— 发上去 Claude Code 直接拒。实测踩过，别删这条。
    r6 = cs.parse_reply('{"tool_use": {"name": "PowerShell", "input": {"description": "跑"}}}\n'
                        '```\npy -3.11 a.py 2>&1; Write-Output "EXIT=$LASTEXITCODE"\n```')
    check('★ PowerShell 命令也能走代码块，且填对 command 字段',
          r6[0] == 'tools'
          and 'Write-Output' in (r6[1][0]['input'].get('command') or '')
          and not r6[1][0]['input'].get('content'),
          str(r6)[:140])

    PS = [{'name': 'PowerShell'}]
    check('★ PowerShell 缺 command 要触发重试',
          cs.incomplete_tool([{'name': 'PowerShell', 'input': {}}]) is not None)
    check('★ 缺 command 的重试提示要求把命令放代码块',
          '代码块' in (cs.retry_reason(
              ('tools', [{'name': 'PowerShell', 'input': {}}]), PS) or ''))

    # ★ 回归：这是用户终端里**真实出现过**的那一坨 —— 模型把 PowerShell 命令
    #   塞进 JSON 字符串却没转义引号（命令里几乎一定有引号），JSON 因此报废。
    #   整条链路是：
    #     parse_reply 判成「普通回答」→ looks_broken 认出 → 重试
    #   而重试若也失败，旧代码就把这坨 JSON 原文当回答交给 Claude Code ——
    #   那一轮没有 tool_use 可调，会话停在提示符上等用户手打「继续」。
    #   钉住它：这条绝不能被当成正经回答放行。
    JUNK = (r'''{"tool_use": [{"name": "PowerShell", "input": {"command": "cd 'D:\创业\听刻'; '''
            r'''py -3.11 -m py_compile x.py 2>&1; Write-Output "EXIT=$LASTEXITCODE"", '''
            r'''"timeout": 600000}}]}''')
    pj = cs.parse_reply(JUNK)
    check('★ 引号没转义的调用被判成「普通回答」（那坨 JSON 就是这么来的）',
          pj[0] == 'reply', str(pj)[:80])
    check('★ 它必须被认成坏输出并触发重试（不能当正经回答放行）',
          cs.looks_broken(JUNK) and cs.should_retry(pj, PS))

    # ★ 回归（实测翻车，用户看到的是红色的 "Invalid tool parameters"）：
    #   模型把工具调用的 JSON **包进 ```json 围栏**时，_attach_code_block 的正则
    #   会把**那坨 JSON 本身**当成「要写的内容」，挂到工具参数上 ——
    #   给 Read 塞一个它根本没有的 `content` 字段，上游直接拒。
    #   根因是提示词漏了「不要 markdown 代码块」那句（加叙述规则时删掉的），
    #   但解析层也必须挡得住 —— 提示词是说服，不是保证。
    fenced = ('我先读一下这个文件。\n\n```json\n'
              '{"tool_use": {"name": "Read", "input": {"file_path": "D:' + B + 'a.py"}}}\n```')
    rf = cs.parse_reply(fenced)
    check('★ 围栏包着工具调用时，不能把那坨 JSON 当成参数塞进去',
          rf[0] == 'tools' and set(rf[1][0]['input']) == {'file_path'},
          str(rf[1][0]['input'])[:140])
    check('★ 叙述末尾的围栏被剥掉（不能给用户看一句半截围栏）',
          rf[2] == '我先读一下这个文件。', repr(rf[2]))

    # 不认识的工具 + 代码块 → **什么都不填**。瞎猜字段名必然参数非法，比不填还糟。
    ru = cs.parse_reply('{"tool_use": {"name": "SomeUnknownTool", "input": {"a": 1}}}'
                        '\n```\n随便什么\n```')
    check('★ 不认识的工具不瞎填字段（猜错必然被上游拒）',
          set(ru[1][0]['input']) == {'a'}, str(ru[1][0]['input'])[:140])

    # 子代理的长字段叫 prompt，不叫 content —— 漏了它会掉进上面那条瞎猜
    rt = cs.parse_reply('{"tool_use": {"name": "Task", "input": {"description": "查"}}}'
                        '\n```\n去查一下 X\n```')
    check('Task 的长字段 prompt 能走代码块补上',
          (rt[1][0]['input'].get('prompt') or '').strip() == '去查一下 X',
          str(rt[1][0]['input'])[:140])

    # ★ 回归（e2e 实测红的）：**两个围栏** —— 第一个包 JSON，第二个才是内容。
    #   抓代码块的正则是贪婪的（必须贪婪），于是它会从第一个 ``` 一路吃到最后一个，
    #   把 JSON 和围栏一起圈成「文件内容」。不拦就是**静默写出垃圾文件**，
    #   拦了就是白重试（e2e 那次就是这么红的）。正确做法是先切掉 JSON 那段。
    two = ('我这就写。\n\n```json\n'
           '{"tool_use": {"name": "Write", "input": {"file_path": "D:' + B + 'a.py"}}}\n'
           '```\n```python\n'
           'print("hello")\n'
           'print("world")\n'
           '```')
    r2f = cs.parse_reply(two)
    check('★ 两个围栏（包 JSON 的 + 真内容的）→ 内容块要取对',
          r2f[0] == 'tools'
          and (r2f[1][0]['input'].get('content') or '').strip()
              == 'print("hello")\nprint("world")',
          repr(r2f[1][0]['input'].get('content'))[:140])
    check('★ 两围栏时叙述也不能带上 JSON 那段',
          r2f[2] == '我这就写。', repr(r2f[2]))

    # ★ 回归：**内容块在前、工具调用在后**。这时 JSON 前面紧挨着的那个 ```
    #   是**内容块的收尾围栏**，不是「包 JSON 的开头围栏」—— 两者长得一模一样。
    #   第一版靠「紧邻的是不是 ```」判断，于是把已经拿到手的内容**整段切掉**，
    #   表现成「Write 缺 content」（静默丢数据）。判据必须是围栏数量的奇偶。
    rev = ('我这就写。\n```python\n'
           'print("hello")\nprint("world")\n'
           '```\n'
           '{"tool_use": {"name": "Write", "input": {"file_path": "D:' + B + 'a.py"}}}')
    rrev = cs.parse_reply(rev)
    check('★ 内容块在前、JSON 在后 → 内容不能被切掉',
          rrev[0] == 'tools'
          and (rrev[1][0]['input'].get('content') or '').strip()
              == 'print("hello")\nprint("world")',
          repr(rrev[1][0]['input'].get('content'))[:140])

    # 短内容直接写 JSON 的老写法必须继续有效
    r2 = cs.parse_reply('{"tool_use": {"name": "Write", "input": '
                        '{"file_path": "D:' + B + 'a.py", "content": "print(1)"}}}')
    check('短内容走 JSON 的老写法仍然有效',
          r2[0] == 'tools' and r2[1][0]['input'].get('content') == 'print(1)', str(r2)[:100])

    # 没有代码块时不能凭空造内容
    r4 = cs.parse_reply('{"tool_use": {"name": "Write", "input": {"file_path": "D:' + B + 'a.py"}}}')
    check('没有代码块时不凭空造 content',
          r4[0] == 'tools' and not r4[1][0]['input'].get('content'), str(r4)[:100])

    # ★ 回归：**内容里自带围栏**时不能被截断。
    #   抓代码块的正则曾经是非贪婪的，遇到内容里第一个 ``` 就收尾。而内容里
    #   带围栏太正常了 —— 写 .md、写带示例的脚本、写这个项目自己的文档都是。
    #   后果是文件被**静默截断**成开头几行：不报错、日志干净，打开文件才发现
    #   少了一大半。这是最危险的一类 bug（静默的数据损坏）。
    md_body = '# 标题\n\n```python\nprint(1)\n```\n\n完。'
    r5 = cs.parse_reply('{"tool_use": {"name": "Write", "input": {"file_path": "D:' + B
                        + 'a.md"}}}\n```markdown\n' + md_body + '\n```')
    got5 = r5[1][0]['input'].get('content', '') if r5[0] == 'tools' else ''
    check('★ 内容里带 ``` 时不被静默截断', got5.strip() == md_body,
          f'原始 {len(md_body)} 字，取到 {len(got5)} 字：{got5[:40]!r}')

    section('单元 · 中间文字（叙述）+ 响应混排')
    # ★ 用户的实际抱怨：「dsc 基本上全是命令，只有最后会输出一段结果文字，
    #   而直接接 API 的不仅是一堆命令，还能看到每个阶段在干什么」。
    #   根因是叙述被丢在两处：parse_reply 不返回它、to_anthropic_tools 只造
    #   tool_use 块。这里把两处都钉住。
    rp = cs.parse_reply('先看看目录里有什么。\n'
                        '{"tool_use": {"name": "Read", "input": {"file_path": "D:' + B + 'a.py"}}}')
    check('★ 夹带的叙述被保留成第三个返回值',
          rp[0] == 'tools' and '先看看目录' in (rp[2] or ''), repr(rp[2])[:80])
    check('纯 JSON（没有叙述）时第三个返回值为空',
          cs.parse_reply('{"tool_use": {"name": "Read", "input": {}}}')[2] == '')
    check('普通回答时第三个返回值也是空（正文在 [1] 里）',
          cs.parse_reply('这就是答案。')[0] == 'reply'
          and cs.parse_reply('这就是答案。')[2] == '')

    # ★ 回归（用户真实会话，加叙述之后才出现的新形状）：
    #   **叙述 + 裸写法**（OpenAI 风格 {"name":..., "arguments":...}，没有 tool_use 外壳）。
    #   整段既不以 { 开头、也没有 tool_use 标记 → 抠不出来、looks_broken 也放行 →
    #   这坨 JSON 被当成「回答」交出去，Claude Code 没有工具可调，这一轮就结束。
    #   两个识别口子原本各自都够用，是「叙述 + 裸写法」把它们中间的缝露出来了。
    bare = ('我先看一下目录结构。\n\n'
            '{"name": "PowerShell", "arguments": {"command": "Get-ChildItem"}}')
    rb = cs.parse_reply(bare)
    check('★ 叙述 + 裸写法也能抠出来',
          rb[0] == 'tools' and rb[1][0]['name'] == 'PowerShell'
          and rb[1][0]['input'].get('command') == 'Get-ChildItem',
          str(rb)[:140])
    check('★ 它的叙述也要保留', rb[2] == '我先看一下目录结构。', repr(rb[2]))

    # 反面：正常回答里举例说明 JSON 长什么样，**不能**被当成工具调用
    example = '举个例子，一个对象可以长这样：{"name": "张三", "age": 18}，就这样。'
    check('★ 正常回答里举的例子不能被误判成工具调用',
          cs.parse_reply(example)[0] == 'reply', str(cs.parse_reply(example))[:110])

    # 正文里先举个「像调用但缺参数字典」的例子，后面才是真调用 —— 要跳过前者找到后者
    mixed = ('格式是这样的：{"name": "x"} 只是个说明。\n'
             '现在开始：\n'
             '{"name": "Read", "arguments": {"file_path": "D:' + B + 'a.py"}}')
    rm = cs.parse_reply(mixed)
    check('★ 先举例、后真调用 → 要找到真的那个',
          rm[0] == 'tools' and rm[1][0]['name'] == 'Read', str(rm)[:140])

    # 响应混排：text 块必须在 tool_use 块**前面**，且 stop_reason 仍是 tool_use
    # （带工具就要让 Claude Code 继续跑，改成 end_turn 这一轮就结束了）
    m = cs.to_anthropic_tools([{'name': 'Read', 'input': {'file_path': 'a'}}], text='我读一下')
    check('★ 带叙述时 content = [text, tool_use]',
          [b['type'] for b in m['content']] == ['text', 'tool_use'],
          str([b['type'] for b in m['content']]))
    check('★ 混排时 stop_reason 仍是 tool_use（不能变 end_turn）',
          m['stop_reason'] == 'tool_use', m['stop_reason'])
    check('不带叙述时不凭空插 text 块',
          [b['type'] for b in cs.to_anthropic_tools(
              [{'name': 'Read', 'input': {}}])['content']] == ['tool_use'])

    # ★ sse_events 原先**零测试覆盖**，而混排是这轮新引入的用法 —— 钉住它。
    ev = cs.sse_events(m)
    # 注意数的是 `event: content_block_stop` 而不是 `content_block_stop` ——
    # 后者在事件名和数据行里各出现一次，会把 2 个块数成 4 个（踩过）。
    check('★ sse 流里 text 块在前、tool_use 块在后（index 0 / 1）',
          ev.index('"index": 0') < ev.index('"index": 1')
          and ev.count('event: content_block_stop') == 2,
          f'stop 事件 {ev.count("event: content_block_stop")} 个')

    section('单元 · 模型申请开开关（[[SEARCH]] 标记）')
    # ★ 真实 API 那边模型自己调 WebSearch；这边换成「它写标记、我们替它开」。
    #   判定必须**严**：只认整条回复就是标记 —— 模型解释这个协议本身时也会
    #   写出这几个字，误判就会白等 30 秒重搜一轮。
    check('[[SEARCH]] 被认出',
          cs.parse_need_marker('[[SEARCH]]') == (True, False))
    check('[[THINK]] 被认出',
          cs.parse_need_marker('[[THINK]]') == (False, True))
    check('[[SEARCH+THINK]] 两个都要',
          cs.parse_need_marker('[[SEARCH+THINK]]') == (True, True))
    check('前后有空白 / 换行也认',
          cs.parse_need_marker('\n  [[search]]  \n') == (True, False))
    check('第一行是标记、后面还写了别的 —— 也认',
          cs.parse_need_marker('[[SEARCH]]\n顺便说一句这个功能刚加上。') == (True, False))
    check('★ 但「标记 + 同一行还有别的字」不算（那是解释协议，不是申请）',
          cs.parse_need_marker('[[SEARCH]] 这个词的意思是申请联网。') == (False, False))
    check('★ 正常回答里提到这些字**不能**触发（整条就是标记才算）',
          cs.parse_need_marker('你可以用 [[SEARCH]] 让我联网搜。') == (False, False))
    check('★ 带工具调用的回复不能被误判成申请',
          cs.parse_need_marker('{"tool_use": {"name": "Read", "input": {}}}') == (False, False))
    check('空回复不误判', cs.parse_need_marker('') == (False, False))

    section('单元 · 工具描述截断（交互类工具不能砍）')
    # ★ 这几个工具的描述原先一律被截到 400 字，「什么时候该用」那段直接被砍光 ——
    #   模型压根不知道有这回事，于是 dsc 里从来不弹选择框、不进 plan mode。
    long_desc = 'X' * 1500
    rq = cs.render_tools([{'name': 'AskUserQuestion', 'description': long_desc,
                           'input_schema': {}}])
    check('★ 交互类工具的描述不被截到 400 字',
          'X' * 1500 in rq, f'只留了 {len(rq)} 字')
    rn = cs.render_tools([{'name': 'SomeNormalTool', 'description': long_desc,
                           'input_schema': {}}])
    check('★ 普通工具仍然截到 400 字（防止提示词悄悄膨胀）',
          'X' * 400 in rn and 'X' * 401 not in rn)

    # ★ 回归：引导必须**真的进到**提示词里。
    #   早先的截断逻辑写死 `parts[:3]`，往工具清单后面加一段，正好会被切掉 ——
    #   而且不报错、日志干净、测试全绿（这类静默失效最危险）。
    #   现在改成用 len() 记账，这条用例就是那个的守门员。
    _t = [{'name': 'Read', 'description': '读文件', 'input_schema': {}}]
    _msgs = [{'role': 'user', 'content': '你好'}]
    check('★ 交互类工具的引导进得了提示词（build_prompt）',
          'AskUserQuestion' in cs.build_prompt('你是助手', _msgs, _t)
          and 'EnterPlanMode' in cs.build_prompt('你是助手', _msgs, _t))
    check('没有工具时全程不提这些工具（免得模型凭空编）',
          'AskUserQuestion' not in cs.build_prompt('你是助手', _msgs, []))
    # delta 是**每轮**都走的路径，规则只在第一轮出现的话，模型第二轮就忘光了
    _d = cs.build_delta_prompt(_msgs)
    check('★ delta 路径也带压缩版引导（否则第二轮起就忘光）',
          'AskUserQuestion' in _d and '不要再调工具' not in _d, _d[:80])
    # ★ 「工具名照抄清单」也必须进 delta。只在 build_prompt 里的话，模型从
    #   第二轮起就把「跑命令那个叫 PowerShell」忘了 —— 而写命令恰恰发生在
    #   后续轮次。这正是第十五轮补 缺陷 34 的同一个坑（规则只在第一轮出现）。
    check('★ delta 路径也提醒工具名（跑命令那个叫 PowerShell）',
          'PowerShell' in _d, _d[:150])

    section('单元 · 工具名认错（Bash → PowerShell）')
    # ★ 回归（真实使用实测）：模型写对了 PowerShell 命令，**唯独名字叫成了 Bash**
    #   （它的训练里「跑命令」就叫 Bash）。旧代码把这种回复换成一句
    #   「请换个方式提问」当**回答** —— 那是助手消息，Claude Code 收到就结束这一轮，
    #   用户只能手打「继续」，整轮白费。
    PS_ONLY = [{'name': 'PowerShell', 'input_schema': {}},
               {'name': 'Read', 'input_schema': {}}]
    _p = cs.parse_reply('{"tool_use": {"name": "Bash", "input": {"command": "Get-Location"}}}')
    _a = cs.apply_tool_aliases(_p, PS_ONLY)
    check('★ Bash 会被自动改名成 PowerShell',
          _a[1][0]['name'] == 'PowerShell' and _a[1][0]['input'] == {'command': 'Get-Location'},
          str(_a[1])[:120])
    # 两个都在 → 尊重模型的选择，别乱改
    _both = cs.apply_tool_aliases(_p, [{'name': 'Bash', 'input_schema': {}}] + PS_ONLY)
    check('Bash 本身可用时不改名', _both[1][0]['name'] == 'Bash')
    # PowerShell 不在可用列表里 → 没得改
    _no = cs.apply_tool_aliases(_p, [{'name': 'Read', 'input_schema': {}}])
    check('没有 PowerShell 可改时保持原样', _no[1][0]['name'] == 'Bash')
    check('普通回答不受影响',
          cs.apply_tool_aliases(('reply', '你好', ''), PS_ONLY)[0] == 'reply')

    check('★ 名字全错时要走重试，而不是回一句「请换个方式提问」',
          (cs.retry_reason(_p, PS_ONLY) or '').find('PowerShell') >= 0,
          repr(cs.retry_reason(_p, PS_ONLY))[:100])
    # 部分名字错的情况不归 retry_reason 管（调用方会过滤掉坏的、留住好的）
    _mix = ('tools', [{'name': 'Bash', 'input': {'command': 'x'}},
                      {'name': 'Read', 'input': {'file_path': 'a'}}], '')
    check('部分名字错时不触发重试（过滤器会留住好的那个）',
          cs.retry_reason(_mix, PS_ONLY) is None)

    # ★ 缺陷 46：**重试路径漏了改名**。原先 `apply_tool_aliases(parse_reply(...))`
    #   只写在主路径上 —— 而重试恰恰是最容易写出 `Bash` 的地方（模型被要求把
    #   刚才那个调用重发一遍时，更依赖训练里的老名字）。实测日志正是这个形状：
    #     02:30:12 [重试] 第 1/2 次：输出像是坏掉的工具调用，重新问一次
    #     02:30:23 [警告] 模型编造了工具 ['Bash'] … 丢弃 → 42 字回答，这轮结束
    #   修法是**收成一个入口**（parse_and_align），不是「记得两处都改」。
    _pa = cs.parse_and_align(
        '{"tool_use": {"name": "Bash", "input": {"command": "Get-Location"}}}', PS_ONLY)
    check('★ parse_and_align 会改名（两个解析点共用的那个入口）',
          _pa[0] == 'tools' and _pa[1][0]['name'] == 'PowerShell', str(_pa[:2])[:120])
    check('parse_and_align 对普通回答原样返回',
          cs.parse_and_align('这就是答案。', PS_ONLY)[0] == 'reply')

    section('单元 · 工具调用形状（听刻会话实测的两种漏法）')
    # ★ 缺陷 48：标记正则不认空白。`{"name"` / `{ "name"` 只认「紧跟」和
    #   「一个空格」，而模型会 pretty-print：
    #       {
    #         "name": "AskUserQuestion",
    #   —— 一个都命中不了，扫描根本不去看那个位置，整个调用被漏掉、
    #   当成「回答」交给 Claude Code，**那一轮就此结束**。
    #   实测 2026-10-03 23:31 听刻会话：同一段文本只差一个换行加缩进，
    #   一个废一个通（日志 `[回答] 1637 字`）。
    _pp = ('我先问清楚再动手。\n\n{\n  "name": "AskUserQuestion",\n'
           '  "arguments": {"questions": []}\n}')
    _r = cs.parse_reply(_pp)
    check('★ 裸写法 pretty-print（换行 + 缩进）也要抠得出来',
          _r[0] == 'tools' and _r[1][0]['name'] == 'AskUserQuestion', str(_r)[:110])
    check('同一行 / 单个空格那两种写法没被改坏',
          cs.parse_reply('引言。\n{"name": "AskUserQuestion", "arguments": {"questions": []}}')[0] == 'tools'
          and cs.parse_reply('引言。\n{ "name": "AskUserQuestion", "arguments": {"questions": []}}')[0] == 'tools')

    # ★ 缺陷 49：`"tool"` 这个键名没进**任何**一张表 —— 而 `_norm_tool` 明明认
    #   `obj.get('tool')`。两个口子不一致，于是参数**完全合规**的写法
    #   也从来轮不到被归一化。修法是三处共用一个键名词汇表。
    _t2 = cs.parse_reply('先确认脚本位置。\n\n{"tool": "PowerShell", "input": {"command": "Get-Location"}}')
    check('★ {"tool": …, "input": …} 也要认（_norm_tool 一直收这个键）',
          _t2[0] == 'tools' and _t2[1][0]['name'] == 'PowerShell'
          and _t2[1][0]['input'] == {'command': 'Get-Location'}, str(_t2[:2])[:110])
    # 平铺写法：参数直接摊在顶层 —— 不收进 input 就等于**把命令丢掉**，
    # 调用会以「缺 command」被拦下，白烧一个来回。
    _t3 = cs.parse_reply('先确认脚本位置。\n\n{"tool": "PowerShell", "command": "Get-Location"}')
    check('★ 平铺参数要收进 input（不然命令就丢了）',
          _t3[0] == 'tools' and _t3[1][0]['input'] == {'command': 'Get-Location'},
          str(_t3[:2])[:110])
    # ★ 兜底：**解不出来**的（截断 / 转义炸了）必须判成「坏」去重试，
    #   绝不许泄漏成「回答」。把失败伪装成回答是最坏的一种失败 ——
    #   上游不会重试、用户只能手打「继续」，而且日志干干净净。
    check('★ 解不出来的平铺写法也要判成「坏」（兜底重试，不许泄漏）',
          cs.looks_broken('先确认一下。\n\n{"tool": "PowerShell", "command": "Get-Loc'))

    section('单元 · Windows 路径转义')
    # ★ 回归：模型写路径时几乎从不转义，而 JSON 里 \b \f \n \r \t \u 都是
    #   **合法转义** —— 于是路径被静默吃掉：
    #     C:\...\buck_hw\figs.py  →  C:\...<退格>uck_hw<换页>igs.py
    #   不报错、文件写到奇怪的地方。必须在解析前先修。
    BS = chr(92)

    def _fp(json_text):
        o = cs._loads_lenient(json_text)
        return (o or {}).get('file_path')

    check('单反斜杠路径能正确还原',
          _fp('{"file_path": "C:' + BS + 'Users' + BS + 'MOONFISH' + BS +
              'buck_hw' + BS + 'figs.py"}')
          == 'C:' + BS + 'Users' + BS + 'MOONFISH' + BS + 'buck_hw' + BS + 'figs.py')
    check('已正确转义的路径不被改坏',
          _fp('{"file_path": "C:\\\\a\\\\b.py"}') == 'C:' + BS + 'a' + BS + 'b.py')
    check('\\t \\n \\r 不被当成真控制字符',
          _fp('{"file_path": "D:' + BS + 'temp' + BS + 'new' + BS + 'repo"}')
          == 'D:' + BS + 'temp' + BS + 'new' + BS + 'repo')
    check('小写 \\users 不会被当成 unicode 转义',
          _fp('{"file_path": "C:' + BS + 'users' + BS + 'x.py"}')
          == 'C:' + BS + 'users' + BS + 'x.py')

    section('单元 · 坏输出识别')
    check('认出坏掉的工具调用', cs.looks_broken('{"tool_use": {"name": "X", "input": {"a": "b\nc"}}}'))
    check('正常回答不误判', not cs.looks_broken('# 标题\n\n这是正常回答'))
    check('空串不误判', not cs.looks_broken(''))

    # ★ 回归：**先解释一句再给坏掉的调用**（混合输出）同样要算坏。
    #   早先这里要求「整段以 { 开头」才算 —— 混合输出不满足，于是既不重试、
    #   也不当工具调用，用户直接看到一坨 JSON。而模型在上一步失败之后特别
    #   爱用这种写法。
    mixed_broken = '好，那条命令挂了。这次改成这样：\n\n{"tool_use": {"name": "X", "input": {"a": "b\nc"}}}'
    check('★ 解释 + 坏调用（混合输出）也认出来', cs.looks_broken(mixed_broken))

    # 但不能因为「文本里出现了 tool_use 这个词」就误判 —— 模型解释我们的协议时
    # 会复述这个词，那是正经回答，白重试一轮（踩过）。
    check('提到 tool_use 这个词不算坏',
          not cs.looks_broken('我不会输出 tool_use 这种东西，直接回答你：你好。'))
    # 正经回答里举例 {"name": ..., "input": ...} 也不算
    check('回答里举例 name/input 不算坏',
          not cs.looks_broken('请求体长这样：{"name": "张三", "input": "值"}，注意 name 必填。'))

    # ★ 回归：模型只吐出一个 `{"` 就收工。这是**被截断的 JSON** ——
    #   按关键字判据完全抓不到（它一个关键字都还没写到），于是被当成「正经回答」
    #   交给 Claude Code：用户界面上就是一个光秃秃的 `{"`，会话卡死在那儿。
    #   实测这种 `[回答] 2 字` 一天出现了 4 次。
    check('★ 光秃秃的 { 没结尾，认得出是坏的', cs.looks_broken('{"'))
    check('★ 写到一半的 JSON 也认得出', cs.looks_broken('{"tool_use": {"name": "Write"'))
    check('★ 缺结尾的长 JSON 也认得出', cs.looks_broken('{"tool_use": {"name": "Write", "input": {}}'))
    # 但**有结尾**的完整 JSON 不算坏 —— 那可能是模型在正经举例
    check('完整 JSON 对象不算坏', not cs.looks_broken('{"name": "张三", "age": 18}'))
    check('花括号开头、有结尾的普通文字不算坏',
          not cs.looks_broken('{这不是 JSON，是普通文字}'))
    # 而且这种「坏的」必须真的能触发重试（前提是这轮带了工具）
    check('★ 半截 JSON 会触发重试',
          cs.should_retry(cs.parse_reply('{"'), [{'name': 'Write'}]))

    # ★ 重试闸门本身。这条规则原先内联在 HTTP handler 里，还挂着一个
    #   `len(...) < 4000` 的长度上限，导致**最需要重试的长工具调用被挡掉**。
    #   抽成 should_retry() 之后能直接测。
    # 注意这里用的是**真换行**（不是 \n 两个字符）—— 模型把长内容内联进 JSON
    # 时最容易犯的错就是这个：字符串里出现了未转义的真换行，JSON 直接作废。
    # （第一版我用 `\n` 两个字符构造，结果那是**合法** JSON、能被正常解析，
    #   测试反过来抓到了我自己理解错。）
    leak_small = ('{"tool_use": {"name": "Write", "input": {"file_path": "D:' + chr(92)
                  + 'a.py", "content": "x\ny"}}}')
    leak_big = ('{"tool_use": {"name": "Write", "input": {"file_path": "D:' + chr(92)
                + 'a.py", "content": "' + ('x' * 70 + '\n') * 300 + '"}}}')
    check(f'坏调用会触发重试（短，{len(leak_small)} 字）',
          cs.should_retry(cs.parse_reply(leak_small), [{'name': 'Write'}]))
    check(f'★ 坏调用会触发重试（长，{len(leak_big)} 字）—— 不能有长度上限',
          cs.should_retry(cs.parse_reply(leak_big), [{'name': 'Write'}]))
    # 没有工具时不能重试：模型真吐出坏调用也无从执行，白等一轮还会拖超时上游
    check('没带工具时不重试',
          not cs.should_retry(cs.parse_reply(leak_small), []))
    # 正常回答不能重试
    check('正常回答不重试',
          not cs.should_retry(cs.parse_reply('# 标题\n\n正常回答'), [{'name': 'Write'}]))

    # ★ 回归：工具调用**缺长字段**时不能就这么放行。
    #   实测翻车：模型那条回复被网页长度上限截断，Write 只解析出 file_path、
    #   content 是空的。这种调用发上去，Claude Code 只回一句
    #   "Error writing file" —— 真正的原因谁都看不出来。
    WT = [{'name': 'Write'}]
    no_content = ('tools', [{'name': 'Write', 'input': {'file_path': 'D:' + chr(92) + 'a.py'}}])
    check('★ 能认出 Write 缺 content', cs.incomplete_tool(no_content[1]) is not None)
    check('★ 缺 content 要触发重试', cs.retry_reason(no_content, WT) is not None)
    check('★ 重试提示里要说清怎么分段写',
          '分段' in (cs.retry_reason(no_content, WT) or '')
          or '分几段' in (cs.retry_reason(no_content, WT) or ''))
    check('Write 有 content 就不算缺',
          cs.incomplete_tool([{'name': 'Write',
                               'input': {'file_path': 'D:' + chr(92) + 'a.py',
                                         'content': 'print(1)'}}]) is None)
    # 短参数为空的工具（比如 Read 没给路径）不归这道闸管 —— 那是另一回事
    check('Read 这类短参数工具不误报',
          cs.incomplete_tool([{'name': 'Read', 'input': {}}]) is None)
    # 正常的工具调用不该被重试
    good = cs.parse_reply('{"tool_use": {"name": "Write", "input": {"file_path": "D:'
                          + chr(92) + 'a.py"}}}\n```python\nprint(1)\n```')
    check('正常 Write 不重试', cs.retry_reason(good, WT) is None, str(good)[:80])
    # 解析成功的工具调用不重试
    check('解析成功的工具调用不重试',
          not cs.should_retry(
              cs.parse_reply('{"tool_use": {"name": "Read", "input": {"file_path": "a"}}}'),
              [{'name': 'Read'}]))

    section('单元 · 提示词构造')
    tools = [{'name': 'Read', 'description': '读文件',
              'input_schema': {'type': 'object', 'properties': {'p': {'type': 'string'}}}}]
    p = cs.build_prompt('你是助手', [{'role': 'user', 'content': '你好'}], tools)
    check('提示词含系统提示', '你是助手' in p)
    check('提示词含工具名', 'Read' in p)
    check('提示词含输出规则', 'tool_use' in p)
    check('提示词含用户消息', '你好' in p)
    check('提示词长度合理', 500 < len(p) < 20000, f'{len(p)} 字')

    # 图片块要变成占位符，不能塞 base64
    msgs = [{'role': 'user', 'content': [
        {'type': 'text', 'text': '看图'},
        {'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/png',
                                     'data': 'iVBORw0KGgo=' * 100}},
    ]}]
    p2 = cs.build_prompt('s', msgs, [])
    check('图片变占位符不塞 base64', 'iVBORw0KGgo' not in p2 and '附件' in p2)

    section('单元 · 图片提取')
    import tempfile
    msgs = [{'role': 'user', 'content': [
        {'type': 'tool_result', 'content': [
            {'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/png',
                                         'data': 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=='}},
        ]},
    ]}]
    paths = cs.extract_attachments(msgs)
    check('从 tool_result 里挖出图片', len(paths) == 1, str(paths))
    if paths:
        check('图片文件真的写出来了', os.path.exists(paths[0]) and os.path.getsize(paths[0]) > 50)
        check('扩展名是 png', paths[0].endswith('.png'))
        check('是合法 PNG 头', open(paths[0], 'rb').read(4) == b'\x89PNG')
        os.remove(paths[0])

    section('单元 · 一致性（防复发）')
    # ★ 背景：这套「取 baseline → 发送 → 等待」的逻辑曾经在四个地方各写一遍
    #   （CLI / MCP / shim / api_server），其中 MCP 那处把 baseline 传成了
    #   「节点个数」而不是「上一轮回答的文本」—— 字符串比整数恒为真，
    #   于是静默返回上一轮的答案，还不报错。
    #   收敛成 ds.ask() 之后，这条测试保证没人再绕过它自己拼。
    #
    # 用 ast 而不是正则：正则只能匹配 `ds.wait_answer(` 这一种写法，
    # 换成 `from deepseek_ask import wait_answer` 或别的别名就失效了；
    # 而且「按行首 # 判注释」对三引号字符串完全不灵。
    # ast 看的是真正的调用节点，两种写法都拦得住。
    root = os.path.dirname(os.path.abspath(__file__))
    offenders = []
    for name in ('claude_shim.py', 'api_server.py', 'mcp_server.py'):
        hits = _bypass_calls(os.path.join(root, name))
        if hits:
            offenders.append(f'{name}:{hits}')
    check('上层都走 ds.ask_in_session()，没有自己拼前置逻辑',
          not offenders, f'这些文件绕过了公共入口：{offenders}')

    # ★ 负向验证：这个检查必须**真的能抓到违规**，否则它就是摆设。
    #   用一个临时文件模拟「绕过公共入口自己拼」的写法，确认会被抓出来。
    import tempfile
    bad_src = (
        'import deepseek_ask as ds\n'
        'def f(page):\n'
        '    ds.ensure_page(page, new_chat=True)\n'      # ← 绕过 ask_in_session
        '    ds.set_think(page, False)\n'
        '    return ds.ask(page, "hi")\n'
    )
    bad_path = os.path.join(tempfile.gettempdir(), '_fake_bypass_server.py')
    try:
        with open(bad_path, 'w', encoding='utf-8') as fh:
            fh.write(bad_src)
        caught = _bypass_calls(bad_path)
        check('检查真的能抓到绕过（负向验证）', len(caught) >= 3, f'只抓到 {caught}')

        # 也要能识别 from-import 的写法（正则版本拦不住的那种）
        alt = 'from deepseek_ask import wait_answer\ndef g(p, b):\n    return wait_answer(p, b, False)\n'
        with open(bad_path, 'w', encoding='utf-8') as fh:
            fh.write(alt)
        check('from-import 的写法也拦得住', len(_bypass_calls(bad_path)) >= 1,
              str(_bypass_calls(bad_path)))

        # 注释和文档字符串里提到函数名**不该**误报
        ok_src = ('# 别用 ds.wait_answer( 这种写法\n'
                  'def h():\n'
                  '    """文档里提一句 wait_answer 不算违规"""\n'
                  '    return 1\n')
        with open(bad_path, 'w', encoding='utf-8') as fh:
            fh.write(ok_src)
        check('注释/文档字符串不误报', _bypass_calls(bad_path) == [],
              str(_bypass_calls(bad_path)))

        # ★ 每一个 ask_web 调用都必须带上附件。
        #   重试那处漏传过 —— 而重试开的是**新对话**，图就彻底丢了，
        #   提示词却还写着「附件就是内容本身」，模型只能回「我看不到图」，
        #   然后重读、再重试，死循环。属于「少写一个参数、后果完全看不出来」
        #   的那种 bug，靠人眼看代码拦不住。
        shim = os.path.join(root, 'claude_shim.py')
        miss = _ask_web_without_attachments(shim)
        check('★ shim 里每个 ask_web 调用都带了附件', miss == [], f'漏传的行号 {miss}')

        with open(bad_path, 'w', encoding='utf-8') as fh:
            fh.write('def g():\n    ask_web(p, None, t, start_limit=1)\n')
        check('漏传附件的写法拦得住（负向验证）',
              _ask_web_without_attachments(bad_path) == [2],
              str(_ask_web_without_attachments(bad_path)))

        with open(bad_path, 'w', encoding='utf-8') as fh:
            fh.write('def g():\n'
                     '    ask_web(p, None, t, att)\n'
                     '    ask_web(p, None, t, attachments=att)\n')
        check('带附件的两种写法都不误报',
              _ask_web_without_attachments(bad_path) == [],
              str(_ask_web_without_attachments(bad_path)))
    finally:
        try:
            os.remove(bad_path)
        except Exception:
            pass

    section('单元 · 跨进程浏览器锁')
    # 只有一个浏览器，多进程同时驱动会互相把页面导航走。
    # 这个锁保证同一时刻只有一个进程在开浏览器。
    #
    # ★ 这一节**必须用临时锁文件**，绝不能拿生产那把（locks\main.lock）做实验。
    #   早先是直接对 dsl.BROWSER_LOCK 先 remove、再往里写一个假 PID 999999 ——
    #   dsc 正在跑的时候跑自测，等于**把别人手里的锁删掉、还伪造出「持有者已死」**：
    #   下一个进程看到锁没了就直接抢，看到假 PID 就判定「持有者不在了、接管」，
    #   于是两个进程同时驱动同一个浏览器、互相把页面导航走。
    #   表现就是「接着同一个对话问，浏览器却开了另一个对话」—— 那个现象第六轮
    #   查了半天没复现出来，真凶一直在这里。
    import deepseek_ask as dsl
    real_lock = dsl.BROWSER_LOCK

    # 跑之前先记下生产锁的样子。注意**不能断言它「不存在」** —— 用户可能正
    # 开着 dsc，那把锁本来就该在；能断言的只是「自测前后它没变过」。
    prod_lock = os.path.join(dsl.LOCK_DIR, 'main.lock')

    def _prod_snapshot():
        try:
            return (os.path.exists(prod_lock), open(prod_lock).read())
        except Exception:
            return (os.path.exists(prod_lock), '')

    prod_before = _prod_snapshot()
    lockf = os.path.join(tempfile.gettempdir(), f'ds_selftest_{os.getpid()}.lock')
    dsl.BROWSER_LOCK = lockf            # lock_path(None) 是运行时读这个全局的
    try:
        if os.path.exists(lockf):
            os.remove(lockf)

        with dsl.browser_lock(timeout=5):
            check('能拿到锁', os.path.exists(lockf))
            # 锁里应该写着当前进程号
            try:
                owner = int(open(lockf).read().strip())
                check('锁里记的是本进程 pid', owner == os.getpid(), f'{owner} vs {os.getpid()}')
            except Exception as e:
                check('锁里记的是本进程 pid', False, str(e))
        check('退出后锁被释放', not os.path.exists(lockf))

        # ★ 死锁接管：模拟「持有者进程已经死了」
        with open(lockf, 'w') as f:
            f.write('999999')          # 一个几乎肯定不存在的 pid
        t0 = time.time()
        try:
            with dsl.browser_lock(timeout=30, poll=0.2):
                dt = time.time() - t0
                check('★ 持有者死了能自动接管', dt < 20, f'等了 {dt:.1f} 秒')
        except Exception as e:
            check('★ 持有者死了能自动接管', False, str(e))
        check('接管后锁也释放了', not os.path.exists(lockf))
    finally:
        dsl.BROWSER_LOCK = real_lock
        try:
            os.remove(lockf)
        except Exception:
            pass

    # ★ 防复发：上面那段要是哪天又被改回「拿生产锁做实验」，这条会立刻红。
    #   光靠注释拦不住 —— 第六轮那次就是注释写着「别抢浏览器」，代码照样在抢。
    prod_after = _prod_snapshot()
    check('★ 自测没有碰生产锁文件', dsl.BROWSER_LOCK == real_lock and prod_after == prod_before,
          f'自测前 存在={prod_before[0]} 内容={prod_before[1]!r} / '
          f'自测后 存在={prod_after[0]} 内容={prod_after[1]!r}')

    section('单元 · 续写不会连点（防「自己把自己打断」）')
    # ★ 回归：点完「继续生成」必须**等新内容真的开始出来**，不能立刻回到
    #   「文本不变就算写完」那套判据。
    #
    #   踩过：早先只写了 `last_change = time.time()`，而稳定性窗口只有 2.5 秒，
    #   网页点完却要 5~30 秒才吐字 —— 2.5 秒一到就判「又写完了」，再点一次，
    #   6 次点击挤在 28 秒内打完，**把正在进行的续写自己打断了**：文本长度
    #   从头到尾纹丝不动（实测 15270 → 15270 → 15273 字），永远写不完。
    #   最后把半截工具调用交给上游，报 "Error writing file"（content 是空的）。
    #
    #   这里用假页面 + 缩短的时间常数测：点完 0.6 秒后才出现新内容，
    #   两次点击的间隔就必须 ≥ 这个延迟。旧代码会在 0.2 秒内就点第二次。
    import deepseek_ask as dsc
    _saved = (dsc.last_answer_text, dsc.answer_done_rendered, dsc.find_continue_button,
              dsc.click_continue, dsc.STABLE_NORMAL, dsc.POLL, dsc.MAX_CONTINUES)
    GEN_DELAY = 0.6                 # 模拟「点完 0.6 秒后新内容才出现」
    st = {'text': 'X' * 200, 'clicks': [], 'append_at': None}

    def _fake_text(_page):
        if st['append_at'] is not None and time.time() >= st['append_at']:
            st['text'] += 'Y' * 50
            st['append_at'] = None
        return st['text']

    class _Btn:
        def click(self):
            st['clicks'].append(time.time())
            st['append_at'] = time.time() + GEN_DELAY

    _btn = _Btn()
    try:
        dsc.last_answer_text = _fake_text
        dsc.answer_done_rendered = lambda _p: True
        dsc.find_continue_button = lambda _p: _btn
        # ★ 实际点击走的是 click_continue（它内部会重抓元素、退化到 JS 点击），
        #   所以要连它一起换掉 —— 只换 find_continue_button 的话点不到假按钮。
        dsc.click_continue = lambda _p, *_a, **_k: (_btn.click(), True)[1]
        dsc.STABLE_NORMAL = 0.2
        dsc.POLL = 0.05
        dsc.MAX_CONTINUES = 2
        txt, err = dsc.wait_answer(object(), '', False,
                                   start_limit=5.0, total_limit=20.0)
        gaps = [st['clicks'][i + 1] - st['clicks'][i]
                for i in range(len(st['clicks']) - 1)]
        check('续写按钮被认出来并点了', len(st['clicks']) >= 2, str(len(st['clicks'])))
        check('★ 两次点击之间要等新内容（不连点）',
              bool(gaps) and min(gaps) >= GEN_DELAY * 0.8,
              '最短间隔 %.2f 秒，生成延迟 %.2f 秒' % (min(gaps) if gaps else -1, GEN_DELAY))
        check('★ 续写出来的内容被保住了',
              err is None and 'Y' * 50 in (txt or ''), f'{err} / {len(txt or "")} 字')
    finally:
        (dsc.last_answer_text, dsc.answer_done_rendered, dsc.find_continue_button,
         dsc.click_continue, dsc.STABLE_NORMAL, dsc.POLL, dsc.MAX_CONTINUES) = _saved

    # ★ 回归（真实会话）：第一次点击**点了没反应**时，必须换种方式再点，
    #   不能一次就认输。实测真实报错是 DrissionPage 的「该元素没有位置及大小」
    #   （React 重渲染的瞬时抖动）；更阴的一种是**点在空气上而 click() 不报错**
    #   （项目自己的坑 1）—— 那种只有等一等才知道没生效。
    #   早先两者都是直接 return，把半截回答当完整回答交出去 → 上游报「缺 content」。
    _saved_c = (dsc.last_answer_text, dsc.answer_done_rendered, dsc.find_continue_button,
                dsc.click_continue, dsc.STABLE_NORMAL, dsc.POLL, dsc.MAX_CONTINUES,
                dsc.CONTINUE_WAIT, dsc.CONTINUE_CLICK_TRIES)
    st2 = {'text': 'X' * 200, 'clicks': 0, 'append_at': None}
    try:
        def _fake_text2(_page):
            if st2['append_at'] is not None and time.time() >= st2['append_at']:
                st2['text'] += 'Y' * 50
                st2['append_at'] = None
            return st2['text']

        def _fake_click2(_page, *_a, **_k):
            st2['clicks'] += 1
            if st2['clicks'] >= 2:          # 第一次没反应，第二次才生效
                st2['append_at'] = time.time() + 0.3
            return True

        dsc.last_answer_text = _fake_text2
        dsc.answer_done_rendered = lambda _p: True
        dsc.find_continue_button = lambda _p: object()
        dsc.click_continue = _fake_click2
        dsc.STABLE_NORMAL = 0.2
        dsc.POLL = 0.05
        dsc.MAX_CONTINUES = 1
        dsc.CONTINUE_WAIT = 0.6          # 缩短，免得用例真跑 45 秒
        dsc.CONTINUE_CLICK_TRIES = 3
        txt2c, err2c = dsc.wait_answer(object(), '', False,
                                       start_limit=5.0, total_limit=20.0)
        check('★ 点了没反应 → 会换种方式再点（不能一次就认输）',
              st2['clicks'] >= 2, f'只点了 {st2["clicks"]} 次')
        check('★ 第二次点上了，续写内容保住',
              err2c is None and 'Y' * 50 in (txt2c or ''),
              f'{err2c} / {len(txt2c or "")} 字')
    finally:
        (dsc.last_answer_text, dsc.answer_done_rendered, dsc.find_continue_button,
         dsc.click_continue, dsc.STABLE_NORMAL, dsc.POLL, dsc.MAX_CONTINUES,
         dsc.CONTINUE_WAIT, dsc.CONTINUE_CLICK_TRIES) = _saved_c

    section('单元 · 服务器繁忙不能当回答')
    # ★ 回归：服务端限流时页面弹「服务器繁忙，请稍后重试」，这一轮**根本没生成出
    #   回答**，回答气泡里只剩 `{"` 两个字。它被当成「正经回答」交给上游后，
    #   用户界面上就是一个光秃秃的 `{"`，会话卡死 —— 而且从现象里完全看不出
    #   是服务端限流（实测今天碰上 4 次）。
    _saved2 = (dsc.last_answer_text, dsc.answer_done_rendered, dsc.find_continue_button,
               dsc.find_server_busy, dsc.STABLE_NORMAL, dsc.POLL, dsc.BUSY_MIN_CHARS)
    try:
        dsc.answer_done_rendered = lambda _p: True
        dsc.find_continue_button = lambda _p: None
        dsc.STABLE_NORMAL = 0.2
        dsc.POLL = 0.05
        dsc.BUSY_MIN_CHARS = 10

        dsc.last_answer_text = lambda _p: '{"'
        dsc.find_server_busy = lambda _p: object()      # 页面上有繁忙提示
        txt, err = dsc.wait_answer(object(), '', False, start_limit=5.0, total_limit=10.0)
        check('★ 繁忙时不能把残渣当回答返回', txt is None and bool(err) and '繁忙' in err,
              f'txt={txt!r} err={err!r}')

        # 正常短回答（不带繁忙提示）必须照常放行 ——
        # 测试里那句「只回答两个字：收到」就是这种，别把它误伤了
        dsc.last_answer_text = lambda _p: '收到'
        dsc.find_server_busy = lambda _p: None
        txt2, err2 = dsc.wait_answer(object(), '', False, start_limit=5.0, total_limit=10.0)
        check('★ 正常短回答不被误伤', txt2 == '收到' and err2 is None,
              f'txt={txt2!r} err={err2!r}')

        # 长回答 + 历史里残留的繁忙气泡 → 也不该误判
        dsc.last_answer_text = lambda _p: '这' * 200
        dsc.find_server_busy = lambda _p: object()
        txt3, err3 = dsc.wait_answer(object(), '', False, start_limit=5.0, total_limit=10.0)
        check('★ 长回答不受历史繁忙气泡影响', txt3 == '这' * 200 and err3 is None,
              f'{len(txt3 or "")} 字 / {err3!r}')
    finally:
        (dsc.last_answer_text, dsc.answer_done_rendered, dsc.find_continue_button,
         dsc.find_server_busy, dsc.STABLE_NORMAL, dsc.POLL, dsc.BUSY_MIN_CHARS) = _saved2

    section('单元 · 各 server 能加载')
    import importlib
    for mod, note in (('mcp_server', 'MCP server（之前一行都没测过）'),
                      ('api_server', 'OpenAI 兼容 API'),
                      ('claude_shim', 'Anthropic 兼容 shim')):
        try:
            importlib.import_module(mod)
            check(f'{mod} 能 import（{note}）', True)
        except Exception as e:
            check(f'{mod} 能 import（{note}）', False, f'{type(e).__name__}: {e}')

    section('单元 · 提示词拼接（公共函数）')
    import deepseek_ask as ds_mod
    short = ds_mod.join_prompt(['aaa', 'bbb', 'ccc', 'ddd'])
    check('不超长时原样拼接', short == 'aaa\n\nbbb\n\nccc\n\nddd', repr(short[:40]))

    old = ds_mod.MAX_PROMPT_CHARS
    try:
        # 用接近真实的比例：头很小、身子很长。500 那种极端值会走「只保头」的分支，
        # 测不到保尾逻辑。
        ds_mod.MAX_PROMPT_CHARS = 5000
        parts = ['HEAD' * 10, 'B' * 100, 'C' * 20000, 'TAIL' * 20]
        out = ds_mod.join_prompt(parts, head_parts=1)
        check('超长时保住了头部', out.startswith('HEAD' * 10), repr(out[:30]))
        check('超长时保住了尾部', out.rstrip().endswith('TAIL' * 20), repr(out[-30:]))
        check('超长时确实被截断', len(out) <= 5200, f'{len(out)} 字')
        check('截断处有省略标记', '已省略' in out)

        # 头部本身就把预算吃光 —— 不能崩，也不能返回空
        ds_mod.MAX_PROMPT_CHARS = 100
        out2 = ds_mod.join_prompt(['X' * 5000, 'Y' * 5000], head_parts=1)
        check('头部超预算时不崩', isinstance(out2, str) and len(out2) > 0, f'{len(out2)} 字')
    finally:
        ds_mod.MAX_PROMPT_CHARS = old

    section('单元 · 故障诊断')
    # diagnose_missing_input 需要 page，用一个假对象验证它不会崩
    class FakePage:
        url = 'https://example.com/other'
        def eles(self, *a, **k):
            return []
        def ele(self, *a, **k):
            return None
    try:
        msg = ds_mod.diagnose_missing_input(FakePage())
        check('诊断函数不崩且给出结论', isinstance(msg, str) and len(msg) > 20, repr(msg[:60]))
        check('诊断里点明了不在 DeepSeek 页面', '不在 DeepSeek' in msg, repr(msg[:120]))
    except Exception as e:
        check('诊断函数不崩且给出结论', False, f'{type(e).__name__}: {e}')

    test_image_flow()      # 纯渲染逻辑，不用浏览器，放单元层

    section('单元 · 会话复用决策（压缩场景）')
    # ★ 这条守着用户实际踩到的 bug：
    #   Claude Code 压缩上下文后消息数会变少（83 → 46），旧逻辑判定「历史回退了」
    #   然后丢掉整个网页对话重开 —— 用户看到的是「接着问，浏览器却开了另一个对话」。
    #   正确行为是：计数对不上也**继续用原对话**，改发最近几条来重新对齐。
    import claude_shim as cs2
    msgs = [{'role': 'user', 'content': f'第{i}条消息'} for i in range(10)]
    URL = 'https://chat.deepseek.com/a/chat/s/6ace6130-fbfb-4bb9-8a59-69cd35429c3d'

    _, goto, payload, note = cs2.decide_prompt(msgs, None, 'sys', [])
    check('新会话 → 开新对话', goto is None and '新会话' in note, note)
    check('新会话 → 发全量', payload == msgs, f'{len(payload)} 条')

    info = {'web_url': URL, 'sent': 8}
    _, goto, payload, note = cs2.decide_prompt(msgs, info, 'sys', [])
    check('正常增量 → 复用原对话', goto == URL, str(goto))
    check('正常增量 → 只发新增的 2 条', len(payload) == 2, f'{len(payload)} 条')

    info = {'web_url': URL, 'sent': 83}          # ← 压缩后：83 > 10
    _, goto, payload, note = cs2.decide_prompt(msgs, info, 'sys', [])
    check('★ 压缩后仍复用原对话（不重开）', goto == URL, f'goto={goto} note={note}')
    check('★ 压缩后发最近几条', 0 < len(payload) <= cs2.REBASE_MSGS, f'{len(payload)} 条')
    check('★ 压缩后提示词非空', bool(note and '压缩' in note), note)

    section('单元 · 会话标识')
    req = {'metadata': {'user_id': '{"device_id":"x","session_id":"abc-123"}'}}
    check('能从 metadata 挖出 session_id', cs.session_id_of(req) == 'abc-123', str(cs.session_id_of(req)))
    check('没有 metadata 时返回 None', cs.session_id_of({}) is None)

    f1 = cs.fingerprint('系统提示A', [{'name': 'X'}])
    f2 = cs.fingerprint('系统提示A', [{'name': 'Y'}])   # 工具变了
    f3 = cs.fingerprint('系统提示B', [{'name': 'X'}])
    check('系统提示相同则指纹相同（工具不影响）', f1 == f2, '这是异步加载 MCP 的关键')
    check('系统提示不同则指纹不同', f1 != f3)

    # ── 缺陷 44：重试路径必须跳过开关对齐 ──
    #
    # ★ 为什么用 AST 静态检查而不是调一次：这条规则是「调用链有没有把参数传对」，
    #   跑起来测要真起浏览器；而静态检查能**精确**钉住那一个调用点。
    #   这跟当初防「重试漏传 attachments」用的是同一招（那个招确实拦下过问题）。
    section('单元 · 缺陷 44（重试不碰开关）')
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            'claude_shim.py'), encoding='utf-8').read()
    import ast as _ast
    _tree = _ast.parse(src)
    # 找所有 ask_web(...) 调用，看哪些显式传了 skip_toggles=True
    _skip_true, _skip_absent = 0, 0
    for _n in _ast.walk(_tree):
        if not (isinstance(_n, _ast.Call) and isinstance(_n.func, _ast.Name)
                and _n.func.id == 'ask_web'):
            continue
        _kw = [k for k in _n.keywords if k.arg == 'skip_toggles']
        if not _kw:
            _skip_absent += 1
        elif isinstance(_kw[0].value, _ast.Constant) and _kw[0].value.value is True:
            _skip_true += 1
    check('★ 有调用点显式传了 skip_toggles=True（重试那处）', _skip_true >= 1,
          '一个都没有 —— 缺陷 44 的修法被改回去了')
    check('正常路径没传 skip_toggles（开关该对齐还得对齐）', _skip_absent >= 1,
          '全都跳过了？那「每轮双向对齐」就废了')
    check('shim 里能用 ds.TRUNCATED_WARN（不是硬编码中文）',
          'ds.TRUNCATED_WARN' in src, '改文案时靠常量，别靠 match 中文')

    # ── 缺陷 46：解析必须只有一个入口（重试路径曾经绕过改名） ──
    #
    # ★ 这条钉的是「结构」而不是「这一次的写法」：全仓只有 parse_and_align
    #   里能出现 parse_reply。将来谁再加一个解析点、绕过改名，这里就红。
    #   （和上面防「重试漏传 attachments」是同一路数：能静态钉住的就别靠记性。）
    _pr_calls = [n for n in _ast.walk(_tree)
                 if isinstance(n, _ast.Call) and isinstance(n.func, _ast.Name)
                 and n.func.id == 'parse_reply']
    check('★ parse_reply 全仓只有一个调用点（都在 parse_and_align 里）',
          len(_pr_calls) == 1,
          f'{len(_pr_calls)} 个调用点 —— 多出来的那个绕过了「改名」这一步')

    # ── 缺陷 45：截断警告是个常量，且不能当真错误处理 ──
    section('单元 · 缺陷 45（截断要说出来）')
    check('TRUNCATED_WARN 常量存在', isinstance(ds_init.TRUNCATED_WARN, str)
          and bool(ds_init.TRUNCATED_WARN), repr(getattr(ds_init, 'TRUNCATED_WARN', None)))
    check('ask_web 里是「不等于警告才 raise」', 'err != ds.TRUNCATED_WARN' in src,
          '直接 if err: raise 的话，警告会被当成失败 → 本来能用的回答变 500')

    # ── 伪流式：能吐的前缀 ──
    section('单元 · 伪流式（可吐前缀）')
    sp = ds_init.streamable_prefix
    check('纯文本回答 → 全文可吐', sp('这是一段普通回答，没有任何花括号') == '这是一段普通回答，没有任何花括号')
    check('★ 工具调用 → 只吐 `{` 之前的叙述',
          sp('我先看一下目录。\n{"tool_use": {"name": "Read"}}') == '我先看一下目录。\n',
          'JSON 不能流，吐出去就是垃圾')
    check('开头的 `{` → 一个字都不吐', sp('{"tool_use": {"name": "Read"}}') == '')
    check('`[` 不算判据（Markdown 里太常见）',
          sp('参考 [1] 和 [2]\n\n结论是……') == '参考 [1] 和 [2]\n\n结论是……',
          '拿 `[` 当判据会频繁误截正常回答')
    check('空串安全', sp('') == '')
    # ★ 流式吐的必须和非流式喂给上游的是**同一份文本**：非流式走 clean()，
    #   流式原先吐的是原文 —— 开深度思考时那行「已深度思考（用时 N 秒）」
    #   会被流出去，而普通路径会把它删掉。同一份回答，两条路产出不一样。
    check('★ 思考标题行到齐后要删掉（和非流式的 clean() 对齐）',
          sp('已深度思考（用时 12 秒）\n\n正文在此') == '正文在此',
          repr(sp('已深度思考（用时 12 秒）\n\n正文在此')))
    # ★ 标题行没写完之前先压住。SSE 的 delta 只能加不能减 ——
    #   把「已深度思考（用时 3」吐出去就收不回来了。
    #   压住是安全的：真等不到换行，_finish_stream 会把结尾补上。
    check('★ 标题行还没写完时先压住不吐',
          sp('已深度思考（用时 3') == '', repr(sp('已深度思考（用时 3')))

    # ── 伪流式：差量算法 ──
    section('单元 · 伪流式（差量不重复）')
    class _FakeW:
        def __init__(self):
            self.buf = b''
        def write(self, b):
            self.buf += b
        def flush(self):
            pass
    class _FakeH:
        def __init__(self):
            self.w = _FakeW()
        def _raw_write(self, b):
            self.w.write(b)
    _h = _FakeH()
    _dw = cs.DeltaWriter(_h)
    _dw.text('你好')
    _dw.text('你好，世界')          # 增量：，世界
    _dw.text('你好')                # 变短 → 必须忽略
    _body = _h.w.buf.decode('utf-8', 'replace')
    _import_re = __import__('re')
    # ★ 只从 **content_block_delta** 事件里抠 text_delta 的 text。
    #   别用宽泛的 `"text": "..."` —— `content_block_start` 里也有个
    #   `'text': ''`，会被一起捞进来，于是「2 片」被数成「3 片」。
    #   （我自己先踩了这个，是自测把它抓出来的。）
    _deltas = []
    for _ev in _body.split('event: content_block_delta'):
        _m = _import_re.search(r'"type": "text_delta", "text": "((?:[^"\\]|\\.)*)"', _ev)
        if _m:
            _deltas.append(_m.group(1))
    check('★ 只吐变长的部分（差量）', len(_deltas) == 2, f'{len(_deltas)} 片：{_deltas}')
    check('★ 变短时不倒退、不重吐', _dw.shown == '你好，世界', repr(_dw.shown))
    check('已吐内容拼起来 = 最后一次的前缀',
          __import__('json').loads('"' + ''.join(_deltas) + '"') == '你好，世界',
          repr(_deltas))

    # ── 伪流式：SSE 收尾的事件序列 ──
    section('单元 · 伪流式（SSE 事件序列）')
    _msg = {'id': 'm1', 'type': 'message', 'role': 'assistant', 'model': 'x',
            'content': [{'type': 'text', 'text': '收到'}],
            'stop_reason': 'end_turn', 'stop_sequence': None,
            'usage': {'input_tokens': 0, 'output_tokens': 0}}
    _h2 = _FakeH()
    _h2.send_response = lambda *a, **k: None
    _h2.send_header = lambda *a, **k: None
    _h2.end_headers = lambda: None
    _h2.wfile = _h2.w
    # ★ 把**真的** Handler 方法挂上去，而不是自己重写一遍 ——
    #   重写的话测的就是「我的测试」而不是被测代码了。
    _h2._finish_stream = cs.Handler._finish_stream.__get__(_h2)
    _h2._raw_write_end = cs.Handler._raw_write_end.__get__(_h2)
    # ★ 走**完整的**流式流程，而不是只调 _finish_stream ——
    #   流式响应是两段发的：
    #     第一段（handler 主流程、在问模型之前）：头 + message_start
    #     第二段（_finish_stream、拿到回答之后）：正文块 + message_stop
    #   只测第二段的话，事件序列里当然没有 message_start。
    cs.stream_headers(_h2)
    _h2._raw_write(cs._sse_event('message_start', {
        'type': 'message_start', 'message': {
            'id': 'm1', 'type': 'message', 'role': 'assistant', 'model': 'x',
            'content': [], 'stop_reason': None, 'stop_sequence': None,
            'usage': {'input_tokens': 0, 'output_tokens': 0}}}))
    _h2._finish_stream(cs.DeltaWriter(_h2), _msg)
    _seq = __import__('re').findall(r'^event: (\S+)', _h2.w.buf.decode('utf-8', 'replace'),
                                    __import__('re').M)
    check('★ 事件序列以 message_start 开头', _seq[:1] == ['message_start'], str(_seq))
    check('★ 事件序列以 message_stop 收尾', _seq[-1:] == ['message_stop'], str(_seq))
    check('★ message_start / stop 各只出现一次',
          _seq.count('message_start') == 1 and _seq.count('message_stop') == 1, str(_seq))
    check('★ 每个开的块都关了（start / stop 数一致）',
          _seq.count('content_block_start') == _seq.count('content_block_stop'), str(_seq))

    # ── 缺陷 47：流式下报错不能改发 JSON（会把响应写坏） ──
    #
    # ★ 头一旦发出去（200 + chunked），响应就**已经提交了** —— 此时再
    #   `send_response(500)` 会把 HTTP 状态行写进**响应体**里，客户端的
    #   chunked 解码器拿它当块长度解析 → int('HTTP/1.1 500 X', 16) → ValueError。
    #   实测探针收到的字节里真的有第二个 `HTTP/1.1 500` 和 Content-Length。
    #   ★ 这和 DeltaWriter 裸写那次是同一个病（往已提交的响应里混格式），
    #     只是入口不同 —— 所以修法是**收成一个出口** `_fail()`，
    #     而不是「记得两处都改」。下面这三条就是钉住这个出口。
    section('单元 · 缺陷 47（流式报错不能写 JSON）')
    check('报错收成一个出口（源码里不再有裸的 self._json(500）',
          'self._json(500' not in src,
          '流式下它会把 HTTP 状态行写进已提交的响应体，客户端直接炸')

    _h3 = _FakeH()
    _h3.send_response = lambda *a, **k: None
    _h3.send_header = lambda *a, **k: None
    _h3.end_headers = lambda: None
    _h3.wfile = _h3.w
    _h3._fail = cs.Handler._fail.__get__(_h3)
    _h3._raw_write_end = cs.Handler._raw_write_end.__get__(_h3)
    cs.stream_headers(_h3)                       # ← 头已经发出去了
    _h3._fail(500, '回答没有开始', streaming=True, dw=None)
    _fb = _h3.w.buf.decode('utf-8', 'replace')
    check('★ 流式报错走 SSE error 事件（客户端会转成 APIError）',
          'event: error' in _fb, _fb[:150])
    check('★ 流式报错不再混进 HTTP 状态行', 'HTTP/1.1 500' not in _fb, _fb[:150])
    check('★ 流式报错也要正确收尾（chunked 的 0 块，否则客户端一直等）',
          _fb.endswith('0\r\n\r\n'), repr(_fb[-24:]))

    # 非流式那半边不能被带坏 —— 还得是一个正经的 500 + JSON 正文
    _h4 = _FakeH()
    _h4.send_response = lambda code, *a: _h4.w.write(b'HTTP/1.1 %d X\r\n' % code)
    _h4.send_header = lambda k, v: _h4.w.write(('%s: %s\r\n' % (k, v)).encode())
    _h4.end_headers = lambda: _h4.w.write(b'\r\n')
    _h4.wfile = _h4.w
    _h4._json = cs.Handler._json.__get__(_h4)
    _h4._fail = cs.Handler._fail.__get__(_h4)
    _h4._fail(500, '回答没有开始')
    _jb = _h4.w.buf.decode('utf-8', 'replace')
    check('非流式报错照旧发 JSON（500 + api_error 正文）',
          'HTTP/1.1 500' in _jb and 'api_error' in _jb, _jb[:150])


# ══════════════════════════════════════════════════════════
# 集成测试：真联网
# ══════════════════════════════════════════════════════════

def test_image_flow():
    """
    图片工具结果的完整链路。

    ★ 回归测试：曾经出现的 bug 是 —— 模型**看不见附件是要读的内容**，
      于是每张图都再调一次 Read，陷入死循环（21 张图 21 轮还读不完）。
      修法是在 tool_result 的渲染里加一句「附件就是文件内容本身，别再去读」。
    """
    import base64
    import struct
    import zlib

    import claude_shim as cs

    section('单元 · 图片工具结果的渲染')

    msgs = [{'role': 'user', 'content': [
        {'type': 'tool_result', 'tool_use_id': 't1', 'content': [
            {'type': 'image', 'source': {'type': 'base64',
                                         'media_type': 'image/png',
                                         'data': 'iVBORw0KGgo=' * 50}}]}]}]
    prompt = cs.build_delta_prompt(msgs)
    check('图片不塞 base64', 'iVBORw0KGgo' not in prompt)
    check('★ 明确说了「附件就是内容、别再调工具读」',
          '就是' in prompt and '不要再调工具' in prompt, repr(prompt[:200]))

    # 纯文本的 tool_result 不该带这句（免得模型该读文件时不去读）
    msgs2 = [{'role': 'user', 'content': [
        {'type': 'tool_result', 'tool_use_id': 't1', 'content': '文件内容在这里'}]}]
    p2 = cs.build_delta_prompt(msgs2)
    check('纯文本结果不误加那句', '不要再调工具' not in p2, repr(p2[:150]))
    check('纯文本结果内容还在', '文件内容在这里' in p2)


def test_live():
    import deepseek_ask as ds

    section('集成 · 浏览器')
    try:
        page, cold = ds.connect()
    except Exception as e:
        check('连接浏览器', False, str(e))
        return
    check('连接浏览器', True)
    print(f'     （{"冷启动" if cold else "接管已有"}）')

    page.get(ds.URL)
    time.sleep(2)
    ok = ds.ensure_page(page, new_chat=True)
    check('能开新对话（登录态有效）', ok)
    if not ok:
        return

    section('集成 · 选择器')
    # 这几个不是「页面上永远有」的：
    #   answer_body     —— 新对话里还没有任何回答
    #   continue_button —— 只在回答被长度限制截断时才出现
    #   server_busy     —— 只在服务端**真的限流**时才出现（选择器是
    #                      `text:服务器繁忙`，纯文案锚点）。要求它「有命中」
    #                      等于要求「此刻 DeepSeek 正在繁忙」，所以它总是在
    #                      不忙的时候红。断言必须区分「选择器坏了」和
    #                      「选择器现在不该命中」——把后者写进前者的判据里，
    #                      得到的就是一条时红时绿的自检。
    fresh_chat_optional = {'answer_body', 'continue_button', 'server_busy'}
    for key, locs in ds.SEL.items():
        n = len(page.eles(locs if isinstance(locs, str) else locs[0], timeout=2))
        if key in fresh_chat_optional:
            check(f'选择器 {key}（新对话可为 0）', True, f'命中 {n} 个')
        else:
            check(f'选择器 {key} 有命中', n > 0, f'命中 {n} 个')

    section('集成 · 发消息与取原文')
    base = ds.last_answer_text(page)
    q = '原样输出这一行，一个字不改：MARKER $env:PATH D:\\创业\\x.txt'
    ds.send_question(page, q, base)
    raw, err = ds.wait_answer(page, base, False)
    check('能收到回答', err is None and bool(raw), str(err))
    if raw:
        got = ds.clean(raw)
        # ★ 这条是 LaTeX 渲染 bug 的回归测试
        check('$env:PATH 没被 LaTeX 毁掉', '$env:PATH' in got, repr(got[:120]))
        check('Windows 路径完整', 'D:\\创业\\x.txt' in got, repr(got[:120]))
        check('回答里没有渲染产生的多余空格', 'e n v' not in got, repr(got[:120]))

    section('集成 · 中文路径与特殊字符')
    base = ds.last_answer_text(page)
    ds.send_question(page, '只回答两个字：收到', base)
    raw, err = ds.wait_answer(page, base, False)
    check('普通对话正常', err is None and '收到' in (raw or ''), repr(raw))

    section('集成 · 附件上传')
    try:
        import struct
        import zlib

        def make_png(path, w=60, h=60):
            raw = b''
            for y in range(h):
                raw += b'\x00'
                for x in range(w):
                    raw += bytes([255, 0, 0]) if x < w // 2 else bytes([0, 0, 255])
            def chunk(tag, data):
                return (struct.pack('>I', len(data)) + tag + data +
                        struct.pack('>I', zlib.crc32(tag + data) & 0xffffffff))
            png = b'\x89PNG\r\n\x1a\n'
            png += chunk(b'IHDR', struct.pack('>IIBBBBB', w, h, 8, 2, 0, 0, 0))
            png += chunk(b'IDAT', zlib.compress(raw))
            png += chunk(b'IEND', b'')
            open(path, 'wb').write(png)

        tmp = os.path.join(os.environ['TEMP'], '_selftest.png')
        make_png(tmp)
        ds.ensure_page(page, new_chat=True)
        up = ds.upload_attachments(page, [tmp])
        check('附件上传调用成功', up)
        base = ds.last_answer_text(page)
        ds.send_question(page, '这张图里有哪些颜色？只列颜色。', base)
        raw, err = ds.wait_answer(page, base, False)
        got = (raw or '')
        check('模型能看见上传的图', '红' in got or '蓝' in got, repr(got[:100]))
        os.remove(tmp)
    except Exception as e:
        check('附件上传', False, f'{type(e).__name__}: {e}')


def test_heal():
    """
    自愈测试：杀掉浏览器，看能不能自己接回来。

    ⚠️ 这个测试会真的杀掉 Chrome，比较暴力 —— 所以默认不跑，用 --heal 显式开。
    """
    import subprocess

    import deepseek_ask as ds

    section('集成 · 自愈（会杀浏览器）')
    page, _ = ds.connect()
    check('初始连接正常', ds.browser_alive(page))

    subprocess.run(['taskkill', '/F', '/IM', 'chrome.exe'],
                   capture_output=True, shell=True)
    time.sleep(3)
    check('杀完之后连接确实断了', not ds.browser_alive(page))

    t0 = time.time()
    try:
        page2 = ds.ensure_browser(page)
        check('ensure_browser 自愈成功', ds.browser_alive(page2),
              f'{time.time()-t0:.1f} 秒')
        page2.get(ds.URL)
        time.sleep(3)
        ok = ds.ensure_page(page2, new_chat=True)
        check('自愈后能开新对话', ok)
    except Exception as e:
        check('ensure_browser 自愈成功', False, str(e)[:90])


def test_window():
    """窗口状态：默认应该是最小化的，三种状态速度一样。"""
    import deepseek_ask as ds

    section('集成 · 窗口')

    page, _ = ds.connect()

    def state():
        try:
            return page.run_cdp('Browser.getWindowForTarget').get('bounds', {}).get('windowState')
        except Exception:
            return '?'

    ds.set_window_visible(page, False)
    time.sleep(1)
    check('隐藏后窗口是最小化的', state() == 'minimized', f'实际 {state()}')

    ds.set_window_visible(page, True)
    time.sleep(1)
    check('显示后窗口恢复正常', state() == 'normal', f'实际 {state()}')

    # 状态没变时不该重复发 CDP 命令（靠内部记账）
    ds.set_window_visible(page, True)
    check('重复调同一状态不报错', True)


def test_attach_signal():
    """附件就绪信号 .ds-animated-size-item 对各类文件都有效。"""
    import os
    import struct
    import zlib

    import deepseek_ask as ds

    section('集成 · 附件就绪信号')
    page, _ = ds.connect()
    page.get(ds.URL)
    time.sleep(2)
    ds.ensure_page(page, new_chat=True)

    def make_png(path):
        w = h = 40
        raw = b''.join(b'\x00' + bytes([200, 40, 40]) * w for _ in range(h))
        def chunk(tag, data):
            return (struct.pack('>I', len(data)) + tag + data +
                    struct.pack('>I', zlib.crc32(tag + data) & 0xffffffff))
        png = b'\x89PNG\r\n\x1a\n'
        png += chunk(b'IHDR', struct.pack('>IIBBBBB', w, h, 8, 2, 0, 0, 0))
        png += chunk(b'IDAT', zlib.compress(raw))
        png += chunk(b'IEND', b'')
        open(path, 'wb').write(png)

    for kind in ('png', 'txt'):
        path = os.path.join(os.environ['TEMP'], f'_st_{kind}.{kind}')
        if kind == 'png':
            make_png(path)
        else:
            open(path, 'w', encoding='utf-8').write('测试内容\n')
        ds.ensure_page(page, new_chat=True)
        t0 = time.time()
        ok = ds.upload_attachments(page, [path])
        dt = time.time() - t0
        check(f'{kind} 上传能确认就绪', ok, f'{dt:.1f} 秒')
        check(f'{kind} 信号出现得快（<5 秒）', dt < 5, f'{dt:.1f} 秒')
        # 浏览器可能还占着这个文件，删不掉无所谓 —— 放在临时目录里，
        # 系统会自己清。绝不能因为清理失败就让整个自测挂掉。
        try:
            os.remove(path)
        except Exception:
            pass


def test_continue_button():
    """
    续写按钮检测。

    ★ 这里**刻意不触发真截断**（要几十秒，还会污染对话）—— 那条路径靠
      「让它写 1~1600 的平方」手工验过（19835 字，写全）。
      自动测只守一件事：**正常回答时不能误报**。
      误报的后果是白点一下按钮，运气不好会打断正在生成的回答。
    """
    import deepseek_ask as ds

    section('集成 · 续写按钮（防误报）')
    page, _ = ds.connect()
    page.get(ds.URL)
    time.sleep(2)
    ds.ensure_page(page, new_chat=True)

    _, err = ds.ask(page, '只回答两个字：收到', False)
    check('正常回答能拿到结果', not err, str(err))
    check('正常回答时没有「继续生成」按钮', ds.find_continue_button(page) is None,
          '误报会导致白点一下')
    check('「继续生成」在选择器表里', 'continue_button' in ds.SEL)


def test_shim_http():
    """shim 的 HTTP 层：不需要真的问模型（用一个假的 prompt 类型判断）"""
    import urllib.request

    section('集成 · shim HTTP')
    try:
        with urllib.request.urlopen('http://127.0.0.1:8799/health', timeout=3) as r:
            j = json.loads(r.read())
        check('shim /health 可达', j.get('ok') is True, str(j))
    except Exception as e:
        check('shim /health 可达', False, f'{e}（shim 没在跑？）')
        return

    try:
        with urllib.request.urlopen('http://127.0.0.1:8799/think', timeout=3) as r:
            j = json.loads(r.read())
        check('/think 能读状态', 'thinking' in j, str(j))
    except Exception as e:
        check('/think 能读状态', False, str(e))

    # 空 messages 必须被挡住，绝不能拿去问模型
    # （回归测试：曾经这个检查被误删，空请求让模型白跑十秒还编了个假工具名）
    t0 = time.time()
    code, body = _post('/v1/messages?beta=true', {'model': 'x', 'messages': []})
    dt = time.time() - t0
    check('空 messages 被拒（400）', code == 400, f'HTTP {code} {body[:80]}')
    check('空 messages 是秒回不是问模型', dt < 3, f'耗时 {dt:.1f} 秒')

    # 带查询串不能 404（Claude Code 打的就是 /v1/messages?beta=true）
    code, _ = _post('/v1/messages?beta=true', {'model': 'x', 'messages': [{'role': 'user', 'content': 'hi'}]})
    check('带查询串的路由不 404', code != 404, f'HTTP {code}')

    # count_tokens：Claude Code 靠它做上下文管理。不实现就会弹
    # 「API Error: Error response」—— 实测就是这么翻车的。
    code, body = _post('/v1/messages/count_tokens?beta=true',
                       {'model': 'x', 'messages': [{'role': 'user', 'content': '你好'}]})
    check('count_tokens 端点存在（不 404）', code == 200, f'HTTP {code} {body[:80]}')
    if code == 200:
        try:
            j = json.loads(body)
            check('count_tokens 返回 input_tokens',
                  isinstance(j.get('input_tokens'), int) and j['input_tokens'] > 0, str(j))
        except Exception as e:
            check('count_tokens 返回 input_tokens', False, str(e))

    # 没有工具时不该编造工具名
    code, body = _post('/v1/messages', {
        'model': 'x', 'tools': [],
        'messages': [{'role': 'user', 'content': '只回答两个字：收到'}]})
    check('无工具时不报错', code == 200, f'HTTP {code}')
    if code == 200:
        try:
            j = json.loads(body)
            types = [c.get('type') for c in j.get('content', [])]
            check('无工具时不返回 tool_use', 'tool_use' not in types, str(types))
        except Exception as e:
            check('无工具时不返回 tool_use', False, str(e))


def _post(path, obj, timeout=90):
    """发一个请求，返回 (状态码, 响应体文本)。"""
    import urllib.error
    import urllib.request
    req = urllib.request.Request(
        f'http://127.0.0.1:8799{path}',
        data=json.dumps(obj).encode(),
        headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode('utf-8', 'replace')
    except Exception as e:
        return -1, str(e)


# ══════════════════════════════════════════════════════════

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--fast', action='store_true', help='只跑离线单元测试')
    ap.add_argument('--live', action='store_true', help='只跑联网测试')
    ap.add_argument('--heal', action='store_true',
                    help='额外跑自愈测试（会真的杀掉 Chrome，比较暴力）')
    args = ap.parse_args()

    t0 = time.time()
    print('=' * 62)
    print('  deepseek_ask 自测')
    print('=' * 62)

    # ★ 自测的日志**不能**写进生产日志文件（logs\YYYY-MM-DD.log）。
    #   两边共用时，自测那几百行会和真实会话的行交错在一起，而且自测里有
    #   **故意制造的异常值** —— 假 PID 999999、临时把 MAX_PROMPT_CHARS 改成
    #   5000 / 100。排查线上问题时翻到这些行，会以为生产出了故障，被带到沟里
    #   （我自己就被 `提示词 20226 字，超限截断到 5000` 骗过一次，
    #    差点去改一个根本没坏的截断逻辑）。
    import deepseek_ask as ds_init
    os.makedirs(ds_init.LOG_DIR, exist_ok=True)
    ds_init._log_fp = open(
        os.path.join(ds_init.LOG_DIR, 'selftest-' + time.strftime('%Y-%m-%d') + '.log'),
        'a', encoding='utf-8')

    if not args.live:
        test_unit()
    if not args.fast:
        test_live()
        test_window()
        test_attach_signal()
        test_continue_button()
        test_shim_http()
        if args.heal:
            test_heal()

    print()
    print('=' * 62)
    print(f'  通过 {len(PASS)}   失败 {len(FAIL)}   耗时 {time.time()-t0:.0f} 秒')
    if FAIL:
        print()
        print('  失败的：')
        for f in FAIL:
            print(f'    ❌ {f}')
    print('=' * 62)
    return 1 if FAIL else 0


if __name__ == '__main__':
    sys.exit(main())
