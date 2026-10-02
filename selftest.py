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
                        'ensure_page', 'set_think', 'ask')


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
              dsc.STABLE_NORMAL, dsc.POLL, dsc.MAX_CONTINUES)
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

    try:
        dsc.last_answer_text = _fake_text
        dsc.answer_done_rendered = lambda _p: True
        dsc.find_continue_button = lambda _p: _Btn()
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
         dsc.STABLE_NORMAL, dsc.POLL, dsc.MAX_CONTINUES) = _saved

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
