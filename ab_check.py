# -*- coding: utf-8 -*-
"""
A/B 对拍：**同一个任务，两条路各跑一遍，把过程和结果摆在一起比。**

    路 A（dsc）    Claude Code → 本地 shim → DeepSeek 网页版（我们的协议模拟）
    路 B（api）    Claude Code → 付费中转   → 原生工具调用

★ 为什么这个对比有意义：两边是**同一个模型家族**（中转那边也是
  deepseek-v4.x），差别只在「工具调用是怎么喂给模型的」——
  A 靠一段文字协议 + 我们从回复里抠，B 靠 API 原生的 tool_use。
  所以比出来的差异，基本就是**我们这一层（协议模拟）的代价**，
  而不是「模型不同」。这正是能拿来改进的东西。

★ 两边发**完全相同**的请求体（同一个 system、同一份工具、同一段任务、
  同样的 metadata）—— 唯一的变量就是那个 URL。不这么控制的话，
  比出来的差异说不清是谁造成的。

用法：
    py -3.11 ab_check.py                 # 两边都跑，出对比报告
    py -3.11 ab_check.py --side api      # 只跑真实 API（快，但**要花钱**）
    py -3.11 ab_check.py --side shim     # 只跑 dsc 那侧
    py -3.11 ab_check.py --task short    # 小任务（省 token，看趋势用）

★ 成本：路 B **按 token 计费**，报告末尾会打总量。
★ 抢浏览器：路 A 要独占浏览器 —— 跑之前确认没有别的 dsc 会话在跑，
  否则联网用例会假红（DEVLOG 缺陷 19）。
"""
import argparse
import json
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# ★ 复用 e2e_check 的驱动件，**不抄一份** —— 同一个职责两份实现，
#   这个项目已经栽过六次（DEVLOG 里记着）。
from e2e_check import (SYS, TOOLS, task_text, run_tool, shim_up, start_shim,
                       read_own_log)

TMP_DIR = os.path.join(HERE, '_ab_tmp')
SHIM_PORT = 8798                 # 和 e2e 一样避开 8799

# ────────────────────────── 任务库 ──────────────────────────
#
# ★ 每个任务**压的是不同的那条路**，不是随便换几个题。选任务的依据是
#   「它会不会经过 shim 里某个分支」，而不是「它难不难」：
#
#     short  一轮写多个文件 + 跑命令   → 多块配对（_attach_code_block）
#     edit   先读一个已有文件再改它    → Edit 的 new_string 走代码块
#                                       （这条**从来没被端到端跑过**）
#     cmd    主要产出是几条命令        → command 字段走代码块
#     long   写 300 行以上的文件       → 网页截断 / 续写 / 重试
#     multi  一次建三个文件再串起来    → 多调用 + 多文件 + 跨轮引用
#
# ★ 任务里的 %(w)s 是**每侧各自的工作目录**（两侧绝不能共用目录，
#   否则先跑的那侧留下的产物会改变后跑那侧的起点 —— e2e 踩过这个坑）。

def _t_short(w):
    return ('请写一个 Python 脚本到 %(w)s\\wordcount.py。\n'
            '脚本功能：读一个文本文件，统计每个词出现的次数，输出前 10 名。\n'
            '要有函数拆分、有 __main__ 入口、有错误处理。\n'
            '同时造一个测试用的文本文件 %(w)s\\sample.txt（内容你自己编）。\n'
            '写完**运行它**验证能跑通，再告诉我文件路径。' % {'w': w})


def _t_edit(w):
    return ('工作目录 %(w)s 里有一个 report.py（已经建好了）。\n'
            '请**先读它**，然后做三处修改：\n'
            '1) 把 top_words 函数改成**返回**列表，不要在函数里直接打印\n'
            '2) 给命令行加上 --top N 参数（默认 10）\n'
            '3) 给每个函数补一句 docstring\n'
            '**用 Edit 工具改，不要整个重写文件**。改完运行一次确认没问题。'
            % {'w': w})


def _t_cmd(w):
    return ('请在 %(w)s 目录下用 PowerShell 依次做这几件事，每步跑完看一眼结果：\n'
            '1) 建一个子目录 logs\n'
            '2) 在 logs 里生成 data.txt，50 行，每行是「行号,当前时间」\n'
            '3) 统计 data.txt 的行数和字节数\n'
            '4) 把 data.txt 按行号倒序输出前 5 行\n'
            '全部用真正的 PowerShell 语法，最后把 3) 4) 的结果告诉我。' % {'w': w})


def _t_long(w):
    return ('请写一个 Python 脚本到 %(w)s\\big.py。\n'
            '脚本里要有 **30 个函数**，每个函数带 docstring 和一行中文注释，'
            '每个函数做一件小事（加减乘除、字符串处理、列表操作等各来几个）。\n'
            '文件长度要在 **300 行以上**，写完运行 `py -3.11 big.py` 确认没有语法错。'
            % {'w': w})


def _t_multi(w):
    return ('请在 %(w)s 里建三个文件：a.py、b.py、c.py，'
            '每个文件里定义一个同名函数（afunc / bfunc / cfunc），各返回一个字符串。\n'
            '再建第四个文件 main.py，import 那三个模块并调用它们、把结果打印出来。\n'
            '最后运行 main.py 确认输出正常。' % {'w': w})


def _seed_edit(w):
    """edit 任务需要一个**已经存在**的文件 —— 由我们建好，不是让模型建。"""
    open(os.path.join(w, 'report.py'), 'w', encoding='utf-8').write(
        '# -*- coding: utf-8 -*-\n'
        '"""词频统计。"""\n'
        'import sys\n'
        '\n'
        'def top_words(path, n):\n'
        '    counts = {}\n'
        '    for line in open(path, encoding="utf-8"):\n'
        '        for w in line.split():\n'
        '            counts[w] = counts.get(w, 0) + 1\n'
        '    ranked = sorted(counts.items(), key=lambda kv: -kv[1])\n'
        '    for w, c in ranked[:n]:\n'
        '        print(w, c)\n'
        '\n'
        'def main():\n'
        '    if len(sys.argv) < 2:\n'
        '        print("用法: report.py <文件>")\n'
        '        return 1\n'
        '    top_words(sys.argv[1], 10)\n'
        '    return 0\n'
        '\n'
        'if __name__ == "__main__":\n'
        '    sys.exit(main())\n')


TASKS = {
    'short': {'text': _t_short, 'setup': None},
    'edit': {'text': _t_edit, 'setup': _seed_edit},
    'cmd': {'text': _t_cmd, 'setup': None},
    'long': {'text': _t_long, 'setup': None},
    'multi': {'text': _t_multi, 'setup': None},
}


def api_endpoint():
    """真实 API 那侧的地址和请求头（从环境变量来，和 claude 用的是同一套）。"""
    base = (os.environ.get('ANTHROPIC_BASE_URL') or '').rstrip('/')
    tok = os.environ.get('ANTHROPIC_AUTH_TOKEN') or os.environ.get('ANTHROPIC_API_KEY')
    model = os.environ.get('ANTHROPIC_MODEL') or 'claude-sonnet-4-5'
    if not base or not tok:
        return None
    return {'base': base, 'model': model,
            'headers': {'content-type': 'application/json',
                        'x-api-key': tok,
                        'authorization': 'Bearer ' + tok,
                        'anthropic-version': '2023-06-01'}}


def post(url, headers, body, timeout=900):
    req = urllib.request.Request(
        url, data=json.dumps(body, ensure_ascii=False).encode('utf-8'),
        headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode('utf-8'))


def looks_like_leaked_call(text):
    """
    这一段文字是不是**本该是工具调用、却被当成回答交出来**了。
    这正是 dsc 那侧独有的坏法（原生 API 不可能有），所以要单独数。
    """
    if not text:
        return False
    t = text.strip()
    if not t.startswith('{'):
        return False
    return ('"tool_use"' in t or '"tool_calls"' in t or
            ('"name"' in t and ('"input"' in t or '"arguments"' in t)))


def run_side(label, url, headers, model, task, workdir, max_rounds=25):
    """跑一侧，返回过程记录。两边用的是同一段循环，所以差异只来自后端。"""
    shutil.rmtree(workdir, ignore_errors=True)
    os.makedirs(workdir, exist_ok=True)
    # ★ 有些任务需要一个**已经存在**的文件（比如「读它然后改它」）。
    #   由我们建好，不是让模型建 —— 否则测的就不是「改文件」那条路了。
    if task.get('setup'):
        task['setup'](workdir)

    sid = 'ab-%s-%d' % (label, int(time.time()))
    body_common = {
        'model': model, 'max_tokens': 8192, 'system': SYS, 'tools': TOOLS,
        # ★ 带上 session_id，和真实 Claude Code 一样 —— 这样 dsc 那侧走的是
        #   **增量**路径（每轮只发新消息），而不是每轮重发全部历史。
        #   两边发的是同一个 body，唯一的变量是 URL。
        'metadata': {'user_id': json.dumps({'session_id': sid})},
    }

    msgs = [{'role': 'user', 'content': task['text'](workdir)}]
    rec = {'label': label, 'rounds': 0, 'tools': [], 'elapsed': 0.0,
           'leaked': 0, 'narrated': 0, 'usage_in': 0, 'usage_out': 0,
           'errors': [], 'texts': [], 'final': '', 'workdir': workdir,
           # ★ 工具**结果**报错的次数 —— 「模型跑的命令失败了几次」。
           #   这是除轮数之外最能说明「谁更靠谱」的量：原生 API 那边几乎
           #   不该有，dsc 这边每失败一次就多烧一轮。
           'tool_errors': 0}
    t0 = time.time()
    for rnd in range(1, max_rounds + 1):
        rec['rounds'] = rnd
        body = dict(body_common, messages=msgs)
        try:
            resp = post(url.rstrip('/') + '/v1/messages', headers, body)
        except urllib.error.HTTPError as e:
            detail = e.read().decode('utf-8', 'replace')[:200]
            rec['errors'].append('第 %d 轮 HTTP %s：%s' % (rnd, e.code, detail))
            print('  [%s] 第 %2d 轮 HTTP %s %s' % (label, rnd, e.code, detail[:80]))
            break
        except Exception as e:
            rec['errors'].append('第 %d 轮请求失败：%s' % (rnd, str(e)[:150]))
            print('  [%s] 第 %2d 轮失败 %s' % (label, rnd, str(e)[:80]))
            break

        u = resp.get('usage') or {}
        rec['usage_in'] += u.get('input_tokens') or 0
        rec['usage_out'] += u.get('output_tokens') or 0

        blocks = resp.get('content') or []
        text = ''.join(b.get('text', '') for b in blocks if b.get('type') == 'text')
        uses = [b for b in blocks if b.get('type') == 'tool_use']
        names = [b.get('name') for b in uses]
        rec['tools'].append(names)
        if text.strip():
            rec['narrated'] += 1
            rec['texts'].append(text.strip())
        if looks_like_leaked_call(text):
            rec['leaked'] += 1
            rec['errors'].append('第 %d 轮：回答里是一坨工具调用原文' % rnd)
        rec['final'] = text or rec['final']

        print('  [%s] 第 %2d 轮  %s%s'
              % (label, rnd, names or ('文本 %d 字' % len(text)),
                 ('   ⚠️ 疑似 JSON 泄漏' if looks_like_leaked_call(text) else '')))
        if not uses:
            break

        results = []
        for tu in uses:
            out, err = run_tool(tu.get('name'), tu.get('input') or {})
            if err:
                rec['tool_errors'] += 1
            print('        %-11s → %s%s'
                  % (tu.get('name'), out[:80].replace('\n', ' '),
                     '   ❌ 这步失败了' if err else ''))
            results.append({'type': 'tool_result', 'tool_use_id': tu.get('id'),
                            'content': [{'type': 'text', 'text': out}],
                            **({'is_error': True} if err else {})})
        msgs.append({'role': 'assistant', 'content': blocks})
        msgs.append({'role': 'user', 'content': results})

    rec['elapsed'] = time.time() - t0
    # 「产物」不写死某个文件名 —— 任务库里每个任务产出的文件都不一样。
    # 记 .py 总字节（粗粒度的工作量）+ 文件清单（细看用）。
    try:
        rec['files'] = sorted(f for f in os.listdir(workdir))
        rec['artifact'] = sum(os.path.getsize(os.path.join(workdir, f))
                              for f in rec['files'] if f.endswith('.py'))
    except OSError:
        rec['files'], rec['artifact'] = [], 0
    with open(os.path.join(workdir, 'transcript.txt'), 'w',
              encoding='utf-8') as f:
        for i, t in enumerate(rec['texts'], 1):
            f.write('─── 叙述 %d ───\n%s\n\n' % (i, t))
    return rec


def show(a, b):
    """把两侧摆一起。只打**能直接指向改动的**那些量。"""
    def seq(rec):
        """把工具序列压成 Tool×N，一眼能看出「谁在反复调同一个」。"""
        comp = []
        for n in [x for names in rec['tools'] for x in names]:
            if comp and comp[-1][0] == n:
                comp[-1][1] += 1
            else:
                comp.append([n, 1])
        return ' → '.join('%s×%d' % (n, c) if c > 1 else n for n, c in comp)

    rows = [
        ('轮数', a['rounds'], b['rounds']),
        ('墙钟（秒）', '%.0f' % a['elapsed'], '%.0f' % b['elapsed']),
        ('工具调用总数', sum(len(x) for x in a['tools']),
         sum(len(x) for x in b['tools'])),
        ('叙述轮数（会说话）', a['narrated'], b['narrated']),
        ('★ 工具调用原文泄漏', a['leaked'], b['leaked']),
        ('报错/异常', len(a['errors']), len(b['errors'])),
        ('产物 gen_report.py（字节）', a['artifact'], b['artifact']),
        # 下面这几行 B 侧**恒为 0 且不可能非 0** —— 原生 API 不存在这些补救机制。
        # 它们量的正是「协议模拟这一层的代价」。
        ('补丁：重试次数', a.get('retries', '—'), '0（不需要）'),
        ('补丁：缺 content', a.get('no_content', '—'), '0（不需要）'),
        ('补丁：续写生成', a.get('continues', '—'), '0（不需要）'),
        ('补丁：服务器繁忙', a.get('busy', '—'), '0（不需要）'),
    ]
    w = max(len(r[0]) for r in rows) + 2
    print()
    print('=' * 78)
    print('  对比结果')
    print('=' * 78)
    print('  %-*s %-16s %-16s' % (w, '', 'A · dsc（协议模拟）', 'B · 真实 API'))
    for name, x, y in rows:
        flag = ''
        if name.startswith('★') and x:
            flag = '   ← 只有 dsc 会这样'
        print('  %-*s %-16s %-16s%s' % (w, name, x, y, flag))
    print()
    print('  A 工具序列: %s' % (seq(a) or '（无）'))
    print('  B 工具序列: %s' % (seq(b) or '（无）'))
    print()
    print('  A token: in %d / out %d' % (a['usage_in'], a['usage_out']))
    print('  B token: in %d / out %d   ← 这一段是要花钱的'
          % (b['usage_in'], b['usage_out']))
    for rec in (a, b):
        if rec['errors']:
            print()
            print('  [%s] 异常记录：' % rec['label'])
            for e in rec['errors'][:8]:
                print('     ·', e)
    print()
    print('  逐轮叙述留在 %s' % TMP_DIR)


def _med(xs):
    xs = sorted(xs)
    return xs[len(xs) // 2] if xs else 0


def summarize(pairs):
    """
    多次对拍汇总。

    ★ 单次对拍**只能找 bug**（结构性错误一次就够定罪），
      要谈「谁更利索」必须有分布 —— 模型每次跑都不一样，
      一次 7:3、下一次可能 6:5，拿单次说倍数是没有统计意义的。
      这里给中位数和范围，让人自己看**分不分得开**。
    """
    def prep(recs):
        out = []
        for r in recs:
            r = dict(r)
            r['tools_n'] = sum(len(x) for x in r['tools'])
            r['patch'] = (r.get('retries', 0) + r.get('no_content', 0)
                          + r.get('continues', 0) + r.get('busy', 0))
            out.append(r)
        return out

    A = prep([a for a, _ in pairs])
    B = prep([b for _, b in pairs])
    n = len(pairs)
    rows = [
        ('轮数 中位', [_med([r['rounds'] for r in A]), _med([r['rounds'] for r in B])]),
        ('轮数 范围', ['%d~%d' % (min(r['rounds'] for r in A),
                                  max(r['rounds'] for r in A)),
                       '%d~%d' % (min(r['rounds'] for r in B),
                                  max(r['rounds'] for r in B))]),
        ('工具调用 中位', [_med([r['tools_n'] for r in A]),
                           _med([r['tools_n'] for r in B])]),
        ('墙钟 中位（秒）', ['%.0f' % _med([r['elapsed'] for r in A]),
                             '%.0f' % _med([r['elapsed'] for r in B])]),
        ('★ 工具结果报错 合计', [sum(r['tool_errors'] for r in A),
                                 sum(r['tool_errors'] for r in B)]),
        ('★ 补丁 合计（重试/缺content/续写/繁忙）',
         [sum(r['patch'] for r in A), sum(r['patch'] for r in B)]),
        ('工具调用原文泄漏 合计', [sum(r['leaked'] for r in A),
                                   sum(r['leaked'] for r in B)]),
        ('产物字节 中位', [_med([r['artifact'] for r in A]),
                           _med([r['artifact'] for r in B])]),
    ]
    print()
    print('=' * 78)
    print('  汇总（%d 轮对拍）' % n)
    print('=' * 78)
    print('  %-34s %-16s %-16s' % ('', 'A · dsc', 'B · 真实 API'))
    for name, (x, y) in rows:
        print('  %-34s %-16s %-16s' % (name, x, y))
    print()
    print('  token：A in %d / out %d      B in %d / out %d   ← B 这些要花钱'
          % (sum(r['usage_in'] for r in A), sum(r['usage_out'] for r in A),
             sum(r['usage_in'] for r in B), sum(r['usage_out'] for r in B)))
    print()
    print('  ★ 怎么读这张表：先看**分不分得开** —— 两个范围要是重叠，')
    print('    那点差异就是噪声，别当结论。看得开、又稳定的那几行才是真差距。')
    print('  逐轮叙述留在 %s\\runN\\' % TMP_DIR)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--side', choices=['both', 'shim', 'api'], default='both')
    ap.add_argument('--task', default='short',
                    help='short/edit/cmd/long/multi，或者 all（每轮把任务库走一遍）'
                         '；full 是 e2e 那道长任务')
    ap.add_argument('--rounds', type=int, default=25)
    ap.add_argument('--repeat', type=int, default=1,
                    help='跑几轮对拍（≥3 才谈得上「系统性差距」）')
    # ★ 轮间必须歇一下（第十八轮补四，实测教训）：连着跑 25 轮之后，
    #   DeepSeek 网页版开始回「服务器繁忙」，随后自测里四条联网用例全红
    #   （「回答没有开始」「三种发送方式都没能把消息发出去」）——
    #   **看起来像我们把代码改坏了，其实是限流**。
    #   冷 3 分钟之后同样的用例 36/0 全绿。
    #   宁可跑慢点，也不要跑出一屏假红。
    ap.add_argument('--cooldown', type=int, default=15,
                    help='每轮之间歇多少秒（防限流；撞上「服务器繁忙」会自动 '
                         '按 4 倍歇）')
    ap.add_argument('--save', action='store_true',
                    help='保留**每一轮**的产物（默认只留最近一轮）')
    args = ap.parse_args()

    # ── 排计划：每轮把任务库走一遍（--task all），或者重复同一个任务 ──
    if args.task == 'all':
        names = list(TASKS)
    elif args.task == 'full':
        names = ['full']
    elif args.task in TASKS:
        names = [args.task]
    else:
        print('❌ 不认识的任务 %r，可选：%s' % (args.task, '、'.join(TASKS)))
        return 2
    plan = [nm for _ in range(args.repeat) for nm in names]

    def task_of(name):
        if name == 'full':
            # e2e 那道长任务 —— task_text 里路径写死了，改成占位符
            t = task_text('%s').replace(
                os.path.join(HERE, '_e2e_tmp', 'gen_report.py'), '%s')
            return {'text': lambda w: t % w, 'setup': None}
        return TASKS[name]

    print('=' * 78)
    print('  A/B 对拍 · %d 轮 · 任务序列：%s'
          % (len(plan), ' → '.join(plan)))
    print('=' * 78)

    if shim_up('http://127.0.0.1:8799') and args.side in ('both', 'shim'):
        print('\n⚠️  8799 上有一个 shim 在跑（可能是你的 dsc 会话）——')
        print('    浏览器只有一个，两边会互相把页面导航走。\n')

    ep = None
    if args.side in ('both', 'api'):
        ep = api_endpoint()
        if not ep:
            print('❌ 环境里没有 ANTHROPIC_BASE_URL / ANTHROPIC_AUTH_TOKEN，'
                  '跑不了 B 侧')
            return 2
        print('  B 侧: %s  model=%s' % (ep['base'], ep['model']))

    # ★ 每一轮的结果**追加**到 summary.tsv —— 跑几个小时的话，
    #   控制台日志会很长，事后要靠这个文件统计（而且中途断了也不丢）。
    os.makedirs(TMP_DIR, exist_ok=True)
    tsv = os.path.join(TMP_DIR, 'summary.tsv')
    if not os.path.exists(tsv):
        with open(tsv, 'w', encoding='utf-8') as f:
            f.write('seq\ttask\tside\trounds\ttools\telapsed\ttool_errors\t'
                    'patch\tleaked\tartifact\terrors\n')

    def record(seq, taskname, rec):
        patch = (rec.get('retries', 0) + rec.get('no_content', 0)
                 + rec.get('continues', 0) + rec.get('busy', 0))
        with open(tsv, 'a', encoding='utf-8') as f:
            f.write('%d\t%s\t%s\t%d\t%d\t%.0f\t%d\t%d\t%d\t%d\t%d\n'
                    % (seq, taskname, rec['label'], rec['rounds'],
                       sum(len(x) for x in rec['tools']), rec['elapsed'],
                       rec['tool_errors'], patch, rec['leaked'],
                       rec['artifact'], len(rec['errors'])))
        print('  ★ 记录 %s/%s：轮 %d · 工具 %d · %.0fs · 报错 %d · '
              '补丁 %d · 产物 %d 字'
              % (taskname, rec['label'], rec['rounds'],
                 sum(len(x) for x in rec['tools']), rec['elapsed'],
                 rec['tool_errors'], patch, rec['artifact']))

    pairs = []
    proc = None
    try:
        for seq, name in enumerate(plan, 1):
            print('\n' + '━' * 78)
            print('  第 %d/%d 轮 · 任务 %s' % (seq, len(plan), name))
            print('━' * 78)
            run_dir = os.path.join(TMP_DIR, 'run%d' % seq)
            if not args.save:
                # 只留最近一轮的产物，不然几小时下来会堆满磁盘
                for old in os.listdir(TMP_DIR):
                    if old.startswith('run') and old != 'run%d' % seq:
                        shutil.rmtree(os.path.join(TMP_DIR, old),
                                      ignore_errors=True)
            a = b = None
            task = task_of(name)

            # ★ 单轮出错**不能中断整场** —— 要跑几小时，中间撞上一次
            #   网页超时/服务器繁忙是常态。出错就跳过这一轮，接着下一轮，
            #   但要把错误记下来（最后统计时能看出「哪类任务容易崩」）。
            try:
                if args.side in ('both', 'shim'):
                    print('[A] dsc（网页版 + 协议模拟）')
                    if proc is None:
                        proc, url, ownlog = start_shim(
                            SHIM_PORT, os.path.join(TMP_DIR, 'run%d' % seq, 'shim'))
                    # ★ shim 只起一次、连着跑很多轮，它的 stderr 是**累积**的 ——
                    #   必须只数这一轮新增的那一段，否则第 2 轮会把第 1 轮的重试
                    #   再数一遍（数出来的是累加，看着像越来越糟）。
                    _before = read_own_log(ownlog)
                    a = run_side('dsc', url, {'content-type': 'application/json'},
                                 'deepseek-web', task,
                                 os.path.join(run_dir, 'shim'), args.rounds)
                    _log = read_own_log(ownlog)[len(_before):]
                    a['retries'] = len(re.findall(r'\[重试\].*重新问一次', _log))
                    a['no_content'] = len(re.findall(r'缺 content', _log))
                    a['continues'] = len(re.findall(r'\[续写\].*点「继续生成」', _log))
                    a['busy'] = len(re.findall(r'\[繁忙\]', _log))
                    record(seq, name, a)

                if args.side in ('both', 'api'):
                    print('\n[B] 真实 API（原生工具调用）')
                    b = run_side('api', ep['base'], ep['headers'], ep['model'],
                                 task, os.path.join(run_dir, 'api'), args.rounds)
                    record(seq, name, b)
            except Exception as e:
                print('  ❌ 第 %d 轮整体失败，跳过：%s' % (seq, str(e)[:160]))
            if a and b:
                pairs.append((a, b))

            # ★ 轮间歇一下。撞上「服务器繁忙」就按 4 倍歇 ——
            #   继续猛打只会让后面每一轮都红，把时间浪费在假故障上。
            if seq < len(plan):
                busy = (a or {}).get('busy', 0)
                nap = args.cooldown * (4 if busy else 1)
                if busy:
                    print('  ⏸️  这一轮撞了 %d 次「服务器繁忙」，歇 %d 秒再继续'
                          % (busy, nap))
                time.sleep(nap)
    finally:
        if proc:
            proc.kill()

    if len(pairs) > 1:
        summarize(pairs)
    print('\n  明细在 %s' % tsv)
    return 0


if __name__ == '__main__':
    sys.exit(main())
