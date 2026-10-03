# -*- coding: utf-8 -*-
"""
端到端自检 —— **按 Claude Code 的方式**驱动 shim 跑一个真实任务。

    python e2e_check.py              # 自起一个 shim（独立端口），跑完自动关
    python e2e_check.py --keep       # 保留临时产物，方便看现场
    python e2e_check.py --shim-url http://127.0.0.1:8799    # 用已在跑的那个

═══ 为什么要有这第三层 ═══

    selftest.py --fast    单元：解析、提示词、锁……
    selftest.py --live    浏览器集成：真的发一条消息、真的传一个附件
    本文件                端到端：**从「Claude Code 会怎么发请求」出发，走完整条链路**

前两层全都绿的情况下，用户还是踩了一整晚的坑 —— 因为真正出问题的是
**「模型怎么调工具 / 我们怎么把结果喂回去」这一层**，而它一直没人测：

  · 长内容被网页长度上限截断 → 半截工具调用发给上游 → "Error writing file"
  · 重试开新对话却**漏传附件** → 模型说「我看不到图」，死循环
  · 服务端限流只剩个 `{"` 残渣 → 被当成「正经回答」交出去
  · 「继续生成」被自己连点打断 → 长文件永远写不完

这些**单元测试一条都测不出来**，因为它们的根因不在解析函数里，
而在「一轮完整的对话是怎么跑下来的」。

═══ 它测什么 ═══

一个真实的写文件任务：让模型写一个两百来行的 Python 脚本并跑通。

**红的条件**（= 链路坏了）：
  1. 文件没写出来，或明显偏小 —— 多半被长度上限截断了
  2. 出现「缺 content」—— 长内容被截断，半截工具调用发给了上游
  3. 跑满轮数还没收工 —— 轮数失控

**只警告不红**（= 效率问题，不是坏了）：
  · 重试次数、Write 次数、碰上几次「服务器繁忙」

  为什么不红：模型偶尔写坏一次 JSON 是正常的，重试机制本来就为此而设；
  服务器繁忙更是 DeepSeek 那边的事。把这些算成失败，自检会时红时绿，
  很快就没人看了 —— 一个没人看的自检等于没有。
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(HERE, 'logs')
TMP_DIR = os.path.join(HERE, '_e2e_tmp')

MAX_ROUNDS = 25
DEFAULT_PORT = 8798          # 故意避开 8799 —— 别抢用户正在用的那个 shim

SYS = ('你是一个 Windows 上的编程助手。你可以读写文件、跑 PowerShell。'
       '工作目录：' + TMP_DIR)

TOOLS = [
    {'name': 'Write', 'description': '把内容写入文件（覆盖）',
     'input_schema': {'type': 'object', 'properties': {
         'file_path': {'type': 'string'}, 'content': {'type': 'string'}},
         'required': ['file_path', 'content']}},
    {'name': 'Read', 'description': '读文件',
     'input_schema': {'type': 'object', 'properties': {
         'file_path': {'type': 'string'}}, 'required': ['file_path']}},
    {'name': 'PowerShell', 'description': '执行一条 PowerShell 命令',
     'input_schema': {'type': 'object', 'properties': {
         'command': {'type': 'string'}}, 'required': ['command']}},
    # ★ Edit 是**必须有的**（第十八轮补三加的）：`_BLOCK_FIELD` 里有
    #   `'Edit': 'new_string'` —— 也就是说 Edit 的正文也走「代码块补给字段」
    #   那条路，而它在此之前**从来没被端到端跑过**（Write 和 PowerShell 都有
    #   任务覆盖，Edit 一个都没有）。改文件的活恰恰是 dsc 用得最多的一类。
    {'name': 'Edit', 'description': '把文件里的一段文本替换成另一段（改文件用这个）',
     'input_schema': {'type': 'object', 'properties': {
         'file_path': {'type': 'string'},
         'old_string': {'type': 'string'},
         'new_string': {'type': 'string'}},
         'required': ['file_path', 'old_string', 'new_string']}},
]


def task_text(outfile):
    """
    任务刻意设计成「必须写一个**比较长**的文件」。

    ★ 为什么非要长：这一层要抓的 bug（长度上限截断、半截工具调用、
      「继续生成」写不完）**只有在内容长到超过单条回复上限时才会出现**。
      写个十行的 hello world 永远测不出来。
    """
    return ('请写一个 Python 脚本到 %s。\n'
            '脚本用 python-docx 生成一份 Word 文档，内容是「二阶系统时域指标」'
            '教学讲义，含：标题、公式说明、一个 Routh 判据示例、一个根轨迹说明'
            '段落、一个 MATLAB 代码附录。要有中文字体设置（宋体/黑体）、'
            '标题层级、公式用居中段落。\n'
            '内容要完整、能直接跑通，长度 200~300 行。\n'
            '写完后**运行它**，确认真的生成了 docx，再告诉我文件路径和字节数。'
            % outfile)


# ────────────────────────── shim 的起停 ──────────────────────────

def shim_up(url, timeout=3):
    try:
        with urllib.request.urlopen(url.rstrip('/') + '/health', timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def start_shim(port, tmp_dir=None):
    """
    自己起一个，跑完就关 —— 这样不依赖用户是否开着 dsc。

    ★ 自己的 shim 的 stderr **单独收进一个文件**，用来数重试次数。
      不能去读公共的 logs/YYYY-MM-DD.log —— 那个文件是**所有 shim 共写**
      的，用户可能正开着 dsc，他那边每一次重试都会被算到我们头上
      （实测第一版就数错了：把自己这轮的 2 次重试数成了 4 次）。
      ds.log() 同时往 stderr 和公共日志各写一份，所以收 stderr 就拿得到，
      而且**只有我们自己**的行。

    tmp_dir 是「这个文件放哪」。默认放本模块的 TMP_DIR（e2e 的行为不变）；
    别的脚本（ab_check）借用本函数时传自己的目录 —— 它不该往一个
    **自己不拥有、也不保证存在**的目录里写（实测：ab_check 借去用，
    而它并不建 _e2e_tmp，于是直接 FileNotFoundError）。
    """
    d = tmp_dir or TMP_DIR
    os.makedirs(d, exist_ok=True)
    errpath = os.path.join(d, 'shim_stderr.log')
    errf = open(errpath, 'wb')
    py = sys.executable
    proc = subprocess.Popen(
        [py, '-u', os.path.join(HERE, 'claude_shim.py'), '--port', str(port)],
        cwd=HERE, stdout=errf, stderr=errf)
    url = 'http://127.0.0.1:%d' % port
    t0 = time.time()
    while time.time() - t0 < 90:
        if shim_up(url):
            return proc, url, errpath
        if proc.poll() is not None:
            raise RuntimeError('shim 起来就退了，先手动跑一下看报什么错')
        time.sleep(1.0)
    proc.kill()
    raise RuntimeError('shim 90 秒还没起来')


def read_own_log(path):
    try:
        with open(path, encoding='utf-8', errors='replace') as f:
            return f.read()
    except OSError:
        return ''


# ────────────────────────── 工具执行 ──────────────────────────

def run_tool(name, inp):
    if name == 'Write':
        p = inp.get('file_path') or ''
        c = inp.get('content')
        if not c:
            return '错误：content 是空的，文件没写成', True
        os.makedirs(os.path.dirname(p) or '.', exist_ok=True)
        with open(p, 'w', encoding='utf-8') as f:
            f.write(c)
        return '已写入 %s（%d 字）' % (p, len(c)), False
    if name == 'Read':
        try:
            return open(inp.get('file_path') or '', encoding='utf-8',
                        errors='replace').read()[:4000], False
        except Exception as e:
            return '读失败: %s' % e, True
    if name == 'Edit':
        p = inp.get('file_path') or ''
        old, new = inp.get('old_string'), inp.get('new_string')
        # ★ new_string 缺了要**明确报错**，不能当成空串去替换 ——
        #   那会把文件里那一段**静默删掉**，而且看起来像成功了。
        if new is None:
            return '错误：new_string 是空的，文件没改（多半是被长度上限截断了）', True
        try:
            src = open(p, encoding='utf-8', errors='replace').read()
        except Exception as e:
            return '读不了 %s: %s' % (p, e), True
        if not old:
            return '错误：old_string 是空的，不猜你要改哪儿', True
        if old not in src:
            return 'old_string 在文件里找不到（要原样复制，包括缩进）', True
        with open(p, 'w', encoding='utf-8') as f:
            f.write(src.replace(old, new, 1))
        return '已改 %s（替换 %d 字 → %d 字）' % (p, len(old), len(new)), False
    if name == 'PowerShell':
        try:
            # ★ PowerShell 的**编码**必须显式摆平（第十八轮补四）。
            #
            #   中文 Windows 上 PowerShell 5.1 默认按 GBK 读写文件，于是模型
            #   最常用的 `Get-Content` / `Set-Content` 会把 UTF-8 的中文**毁掉**
            #   ——而且是**改坏文件本身**，不只是显示乱码：实测
            #       Get-Content t.txt; Set-Content t.txt (Get-Content t.txt)
            #   跑完文件里的 `中文` 变成 `\xe6\x96?`（那个 `?` 是 0x3F，真的写进去了）。
            #
            #   后果：模型看到乱码 → 去修「编码问题」→ 越修越乱，实测长任务
            #   那一轮它自己叙述：「中文在 Set-Content 里被搞成乱码了」、
            #   「改用 .NET 的 UTF-8 读写」——**十几轮里有好几轮花在这上面**。
            #
            #   ★ 这是**两边共用**的执行环境，所以它不是「偏袒谁」，而是
            #     **噪声**：它测的是「模型能不能从 GBK 损坏里爬出来」，
            #     而不是我们想测的「协议层有没有问题」。摆平它，量到的才是
            #     协议层的差别。
            #   （真实 Claude Code 在 Windows 上也做了这类编码处理。）
            setup = ('$OutputEncoding=[Text.Encoding]::UTF8;'
                     '[Console]::OutputEncoding=[Text.Encoding]::UTF8;'
                     "$PSDefaultParameterValues['Get-Content:Encoding']='utf8';"
                     "$PSDefaultParameterValues['Set-Content:Encoding']='utf8';"
                     "$PSDefaultParameterValues['Out-File:Encoding']='utf8';"
                     "$PSDefaultParameterValues['Add-Content:Encoding']='utf8';")
            r = subprocess.run(
                ['powershell', '-NoProfile', '-Command',
                 setup + (inp.get('command') or '')],
                capture_output=True, text=True, timeout=180,
                encoding='utf-8', errors='replace')
            return (r.stdout or '') + (r.stderr or ''), r.returncode != 0
        except Exception as e:
            return '执行失败: %s' % e, True
    return '未知工具 ' + name, True


# ────────────────────────── 主流程 ──────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', type=int, default=DEFAULT_PORT)
    ap.add_argument('--shim-url', default=None,
                    help='用已经跑着的 shim，而不是自己起一个')
    ap.add_argument('--keep', action='store_true', help='保留临时产物')
    ap.add_argument('--rounds', type=int, default=MAX_ROUNDS)
    args = ap.parse_args()

    # ★ 开跑前先清空临时目录（第十八轮补）。
    #
    # 原来只在**结束时**清、而且 `--keep` 时不清理 —— 于是 `--keep` 留下的
    # 成品（gen_report.py + 已生成的 docx）会改变**下一轮任务**的起始条件：
    # 模型 Get-ChildItem 看见文件已经在了，就转去「改它 / 验证它」而不是写它，
    # 于是轮数暴涨、Write 反复重写、半路撞上长度上限 → `缺 content` → **假红**。
    #
    # 实测代价：同一份代码，脏目录跑 3 次红 2 次，干净目录跑 3 次全绿。
    # 假红的代价极高 —— 它会让人去改根本没坏的代码（这轮就差点）。
    #
    # 注意「开始时清」和 `--keep` 不冲突：`--keep` 的语义是**跑完别删**，
    # 不是「别动我上次的产物」。要留着上一次的，就先自己拷走。
    import shutil as _shutil
    _shutil.rmtree(TMP_DIR, ignore_errors=True)
    os.makedirs(TMP_DIR, exist_ok=True)
    outfile = os.path.join(TMP_DIR, 'gen_report.py')

    proc, url, ownlog = None, args.shim_url, None
    if url is None:
        print('[e2e] 自己起一个 shim（端口 %d）…' % args.port)
        proc, url, ownlog = start_shim(args.port)
    else:
        if not shim_up(url):
            print('[e2e] ❌ %s 上没有 shim 在跑' % url)
            return 2
    print('[e2e] shim:', url)

    msgs = [{'role': 'user', 'content': task_text(outfile)}]

    writes, rounds, final, t0 = [], 0, '', time.time()
    try:
        for rounds in range(1, args.rounds + 1):
            body = json.dumps({'model': 'deepseek-web', 'max_tokens': 8192,
                               'system': SYS, 'tools': TOOLS, 'messages': msgs},
                              ensure_ascii=False).encode('utf-8')
            req = urllib.request.Request(
                url.rstrip('/') + '/v1/messages', data=body,
                headers={'Content-Type': 'application/json'})
            try:
                with urllib.request.urlopen(req, timeout=900) as r:
                    resp = json.loads(r.read().decode('utf-8'))
            except urllib.error.HTTPError as e:
                print('[e2e] 第 %d 轮 HTTP %s：%s' % (rounds, e.code,
                                                  e.read().decode('utf-8', 'replace')[:200]))
                break
            except Exception as e:
                print('[e2e] 第 %d 轮请求失败：%s' % (rounds, str(e)[:200]))
                break

            blocks = resp.get('content') or []
            final = ''.join(b.get('text', '') for b in blocks
                            if b.get('type') == 'text')
            uses = [b for b in blocks if b.get('type') == 'tool_use']
            names = [b.get('name') for b in uses]
            print('[e2e] 第 %2d 轮  %s' % (rounds, names or ('文本 %d 字' % len(final))))

            if not uses:
                break

            results = []
            for tu in uses:
                if tu.get('name') == 'Write':
                    writes.append(len((tu.get('input') or {}).get('content') or ''))
                out, err = run_tool(tu.get('name'), tu.get('input') or {})
                print('        %-11s → %s' % (tu.get('name'), out[:90].replace('\n', ' ')))
                results.append({'type': 'tool_result', 'tool_use_id': tu.get('id'),
                                'content': [{'type': 'text', 'text': out}],
                                **({'is_error': True} if err else {})})
            msgs.append({'role': 'assistant', 'content': blocks})
            msgs.append({'role': 'user', 'content': results})
    finally:
        if proc:
            proc.kill()

    dt = time.time() - t0
    delta = read_own_log(ownlog) if ownlog else ''

    # ★ 数「重新问一次」那几行 —— 一次重试会打**两行**日志
    #   （触发一行 + 结果一行），按 `[重试]` 数会把次数翻倍。
    #   实测第一版就是这么把 2 次数成 4 次的。
    retries = len(re.findall(r'\[重试\].*重新问一次', delta))
    busy = len(re.findall(r'\[繁忙\]', delta))
    continues = len(re.findall(r'\[续写\].*点「继续生成」', delta))
    no_content = len(re.findall(r'缺 content', delta))

    ok_file = os.path.exists(outfile)
    size = os.path.getsize(outfile) if ok_file else 0
    ran = bool(re.search(r'\.docx', final)) or ok_file

    print()
    print('=' * 62)
    print('  轮数        %d' % rounds)
    print('  耗时        %.0f 秒' % dt)
    print('  Write 次数  %d  内容长度 %s' % (len(writes), writes))
    print('  重试        %d 次（其中「缺 content」%d 次）' % (retries, no_content))
    print('  繁忙        %d 次' % busy)
    print('  续写        %d 次' % continues)
    print('  产出        %s' % ('%d 字节' % size if ok_file else '★ 没有'))
    print('=' * 62)

    # 断言的取舍：
    #   · 「写不出来 / 写坏了」= **坏掉**，必须红
    #   · 「重试了几次」= **效率**问题，不红。模型偶尔写坏一次 JSON 是正常的，
    #     重试机制本来就为此而设；只有**失控**（超过预算）才值得报警。
    #     把它算成失败的话，这个自检会时红时绿，很快就没人看了。
    fails, warns = [], []
    if not ok_file or size < 3000:
        fails.append('文件没写出来 / 太小（%d 字节）—— 多半被截断了' % size)
    if no_content:
        fails.append('出现「缺 content」%d 次 —— 长内容又被截断了' % no_content)
    if rounds >= args.rounds:
        fails.append('跑满了 %d 轮还没收工 —— 轮数失控' % args.rounds)
    if len(writes) > 3:
        warns.append('Write 调了 %d 次 —— 模型在分段绕路，偏慢' % len(writes))
    if retries > 3:
        warns.append('重试 %d 次 —— 偏多' % retries)
    if busy:
        warns.append('碰上服务器繁忙 %d 次（不是我们的问题，但会拖慢）' % busy)

    for w in warns:
        print('  ⚠️ ', w)
    if fails:
        print('  ❌ 不通过：')
        for f in fails:
            print('     ·', f)
    else:
        print('  ✅ 通过')

    if not args.keep:
        try:
            import shutil
            shutil.rmtree(TMP_DIR, ignore_errors=True)
        except Exception:
            pass
    else:
        print('  （--keep：产物留在 %s）' % TMP_DIR)
    return 1 if fails else 0


if __name__ == '__main__':
    sys.exit(main())
